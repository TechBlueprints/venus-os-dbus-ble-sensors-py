# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Device resolution and connection for GATT work, on top of bcmv2.

Everything here runs on the BLE event loop thread (:mod:`ble_async_loop`)
and touches only bleak — no dbus-python, which is confined to the GLib
main thread.

**Why resolution is ours and not bleak's.**  Handed a plain address,
bleak's BlueZ backend calls ``BleakScanner.find_device_by_address`` — and
does it with an explicit backend argument, which sidesteps the catcher's
rebinding, so the discovery would be unclaimed and invisible to the rest
of the box.  It also drives the scan through BlueZ, which materialises a
``Device1`` object per advertiser it sees; the HCI tap exists to keep that
from happening (see :mod:`hci_scan_control`), and paying it on every
setpoint write would undo the point.

So resolution goes, in order:

1. **Ask BlueZ what it already knows.**  Our chargers are bonded, and a
   bonded device keeps its ``Device1`` object on the adapter it is bonded
   to across reboots.  This costs no radio time at all.  The resulting
   ``BLEDevice`` carries its D-Bus path, which bcmv2 reads as an explicit
   adapter choice — right, because a bond *is* an adapter choice — and
   still lands claims and connection tuning on the card the link uses.
2. **Only if BlueZ has never seen it** — first provisioning, or a bond
   that was removed — fall back to a discovery.  That runs through bcmv2's
   wrapped scanner, so it holds the adapter's hard ``hciN.scan`` claim for
   its duration and the rest of the box knows the card is busy.  This is
   the one path that puts a radio into active scan, and it is bounded by
   :data:`DISCOVERY_TIMEOUT_S`.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How long a fallback discovery may run before we give up on the device.
DISCOVERY_TIMEOUT_S = 12.0

# Attempts bleak-retry-connector makes per connection.  Kept modest: a
# charger that is out of range should fail the write and let the caller's
# queue retry later, not tie up the writer for a minute.
CONNECT_ATTEMPTS = 3
CONNECT_TIMEOUT_S = 20.0


def make_ble_device(address: str, path: str, props: dict | None):
    """Build a ``BLEDevice`` for a device BlueZ already knows.

    *props* is the ``org.bluez.Device1`` property dict as plain Python
    values.  bleak reads only ``Adapter`` and ``Alias`` out of it; the
    D-Bus path is what actually matters, since carrying one is what makes
    bleak connect without scanning first.
    """
    from bleak.backends.device import BLEDevice

    props = dict(props or {})
    name = props.get("Alias") or props.get("Name") or address
    return BLEDevice(address, name, {"path": path, "props": props})


async def _discover(address: str, timeout: float):
    """Active-discovery fallback.  Returns a ``BLEDevice`` or ``None``.

    The catcher is installed with ``wrap_scanner=True``, so this is a
    bcmv2 scanner: it ranks the cards by live occupancy, takes the
    winner's hard ``hciN.scan`` claim for the duration, skips cards
    another process is already scanning on, and releases on stop.  We
    therefore pick no adapter and hold no claim by hand — resolving
    ``bleak.BleakScanner`` at call time is what gets us the wrapped class.

    Bounded by *timeout* because this is the one place the service scans
    actively, and it should be over quickly.
    """
    import bleak

    logger.info("%s: not known to BlueZ — running a %.0fs discovery",
                address, timeout)
    return await bleak.BleakScanner.find_device_by_address(
        address, timeout=timeout)


async def resolve(address: str, path: str | None = None,
                  props: dict | None = None,
                  timeout: float = DISCOVERY_TIMEOUT_S):
    """Return a connectable ``BLEDevice`` for *address*.

    *path*/*props* come from the caller's BlueZ object-manager lookup on
    the GLib thread (see :func:`ble_gatt_dbus.lookup_device`).  Raises
    :class:`DeviceNotFound` when neither BlueZ nor a discovery can produce
    the device.
    """
    if path:
        return make_ble_device(address, path, props)
    device = await _discover(address, timeout)
    if device is None:
        raise DeviceNotFound(
            f"{address}: not known to BlueZ and no advertisement seen "
            f"in {timeout:.0f}s")
    return device


class DeviceNotFound(Exception):
    """Raised when a device can be neither looked up nor discovered."""


def unreachable(exc: BaseException) -> bool:
    """Whether *exc* just means "the device is not answering right now".

    Callers retry these on a timer, so they are an expected steady state
    for a sensor that is switched off, out of range, or simply not near
    an adapter we are allowed to use — not a fault to be reported with a
    stack trace on every attempt.  Anything else is a real bug and should
    keep its traceback.

    bleak's own error for this is raised from several layers, so match on
    the type names rather than importing them: BleakNotFoundError comes
    from bleak-retry-connector, BleakDeviceNotFoundError from bleak, and
    :class:`DeviceNotFound` from our own resolution step.
    """
    if isinstance(exc, DeviceNotFound):
        return True
    chain = (exc, exc.__cause__, exc.__context__)
    names = {type(link).__name__ for link in chain if link is not None}
    return bool(names & {"BleakNotFoundError", "BleakDeviceNotFoundError"})


async def connect(device, name: str | None = None):
    """Connect to *device* through bcmv2, with retry semantics.

    bcmv2 routes but never retries — that is bleak-retry-connector's job,
    and going through it also avoids the bare-``connect()`` warning the
    catcher logs for callers that skip it.
    """
    import bleak
    from bleak_retry_connector import establish_connection

    # Resolved at call time, never imported by value: the catcher rebinds
    # ``bleak.BleakClient`` at install, and a module-level ``from bleak
    # import BleakClient`` here would capture the unrouted original.
    return await establish_connection(
        bleak.BleakClient,
        device,
        name or getattr(device, "address", "device"),
        max_attempts=CONNECT_ATTEMPTS,
        timeout=CONNECT_TIMEOUT_S,
    )


async def disconnect(client) -> None:
    """Best-effort teardown; a failed disconnect must not fail the write."""
    try:
        await client.disconnect()
    except Exception:
        logger.debug("disconnect failed (link probably already gone)",
                     exc_info=True)


async def settle(seconds: float) -> None:
    """Give the peripheral a beat between protocol steps."""
    await asyncio.sleep(seconds)
