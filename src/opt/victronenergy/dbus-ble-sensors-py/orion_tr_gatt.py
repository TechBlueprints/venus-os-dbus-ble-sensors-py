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
        await victron_vreg.write_register(client, register_id, value_bytes)
    finally:
        await ble_gatt_link.disconnect(client)


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
                       on_done: Optional[Callable] = None):
        """Start an asynchronous register write.

        Args:
            mac: Device MAC (e.g. ``"EF:C1:11:9D:A3:91"``)
            passkey: BLE pairing passkey, used only if BlueZ has no bond
            register_id: VREG register id
            value_bytes: Value bytes (little-endian)
            on_done: ``Callback(success: bool)``, called on the GLib thread
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
        path, props = ble_gatt_dbus.lookup_device(self._bus, mac)
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
