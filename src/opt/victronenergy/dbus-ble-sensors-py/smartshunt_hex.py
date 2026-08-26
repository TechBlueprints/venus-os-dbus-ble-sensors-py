# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Long-lived SmartShunt HEX session: live VREGs plus the ad-key read.

The bench unit Pushes battery registers on the ``306b`` tunnel after
GetDevices + instance subscribe.  This module keeps that session up,
decodes the Pushes, and keeps asking for ``0xEC65`` on the same link.
"""
from __future__ import annotations

import asyncio
import binascii
import logging
import struct
import time
from typing import Callable, Optional

import dbus

import ble_async_loop
import ble_gatt_dbus
import ble_gatt_link
import victron_vreg as vreg
from dbus_bus import get_bus

logger = logging.getLogger(__name__)

# Provisioning window — same as the IP22/Orion key CLI.  The writer
# window (FA 14 / F9 08) is for short SetValue, not pulling Pushes.
_CTRL_CHUNK = bytes([vreg.OPCODE_CHUNK_SIZE, 0x80, 0xFF])
_CTRL_CREDITS = bytes([vreg.OPCODE_READY_TO_RECV, 0x80])

# One GetValue at a time on the live instance.  A write storm drops BlueZ.
_LIVE_VREGS = (
    0xED8D, 0xED8C, 0xED8F,
    0x0FFF, 0x0FFE, 0xEEFF,
    0x010A, 0x010B,
)
_KEY_BATCH = (vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY)
_SCAN_VREGS = _LIVE_VREGS + (
    vreg.VREG_ADVERTISEMENT_KEY, vreg.VREG_BLE_MAC_ADDRESS, 0x0140,
)

_RECONNECT_S = 5.0

# How often an ongoing "device is not answering" condition may be logged,
# per MAC.  A sensor that is switched off or out of range fails on every
# reconnect for as long as it stays away, and a full traceback each time
# writes tens of KB a minute onto the eMMC (measured on prod: ~700 B/s,
# roughly 57 MiB/day, with multilog rotating every ~20s).  The condition
# still needs to be visible, so log it once, then once every quarter hour
# with a count of what was suppressed.
_UNREACHABLE_LOG_INTERVAL_S = 900.0

_started: set[str] = set()
_callbacks: dict[str, Callable[[dict], None]] = {}
# mac -> (last logged monotonic, suppressed count, last message)
_unreachable_state: dict[str, tuple[float, int, str]] = {}


def _note_unreachable(mac: str, exc: BaseException) -> None:
    """Log an expected "not answering" failure, at most once per window.

    Deliberately one line and no traceback: the stack is identical every
    time and says nothing the message does not.
    """
    message = str(exc) or type(exc).__name__
    now = time.monotonic()
    last, suppressed, previous = _unreachable_state.get(mac, (0.0, 0, ""))
    # Log immediately when the condition is new or its message changed —
    # a different error is different news.
    if message == previous and (now - last) < _UNREACHABLE_LOG_INTERVAL_S:
        _unreachable_state[mac] = (last, suppressed + 1, previous)
        logger.debug("SmartShunt HEX %s still unreachable: %s", mac, message)
        return
    if suppressed:
        logger.warning(
            "SmartShunt HEX %s unreachable: %s (%d further attempt(s) "
            "since the last report)", mac, message, suppressed)
    else:
        logger.warning("SmartShunt HEX %s unreachable: %s", mac, message)
    _unreachable_state[mac] = (now, 0, message)


def _note_reachable(mac: str) -> None:
    """Forget the suppression state once a session succeeds."""
    _unreachable_state.pop(mac, None)


class _Collector:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self._bulk = bytearray()
        self.puk: list[bytes] = []
        self.pin: list[bytes] = []
        self.f7 = False
        self.f7_n = 2

    def on_last(self, _char, data: bytearray) -> None:
        chunk = bytes(data)
        if not (len(chunk) >= 5 and chunk[0] == 0x08
                and chunk[2:5] == b"\x19\xec\x5a"):
            logger.info("SmartShunt HEX LAST %s", chunk[:48].hex())
        full = bytes(self._bulk) + chunk
        self._bulk.clear()
        self.frames.append(full)

    def on_bulk(self, _char, data: bytearray) -> None:
        chunk = bytes(data)
        logger.info("SmartShunt HEX BULK %s", chunk[:48].hex())
        self._bulk.extend(chunk)

    def on_ctrl(self, _char, data: bytearray) -> None:
        raw = bytes(data)
        logger.info("SmartShunt HEX CTRL notify %s", raw.hex())
        if raw[:1] == b"\xf7":
            self.f7 = True
            if len(raw) >= 3:
                self.f7_n = int.from_bytes(raw[1:3], "little") or 2

    def on_puk(self, _char, data: bytearray) -> None:
        self.puk.append(bytes(data))

    def on_pin(self, _char, data: bytearray) -> None:
        self.pin.append(bytes(data))


async def _start_notify(client, char, callback, acquired=None) -> bool:
    if client.services.get_characteristic(char) is None:
        return False
    try:
        await client.start_notify(char, callback,
                                  bluez={"use_start_notify": False})
        if acquired is not None:
            acquired.append(char)
        return True
    except Exception:
        try:
            await client.start_notify(char, callback)
            if acquired is not None:
                acquired.append(char)
            return True
        except Exception:
            return False

async def _stop_notify_all(client, acquired) -> None:
    """Release every notify we hold, before the link goes away.

    BlueZ 5.72 stores the notify client into ``chrc->notify_io->data``
    without a reference, so an acquire still outstanding at disconnect
    leaves a dangling pointer that detonates 30-120 s later in
    temporary-device cleanup — far from anything that names us.
    Upstream fixed it in 5.84/5.86; Venus ships 5.72, so releasing is
    our job.  We ask for the acquire path deliberately (StartNotify
    plus PropertiesChanged delivers empty payloads on these
    characteristics once SMP-paired), which is what makes us the only
    consumer on this box that can plant one.

    Best effort and never raising: a failure must not mask the caller's
    exception, and an already-dead link fails every one of these
    harmlessly.
    """
    while acquired:
        char = acquired.pop()
        try:
            await client.stop_notify(char)
        except Exception:
            logger.debug("stop_notify failed on %s", char, exc_info=True)


async def _credits(client, n: int = 8) -> None:
    try:
        await client.write_gatt_char(
            vreg.CHAR_CONTROL,
            bytes([vreg.OPCODE_READY_TO_RECV, n & 0xFF]),
            response=False)
    except Exception:
        pass


async def _prep(client) -> None:
    """FA 14 + F9 08 immediately before a DATA_LAST command."""
    try:
        await client.write_gatt_char(vreg.CHAR_CONTROL, _CTRL_CHUNK,
                                     response=False)
        await asyncio.sleep(0.25)
        await _credits(client, 8)
        await asyncio.sleep(0.15)
    except Exception:
        pass


async def _write(client, payload: bytes, prep: bool = False) -> None:
    if prep:
        await _prep(client)
    await client.write_gatt_char(vreg.CHAR_DATA_LAST, payload, response=False)


def _handle_f7(collector: _Collector) -> int:
    if not collector.f7:
        return 0
    n = collector.f7_n
    collector.f7 = False
    return n


async def _handshake(client) -> None:
    try:
        header = bytes(await client.read_gatt_char(vreg.CHAR_CONTROL))
        logger.info("SmartShunt HEX CTRL header %s", header.hex())
    except Exception as exc:
        logger.warning("SmartShunt HEX CTRL read: %s", exc)
    await client.write_gatt_char(vreg.CHAR_CONTROL, _CTRL_CHUNK,
                                 response=False)
    await asyncio.sleep(0.3)
    await _credits(client)
    await asyncio.sleep(0.3)


async def _puk_pin(client, collector: _Collector, passkey: int,
                   acquired: list) -> None:
    if client.services.get_characteristic(vreg.CHAR_PUK) is None:
        return
    await _start_notify(client, vreg.CHAR_PUK, collector.on_puk, acquired)
    for _attempt in range(3):
        collector.puk.clear()
        nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
        crc = struct.pack("<I", binascii.crc32(nonce) & 0xFFFFFFFF)
        await client.write_gatt_char(vreg.CHAR_PUK, crc, response=False)
        await asyncio.sleep(1.2)
        if any(r == b"\x00" for r in collector.puk):
            break
    if client.services.get_characteristic(vreg.CHAR_PIN) is None:
        return
    await _start_notify(client, vreg.CHAR_PIN, collector.on_pin, acquired)
    collector.pin.clear()
    nonce = bytes(await client.read_gatt_char(vreg.CHAR_PUK))
    await client.write_gatt_char(
        vreg.CHAR_PIN, nonce + struct.pack("<I", passkey), response=False)
    await asyncio.sleep(1.5)


def _parse_instances(frames) -> list[int]:
    joined = b"".join(frames)
    start = joined.find(b"\x02\x9f")
    if start < 0:
        return [0, 3]
    body = joined[start + 2:]
    end = body.find(b"\xff")
    if end < 0:
        return [0, 3]
    vals = [b for b in body[:end] if b < 24]
    return vals[0::2] or [0, 3]


def _emit(vreg_id: int, payload: bytes, on_update, inst=None) -> None:
    if vreg_id == 0xEC5A:
        return
    logger.info("SmartShunt HEX push 0x%04X inst %s %s",
                vreg_id, inst, payload.hex())
    fields = vreg.decode_smartshunt_vreg(vreg_id, payload)
    if fields:
        logger.info("SmartShunt HEX 0x%04X → %s", vreg_id, fields)
        on_update(fields)
    elif vreg_id == vreg.VREG_ADVERTISEMENT_KEY and len(payload) == 16:
        on_update({"advertisement_key": payload.hex()})


def _drain(collector: _Collector, on_update, seen: int) -> int:
    while seen < len(collector.frames):
        frame = collector.frames[seen]
        seen += 1
        parsed = vreg.parse_push_frame(frame)
        if parsed is not None:
            _emit(parsed[1], parsed[2], on_update, parsed[0])
        elif frame[:1] == b"\x07":
            logger.info("SmartShunt HEX ACK %s", frame[:16].hex())
        elif frame[:1] == b"\x09":
            logger.info("SmartShunt HEX ERR %s", frame[:16].hex())
        elif frame[:1] not in (b"\x02", b""):
            logger.info("SmartShunt HEX unparsed %s", frame[:48].hex())
        key = vreg.scan_for_key([frame])
        if key is not None:
            on_update({"advertisement_key": key.hex()})
        for reg in _SCAN_VREGS:
            payload = vreg.scan_for_vreg([frame], reg)
            if payload is not None and (parsed is None or parsed[1] != reg):
                _emit(reg, payload, on_update)
    return seen


async def _pull_ready(client, collector: _Collector) -> None:
    n = _handle_f7(collector)
    if n:
        await _credits(client, n)


async def _await_frames(client, collector: _Collector, on_update,
                        seen: int, window: float) -> int:
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        if collector.f7:
            await _pull_ready(client, collector)
        else:
            await _credits(client, 8)
        if collector._bulk:
            collector.frames.append(bytes(collector._bulk))
            collector._bulk.clear()
        seen = _drain(collector, on_update, seen)
    return seen


async def _ask_key(client, collector: _Collector, on_update,
                   seen: int) -> tuple[Optional[bytes], int]:
    """Official GetValues: instance 0, [0xEC66, 0xEC65]."""
    payload = vreg.encode_read_commands(_KEY_BATCH, instance=0,
                                        definite=True)
    before = len(collector.frames)
    logger.info("SmartShunt HEX GetValues key %s", payload.hex())
    await _write(client, payload)
    seen = await _await_frames(client, collector, on_update, seen, 3.5)
    key = vreg.scan_for_key(collector.frames[before:])
    if key is not None:
        logger.info("SmartShunt HEX recovered 0xEC65")
    return key, seen


async def _session(mac: str, passkey: int,
                   on_update: Callable[[dict], None],
                   path: Optional[str] = None,
                   props: Optional[dict] = None) -> bool:
    device = await ble_gatt_link.resolve(mac, path, props)
    client = await ble_gatt_link.connect(device, mac)
    collector = _Collector()
    acquired: list = []
    try:
        await _start_notify(client, vreg.CHAR_CONTROL, collector.on_ctrl, acquired)
        await _start_notify(client, vreg.CHAR_DATA_LAST, collector.on_last, acquired)
        await _start_notify(client, vreg.CHAR_DATA_BULK, collector.on_bulk, acquired)
        await asyncio.sleep(0.4)
        await _handshake(client)
        await _puk_pin(client, collector, passkey, acquired)
        await _handshake(client)

        await _write(client, vreg.encode_get_devices())
        await asyncio.sleep(1.5)
        await _credits(client)
        instances = _parse_instances(collector.frames)
        unit = [i for i in instances if i != 3] or [0]
        logger.info("SmartShunt HEX instances %s — subscribe %s",
                    instances, unit)
        seen = 0
        for inst in unit:
            await _write(client, vreg.encode_subscribe_instance(inst))
            await asyncio.sleep(0.5)
            await _credits(client)
            seen = _drain(collector, on_update, seen)

        key, seen = await _ask_key(client, collector, on_update, seen)
        if key is not None:
            on_update({"advertisement_key": key.hex()})
            logger.info("SmartShunt HEX key stored — releasing GATT for ads")
            return True

        next_key = time.monotonic() + 60.0
        while True:
            await asyncio.sleep(0.4)
            n = _handle_f7(collector)
            if n:
                await _credits(client, n)
            else:
                await _credits(client)
            if collector._bulk:
                collector.frames.append(bytes(collector._bulk))
                collector._bulk.clear()
            seen = _drain(collector, on_update, seen)
            if time.monotonic() >= next_key:
                key, seen = await _ask_key(
                    client, collector, on_update, seen)
                if key is not None:
                    on_update({"advertisement_key": key.hex()})
                    logger.info(
                        "SmartShunt HEX key stored — releasing GATT for ads")
                    return True
                next_key = time.monotonic() + 60.0
        return False
    finally:
        # Before the link goes away, not after — see _stop_notify_all.
        await _stop_notify_all(client, acquired)
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)


async def _run_forever(mac: str, passkey: int,
                       on_update: Callable[[dict], None],
                       path: Optional[str],
                       props: Optional[dict]) -> None:
    while True:
        try:
            if await _session(mac, passkey, on_update, path, props):
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A device that is off, out of range, or not near an adapter
            # we may scan on fails identically on every reconnect.  That
            # is an expected steady state, not a fault worth a stack per
            # attempt — see _note_unreachable.  Anything else keeps its
            # traceback, because anything else is a bug.
            if ble_gatt_link.unreachable(exc):
                _note_unreachable(mac, exc)
            else:
                logger.exception("SmartShunt HEX session dropped for %s", mac)
        else:
            _note_reachable(mac)
        await asyncio.sleep(_RECONNECT_S)


def start(mac: str, passkey: int,
          on_update: Callable[[dict], None]) -> bool:
    """Run the HEX pump on the BLE loop.  *on_update* is GLib-thread."""
    mac = mac.upper()
    _callbacks[mac] = on_update
    if mac in _started:
        return True
    if not ble_async_loop.start():
        logger.error("SmartShunt HEX: BLE loop unavailable")
        return False
    _started.add(mac)

    # get_bus, not dbus.SystemBus(): the latter is a second
    # process-wide connection to the same daemon for the same
    # purpose, and connections are the resource under pressure.
    # Main-thread only — start() is reached from the
    # advertisement handler, and only path/props cross to the
    # BLE loop thread.
    bus = get_bus("org.bluez")
    suffix = "/dev_" + mac.replace(":", "_")
    try:
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        for obj_path in sorted(str(p) for p in om.GetManagedObjects()):
            if obj_path.endswith(suffix):
                try:
                    dbus.Interface(bus.get_object("org.bluez", obj_path),
                                   "org.bluez.Device1").Disconnect()
                except dbus.DBusException:
                    pass
    except Exception:
        logger.exception("%s: pre-disconnect failed", mac)
    path, props = ble_gatt_dbus.lookup_device(bus, mac)

    def deliver(fields: dict) -> None:
        from gi.repository import GLib

        def _go() -> bool:
            cb = _callbacks.get(mac)
            if cb is None:
                return False
            try:
                cb(fields)
            except Exception:
                logger.exception("SmartShunt HEX update callback")
            return False

        GLib.idle_add(_go)

    logger.info("SmartShunt HEX session starting for %s", mac)
    return ble_async_loop.submit(
        lambda: _run_forever(mac, passkey, deliver, path, props))
