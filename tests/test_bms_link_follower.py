"""
BmsLinkFollower logic against an injected fake bus.

The follower's D-Bus surface is isolated behind the bus_ops object, so
these tests drive the pure engagement/disengagement/setpoint logic with a
fake — no D-Bus, no systemcalc, no charger role.
"""
from __future__ import annotations

from bms_link_follower import (
    ACTIVE_BMS_PATH,
    BmsLinkFollower,
    CHARGE_CURRENT_PATH,
    CHARGE_VOLTAGE_PATH,
    DVCC_PATH,
    SETTINGS_SERVICE,
    SYSTEMCALC_SERVICE,
)

BMS = "com.victronenergy.battery.jbd_bms__a4c138334124"
CHARGER = "com.victronenergy.charger.ip22_f286c3324cd2"


class FakeBus:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.chargers = [CHARGER]
        self.reject_paths = set()

    def get(self, service, path):
        return self.values.get((service, path))

    def set(self, service, path, value):
        if path in self.reject_paths:
            return False
        self.writes.append((service, path, value))
        return True

    def charger_services(self):
        return list(self.chargers)

    def with_bms(self, cvl=13.8, ccl=50.0, dvcc=1):
        self.values[(SETTINGS_SERVICE, DVCC_PATH)] = dvcc
        self.values[(SYSTEMCALC_SERVICE, ACTIVE_BMS_PATH)] = BMS
        self.values[(BMS, CHARGE_VOLTAGE_PATH)] = cvl
        self.values[(BMS, CHARGE_CURRENT_PATH)] = ccl
        return self


def test_engages_and_writes_setpoints():
    bus = FakeBus().with_bms(cvl=13.8, ccl=50.0)
    follower = BmsLinkFollower(bus_ops=bus)
    assert follower.tick() is True
    assert bus.writes == [
        (CHARGER, "/Settings/BmsPresent", 1),
        (CHARGER, "/Link/ChargeVoltage", 13.8),
        (CHARGER, "/Link/ChargeCurrent", 50.0),
    ]


def test_unchanged_limits_stay_off_the_bus():
    bus = FakeBus().with_bms()
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.writes.clear()
    follower.tick()
    assert bus.writes == []


def test_changed_limits_are_rewritten():
    bus = FakeBus().with_bms(ccl=50.0)
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.writes.clear()
    bus.values[(BMS, CHARGE_CURRENT_PATH)] = 20.0
    follower.tick()
    assert (CHARGER, "/Link/ChargeCurrent", 20.0) in bus.writes
    # engagement is not repeated
    assert (CHARGER, "/Settings/BmsPresent", 1) not in bus.writes


def test_bms_loss_releases_the_charger():
    bus = FakeBus().with_bms()
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.writes.clear()
    del bus.values[(SYSTEMCALC_SERVICE, ACTIVE_BMS_PATH)]
    follower.tick()
    assert bus.writes == [(CHARGER, "/Settings/BmsPresent", 0)]
    # and only once
    bus.writes.clear()
    follower.tick()
    assert bus.writes == []


def test_dvcc_off_counts_as_absent():
    # systemcalc keeps /ActiveBmsService set with DVCC off (verified on
    # Venus 3.72), so the toggle must gate the follower directly
    bus = FakeBus().with_bms()
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.writes.clear()
    bus.values[(SETTINGS_SERVICE, DVCC_PATH)] = 0
    follower.tick()
    assert bus.writes == [(CHARGER, "/Settings/BmsPresent", 0)]


def test_dvcc_never_configured_counts_as_absent():
    bus = FakeBus().with_bms()
    del bus.values[(SETTINGS_SERVICE, DVCC_PATH)]
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    assert bus.writes == []


def test_bms_without_limits_counts_as_absent():
    bus = FakeBus().with_bms()
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.writes.clear()
    del bus.values[(BMS, CHARGE_CURRENT_PATH)]
    follower.tick()
    assert bus.writes == [(CHARGER, "/Settings/BmsPresent", 0)]


def test_republished_charger_is_reengaged():
    bus = FakeBus().with_bms()
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    # role service drops off the bus (device disabled), then returns
    bus.chargers = []
    follower.tick()
    bus.chargers = [CHARGER]
    bus.writes.clear()
    follower.tick()
    assert bus.writes[0] == (CHARGER, "/Settings/BmsPresent", 1)


def test_failed_engagement_never_writes_setpoints():
    bus = FakeBus().with_bms()
    bus.reject_paths.add("/Settings/BmsPresent")
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    assert all(path == "/Settings/BmsPresent" for _, path, _ in bus.writes) is True or bus.writes == []


def test_partial_setpoint_write_retries_next_tick():
    bus = FakeBus().with_bms()
    bus.reject_paths.add("/Link/ChargeCurrent")
    follower = BmsLinkFollower(bus_ops=bus)
    follower.tick()
    bus.reject_paths.clear()
    bus.writes.clear()
    follower.tick()
    assert (CHARGER, "/Link/ChargeVoltage", 13.8) in bus.writes
    assert (CHARGER, "/Link/ChargeCurrent", 50.0) in bus.writes


class _Conn:
    """Stands in for a private BusConnection; liveness is scriptable."""

    def __init__(self, connected=True):
        self.connected = connected
        self.closed = False

    def get_is_connected(self):
        return self.connected

    def close(self):
        self.closed = True

    def list_names(self):
        return []


def _ops_with(monkeypatch, first_conn):
    """A real DbusBusOps whose bus constructor is scripted."""
    import bms_link_follower as blf

    made = [first_conn]

    def _make():
        return made.pop(0) if made else _Conn()

    monkeypatch.setattr(blf.DbusBusOps, "_new_bus", staticmethod(_make))
    return blf.DbusBusOps(), made


def test_a_live_bus_is_left_alone(monkeypatch) -> None:
    conn = _Conn()
    ops, _ = _ops_with(monkeypatch, conn)
    assert ops._live_bus() is conn
    assert conn.closed is False


def test_a_dead_bus_is_rebuilt_and_said_out_loud(monkeypatch, caplog) -> None:
    """A dbus-daemon restart must not leave the follower silently inert.

    With NULL_MAIN_LOOP nothing ever dispatches the Disconnected
    message, so exit-on-disconnect never fires and get/set would just
    swallow exceptions against a dead socket forever — the charger
    keeping its last setpoints with nothing saying why.
    """
    dead = _Conn(connected=False)
    ops, _ = _ops_with(monkeypatch, dead)

    caplog.set_level("WARNING", logger="bms_link_follower")
    fresh = ops._live_bus()
    assert fresh is not dead
    assert fresh.get_is_connected()
    assert dead.closed is True, "the corpse must not keep counting"
    assert "rebuilding" in caplog.text


def test_rebuild_is_rate_limited(monkeypatch) -> None:
    # A daemon that is genuinely gone costs one attempt per interval,
    # not one per follower tick.
    import bms_link_follower as blf

    dead = _Conn(connected=False)
    made = []

    def _make():
        conn = _Conn(connected=False)   # rebuilds also come up dead
        made.append(conn)
        return conn

    monkeypatch.setattr(blf.DbusBusOps, "_new_bus",
                        staticmethod(lambda: dead))
    ops = blf.DbusBusOps()
    monkeypatch.setattr(blf.DbusBusOps, "_new_bus", staticmethod(_make))

    ops._live_bus()
    ops._live_bus()
    ops._live_bus()
    assert len(made) == 1, "retries inside the window must not rebuild"


def test_a_closed_follower_stays_closed(monkeypatch) -> None:
    # close() means the owner stopped us; a rebuild here would resurrect
    # a connection nothing will ever close again.
    conn = _Conn()
    ops, _ = _ops_with(monkeypatch, conn)
    ops.close()
    assert conn.closed is True
    assert ops._live_bus() is None
