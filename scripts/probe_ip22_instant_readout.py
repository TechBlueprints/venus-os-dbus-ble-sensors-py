#!/usr/bin/env python3
"""Read/set IP22 Instant Readout mode (0xEC7D) and sample ads after release."""
from __future__ import annotations

import asyncio
import os
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
import hci_advertisement_tap as tap  # noqa: E402
import victron_vreg as vreg  # noqa: E402

MAC = "F2:86:C3:32:4C:D2"
PASSKEY = 14916
ADAPTER = "hci0"


class Coll:
    def __init__(self):
        self.frames: list[bytes] = []
        self.bulk = bytearray()
        self.credits = asyncio.Queue()

    def on_last(self, _c, data):
        full = bytes(self.bulk) + bytes(data)
        self.bulk.clear()
        self.frames.append(full)
        print(f"  LAST {full.hex()}", flush=True)

    def on_bulk(self, _c, data):
        self.bulk.extend(data)
        print(f"  BULK {bytes(data).hex()}", flush=True)

    def on_ctrl(self, _c, data):
        raw = bytes(data)
        print(f"  CTRL {raw.hex()}", flush=True)
        if raw[:1] == b"\xf7":
            n = int.from_bytes(raw[1:3], "little") if len(raw) >= 3 else 2
            try:
                self.credits.put_nowait(n or 2)
            except asyncio.QueueFull:
                pass


async def notify(client, char, cb):
    try:
        await client.start_notify(char, cb, bluez={"use_start_notify": False})
    except Exception:
        await client.start_notify(char, cb)


async def handshake(client):
    hdr = bytes(await client.read_gatt_char(vreg.CHAR_CONTROL))
    print(f"CTRL header {hdr.hex()}", flush=True)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xFA\x80\xFF",
                                 response=False)
    await asyncio.sleep(0.3)
    await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                 response=False)
    await asyncio.sleep(0.3)


async def credit(client, n=0x80):
    await client.write_gatt_char(vreg.CHAR_CONTROL, bytes([0xF9, n & 0xFF]),
                                 response=False)


async def drain(client, coll, window):
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        try:
            n = await asyncio.wait_for(coll.credits.get(), timeout=0.25)
            await credit(client, n)
        except asyncio.TimeoutError:
            await credit(client, 0x80)


async def send(client, coll, label, payload, window=2.0):
    print(f">> {label} {payload.hex()}", flush=True)
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload, response=False)
    await drain(client, coll, window)


def sample_ads(seconds=20.0):
    sock = tap.create_tap_socket()
    counts = {}
    samples = []
    end = time.time() + seconds
    while time.time() < end:
        readable, _, _ = __import__("select").select([sock], [], [], 0.5)
        if not readable:
            continue
        try:
            raw = sock.recv(4096)
        except BlockingIOError:
            continue
        for adv in tap.parse_monitor_frame(raw, mfg_filter={0x02E1}):
            if adv.mac != "f286c3324cd2":
                continue
            for mid, data in adv.manufacturer_data.items():
                key = len(data)
                counts[key] = counts.get(key, 0) + 1
                if len(samples) < 8:
                    samples.append((adv.adapter_index, adv.rssi, data.hex()))
    sock.close()
    print(f"ad length counts: {counts}", flush=True)
    for i, (hci, rssi, hx) in enumerate(samples):
        print(f"  sample {i} hci{hci} rssi={rssi} {hx}", flush=True)


async def main():
    ble_catcher.install()
    bus = dbus.SystemBus()
    suffix = "/dev_" + MAC.replace(":", "_")
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

    path, props = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter=ADAPTER)
    device = await ble_gatt_link.resolve(MAC, path, props)
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = await ble_gatt_link.connect(device, MAC)
    coll = Coll()
    try:
        await notify(client, vreg.CHAR_CONTROL, coll.on_ctrl)
        await notify(client, vreg.CHAR_DATA_LAST, coll.on_last)
        await notify(client, vreg.CHAR_DATA_BULK, coll.on_bulk)
        await asyncio.sleep(0.4)
        await handshake(client)
        await send(client, coll, "GetDevices", vreg.encode_get_devices(), 2.0)
        await send(client, coll, "Sub0", vreg.encode_subscribe_instance(0), 1.5)
        await send(client, coll, "Get EC7D",
                   vreg.encode_read_command(0xEC7D, 0), 2.5)
        await send(client, coll, "Get ED8D",
                   vreg.encode_read_command(0xED8D, 0), 2.5)
        found = vreg.scan_for_vreg(coll.frames, 0xEC7D)
        print(f"EC7D raw={None if found is None else found.hex()}", flush=True)
        if found != b"\x01":
            await send(client, coll, "EC7D=1",
                       vreg.encode_write_command(0xEC7D, b"\x01"), 2.0)
            await send(client, coll, "Get EC7D after",
                       vreg.encode_read_command(0xEC7D, 0), 2.5)
            found = vreg.scan_for_vreg(coll.frames, 0xEC7D)
            print(f"EC7D after write={None if found is None else found.hex()}",
                  flush=True)
    finally:
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)
        stop.set()
        pump.cancel()
        try:
            await pump
        except Exception:
            pass

    print("sampling ads after GATT release", flush=True)
    sample_ads(22.0)


if __name__ == "__main__":
    asyncio.run(main())
