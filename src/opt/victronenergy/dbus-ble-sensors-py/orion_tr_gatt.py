# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Non-blocking VREG writer for Victron chargers (Orion-TR, Blue Smart IP22).

This is the service's only outbound GATT path: DVCC setpoints, on/off, and
the user's persisted charge limits all arrive here as register writes.

The connection itself belongs to **bcmv2** (``bleak-connection-manager``,
installed by :mod:`ble_catcher`), so a write places its link with full
knowledge of what the rest of the box is doing with the radios, and
publishes claims of its own while the link is up.  What used to be a
hand-rolled BlueZ D-Bus state machine here — pair, connect, wait for
``ServicesResolved``, walk ``GetManagedObjects`` for the characteristics,
write, disconnect — is now bleak's problem, and the multi-adapter retry
walk is bcmv2's.

The GLib/asyncio split is strict, because the two mainloops must not
touch each other's objects:

* **GLib thread** — everything dbus-python: looking the device up in
  BlueZ, and registering the pairing agent (:mod:`ble_gatt_dbus`).
* **BLE loop thread** — everything bleak: resolve, connect, write
  (:mod:`ble_async_loop`, :mod:`ble_gatt_link`, :mod:`victron_vreg`).

Callers see none of that.  :meth:`AsyncGATTWriter.write_register` returns
immediately and ``on_done(success)`` fires on the GLib thread, which is
the same contract ``ble_charger_common``'s write queue has always used.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import dbus
import dbus.mainloop.glib  # noqa: F401 — sets up the glib mainloop integration

import ble_async_loop
import ble_gatt_dbus
import ble_gatt_link
import victron_vreg

logger = logging.getLogger(__name__)

# Ceiling on one write, end to end: resolve (possibly a discovery),
# connect with retries, pair, write, disconnect.  Generous, because the
# caller's queue collapses repeat setpoints rather than piling them up —
# but bounded, because a wedged write would block every later one.
OPERATION_TIMEOUT_S = 90.0


async def _perform_write(address: str, path: Optional[str],
                         props: Optional[dict], register_id: int,
                         value_bytes: bytes, pair: bool) -> None:
    """Resolve → connect → (pair) → write → disconnect, on the BLE loop."""
    device = await ble_gatt_link.resolve(address, path, props)
    client = await ble_gatt_link.connect(device, address)
    try:
        if pair:
            # Idempotent in bleak, but we only get here when BlueZ told us
            # the device is unbonded — and only then is our passkey agent
            # registered to answer for it.
            logger.info("%s: pairing", address)
            await client.pair()
        started = time.monotonic()
        await victron_vreg.write_register(client, register_id, value_bytes)
        # Success needs its own line.  Without one, the only evidence a
        # write worked is the absence of a failure, which cannot
        # distinguish "wrote" from "never ran" — and the latency is what
        # tells you a write that succeeded was still fighting for the
        # radio.
        logger.info("%s: GATT write %#06x ok in %.1fs",
                    address, register_id, time.monotonic() - started)
    finally:
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)


class _ReadCollector:
    """Assemble DATA_BULK + DATA_LAST into complete HEX frames."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self._bulk = bytearray()
        self.f7_n = 0x80

    def on_last(self, _char, data) -> None:
        self.frames.append(bytes(self._bulk) + bytes(data))
        self._bulk.clear()

    def on_bulk(self, _char, data) -> None:
        self._bulk.extend(data)

    def on_ctrl(self, _char, data) -> None:
        raw = bytes(data)
        if raw[:1] == b"\xf7" and len(raw) >= 3:
            self.f7_n = int.from_bytes(raw[1:3], "little") or 0x80


async def _start_notify(client, char, callback, acquired=None) -> None:
    """Subscribe, preferring AcquireNotify, recording it for teardown.

    ``acquired`` is a list the caller passes so :func:`_stop_notify_all`
    knows what to release.  That matters more than it looks: bluetoothd
    5.72 stores the notify client into ``chrc->notify_io->data`` without
    taking a reference, so an acquire still outstanding when the link
    goes away leaves a dangling pointer that detonates when the
    temporary device is cleaned up 30-120 s later.  Upstream fixed it in
    5.84/5.86; Venus ships 5.72.

    We ask for the fd-based path deliberately — StartNotify plus
    PropertiesChanged delivers empty payloads for these characteristics
    once the link is SMP-paired — so releasing it is our job.
    """
    try:
        await client.start_notify(char, callback,
                                  bluez={"use_start_notify": False})
        if acquired is not None:
            acquired.append(char)
        return
    except Exception:
        pass
    try:
        await client.start_notify(char, callback)
        if acquired is not None:
            acquired.append(char)
    except Exception:
        logger.warning("HEX notify failed on %s", char)


async def _stop_notify_all(client, acquired, ok: bool) -> None:
    """Release every notify we hold, before the link goes away.

    Best effort and never raising: a failure here must not mask the
    caller's own exception, and an already-dead link makes every one of
    these fail harmlessly.

    Residual hazard worth naming rather than hiding: this is a coroutine,
    so a cancellation between the last operation and here skips it
    entirely.  That window is now the only one that leaves an acquire
    outstanding, where before every session did.
    """
    if not acquired:
        return
    # Release ONLY on a session that completed normally.  Callers pass
    # ok=False when unwinding from an exception.
    #
    # This deliberately does NOT test client.is_connected.  That reads
    # BlueZ's cached Connected property, which is exactly the signal
    # that lies on a phantom connection — the first version of this
    # guard used it and prod kept crashing at the same rate, because a
    # failing session often still reports itself connected.
    #
    # Why it matters: releasing a notify on a link BlueZ has already
    # torn down is the "notify client already freed" precondition for
    # the 5.72 UAF, and the release walks notify_io_destroy, the crash
    # site, deliberately.  Measured on prod, six of six SIGSEGVs landed
    # within 0-1 s of a session drop.  A failed session has nothing
    # worth releasing anyway.
    if not ok:
        acquired.clear()
        return
    while acquired:
        char = acquired.pop()
        try:
            await client.stop_notify(char)
        except Exception:
            logger.debug("stop_notify failed on %s", char, exc_info=True)


def _push_payload(frames: list[bytes], register_id: int) -> Optional[bytes]:
    """Walk assembled HEX frames for a Push of *register_id*."""
    for frame in frames:
        parsed = victron_vreg.parse_push_frame(frame)
        if parsed is None:
            continue
        _inst, vreg_id, payload = parsed
        if vreg_id == register_id and payload:
            return payload
    return None


async def _credits(client, n: int = 0x80) -> None:
    try:
        await client.write_gatt_char(
            victron_vreg.CHAR_CONTROL,
            bytes([victron_vreg.OPCODE_READY_TO_RECV, n & 0xFF]),
            response=False)
    except Exception:
        pass


async def _perform_provision(address: str, path, props,
                             passkey: int, pair: bool,
                             timeout_s: float) -> dict:
    """Resolve -> connect -> hex_key_session.provision_session -> teardown.

    The dbus-python half (device lookup, pairing agent) happened on the
    GLib thread in :meth:`AsyncGATTWriter.provision_key` before this was
    scheduled; from here down it is pure bleak on the BLE loop.
    """
    import hex_key_session

    device = await ble_gatt_link.resolve(address, path, props)
    client = await ble_gatt_link.connect(device, address)
    try:
        return await hex_key_session.provision_session(
            client, passkey, timeout_s, pair=pair)
    finally:
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)


async def _perform_read(address: str, path: Optional[str],
                        props: Optional[dict], register_ids: list[int],
                        extra_writes: list[tuple[int, bytes]],
                        pair: bool) -> dict[int, bytes]:
    """Resolve → connect → GetValue (and optional SetValue) → disconnect."""
    device = await ble_gatt_link.resolve(address, path, props)
    client = await ble_gatt_link.connect(device, address)
    values: dict[int, bytes] = {}
    acquired: list = []
    ok = False
    try:
        if pair:
            logger.info("%s: pairing", address)
            await client.pair()
        collector = _ReadCollector()
        await _start_notify(client, victron_vreg.CHAR_CONTROL,
                            collector.on_ctrl, acquired)
        await _start_notify(client, victron_vreg.CHAR_DATA_LAST,
                            collector.on_last, acquired)
        await _start_notify(client, victron_vreg.CHAR_DATA_BULK,
                            collector.on_bulk, acquired)
        # CTRL read switches the peripheral into CBOR mode.
        try:
            await client.read_gatt_char(victron_vreg.CHAR_CONTROL)
        except Exception:
            pass
        await client.write_gatt_char(
            victron_vreg.CHAR_CONTROL, b"\xFA\x80\xFF", response=False)
        await asyncio.sleep(victron_vreg.HANDSHAKE_SETTLE_S)
        await _credits(client, 0x80)
        await asyncio.sleep(victron_vreg.HANDSHAKE_SETTLE_S)
        # A GetValue as the first CBOR request is often swallowed.
        # Subscribe the public temperature register to open the Push
        # stream — not instance 0, which floods the IP22.
        await client.write_gatt_char(
            victron_vreg.CHAR_DATA_LAST,
            bytes([0x03, 0x00, 0x9F, 0x19, 0xED, 0xDB, 0xFF]),
            response=False)
        await asyncio.sleep(0.8)
        await _credits(client, collector.f7_n)

        # Read first.  A SetValue (Instant Readout enable) can flood
        # the tunnel and starve the GetValue Push we actually need.
        for register_id in register_ids:
            await client.write_gatt_char(
                victron_vreg.CHAR_DATA_LAST,
                victron_vreg.encode_read_command(register_id),
                response=False)
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.3)
                raw = victron_vreg.scan_for_vreg(collector.frames, register_id)
                if raw is None:
                    raw = _push_payload(collector.frames, register_id)
                if raw is not None:
                    values[register_id] = raw
                    break
                await _credits(client, collector.f7_n)

        for register_id, value_bytes in extra_writes:
            await client.write_gatt_char(
                victron_vreg.CHAR_DATA_LAST,
                victron_vreg.encode_write_command(register_id, value_bytes),
                response=False)
            logger.info("%s: HEX write 0x%04X = %s",
                        address, register_id, value_bytes.hex())
            await asyncio.sleep(0.4)
            await _credits(client, collector.f7_n)

        if not values:
            logger.info("%s: HEX read saw %d frames, no live VREGs",
                        address, len(collector.frames))
        ok = True
        return values
    finally:
        # Release the notify acquires BEFORE the link goes away.  On
        # BlueZ 5.72 an outstanding acquire at disconnect is a dangling
        # chrc->notify_io->data, and the crash lands 30-120 s later in
        # temporary-device cleanup, far from anything that names us.
        # DISABLED pending diagnosis.  Releasing notifies was added to
        # stop stranded acquires planting the BlueZ 5.72 UAF; prod's
        # crash rate then went from ~5-9/hr to 30-50/hr, with every
        # SIGSEGV landing within 0-1 s of one of our session teardowns.
        #
        # Two narrowing attempts both failed to move the rate: skipping
        # the release on a dead link (is_connected lies on a phantom
        # connection, which is exactly the failing case) and skipping it
        # on a failed session (rate got worse still).  So the mechanism
        # is not understood, and the release is off entirely until it
        # is — this restores the behaviour prod ran at its lower rate.
        #
        # Cost of being here: acquires are stranded again, which is the
        # leak this was meant to fix.  That is the lesser harm at
        # 50 crashes/hr.
        acquired.clear()
        try:
            await ble_gatt_link.disconnect(client)
        finally:
            # Synchronous, so it still runs if the await above is
            # cut short by cancellation — that is when the socket
            # is most likely to be stranded.
            ble_gatt_link.force_close(client)


class AsyncGATTWriter:
    """Single-slot, non-blocking VREG writer.

    One write at a time by design: the chargers do not appreciate
    overlapping sessions, and ``ble_charger_common`` already collapses
    bursts for the same register while :attr:`busy` is set.
    """

    def __init__(self, bus: dbus.SystemBus):
        self._bus = bus
        self._busy = False
        self._agent: Optional[ble_gatt_dbus.PairingAgent] = None

    @property
    def busy(self) -> bool:
        return self._busy

    def write_register(self, mac: str, passkey: int, register_id: int,
                       value_bytes: bytes,
                       on_done: Optional[Callable] = None,
                       prefer_adapter: Optional[str] = None):
        """Start an asynchronous register write.

        Args:
            mac: Device MAC (e.g. ``"EF:C1:11:9D:A3:91"``)
            passkey: BLE pairing passkey, used only if BlueZ has no bond
            register_id: VREG register id
            value_bytes: Value bytes (little-endian)
            on_done: ``Callback(success: bool)``, called on the GLib thread
            prefer_adapter: The card to resolve the device on when more
                than one knows it, as a MAC (what
                ``get_preferred_adapter`` stores) or a legacy ``hciN``.
                Without it the lookup falls back to
                connected-then-bonded order, which on a multi-card box
                can hand back a path on the adapter that is busy
                scanning; the connection then never completes and the
                write dies on the operation timeout.
        """
        if self._busy:
            logger.warning("GATT writer busy, rejecting write for %s", mac)
            if on_done:
                on_done(False)
            return

        mac = mac.upper()
        self._busy = True

        if not ble_async_loop.start():
            logger.error("%s: BLE connection stack unavailable — cannot "
                         "write VREG 0x%04X", mac, register_id)
            self._finish(on_done, False)
            return

        # dbus-python work, on this (GLib) thread only.
        path, props = ble_gatt_dbus.lookup_device(
            self._bus, mac, prefer_adapter=prefer_adapter)
        needs_pair = not (props or {}).get("Paired")
        if needs_pair:
            self._agent = ble_gatt_dbus.PairingAgent(self._bus, passkey, mac)
            self._agent.register()

        logger.info("GATT write starting for %s: reg=0x%04X val=%s%s",
                    mac, register_id, value_bytes.hex(),
                    "" if path else " (device unknown to BlueZ)")

        def make_coro():
            return asyncio.wait_for(
                _perform_write(mac, path, props, register_id,
                               bytes(value_bytes), needs_pair),
                timeout=OPERATION_TIMEOUT_S)

        def settled(_result, error):
            if error is not None:
                if isinstance(error, asyncio.TimeoutError):
                    logger.error("%s: GATT write 0x%04X timed out after "
                                 "%.0fs", mac, register_id,
                                 OPERATION_TIMEOUT_S)
                else:
                    logger.error("%s: GATT write 0x%04X failed: %s",
                                 mac, register_id, error)
            self._finish(on_done, error is None)

        if not ble_async_loop.submit(make_coro, settled):
            logger.error("%s: could not schedule GATT write 0x%04X",
                         mac, register_id)
            self._finish(on_done, False)

    def read_registers(self, mac: str, passkey: int,
                       register_ids: list[int],
                       extra_writes: Optional[list[tuple[int, bytes]]] = None,
                       on_done: Optional[Callable] = None):
        """Short HEX GetValue session.  ``on_done(success, values)``.

        ``extra_writes`` is a list of ``(register_id, value_bytes)`` applied
        before the reads — used to turn Instant Readout on (``0xEC7D=1``)
        without a second connect.  The slot is the same one as
        :meth:`write_register`, so a DVCC setpoint and a telemetry poll
        never overlap.
        """
        if self._busy:
            logger.warning("GATT writer busy, rejecting read for %s", mac)
            if on_done:
                on_done(False, {})
            return

        mac = mac.upper()
        self._busy = True
        writes = list(extra_writes or [])

        if not ble_async_loop.start():
            logger.error("%s: BLE connection stack unavailable — cannot "
                         "read VREGs", mac)
            self._finish_read(on_done, False, {})
            return

        path, props = ble_gatt_dbus.lookup_device(self._bus, mac)
        needs_pair = not (props or {}).get("Paired")
        if needs_pair:
            self._agent = ble_gatt_dbus.PairingAgent(self._bus, passkey, mac)
            self._agent.register()

        logger.info("HEX read starting for %s: regs=%s writes=%s",
                    mac, [f"0x{r:04X}" for r in register_ids],
                    [f"0x{r:04X}" for r, _v in writes])

        def make_coro():
            return asyncio.wait_for(
                _perform_read(mac, path, props, list(register_ids),
                              writes, needs_pair),
                timeout=OPERATION_TIMEOUT_S)

        def settled(result, error):
            if error is not None:
                if isinstance(error, asyncio.TimeoutError):
                    logger.error("%s: HEX read timed out after %.0fs",
                                 mac, OPERATION_TIMEOUT_S)
                else:
                    logger.error("%s: HEX read failed: %s", mac, error)
                self._finish_read(on_done, False, {})
                return
            self._finish_read(on_done, True, result or {})

        if not ble_async_loop.submit(make_coro, settled):
            logger.error("%s: could not schedule HEX read", mac)
            self._finish_read(on_done, False, {})


    def provision_key(self, mac: str, passkey: int,
                      on_done: Callable,
                      prefer_adapter: Optional[str] = None,
                      timeout_s: float = 60.0):
        """Read the Instant Readout advertisement key (VREG 0xEC65).

        ``on_done(payload_or_None)``, on the GLib thread.  The payload is
        what the drivers persist: key/firmware/product_id/temperature/
        hardware_version, plus ``adapter``.

        This used to be a subprocess (orion_tr_key_cli), which meant two
        of our own processes connecting to one device — the collision
        BlueZ refuses (dev->att_io).  In-process it shares this writer's
        single slot with every mode write and telemetry poll, so the
        serialisation is the slot itself and there is no second process
        left to referee.

        Provisioning can legitimately hold the slot for up to
        ``timeout_s``: it runs once per device, and a mode write arriving
        meanwhile is rejected-with-callback exactly as during any other
        busy window.  The overall bound mirrors the old subprocess bound
        (timeout_s + 20) so a wedged session frees the slot rather than
        holding it forever.
        """
        if self._busy:
            logger.warning("GATT writer busy, rejecting provisioning for %s",
                           mac)
            on_done(None)
            return

        mac = mac.upper()
        self._busy = True

        if not ble_async_loop.start():
            logger.error("%s: BLE connection stack unavailable — cannot "
                         "provision", mac)
            self._finish(lambda ok: on_done(None), False)
            return

        # dbus-python work, on this (GLib) thread only.
        path, props = ble_gatt_dbus.lookup_device(
            self._bus, mac, prefer_adapter=prefer_adapter)
        needs_pair = not (props or {}).get("Paired")
        if needs_pair:
            self._agent = ble_gatt_dbus.PairingAgent(self._bus, passkey, mac)
            self._agent.register()

        logger.info("Key provisioning starting for %s%s", mac,
                    "" if path else " (device unknown to BlueZ)")

        adapter = ble_gatt_dbus.adapter_from_path(path)

        def make_coro():
            return asyncio.wait_for(
                _perform_provision(mac, path, props, passkey, needs_pair,
                                   timeout_s),
                timeout=timeout_s + 20.0)

        def settled(result, error):
            if error is not None:
                if isinstance(error, asyncio.TimeoutError):
                    logger.error("%s: provisioning timed out after %.0fs",
                                 mac, timeout_s + 20.0)
                else:
                    logger.error("%s: provisioning failed: %s", mac, error)
                self._finish(lambda ok: on_done(None), False)
                return
            payload = dict(result or {})
            payload.setdefault("adapter", adapter)
            self._finish(lambda ok: on_done(payload), True)

        if not ble_async_loop.submit(make_coro, settled):
            logger.error("%s: could not schedule provisioning", mac)
            self._finish(lambda ok: on_done(None), False)

    def _finish_read(self, on_done: Optional[Callable], success: bool,
                     values: dict) -> None:
        if self._agent is not None:
            self._agent.unregister()
            self._agent = None
        self._busy = False
        if on_done is not None:
            try:
                on_done(success, values)
            except Exception:
                logger.exception("HEX read completion callback raised")

    def _finish(self, on_done: Optional[Callable], success: bool) -> None:
        """Release the slot and the agent, then report to the caller."""
        if self._agent is not None:
            self._agent.unregister()
            self._agent = None
        self._busy = False
        if on_done is not None:
            try:
                on_done(success)
            except Exception:
                logger.exception("GATT write completion callback raised")
