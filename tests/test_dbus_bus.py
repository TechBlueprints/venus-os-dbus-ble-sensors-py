"""The connection cache must give connections back.

Connections are a hard, shared, per-UID resource on Venus: root's limit
is the built-in 256 (the line in system.conf is commented out, and the
generous override lives in session.conf, which governs a different bus),
and every service on the box draws from it.  This service holds roughly
2.5 per published device by design — one per VeDbusService name, because
two services on one connection would collide registering '/'.

That design is fine as long as the cache is not append-only.  Two ways
it could become append-only, both fixed here:

  * a stale entry replaced without closing the old connection, and
  * an owner that goes away for good without releasing its key.

Losing the last Python reference does NOT close the socket — the GLib
watches pin the connection at the C level, which is the whole reason
this cache exists.
"""
from __future__ import annotations

import dbus

import dbus_bus


class _Bus:
    """Stands in for a BusConnection; records whether it was closed."""

    def __init__(self, connected: bool = True):
        self.closed = False
        self._connected = connected

    def get_is_connected(self) -> bool:
        return self._connected

    def close(self) -> None:
        self.closed = True


def _seed(monkeypatch, *buses):
    """Make SystemBus() hand out *buses* in order."""
    made = list(buses)
    monkeypatch.setattr(dbus_bus, "_bus_instances", {})
    monkeypatch.setattr(dbus_bus, "SystemBus", lambda: made.pop(0))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)


def test_same_key_reuses_one_connection(monkeypatch) -> None:
    first = _Bus()
    _seed(monkeypatch, first, _Bus())
    assert dbus_bus.get_bus("com.victronenergy.tank.a") is first
    assert dbus_bus.get_bus("com.victronenergy.tank.a") is first


def test_replacing_a_dead_connection_closes_it(monkeypatch) -> None:
    dead, fresh = _Bus(connected=False), _Bus()
    _seed(monkeypatch, fresh)     # the only bus SystemBus() will hand out
    dbus_bus._bus_instances["k"] = dead

    assert dbus_bus.get_bus("k") is fresh
    assert dead.closed is True, "dropping the reference does not close it"


def test_release_closes_and_evicts(monkeypatch) -> None:
    bus = _Bus()
    _seed(monkeypatch, bus, _Bus())
    dbus_bus.get_bus("com.victronenergy.tank.gone")

    dbus_bus.release_bus("com.victronenergy.tank.gone")
    assert bus.closed is True
    assert "com.victronenergy.tank.gone" not in dbus_bus._bus_instances


def test_release_is_a_no_op_for_an_unknown_key(monkeypatch) -> None:
    _seed(monkeypatch)
    dbus_bus.release_bus("never.seen")        # must not raise


def test_release_will_not_close_someone_elses_connection(monkeypatch) -> None:
    # The device left, then came back and re-registered before the old
    # role service was torn down.  Tearing it down must not close the
    # live connection the new one is using.
    old, new = _Bus(), _Bus()
    _seed(monkeypatch, new)
    dbus_bus._bus_instances["k"] = new

    dbus_bus.release_bus("k", old)
    assert new.closed is False
    assert dbus_bus._bus_instances["k"] is new


def test_release_survives_a_close_that_raises(monkeypatch) -> None:
    class _Stubborn(_Bus):
        def close(self):
            raise RuntimeError("already gone")

    _seed(monkeypatch)
    dbus_bus._bus_instances["k"] = _Stubborn()
    dbus_bus.release_bus("k")                 # must not propagate
    assert "k" not in dbus_bus._bus_instances


def test_hitting_the_ceiling_says_what_was_lost(monkeypatch, caplog) -> None:
    # The failure this line exists for: the caller is registering a
    # newly discovered device, and a device that cannot register never
    # appears at all — no alarm, no SENSOR_NOVALUE, indistinguishable
    # from being out of range.
    class _Limits(dbus.DBusException):
        def get_dbus_name(self):
            return "org.freedesktop.DBus.Error.LimitsExceeded"

    def _boom():
        raise _Limits("The maximum number of active connections for UID "
                      "has been reached")

    monkeypatch.setattr(dbus_bus, "_bus_instances", {})
    monkeypatch.setattr(dbus_bus, "SystemBus", _boom)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    caplog.set_level("CRITICAL", logger="dbus_bus")
    try:
        dbus_bus.get_bus("com.victronenergy.tank.brand_new")
    except _Limits:
        pass
    else:
        raise AssertionError("the caller still has to see the failure")

    text = caplog.text
    assert "per-UID limit" in text
    assert "com.victronenergy.tank.brand_new" in text
    assert "never in range" in text


def test_an_unrelated_dbus_error_is_not_relabelled(monkeypatch) -> None:
    class _Other(dbus.DBusException):
        def get_dbus_name(self):
            return "org.freedesktop.DBus.Error.NoServer"

    def _boom():
        raise _Other("no server")

    monkeypatch.setattr(dbus_bus, "_bus_instances", {})
    monkeypatch.setattr(dbus_bus, "SystemBus", _boom)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    try:
        dbus_bus.get_bus("k")
    except _Other:
        pass
    else:
        raise AssertionError("must propagate unchanged")
