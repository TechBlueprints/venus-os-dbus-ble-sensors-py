#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""
Probe a Victron BLE charger / DC-DC for VREG implementation status.

Runs on a Cerbo (or any host with a paired Victron device on BlueZ),
talks the same CBOR-framed VE.Direct HEX protocol the driver uses, and
emits a report of which VREGs respond, with what value, and what kind
of write a 1-byte sentinel triggers (code 1 = unknown register, code
2 = parameter / size error -> register exists, code 3 = read-only,
empty = write accepted).

Use it to:

  - Locate the Orion-TR's max-current VREG (gap #1)
  - Find the Orion-TR's Function (Charger / PSU) VREG (gap #4)
  - Confirm IP22 optional charge-profile VREGs before wiring writable
    settings paths (gap #9 - Equalize voltage/duration, AbsorptionMaxTime,
    BulkMaxTime, RebulkVoltage)

Usage:

  ./scripts/probe_charger_vregs.py --mac ED:47:4D:2A:7C:2A --range 0xEDD0-0xEDFF
  ./scripts/probe_charger_vregs.py --mac FF:13:42:2B:7A:4B --candidates current
  ./scripts/probe_charger_vregs.py --mac ED:47:4D:2A:7C:2A --candidates ip22-optional

The link goes through bcmv2 like the service's own, so the probe picks its
adapter with the same claim awareness and is visible to every other BLE
service while it runs.  It still wants the device to itself, though - the
driver holds a session of its own - so stop the service first:

  svc -d /service/dbus-ble-sensors-py
  ./scripts/probe_charger_vregs.py ...
  svc -u /service/dbus-ble-sensors-py
"""
from __future__ import annotations

import argparse
import asyncio
import binascii
import os
import struct
import sys

import dbus
import dbus.mainloop.glib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

# The driver package is a sibling of this script's directory.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

import ble_catcher  # noqa: E402
import ble_gatt_dbus  # noqa: E402
import ble_gatt_link  # noqa: E402
import victron_vreg as vreg  # noqa: E402

# --- Candidate VREG sets ----------------------------------------------------

CANDIDATES = {
    # Orion-TR / IP22 universals — already known
    "core": [
        0x0200, 0x0202, 0x0207, 0xEDF0, 0xEDF1, 0xEDF6, 0xEDF7,
    ],
    # Where to look for the Orion-TR's max-current VREG that IP22 puts
    # at 0xEDF0.  Sweeps the surrounding charge-profile region.
    "current": [
        0xEDB0, 0xEDB1, 0xEDB2, 0xEDB3, 0xEDC0, 0xEDC1, 0xEDC2,
        0xEDC3, 0xEDC4, 0xEDC5, 0xEDD0, 0xEDD2, 0xEDD4, 0xEDD6,
        0xEDD8, 0xEDDC, 0xEDDD, 0xEDE0, 0xEDE3, 0xEDE5, 0xEDE7,
        0xEDEA, 0xEDED, 0xEDEE, 0xEDF8, 0xEDF9, 0xEDFD, 0xEDFF,
        0x0270, 0x0271, 0x2003,
    ],
    # Where the Function (Charger / PSU) VREG might live.  Mode-style
    # VREGs commonly cluster in 0x02xx and the front-half of 0xEDxx.
    "function": [
        0x0203, 0x0204, 0x0205, 0x0206, 0x0208, 0x020A, 0x020B,
        0x020F, 0x0210, 0x0211, 0x0220, 0x0221,
        0xEDD3, 0xEDDF, 0xEDE6,
    ],
    # IP22 optional charge-profile registers — verify before wiring
    # writable settings paths.  Solar-charger-class layout suggests
    # these but it isn't guaranteed on AC-charger firmware.
    "ip22-optional": [
        0xEDF3, 0xEDF4, 0xEDF5, 0xEDFA, 0xEDFB, 0xEDFC, 0xEDFD,
        0xEDFE, 0xEDFF,
    ],
    # SmartShunt / BMV live + identity + Instant Readout key.
    "smartshunt": [
        0x0100, 0x0102, 0x010A, 0x010B, 0x010C, 0x0140, 0x0150,
        0x0FFE, 0x0FFF, 0x1000,
        0xED8C, 0xED8D, 0xED8F,
        0xEEFF, 0xEEB0, 0xEEB1, 0xEEB2, 0xEEB3, 0xEEB4, 0xEEB5,
        0xEEB7, 0xEEB8, 0xEC65, 0xEC66, 0xEC7D, 0xEE00, 0x0300, 0x034F,
    ],
}

# --- BLE plumbing -----------------------------------------------------------

class _Collector:
    """Accumulates reassembled CBOR pushes as one flat byte stream.

    The probe searches raw bytes for per-register push/error patterns
    rather than parsing frames, so unlike the provisioner's collector this
    one just concatenates.
    """

    def __init__(self) -> None:
        self.data = bytearray()
        self._bulk = bytearray()

    def on_last(self, _char, chunk: bytearray) -> None:
        self.data.extend(bytes(self._bulk) + bytes(chunk))
        self._bulk.clear()

    def on_bulk(self, _char, chunk: bytearray) -> None:
        self._bulk.extend(chunk)


async def _open_session(mac: str, passkey: int):
    """Connect, authenticate, and run the CTRL handshake.

    Returns ``(client, collector, agent, stop, pump)`` - the caller drives
    requests through the client and reads accumulated responses out of the
    collector.
    """
    bus = dbus.SystemBus()

    # Drop any stale session first: the device-side session lingers after
    # a previous connection and can reject a fresh PUK until torn down.
    suffix = "/dev_" + mac.upper().replace(":", "_")
    try:
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        for path in sorted(str(p) for p in om.GetManagedObjects()):
            if path.endswith(suffix):
                try:
                    dbus.Interface(bus.get_object("org.bluez", path),
                                   "org.bluez.Device1").Disconnect()
                except dbus.DBusException:
                    pass
    except dbus.DBusException:
        pass

    path, props = ble_gatt_dbus.lookup_device(bus, mac)
    device = await ble_gatt_link.resolve(mac, path, props)

    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    agent = None
    if not (props or {}).get("Paired"):
        agent = ble_gatt_dbus.PairingAgent(bus, passkey, mac)
        agent.register()

    client = await ble_gatt_link.connect(device, mac)
    if agent is not None:
        await client.pair()

    # PUK CRC, then PIN: both best-effort, exactly as before - a firmware
    # that does not need them simply ignores the round trip.
    try:
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
        await client.write_gatt_char(vreg.CHAR_PUK, crc, response=True)
        await asyncio.sleep(0.5)
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        await client.write_gatt_char(
            vreg.CHAR_PIN, nonce + struct.pack("<I", passkey), response=True)
        await asyncio.sleep(0.5)
    except Exception as exc:
        print(f"  (auth step skipped: {exc})")

    collector = _Collector()
    for char, callback in ((vreg.CHAR_DATA_LAST, collector.on_last),
                           (vreg.CHAR_DATA_BULK, collector.on_bulk)):
        if client.services.get_characteristic(char) is None:
            continue
        try:
            # AcquireNotify: on Venus OS a paired link delivers empty
            # PropertiesChanged payloads for these characteristics.
            await client.start_notify(char, callback,
                                      bluez={"use_start_notify": False})
        except Exception:
            await client.start_notify(char, callback)
    await asyncio.sleep(0.3)

    # Reading CTRL is what puts the device into CBOR mode.
    try:
        await client.read_gatt_char(vreg.CHAR_CONTROL)
    except Exception:
        pass
    await asyncio.sleep(0.15)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xFA\x80\xFF",
                                 response=False)
    await asyncio.sleep(0.25)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                 response=False)
    await asyncio.sleep(0.4)
    # Prime the outgoing stream with a subscribe to a chatty register.
    await client.write_gatt_char(vreg.CHAR_DATA_LAST,
                                 b"\x03\x00\x9F\x19\xED\xDB\xFF",
                                 response=False)
    await asyncio.sleep(0.8)
    return client, collector, agent, stop, pump


def _decode_value(data: bytes, off: int):
    """CBOR-decode a single value at offset `off` of `data`.  Returns
    a tuple (kind, value_repr) or None if the data runs out."""
    if off >= len(data):
        return None
    h = data[off]
    if 0x00 <= h <= 0x17:
        return ("uint", h)
    if h == 0x18 and off + 1 < len(data):
        return ("uint", data[off + 1])
    if h == 0x19 and off + 2 < len(data):
        return ("uint", struct.unpack(">H", bytes(data[off + 1:off + 3]))[0])
    if h == 0x1A and off + 4 < len(data):
        return ("uint", struct.unpack(">I", bytes(data[off + 1:off + 5]))[0])
    if 0x40 <= h <= 0x57:
        ln = h & 0x1F
        if off + 1 + ln <= len(data):
            return ("bstr", bytes(data[off + 1:off + 1 + ln]).hex())
    if h == 0x58 and off + 1 < len(data):
        ln = data[off + 1]
        if off + 2 + ln <= len(data):
            return ("bstr", bytes(data[off + 2:off + 2 + ln]).hex())
    if 0x60 <= h <= 0x77:
        ln = h & 0x1F
        if off + 1 + ln <= len(data):
            try:
                return ("tstr", bytes(
                    data[off + 1:off + 1 + ln]).decode("ascii"))
            except UnicodeDecodeError:
                pass
    return ("?", f"h={h:02x}")

async def _probe_one(client, collector: _Collector, reg: int,
                     write_sentinel: bool = False, pump_ms: int = 500):
    """Probe one VREG.  Returns a dict describing what we observed."""
    pre_len = len(collector.data)
    if write_sentinel:
        # 1-byte SetValue - distinguishes unknown (code 1) from
        # everything else.  Crucially, we don't want this to actually
        # take effect, so use a value the firmware will reject.
        payload = bytes([0x06, 0x00, 0x9F, 0x19,
                         (reg >> 8) & 0xFF, reg & 0xFF,
                         0x40,  # bstr length 0
                         0xFF])
    else:
        payload = bytes([0x05, 0x00, 0x9F, 0x19,
                         (reg >> 8) & 0xFF, reg & 0xFF,
                         0xFF])
    try:
        await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload,
                                     response=False)
    except Exception as exc:
        return {"reg": reg, "lost": str(exc)}
    await asyncio.sleep(pump_ms / 1000.0)
    new_data = bytes(collector.data[pre_len:])
    push_pat = bytes([0x08, 0x00, 0x19, (reg >> 8) & 0xFF, reg & 0xFF])
    err_pat = bytes([0x09, 0x00, 0x19, (reg >> 8) & 0xFF, reg & 0xFF])
    pi = new_data.find(push_pat)
    ei = new_data.find(err_pat)
    if pi >= 0:
        v = _decode_value(new_data, pi + 5)
        return {"reg": reg, "kind": "push", "value": v}
    if ei >= 0:
        code = new_data[ei + 5] if ei + 5 < len(new_data) else 0
        return {"reg": reg, "kind": "error", "code": code}
    return {"reg": reg, "kind": "silent"}


def _expand_range(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a, 0), int(b, 0) + 1))
    return [int(spec, 0)]

async def _run(args, regs) -> None:
    client, collector, agent, stop, pump = await _open_session(
        args.mac.upper(), args.passkey)
    try:
        for reg in regs:
            r = await _probe_one(client, collector, reg,
                                 write_sentinel=args.write_sentinel,
                                 pump_ms=args.pump_ms)
            tag = "0x{:04X}".format(r["reg"])
            if "lost" in r:
                print(f"  {tag}: connection lost - {r['lost']}")
                break
            kind = r["kind"]
            if kind == "push":
                v = r["value"]
                print(f"  {tag}: {v[0]} = {v[1]}")
            elif kind == "error":
                code = r["code"]
                meaning = {
                    1: "unknown register",
                    2: "parameter / size error -> register EXISTS",
                    3: "read-only -> register EXISTS",
                }.get(code, f"code {code}")
                if code != 1:
                    print(f"  {tag}: ERR code {code} ({meaning})")
            else:
                print(f"  {tag}: silent (no response)")
    finally:
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)
        if agent is not None:
            agent.unregister()
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mac", required=True,
                   help="Device MAC address (AA:BB:CC:DD:EE:FF)")
    p.add_argument("--passkey", type=int, default=14916,
                   help="GATT passkey (default 14916 - Cerbo default PIN)")
    p.add_argument("--range",
                   help="Inclusive 0xAAAA-0xBBBB or single 0xAAAA")
    p.add_argument("--candidates", choices=sorted(CANDIDATES.keys()),
                   help="Use a named candidate set "
                        "(core / current / function / ip22-optional / smartshunt)")
    p.add_argument("--write-sentinel", action="store_true",
                   help="Use 1-byte SetValue instead of GetValue.  Returns "
                        "code 2 for registers that exist but didn't accept "
                        "the size - useful when the device only responds "
                        "to writes (some firmwares).")
    p.add_argument("--pump-ms", type=int, default=500,
                   help="ms to wait after each request (default 500)")
    p.add_argument("--adapter", default=None,
                   help="Pin the link to this adapter (e.g. hci1) instead "
                        "of letting bcmv2 place it")
    args = p.parse_args()

    if args.range:
        regs = _expand_range(args.range)
    elif args.candidates:
        regs = CANDIDATES[args.candidates]
    else:
        sys.exit("must provide --range or --candidates")

    pins = [f"{args.mac.upper()}@{args.adapter}"] if args.adapter else []
    if not ble_catcher.install(owner="dbus-ble-sensors-py.probe",
                               extra_adapters=pins):
        sys.exit("BLE connection stack unavailable - run "
                 "'git submodule update --init --recursive'")

    print(f"Probing {len(regs)} VREG(s) on {args.mac} "
          f"({'write sentinel' if args.write_sentinel else 'GetValue'}, "
          f"{args.pump_ms} ms each)...")
    print()

    asyncio.run(_run(args, regs))


if __name__ == "__main__":
    main()
