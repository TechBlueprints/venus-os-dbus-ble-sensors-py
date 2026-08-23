#!/usr/bin/env python3
"""Subscribe-and-dump probe for a Victron SmartShunt HEX tunnel.

The charger GetValue (opcode 0x05) path is silent on the bench SmartShunt;
Subscribe (0x03) is what the device ACKs.  This script opens the same
306b session as orion_tr_key_cli, subscribes a battery-monitor VREG set,
and dumps every reassembled LAST frame.
"""
from __future__ import annotations

import argparse
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
import victron_vreg as vreg  # noqa: E402

# Identity + live BMV registers from VictronConnect 6.32 vregs.json
REGS = [
    0x0100, 0x0102, 0x010A, 0x010B, 0x010C, 0x0140,
    0x0FFE, 0x0FFF, 0x1000,
    0xED8C, 0xED8D, 0xED8F,
    0xEEFF, 0xEEB0, 0xEEB2, 0xEEB4, 0xEEB5,
    0xEC65, 0xEDDB,
]


class _Collector:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self._bulk = bytearray()
        self.need_credit = asyncio.Event()

    def on_last(self, _char, data: bytearray) -> None:
        full = bytes(self._bulk) + bytes(data)
        self._bulk.clear()
        self.frames.append(full)
        print(f"[LAST] {len(full):3d}B  {full.hex()}", flush=True)

    def on_bulk(self, _char, data: bytearray) -> None:
        self._bulk.extend(data)
        print(f"[BULK] +{len(data)}B {bytes(data).hex()}", flush=True)

    def on_ctrl(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        print(f"[CTRL] {raw.hex()}", flush=True)
        if raw and raw[0] == 0xF7:
            self.need_credit.set()


def _subscribe(reg: int) -> bytes:
    return bytes([0x03, 0x00, 0x9F, 0x19, (reg >> 8) & 0xFF, reg & 0xFF, 0xFF])


def _getvalue(reg: int, opcode: int = 0x05) -> bytes:
    return bytes([opcode, 0x00, 0x9F, 0x19,
                  (reg >> 8) & 0xFF, reg & 0xFF, 0xFF])


async def _run(mac: str, passkey: int, adapter: str | None,
               listen_s: float) -> None:
    bus = dbus.SystemBus()
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

    path, props = ble_gatt_dbus.lookup_device(bus, mac,
                                              prefer_adapter=adapter)
    device = await ble_gatt_link.resolve(mac, path, props)
    stop = asyncio.Event()
    pump = asyncio.create_task(ble_gatt_dbus.pump_default_context(stop))
    client = await ble_gatt_link.connect(device, mac)
    collector = _Collector()
    try:
        for char, cb in ((vreg.CHAR_CONTROL, collector.on_ctrl),
                         (vreg.CHAR_DATA_LAST, collector.on_last),
                         (vreg.CHAR_DATA_BULK, collector.on_bulk)):
            try:
                await client.start_notify(char, cb,
                                          bluez={"use_start_notify": False})
            except Exception:
                await client.start_notify(char, cb)
        await asyncio.sleep(0.4)
        try:
            hdr = bytes(await client.read_gatt_char(vreg.CHAR_CONTROL))
            print(f"CTRL header: {hdr.hex()}", flush=True)
        except Exception as exc:
            print(f"CTRL read: {exc}", flush=True)
        await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xFA\x80\xFF",
                                     response=False)
        await asyncio.sleep(0.3)
        await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                     response=False)
        await asyncio.sleep(0.4)

        async def credit() -> None:
            await client.write_gatt_char(vreg.CHAR_CONTROL, b"\xF9\x80",
                                         response=False)

        async def credit_pump(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(collector.need_credit.wait(),
                                           timeout=0.35)
                    collector.need_credit.clear()
                    await credit()
                except asyncio.TimeoutError:
                    await credit()

        print("--- batch subscribe ---", flush=True)
        items = [vreg.cbor_uint(r) for r in REGS]
        batch = (vreg.cbor_uint(3) + vreg.cbor_uint(0)
                 + vreg.cbor_array(items))
        print(f"SUB batch {len(REGS)} regs {batch.hex()}", flush=True)
        await client.write_gatt_char(vreg.CHAR_DATA_LAST, batch,
                                     response=False)
        await credit_pump(6.0)

        print("--- per-reg subscribe ---", flush=True)
        for reg in (0x0100, 0xED8D, 0x0FFF, 0xEEFF, 0xEC65):
            payload = _subscribe(reg)
            print(f"SUB 0x{reg:04X} {payload.hex()}", flush=True)
            await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload,
                                         response=False)
            await credit_pump(1.5)

        print(f"--- listen {listen_s:.0f}s ---", flush=True)
        await credit_pump(listen_s)

        print("--- GetValue 0x0100 / 0xED8D / 0x0FFF ---", flush=True)
        for reg in (0x0100, 0xED8D, 0x0FFF, 0xEC65):
            payload = _getvalue(reg)
            print(f"GET 0x{reg:04X} {payload.hex()}", flush=True)
            await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload,
                                         response=False)
            await asyncio.sleep(0.8)

        print("--- PUK + privileged 0xEC65 + HEX Get ---", flush=True)
        try:
            import binascii
            import struct
            nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
            crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
            print(f"PUK nonce={nonce.hex()} crc={crc.hex()}", flush=True)
            await client.write_gatt_char(vreg.CHAR_PUK, crc, response=False)
            await asyncio.sleep(1.5)
            hex_get = ":765ECFD\n".encode("ascii")
            print(f"HEX GET 0xEC65 {hex_get!r}", flush=True)
            await client.write_gatt_char(vreg.CHAR_DATA_LAST, hex_get,
                                         response=False)
            await credit_pump(4.0)
            priv = _getvalue(0xEC65, opcode=0x25)
            print(f"GET 0xEC65 opcode 0x25 {priv.hex()}", flush=True)
            await client.write_gatt_char(vreg.CHAR_DATA_LAST, priv,
                                         response=False)
            await credit_pump(4.0)
        except Exception as exc:
            print(f"PUK/HEX step: {exc}", flush=True)

        print(f"frames: {len(collector.frames)}", flush=True)
        for i, fr in enumerate(collector.frames):
            print(f"  [{i}] {fr.hex()}", flush=True)
    finally:
        await ble_gatt_link.disconnect(client)
        stop.set()
        try:
            await pump
        except asyncio.CancelledError:
            pass


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mac", default="DF:1B:3B:4E:05:41")
    p.add_argument("--passkey", type=int, default=14916)
    p.add_argument("--adapter", default="hci0")
    p.add_argument("--listen", type=float, default=8.0)
    args = p.parse_args()
    pins = [f"{args.mac.upper()}@{args.adapter}"] if args.adapter else []
    if not ble_catcher.install(owner="dbus-ble-sensors-py.shuntprobe",
                               extra_adapters=pins):
        print("catcher unavailable", file=sys.stderr)
        return 1
    asyncio.run(_run(args.mac.upper(), args.passkey, args.adapter,
                     args.listen))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
