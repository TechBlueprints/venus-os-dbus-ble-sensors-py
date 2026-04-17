#!/usr/bin/env python3
"""Standalone Orion-TR advertisement-key fetcher.

Short-lived synchronous helper that the main service shells out to
whenever it needs to read VREG 0xEC65 from a paired Orion-TR.  Running
as a subprocess isolates the provisioning from any long-running process
state (bleak's BlueZ manager, dbus-python proxy cache, etc.) that we
have seen corrupt CCCD writes after repeated connect/disconnect cycles.

Usage::

    python3 orion_tr_key_cli.py MAC [--passkey N] [--timeout S]

On success prints the recovered 32-char hex key to stdout and exits 0.
On failure prints a diagnostic to stderr and exits non-zero.

The on-device reference harness
``dbus-victron-orion-tr/sample-driver/test-scripts/test_keyx_ec65_v3.py``
is the source of truth for the PUK + VREG flow; this CLI keeps that
structure intentionally close to avoid introducing drift.
"""
from __future__ import annotations

import argparse
import binascii
import json
import struct
import sys
import time

import dbus
import dbus.mainloop.glib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
from gi.repository import GLib  # noqa: E402

SVC_306B = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CTRL_UUID = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
DATA_LAST_UUID = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
DATA_BULK_UUID = "306b0004-b081-4037-83dc-e59fcc3cdfd0"
C6_UUID = "97580006-ddf1-48be-b73e-182664615d8e"

def _err(*a):
    print(*a, file=sys.stderr, flush=True)

def _cbor_uint(n):
    if n < 24:
        return bytes([n])
    if n < 256:
        return bytes([0x18, n])
    if n < 65536:
        return bytes([0x19, (n >> 8) & 0xFF, n & 0xFF])
    return bytes([0x1A, (n >> 24) & 0xFF, (n >> 16) & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])

def _cbor_array(items):
    return bytes([0x9F]) + b"".join(items) + bytes([0xFF])

def _scan_for_key(blobs):
    target = bytes([0x19, 0xEC, 0x65, 0x50])
    joined = b"".join(blobs)
    idx = joined.find(target)
    if idx >= 0 and idx + 4 + 16 <= len(joined):
        return joined[idx + 4 : idx + 4 + 16]
    return None

def _scan_for_vreg(blobs, vreg: int):
    """Extract the byte string that a Push response carries for *vreg*.

    Looks for the CBOR encoding of a uint16 register id followed by a
    short (<24 byte) bstr header and returns the payload bytes — or
    ``None`` if no matching entry is found in *blobs*.
    """
    marker = bytes([0x19, (vreg >> 8) & 0xFF, vreg & 0xFF])
    joined = b"".join(blobs)
    idx = 0
    while True:
        idx = joined.find(marker, idx)
        if idx < 0:
            return None
        after = idx + len(marker)
        if after >= len(joined):
            return None
        hdr = joined[after]
        # 0x40-0x57 -> CBOR short bstr, length = hdr & 0x1F
        if 0x40 <= hdr <= 0x57:
            blen = hdr & 0x1F
            start = after + 1
            if start + blen <= len(joined):
                return joined[start : start + blen]
        idx = after

def _find_bluez_device(bus, mac):
    """Find the BlueZ device object path + adapter path across all adapters."""
    om = dbus.Interface(bus.get_object("org.bluez", "/"),
                        "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()
    suffix = "/dev_" + mac.upper().replace(":", "_")
    for path in sorted(objects.keys()):
        if path.endswith(suffix) and "org.bluez.Device1" in objects[path]:
            # adapter path is everything before /dev_
            adapter_path = path[:path.index(suffix)]
            return str(path), str(adapter_path)
    # Fallback: construct from hci0
    return ("/org/bluez/hci0" + suffix,
            "/org/bluez/hci0")

def provision(mac, passkey, timeout_s):
    bus = dbus.SystemBus()
    dev_path, adapter_path = _find_bluez_device(bus, mac)
    _err(f"Using {adapter_path} for {mac} (device {dev_path})")
    ctx = GLib.MainContext.default()

    def pump(ms):
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)

    collected = []
    bulk_buf = bytearray()

    def on_last(_i, changed, _inv):
        if "Value" not in changed:
            return
        data = bytes(int(b) for b in changed["Value"])
        full = bytes(bulk_buf) + data
        bulk_buf.clear()
        collected.append(full)
        _err(f"[LAST] {len(full)}B: {full.hex()}")

    def on_bulk(_i, changed, _inv):
        if "Value" not in changed:
            return
        data = bytes(int(b) for b in changed["Value"])
        bulk_buf.extend(data)
        _err(f"[BULK] +{len(data)}B: {data.hex()}")

    # --- Connect ---
    _err(f"Connecting to {mac}...")
    dev_obj = bus.get_object("org.bluez", dev_path)
    device = dbus.Interface(dev_obj, "org.bluez.Device1")
    dev_props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
    try:
        device.Connect()
    except dbus.DBusException as e:
        if "Already Connected" not in str(e):
            raise
    for _ in range(30):
        pump(500)
        try:
            if bool(dev_props.Get("org.bluez.Device1", "ServicesResolved")):
                break
        except dbus.DBusException:
            pass
    else:
        raise RuntimeError("ServicesResolved never fired")
    _err("Connected.")

    # --- Discover chars ---
    om = dbus.Interface(bus.get_object("org.bluez", "/"),
                        "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()
    chars_9758 = {}
    chars_306b = {}
    for path, ifs in objects.items():
        if "org.bluez.GattCharacteristic1" not in ifs:
            continue
        if not path.startswith(dev_path):
            continue
        cp = ifs["org.bluez.GattCharacteristic1"]
        uuid = str(cp.get("UUID", ""))
        svc_path = str(cp.get("Service", ""))
        if uuid.startswith("9758"):
            chars_9758[uuid] = path
        elif uuid.startswith("306b") and svc_path in objects:
            si = objects[svc_path]
            if "org.bluez.GattService1" in si:
                if str(si["org.bluez.GattService1"].get("UUID", "")) == SVC_306B:
                    chars_306b[uuid] = path

    if C6_UUID not in chars_9758:
        raise RuntimeError("PUK characteristic 9758…06 not found")
    if CTRL_UUID not in chars_306b or DATA_LAST_UUID not in chars_306b:
        raise RuntimeError("306b CTRL/DATA_LAST not found")

    ci6 = dbus.Interface(bus.get_object("org.bluez", chars_9758[C6_UUID]),
                         "org.bluez.GattCharacteristic1")
    ctrl = dbus.Interface(bus.get_object("org.bluez", chars_306b[CTRL_UUID]),
                          "org.bluez.GattCharacteristic1")
    dlast = dbus.Interface(bus.get_object("org.bluez", chars_306b[DATA_LAST_UUID]),
                           "org.bluez.GattCharacteristic1")

    bus.add_signal_receiver(
        on_last,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        path=chars_306b[DATA_LAST_UUID])
    if DATA_BULK_UUID in chars_306b:
        bus.add_signal_receiver(
            on_bulk,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path=chars_306b[DATA_BULK_UUID])
        dbulk = dbus.Interface(
            bus.get_object("org.bluez", chars_306b[DATA_BULK_UUID]),
            "org.bluez.GattCharacteristic1")
        dbulk.StartNotify()

    dlast.StartNotify()
    pump(500)

    # --- PUK CRC auth ---
    # Occasionally the device returns 0x03 (CRC mismatch) even when we
    # just hashed its current nonce — racing with a stale notification.
    # Retry a few times reading a fresh nonce each round.
    puk_responses = []

    def on_puk(_i, changed, _inv):
        if "Value" not in changed:
            return
        puk_responses.append(bytes(int(b) for b in changed["Value"]))
        _err(f"[PUK] {len(puk_responses[-1])}B: {puk_responses[-1].hex()}")

    bus.add_signal_receiver(
        on_puk,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        path=chars_9758[C6_UUID])
    ci6.StartNotify()
    pump(300)

    puk_ok = False
    for attempt in range(1, 4):
        puk_responses.clear()
        nonce = bytes(int(b) for b in ci6.ReadValue({}))
        crc = binascii.crc32(nonce) & 0xFFFFFFFF
        crc_bytes = struct.pack("<I", crc)
        _err(f"PUK auth attempt {attempt}: nonce={nonce.hex()} "
             f"crc={crc_bytes.hex()}")
        ci6.WriteValue(list(crc_bytes), {"type": "command"})
        pump(1500)
        if any(d == b"\x00" for d in puk_responses):
            puk_ok = True
            _err("PUK CRC OK")
            break
        _err(f"PUK attempt {attempt} rejected (responses="
             f"{[d.hex() for d in puk_responses]})")
        pump(500)

    if not puk_ok:
        raise RuntimeError("PUK CRC not accepted after 3 attempts")

    # --- Flow control ---
    ctrl.WriteValue([0xFA, 0x14], {"type": "command"})
    pump(300)
    ctrl.WriteValue([0xF9, 0x08], {"type": "command"})
    pump(300)

    # --- Priming: Subscribe to 0xEDDB (charger temp) first ---
    # The reference harness does this before attempting any GetValue;
    # without it the device does not emit further notifications.
    prime = (_cbor_uint(0x03) + _cbor_uint(0)
             + _cbor_array([_cbor_uint(0xEDDB)]))
    _err(f"Subscribe 0xEDDB (prime): {prime.hex()}")
    dlast.WriteValue(list(prime), {"type": "command"})
    prime_deadline = time.monotonic() + 3.0
    while time.monotonic() < prime_deadline and not collected:
        pump(400)
        try:
            ctrl.WriteValue([0xF9, 0x08], {"type": "command"})
        except Exception:
            pass

    # --- GetValue 0xEC65 ---
    cmd = (_cbor_uint(0x05) + _cbor_uint(0)
           + _cbor_array([_cbor_uint(0xEC65)]))
    collected.clear()
    bulk_buf.clear()
    _err(f"GetValue 0xEC65: {cmd.hex()}")
    dlast.WriteValue(list(cmd), {"type": "command"})

    deadline = time.monotonic() + min(timeout_s, 15.0)
    key = None
    while time.monotonic() < deadline:
        pump(500)
        key = _scan_for_key(collected)
        if key is not None:
            _err(f"Recovered key: {len(key)}B")
            break
        try:
            ctrl.WriteValue([0xF9, 0x08], {"type": "command"})
        except Exception:
            pass

    if key is None:
        try:
            device.Disconnect()
        except Exception:
            pass
        raise RuntimeError(
            f"no 16-byte key in VREG 0xEC65 response "
            f"({len(collected)} chunks, "
            f"{sum(len(c) for c in collected)}B total)")

    # --- Opportunistically read firmware (0x0140) and product id ------
    # (0x0100) in the same paired session, one more GetValue round-trip
    # each.  We don't fail the whole flow if a register is unavailable —
    # some firmwares may not expose a particular register.
    def _fetch_vreg(vreg: int, label: str,
                    timeout: float = 4.0) -> str:
        try:
            req = (_cbor_uint(0x05) + _cbor_uint(0)
                   + _cbor_array([_cbor_uint(vreg)]))
            collected.clear()
            bulk_buf.clear()
            _err(f"GetValue 0x{vreg:04X} ({label}): {req.hex()}")
            dlast.WriteValue(list(req), {"type": "command"})
            deadline_local = time.monotonic() + timeout
            while time.monotonic() < deadline_local:
                pump(400)
                val = _scan_for_vreg(collected, vreg)
                if val is not None:
                    _err(f"Recovered {label} bytes: {val.hex()}")
                    return val.hex()
                try:
                    ctrl.WriteValue([0xF9, 0x08], {"type": "command"})
                except Exception:
                    pass
        except Exception as e:
            _err(f"{label} read failed (non-fatal): {e}")
        return None

    firmware_hex = _fetch_vreg(0x0140, "firmware")
    product_id_hex = _fetch_vreg(0x0100, "product id")
    temperature_hex = _fetch_vreg(0xEDDB, "temperature")

    # Read DeviceInfo (97580002) for hardware version — this is a plain
    # GATT ReadValue, no CBOR/flow-control needed.
    hw_version = None
    try:
        if "97580002-ddf1-48be-b73e-182664615d8e" in chars_9758:
            di_iface = dbus.Interface(
                bus.get_object("org.bluez",
                               chars_9758["97580002-ddf1-48be-b73e-182664615d8e"]),
                "org.bluez.GattCharacteristic1")
            di_val = bytes(int(b) for b in di_iface.ReadValue({}))
            _err(f"DeviceInfo: {len(di_val)}B: {di_val.hex()}")
            if len(di_val) >= 4:
                hw_rev = int.from_bytes(di_val[2:4], "little")
                hw_version = str(hw_rev)
                _err(f"Hardware revision: {hw_version}")
    except Exception as e:
        _err(f"DeviceInfo read failed (non-fatal): {e}")

    try:
        device.Disconnect()
    except Exception:
        pass

    return {
        "key": key.hex(),
        "firmware": firmware_hex,
        "product_id": product_id_hex,
        "temperature": temperature_hex,
        "hardware_version": hw_version,
    }

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("mac")
    ap.add_argument("--passkey", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args()
    try:
        result = provision(args.mac, args.passkey, args.timeout)
    except Exception as e:
        _err(f"orion-tr key provisioning failed: {e}")
        return 1
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0

if __name__ == "__main__":
    sys.exit(main())
