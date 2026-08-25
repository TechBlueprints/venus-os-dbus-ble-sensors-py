#!/usr/bin/env python3
"""SmartShunt: subscribe 0+1 (not 3), GetValue on both, alt encodings."""
from __future__ import annotations

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

MAC = "DF:1B:3B:4E:05:41"


class C:
    def __init__(self):
        self.frames = []
        self.bulk = bytearray()
        self.q: asyncio.Queue = asyncio.Queue()

    def on_last(self, _c, d):
        full = bytes(self.bulk) + bytes(d)
        self.bulk.clear()
        self.frames.append(full)
        print(f"  LAST {full[:64].hex()}", flush=True)

    def on_bulk(self, _c, d):
        self.bulk.extend(d)
        print(f"  BULK {bytes(d)[:64].hex()}", flush=True)

    def on_ctrl(self, _c, d):
        raw = bytes(d)
        print(f"  CTRL {raw.hex()}", flush=True)
        if raw[:1] == b"\xf7":
            n = int.from_bytes(raw[1:3], "little") if len(raw) >= 3 else 2
            try:
                self.q.put_nowait(n or 2)
            except asyncio.QueueFull:
                pass


async def ntf(client, char, cb):
    try:
        await client.start_notify(char, cb, bluez={"use_start_notify": False})
        print(f"AcquireNotify {char[-12:]} ok", flush=True)
    except Exception as exc:
        print(f"AcquireNotify {char[-12:]} {exc}", flush=True)
        await client.start_notify(char, cb)


async def hs(client):
    print("CTRL", bytes(await client.read_gatt_char(vreg.CHAR_CONTROL)).hex(),
          flush=True)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xFA\x80\xFF",
                                 response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                 response=False)
    await asyncio.sleep(0.3)


async def cred(client, n=0x80):
    await client.write_gatt_char(vreg.CHAR_CONTROL, bytes([0xF9, n & 0xFF]),
                                 response=False)


async def wait(client, c: C, s: float):
    end = time.monotonic() + s
    while time.monotonic() < end:
        try:
            n = await asyncio.wait_for(c.q.get(), timeout=0.2)
            # v4: try the F7 count, then a full window
            await cred(client, n)
            await cred(client, 0x80)
        except asyncio.TimeoutError:
            await cred(client, 0x80)


async def go():
    bus = dbus.SystemBus()
    suffix = "/dev_" + MAC.replace(":", "_")
    om = dbus.Interface(bus.get_object("org.bluez", "/"),
                        "org.freedesktop.DBus.ObjectManager")
    for p in om.GetManagedObjects():
        if str(p).endswith(suffix):
            try:
                dbus.Interface(bus.get_object("org.bluez", str(p)),
                               "org.bluez.Device1").Disconnect()
            except dbus.DBusException:
                pass
    path, props = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter="hci0")
    device = await ble_gatt_link.resolve(MAC, path, props)
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = await ble_gatt_link.connect(device, MAC)
    c = C()
    try:
        await ntf(client, vreg.CHAR_CONTROL, c.on_ctrl)
        await ntf(client, vreg.CHAR_DATA_LAST, c.on_last)
        await ntf(client, vreg.CHAR_DATA_BULK, c.on_bulk)
        await asyncio.sleep(0.4)
        await hs(client)
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
        await client.write_gatt_char(vreg.CHAR_PUK, crc, response=False)
        await asyncio.sleep(1.2)
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        await client.write_gatt_char(
            vreg.CHAR_PIN, nonce + struct.pack("<I", 14916), response=False)
        await asyncio.sleep(1.2)
        await hs(client)

        async def w(label, payload, t=2.2):
            print(f">> {label} {payload.hex()}", flush=True)
            await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload,
                                         response=False)
            await wait(client, c, t)

        await w("GetDevices", b"\x01", 2.0)
        await w("Sub0", b"\x03\x00", 1.2)
        await w("Sub1", b"\x03\x01", 1.2)

        for inst in (0, 1):
            await w(f"ED8D i{inst}",
                    vreg.encode_read_command(0xED8D, inst), 2.5)
            await w(f"0100 i{inst}",
                    vreg.encode_read_command(0x0100, inst), 2.0)
            await w(f"key i{inst}",
                    vreg.encode_read_commands(
                        [0xEC66, 0xEC65], inst), 3.0)

        # alt encodings on inst 1
        await w("ED8D no-array", bytes([0x05, 0x01, 0x19, 0xED, 0x8D]), 2.0)
        await w("ED8D def-array",
                bytes([0x05, 0x01, 0x81, 0x19, 0xED, 0x8D]), 2.0)
        await w("pathlist 0A", b"\x0A", 1.5)
        await w("pathlist 0A00 9fff", b"\x0A\x00\x9F\xFF", 1.5)
        await w("pathlist 0B", b"\x0B", 1.5)
        print("done frames", len(c.frames), flush=True)
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


if __name__ == "__main__":
    ble_catcher.install(owner="dbus-ble-sensors-py.inst1",
                        extra_adapters=[f"{MAC}@hci0"])
    asyncio.run(go())
