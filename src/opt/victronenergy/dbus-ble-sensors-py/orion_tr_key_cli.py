#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Standalone Victron advertisement-key fetcher (Orion-TR, Blue Smart IP22).

Short-lived helper the main service shells out to whenever it needs to
read VREG 0xEC65 from a paired charger.  Running as a subprocess isolates
provisioning from any long-running process state — bleak's BlueZManager,
the dbus-python proxy cache — that we have seen corrupt CCCD writes after
repeated connect/disconnect cycles within one service lifetime.

Usage::

    python3 orion_tr_key_cli.py MAC [--passkey N] [--timeout S]
                                    [--preferred-adapter hciX]

On success prints a JSON object to stdout and exits 0; on failure prints
diagnostics to stderr and exits non-zero.  Callers depend on that
contract (see ``_run_key_cli`` in both charger drivers).

The connection runs through bcmv2 like every other link this project
opens, so a provisioning attempt is visible to — and placed around — the
other BLE services sharing these radios.  ``--preferred-adapter`` becomes
a bcmv2 pin: try the card that worked last time first, then walk.

Protocol notes, all of them hard-won on real hardware:

* **Notifications must use AcquireNotify.**  On Venus OS, StartNotify plus
  PropertiesChanged delivers *empty* payloads for the 306b characteristics
  once the link is SMP-paired, so bleak is asked for the fd-based path
  (``bluez={"use_start_notify": False}``) with StartNotify only as a
  fallback.
* **The CTRL read is load-bearing.**  Reading the control characteristic
  is what puts the device into CBOR mode; skip it and DATA_LAST never
  fires.
* **Prime before asking.**  A GetValue sent as the very first CBOR request
  is sometimes swallowed before the device has established its outgoing
  stream, so a plain subscribe to a chatty public register goes first.
* **Official key fetch is GetDevices, then instance-0 GetValues.**  The
  HEX client asks for the device list (opcode ``0x01``), subscribes
  instance ``0``, then GetValues ``0x05`` for ``[0xEC66, 0xEC65]`` in
  one request.  It never sends opcode ``0x25``.
* **0x25 is an Orion fallback only.**  Some charger firmwares ACK plain
  GetValue with RequestedEncryptionNotSupported; ``0x05 | 0x20`` still
  works there.  After PUK+PIN we retry the official batch, then ``0x25``,
  then a lone ``0x05``, then the ASCII HEX Get.
"""
from __future__ import annotations

import argparse
import asyncio
import binascii
import json
import struct
import sys
import time

import dbus
import dbus.mainloop.glib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

import ble_catcher  # noqa: E402
import ble_gatt_dbus  # noqa: E402
import ble_gatt_link  # noqa: E402
import victron_vreg as vreg  # noqa: E402

# Flow-control values this flow uses.  Deliberately not the writer's:
# the provisioning session asks for the large chunk size and a full
# credit window, because it is pulling register pushes rather than
# pushing one short command.
_CTRL_CHUNK = bytes([vreg.OPCODE_CHUNK_SIZE, 0x80, 0xFF])
_CTRL_CREDITS = bytes([vreg.OPCODE_READY_TO_RECV, 0x80])

# A chatty public register (temperature), subscribed to purely to make
# the device start pushing.
VREG_TEMPERATURE = 0xEDDB
_PRIME = bytes([0x03, 0x00, 0x9F, 0x19, 0xED, 0xDB, 0xFF])

# Official key fetch: GetValues instance 0 for MAC + advertisement key.
# 0x25 remains an Orion-only fallback when 0x05 is refused.
_GET_KEY_OFFICIAL = vreg.encode_read_commands(
    [vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY])
_GET_KEY = bytes([0x25, 0x00, 0x9F, 0x19, 0xEC, 0x65, 0xFF])
_SUBSCRIBE_INSTANCE0 = vreg.encode_subscribe_instance(0)

VREG_FIRMWARE = 0x0140
VREG_PRODUCT_ID = 0x0100
CHAR_DEVICE_INFO = "97580002-ddf1-48be-b73e-182664615d8e"

_FAST_PHASE_S = 6.0
_PRIME_WINDOW_S = 3.0
_CREDIT_INTERVAL_S = 0.4


def _err(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


class _Collector:
    """Reassembles the device's chunked CBOR pushes.

    Bulk frames accumulate until a "last chunk" frame closes the message;
    everything else is kept for diagnostics only.
    """

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self._bulk = bytearray()
        self.puk: list[bytes] = []
        self.pin: list[bytes] = []

    def reset(self) -> None:
        self.frames.clear()
        self._bulk.clear()

    def on_last(self, _char, data: bytearray) -> None:
        full = bytes(self._bulk) + bytes(data)
        self._bulk.clear()
        self.frames.append(full)
        _err(f"[LAST] {len(full)}B: {full.hex()}")

    def on_bulk(self, _char, data: bytearray) -> None:
        self._bulk.extend(data)
        _err(f"[BULK] +{len(data)}B: {data.hex()}")

    def on_ctrl(self, _char, data: bytearray) -> None:
        # Device-side control traffic (F7 error, F9 credits, F8 buffer
        # clear).  Logged only — the handshake is driven explicitly below,
        # and reacting here would race our own writes.
        if data:
            _err(f"[CTRL-RX] {len(data)}B: {bytes(data).hex()}")

    def on_puk(self, _char, data: bytearray) -> None:
        self.puk.append(bytes(data))
        _err(f"[PUK] {len(data)}B: {bytes(data).hex()}")

    def on_pin(self, _char, data: bytearray) -> None:
        self.pin.append(bytes(data))
        _err(f"[PIN] {len(data)}B: {bytes(data).hex()}")


async def _start_notify(client, char, callback) -> bool:
    """Subscribe, preferring AcquireNotify.  Returns False if absent."""
    if client.services.get_characteristic(char) is None:
        return False
    try:
        await client.start_notify(char, callback,
                                  bluez={"use_start_notify": False})
        return True
    except Exception as exc:
        _err(f"AcquireNotify {char} failed ({exc}); trying StartNotify")
    try:
        await client.start_notify(char, callback)
        return True
    except Exception as exc:
        _err(f"StartNotify {char} failed: {exc}")
        return False


async def _credits(client) -> None:
    """Hand the device another credit window; failures are not fatal."""
    try:
        await client.write_gatt_char(vreg.CHAR_CONTROL, _CTRL_CREDITS,
                                     response=False)
    except Exception:
        pass


async def _handshake(client) -> None:
    """CTRL read (puts the device in CBOR mode) plus the credit writes."""
    try:
        header = await client.read_gatt_char(vreg.CHAR_CONTROL)
        _err(f"CTRL header: {bytes(header).hex()}")
    except Exception as exc:
        _err(f"CTRL read: {exc} — proceeding anyway")
    await client.write_gatt_char(vreg.CHAR_CONTROL, _CTRL_CHUNK,
                                 response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(vreg.CHAR_CONTROL, _CTRL_CREDITS,
                                 response=False)
    await asyncio.sleep(0.4)


async def _prime(client, collector: _Collector) -> None:
    """Subscribe to a chatty register so the outgoing stream starts."""
    _err(f"Subscribe 0x{VREG_TEMPERATURE:04X} (prime): {_PRIME.hex()}")
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, _PRIME, response=False)
    deadline = time.monotonic() + _PRIME_WINDOW_S
    while time.monotonic() < deadline and not collector.frames:
        await asyncio.sleep(_CREDIT_INTERVAL_S)
        await _credits(client)


def _refused_encryption(frames) -> bool:
    """Whether the device answered 0xEC65 with "encryption not supported".

    ACK error 2 arrives as ``07 19 EC 65 <opcode> 02``; either opcode
    variant means the same thing — do the full auth first.
    """
    joined = b"".join(frames)
    return (b"\x19\xec\x65\x05\x02" in joined
            or b"\x19\xec\x65\x25\x02" in joined)


async def _puk_pin_auth(client, collector: _Collector, passkey: int) -> None:
    """PUK CRC + PIN auth on the 9758 service.

    Required on the first provisioning of firmwares that gate the key
    register behind it (observed on the 0xA3D5 48 V Buck-Boost); a no-op
    round trip on older ones.
    """
    for attempt in range(1, 4):
        collector.puk.clear()
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
        _err(f"PUK auth attempt {attempt}: nonce={nonce.hex()} crc={crc.hex()}")
        await client.write_gatt_char(vreg.CHAR_PUK, crc, response=False)
        await asyncio.sleep(1.5)
        if any(r == b"\x00" for r in collector.puk):
            _err("PUK CRC OK")
            break
        _err(f"PUK attempt {attempt} rejected "
             f"(responses={[r.hex() for r in collector.puk]})")
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError("PUK CRC not accepted after 3 attempts")

    # PIN auth: nonce + LE32(passkey).  Needed on 0xA3D5 firmware,
    # harmless elsewhere, and never fatal — some firmwares simply do not
    # answer, and the key read that follows is the real test.
    if client.services.get_characteristic(vreg.CHAR_PIN) is None:
        return
    collector.pin.clear()
    if not await _start_notify(client, vreg.CHAR_PIN, collector.on_pin):
        return
    try:
        await asyncio.sleep(0.2)
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        payload = nonce + struct.pack("<I", passkey)
        _err(f"PIN auth: nonce+PIN = {payload.hex()}")
        await client.write_gatt_char(vreg.CHAR_PIN, payload, response=False)
        await asyncio.sleep(2.0)
        if any(r == b"\x00" for r in collector.pin):
            _err("PIN accepted")
        else:
            _err(f"PIN responses={[r.hex() for r in collector.pin]} — "
                 f"continuing anyway")
    except Exception as exc:
        _err(f"PIN step failed (non-fatal): {exc}")


def _scan_hex_key(frames) -> bytes | None:
    """Pull a 16-byte key out of a VE.Direct HEX ``:8...`` reply."""
    joined = b"".join(frames)
    text = joined.decode("ascii", errors="ignore")
    cbor = vreg.scan_for_key(frames)
    if cbor is not None:
        return cbor
    # ``:8 <id_lo> <id_hi> <flags> <32 hex chars of key> <cs>``
    marker = "8" + "65EC"
    idx = text.upper().find(marker)
    if idx >= 0:
        rest = text[idx + len(marker):]
        # skip optional 2-hex flags, then take 32 hex digits
        hexdigits = "".join(c for c in rest if c in "0123456789abcdefABCDEF")
        if len(hexdigits) >= 34:
            blob = hexdigits[2:34]
            try:
                raw = bytes.fromhex(blob)
            except ValueError:
                raw = b""
            if len(raw) == 16:
                return raw
        if len(hexdigits) >= 32:
            try:
                raw = bytes.fromhex(hexdigits[:32])
            except ValueError:
                raw = b""
            if len(raw) == 16:
                return raw
    return None


async def _ask_key(client, collector: _Collector, request: bytes,
                   label: str, window: float) -> bytes | None:
    collector.reset()
    _err(f"Get 0xEC65 ({label}): {request.hex() if request[:1] != b':' else request!r}")
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, request, response=False)
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        key = _scan_hex_key(collector.frames)
        if key is not None:
            _err(f"Recovered key: {len(key)}B")
            return key
        await _credits(client)
    return None


def _parse_device_list_instances(frames) -> list[int]:
    """Instances from a GetDevices Push ``02 9F <uints...> FF``.

    Wire we see is a flat uint list in pairs ``(instance, extra)``.
    """
    joined = b"".join(frames)
    start = joined.find(b"\x02\x9f")
    if start < 0:
        return [0]
    body = joined[start + 2:]
    end = body.find(b"\xff")
    if end < 0:
        return [0]
    vals = []
    i = 0
    data = body[:end]
    while i < len(data):
        if data[i] < 24:
            vals.append(data[i])
            i += 1
        else:
            break
    instances = vals[0::2] or [0]
    _err(f"GetDevices instances: {instances} (raw {data.hex()})")
    return instances


async def _official_key_preamble(client, collector: _Collector) -> None:
    """GetDevices, then subscribe every instance the list returned."""
    collector.reset()
    devices = vreg.encode_get_devices()
    _err(f"GetDevices: {devices.hex()}")
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, devices, response=False)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        await _credits(client)
    instances = _parse_device_list_instances(collector.frames)
    for inst in instances:
        req = vreg.encode_subscribe_instance(inst)
        _err(f"Subscribe instance {inst}: {req.hex()}")
        await client.write_gatt_char(vreg.CHAR_DATA_LAST, req, response=False)
        await asyncio.sleep(0.5)
        await _credits(client)
    return instances


async def _read_key(client, collector: _Collector, passkey: int,
                    timeout_s: float) -> bytes:
    """Fetch VREG 0xEC65 the way the official HEX client does, then fall back.

    Lead with GetDevices + subscribe of every listed instance, then
    GetValues ``[0xEC66, 0xEC65]`` (opcode ``0x05``).  Some Orion
    firmwares still need ``0x25`` after PUK+PIN; that stays a fallback.
    """
    instances = await _official_key_preamble(client, collector)
    voltage = vreg.encode_read_command(0xED8D)
    await _ask_key(client, collector, voltage, "voltage 0xED8D", 4.0)
    for inst in instances:
        batch = vreg.encode_read_commands(
            [vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY],
            instance=inst)
        key = await _ask_key(
            client, collector, batch,
            f"0x05 [EC66,EC65] inst {inst}", 8.0)
        if key is not None:
            return key

    key = await _ask_key(client, collector, _GET_KEY, "fast 0x25", 6.0)
    if key is not None:
        return key

    refused = _refused_encryption(collector.frames)
    _err("No key on per-instance 0x05 / fast 0x25"
         + (" (encryption refused)" if refused else " (F7 / no EC65 push)")
         + " — trying PUK+PIN")
    try:
        await _puk_pin_auth(client, collector, passkey)
    except Exception as exc:
        _err(f"PUK+PIN failed (continuing): {exc}")
    await _handshake(client)
    instances = await _official_key_preamble(client, collector)

    authed_window = min(timeout_s, 12.0)
    for inst in instances:
        batch = vreg.encode_read_commands(
            [vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY],
            instance=inst)
        key = await _ask_key(
            client, collector, batch,
            f"authed 0x05 [EC66,EC65] inst {inst}", authed_window)
        if key is not None:
            return key
        priv = bytes([0x25, inst, 0x9F, 0x19, 0xEC, 0x65, 0xFF])
        key = await _ask_key(
            client, collector, priv, f"authed 0x25 inst {inst}", 6.0)
        if key is not None:
            return key

    hex_cmd = vreg.encode_vedirect_hex_get(vreg.VREG_ADVERTISEMENT_KEY)
    key = await _ask_key(client, collector, hex_cmd, "VE.Direct HEX", 8.0)
    if key is not None:
        return key

    raise RuntimeError(
        f"no 16-byte key in VREG 0xEC65 response "
        f"({len(collector.frames)} chunks, "
        f"{sum(len(f) for f in collector.frames)}B total)")


async def _fetch_vreg(client, collector: _Collector, register: int,
                      label: str, timeout: float = 4.0) -> str | None:
    """One best-effort GetValue round trip.  ``None`` when unavailable.

    Never fatal: a firmware that does not expose a given register should
    not cost us the key we already have.
    """
    try:
        request = vreg.encode_read_command(register)
        collector.reset()
        _err(f"GetValue 0x{register:04X} ({label}): {request.hex()}")
        await client.write_gatt_char(vreg.CHAR_DATA_LAST, request,
                                     response=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            value = vreg.scan_for_vreg(collector.frames, register)
            if value is not None:
                _err(f"Recovered {label} bytes: {value.hex()}")
                return value.hex()
            await _credits(client)
    except Exception as exc:
        _err(f"{label} read failed (non-fatal): {exc}")
    return None


async def _read_hardware_version(client) -> str | None:
    """Hardware revision from DeviceInfo — a plain read, no CBOR."""
    try:
        if client.services.get_characteristic(CHAR_DEVICE_INFO) is None:
            return None
        value = bytes(await client.read_gatt_char(CHAR_DEVICE_INFO))
        _err(f"DeviceInfo: {len(value)}B: {value.hex()}")
        if len(value) >= 4:
            revision = str(int.from_bytes(value[2:4], "little"))
            _err(f"Hardware revision: {revision}")
            return revision
    except Exception as exc:
        _err(f"DeviceInfo read failed (non-fatal): {exc}")
    return None


def _pre_disconnect(bus, mac: str) -> None:
    """Tear down any stale session on every adapter that knows this MAC.

    After a previous successful provisioning the device-side session
    lingers, and a re-PUK can be rejected until the link is torn down and
    rebuilt.  dbus-python, on the main thread, before any async work.
    """
    suffix = "/dev_" + mac.upper().replace(":", "_")
    try:
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        paths = sorted(str(p) for p in om.GetManagedObjects())
    except dbus.DBusException as exc:
        _err(f"object lookup failed: {exc}")
        return
    for path in paths:
        if not path.endswith(suffix):
            continue
        try:
            dbus.Interface(bus.get_object("org.bluez", path),
                           "org.bluez.Device1").Disconnect()
            _err(f"Pre-disconnected {path}")
        except dbus.DBusException:
            pass


async def provision(mac: str, passkey: int, timeout_s: float,
                    preferred_adapter: str | None = None) -> dict:
    bus = dbus.SystemBus()
    _pre_disconnect(bus, mac)

    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=preferred_adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    adapter = ble_gatt_dbus.adapter_from_path(
        (getattr(device, "details", None) or {}).get("path"))
    _err(f"Using {adapter or 'bcmv2-selected adapter'} for {mac}")

    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    agent = None
    if not (props or {}).get("Paired"):
        agent = ble_gatt_dbus.PairingAgent(bus, passkey, mac)
        agent.register()

    client = None
    try:
        client = await ble_gatt_link.connect(device, mac)
        if agent is not None:
            _err(f"Pairing with {mac} (passkey {passkey:06d})")
            await client.pair()
            _err("Paired")

        collector = _Collector()
        # CTRL first: the device wants its CCCD set before it will push
        # the session header.
        await _start_notify(client, vreg.CHAR_CONTROL, collector.on_ctrl)
        await _start_notify(client, vreg.CHAR_DATA_LAST, collector.on_last)
        await _start_notify(client, vreg.CHAR_DATA_BULK, collector.on_bulk)
        await _start_notify(client, vreg.CHAR_PUK, collector.on_puk)
        await asyncio.sleep(0.5)

        await _handshake(client)
        await _prime(client, collector)

        key = await _read_key(client, collector, passkey, timeout_s)

        firmware = await _fetch_vreg(client, collector, VREG_FIRMWARE,
                                     "firmware")
        product_id = await _fetch_vreg(client, collector, VREG_PRODUCT_ID,
                                       "product id")
        temperature = await _fetch_vreg(client, collector, VREG_TEMPERATURE,
                                        "temperature")
        hardware_version = await _read_hardware_version(client)

        return {
            "key": key.hex(),
            "firmware": firmware,
            "product_id": product_id,
            "temperature": temperature,
            "hardware_version": hardware_version,
            "adapter": adapter,
        }
    finally:
        if client is not None:
            await ble_gatt_link.disconnect(client)
        if agent is not None:
            agent.unregister()
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


async def telemetry(mac: str, passkey: int, timeout_s: float,
                    preferred_adapter: str | None = None) -> dict:
    """Short live-VREG session: voltage + state, then enable Instant Readout."""
    bus = dbus.SystemBus()
    _pre_disconnect(bus, mac)
    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=preferred_adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    adapter = ble_gatt_dbus.adapter_from_path(
        (getattr(device, "details", None) or {}).get("path"))
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = None
    try:
        client = await ble_gatt_link.connect(device, mac)
        collector = _Collector()
        await _start_notify(client, vreg.CHAR_CONTROL, collector.on_ctrl)
        await _start_notify(client, vreg.CHAR_DATA_LAST, collector.on_last)
        await _start_notify(client, vreg.CHAR_DATA_BULK, collector.on_bulk)
        await asyncio.sleep(0.4)
        await _handshake(client)
        await _prime(client, collector)
        voltage = await _fetch_vreg(client, collector, 0xED8D, "voltage")
        state = await _fetch_vreg(client, collector, 0x0201, "state")
        try:
            await client.write_gatt_char(
                vreg.CHAR_DATA_LAST,
                vreg.encode_write_command(
                    vreg.VREG_BLE_ADVERTISEMENT_MODE, b"\x01"),
                response=False)
            _err("Set 0xEC7D = 01")
            await asyncio.sleep(0.5)
            await _credits(client)
        except Exception as exc:
            _err(f"0xEC7D write failed (non-fatal): {exc}")
        return {
            "voltage": voltage,
            "device_state": state,
            "adapter": adapter,
        }
    finally:
        if client is not None:
            await ble_gatt_link.disconnect(client)
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("mac")
    ap.add_argument("--passkey", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--preferred-adapter", default=None,
                    help="Try this adapter first (e.g. hci1)")
    ap.add_argument("--telemetry", action="store_true",
                    help="Read live voltage/state and enable Instant Readout")
    args = ap.parse_args()

    mac = args.mac.upper()
    # A pin, not a restriction: bcmv2 walks a pinned device's preference
    # list failure-driven, so a dead preferred card costs one attempt.
    pins = [f"{mac}@{args.preferred_adapter}"] if args.preferred_adapter else []
    if not ble_catcher.install(owner="dbus-ble-sensors-py.keycli",
                               extra_adapters=pins):
        _err("BLE connection stack unavailable — cannot provision "
             "(run 'git submodule update --init --recursive')")
        return 1

    work = telemetry if args.telemetry else provision
    label = "telemetry" if args.telemetry else "key provisioning"
    try:
        result = asyncio.run(
            asyncio.wait_for(
                work(mac, args.passkey, args.timeout,
                     preferred_adapter=args.preferred_adapter),
                timeout=args.timeout + 30.0))
    except Exception as exc:
        _err(f"{label} failed: {exc}")
        return 1

    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
