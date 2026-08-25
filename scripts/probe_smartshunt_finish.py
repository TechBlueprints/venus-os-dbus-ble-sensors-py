#!/usr/bin/env python3
"""Compare IP22 (working GetValue) with SmartShunt, then try official extras.

Official SO: getDevices, subscribe, getPathList, sendKeepAlive, setValue
0xEC7E (VE-Reg GATT enable).  F7 is credited immediately from the notify.
"""
from __future__ import annotations

import argparse
import asyncio
import binascii
import os
import struct
import sys
import time

import dbus
import dbus.mainloop.glib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

import ble_catcher  # noqa: E402
import ble_gatt_dbus  # noqa: E402
import ble_gatt_link  # noqa: E402
import victron_vreg as vreg  # noqa: E402


class Coll:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.bulk = bytearray()
        self.credits: asyncio.Queue = asyncio.Queue()

    def on_last(self, _c, data) -> None:
        full = bytes(self.bulk) + bytes(data)
        self.bulk.clear()
        self.frames.append(full)
        print(f"  LAST {full.hex()}", flush=True)

    def on_bulk(self, _c, data) -> None:
        self.bulk.extend(data)
        print(f"  BULK {bytes(data).hex()}", flush=True)

    def on_ctrl(self, _c, data) -> None:
        raw = bytes(data)
        print(f"  CTRL {raw.hex()}", flush=True)
        if raw[:1] == b"\xf7":
            n = int.from_bytes(raw[1:3], "little") if len(raw) >= 3 else 2
            try:
                self.credits.put_nowait(n or 2)
            except asyncio.QueueFull:
                pass


async def notify(client, char, cb) -> None:
    try:
        await client.start_notify(char, cb, bluez={"use_start_notify": False})
    except Exception:
        await client.start_notify(char, cb)


async def handshake(client) -> None:
    hdr = bytes(await client.read_gatt_char(vreg.CHAR_CONTROL))
    print(f"CTRL header {hdr.hex()}", flush=True)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xFA\x80\xFF",
                                 response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                 response=False)
    await asyncio.sleep(0.3)


async def credit(client, n: int = 0x80) -> None:
    await client.write_gatt_char(vreg.CHAR_CONTROL, bytes([0xF9, n & 0xFF]),
                                 response=False)


async def drain_f7(client, coll: Coll, window: float) -> None:
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        try:
            n = await asyncio.wait_for(coll.credits.get(), timeout=0.25)
            print(f"  F9 {n:02x} (from F7)", flush=True)
            await credit(client, n)
        except asyncio.TimeoutError:
            await credit(client, 0x80)


async def puk_pin(client, passkey: int) -> None:
    if client.services.get_characteristic(vreg.CHAR_PUK) is None:
        print("no PUK char", flush=True)
        return
    nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
    crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
    print(f"PUK nonce {nonce.hex()} crc {crc.hex()}", flush=True)
    await client.write_gatt_char(vreg.CHAR_PUK, crc, response=False)
    await asyncio.sleep(1.2)
    nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
    await client.write_gatt_char(
        vreg.CHAR_PIN, nonce + struct.pack("<I", passkey), response=False)
    print(f"PIN {passkey:06d}", flush=True)
    await asyncio.sleep(1.2)


async def send(client, coll: Coll, label: str, payload: bytes,
               window: float = 2.0) -> None:
    print(f">> {label} {payload.hex() if payload[:1] != b':' else payload!r}",
          flush=True)
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload, response=False)
    await drain_f7(client, coll, window)


async def session(mac: str, passkey: int, adapter: str, extra: bool) -> None:
    bus = dbus.SystemBus()
    suffix = "/dev_" + mac.replace(":", "_")
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
    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = await ble_gatt_link.connect(device, mac)
    coll = Coll()
    try:
        await notify(client, vreg.CHAR_CONTROL, coll.on_ctrl)
        await notify(client, vreg.CHAR_DATA_LAST, coll.on_last)
        await notify(client, vreg.CHAR_DATA_BULK, coll.on_bulk)
        await asyncio.sleep(0.4)
        await handshake(client)
        await puk_pin(client, passkey)
        await handshake(client)

        await send(client, coll, "GetDevices", vreg.encode_get_devices(), 2.0)
        await send(client, coll, "Sub0", vreg.encode_subscribe_instance(0), 1.5)

        await send(client, coll, "Get ED8D",
                   vreg.encode_read_command(0xED8D, 0), 2.5)
        await send(client, coll, "Get EC65 official",
                   vreg.encode_read_commands(
                       [vreg.VREG_BLE_MAC_ADDRESS,
                        vreg.VREG_ADVERTISEMENT_KEY], 0), 3.0)

        if extra:
            for op in (0x04, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10):
                await send(client, coll, f"op {op:02x} inst0",
                           bytes([op, 0x00]), 1.2)
            await send(client, coll, "keep-alive 00", b"\x00", 1.0)
            await send(client, coll, "EC7E=1",
                       vreg.encode_write_command(0xEC7E, b"\x01"), 2.0)
            await send(client, coll, "EC7D=1",
                       vreg.encode_write_command(0xEC7D, b"\x01"), 2.0)
            await send(client, coll, "Get EC65 after EC7E",
                       vreg.encode_read_commands(
                           [vreg.VREG_BLE_MAC_ADDRESS,
                            vreg.VREG_ADVERTISEMENT_KEY], 0), 3.0)
            await send(client, coll, "Get ED8D after EC7E",
                       vreg.encode_read_command(0xED8D, 0), 2.5)
            tun = vreg.encode_write_command(
                0x0030, vreg.encode_vedirect_hex_get(0xEC65))
            await send(client, coll, "TUNNEL :7 EC65", tun, 3.0)

        print(f"frames {len(coll.frames)}", flush=True)
        for i, fr in enumerate(coll.frames):
            if fr[:1] not in (b"\x07",) or True:
                print(f"  [{i}] {fr.hex()}", flush=True)
    finally:
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mac")
    ap.add_argument("--passkey", type=int, default=14916)
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--extra", action="store_true")
    args = ap.parse_args()
    mac = args.mac.upper()
    pins = [f"{mac}@{args.adapter}"]
    if not ble_catcher.install(owner="dbus-ble-sensors-py.finishprobe",
                               extra_adapters=pins):
        return 1
    asyncio.run(session(mac, args.passkey, args.adapter, args.extra))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
