"""A disconnect that fails must still close the D-Bus socket.

bleak closes its MessageBus in the last statements of disconnect(),
after its own try/finally, so a BlueZ Disconnect that raises skips them
and strands the connection.  On Venus the system bus allows 256
connections per UID — the limit line in system.conf is commented out and
the generous override is in session.conf, which governs a different bus
— so stranded connections are a countable resource.  When that ceiling
was reached on prod, the visible symptom was that any service trying to
RESTART could not reach the bus at all.
"""
from __future__ import annotations

import asyncio

import ble_gatt_link


class _Bus:
    def __init__(self, raise_on_close=False):
        self.closed = False
        self._raise = raise_on_close

    def disconnect(self):
        if self._raise:
            raise RuntimeError("bus close failed")
        self.closed = True


class _Backend:
    def __init__(self, bus):
        self._bus = bus


class _Client:
    def __init__(self, bus, disconnect_raises=False, hang=False):
        self._backend = _Backend(bus)
        self._raises = disconnect_raises
        self._hang = hang

    async def disconnect(self):
        if self._hang:
            await asyncio.sleep(3600)
        if self._raises:
            raise RuntimeError("BlueZ Disconnect failed")


def test_force_close_closes_the_bus() -> None:
    bus = _Bus()
    client = _Client(bus)
    ble_gatt_link.force_close(client)
    assert bus.closed is True
    assert client._backend._bus is None      # not closed twice later


def test_force_close_is_idempotent() -> None:
    client = _Client(_Bus())
    ble_gatt_link.force_close(client)
    ble_gatt_link.force_close(client)        # must not raise


def test_force_close_survives_a_raising_bus() -> None:
    bus = _Bus(raise_on_close=True)
    client = _Client(bus)
    ble_gatt_link.force_close(client)        # must not propagate
    assert client._backend._bus is None


def test_a_raising_disconnect_still_closes_the_bus() -> None:
    # The exact bleak bug: Disconnect raises, so bleak's own bus close
    # never runs.
    bus = _Bus()
    client = _Client(bus, disconnect_raises=True)
    asyncio.get_event_loop().run_until_complete(
        ble_gatt_link.disconnect(client))
    assert bus.closed is True


def test_cancellation_still_closes_the_bus() -> None:
    # Our own wait_for timeout cancels mid-disconnect.  An await in a
    # finally would re-raise immediately and never clean up; the
    # synchronous close is why this passes.
    bus = _Bus()
    client = _Client(bus, hang=True)

    async def scenario():
        task = asyncio.ensure_future(ble_gatt_link.disconnect(client))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.get_event_loop().run_until_complete(scenario())
    assert bus.closed is True


def test_no_backend_is_harmless() -> None:
    class _Bare:
        pass
    ble_gatt_link.force_close(_Bare())       # must not raise
