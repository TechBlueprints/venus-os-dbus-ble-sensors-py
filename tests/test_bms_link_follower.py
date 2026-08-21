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

    def with_bms(self, cvl=13.8, ccl=50.0):
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
