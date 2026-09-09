"""The advertisement router routes by advertised-name prefix.

The Power Watchdog (and other name-routed devices) carry no distinctive
manufacturer id — they are identified by a local-name prefix (``WD_``,
``PM``).  The router gained a fifth registration type,
``/ble_advertisements/{service}/name_prefix/{prefix}``, so such a service
can ride sensors-py's single passive tap instead of running its own scan,
exactly the way the internal EasyStart driver does.
"""
from __future__ import annotations

import os

import pytest

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


@pytest.fixture()
def router(monkeypatch):
    import dbus
    # conftest stubs dbus scalar/array types as arg-less classes; make the
    # ones the emit path constructs pass their value through so the real
    # process_name_advertisement can run and we can inspect the signal.
    monkeypatch.setattr(dbus, "Array", lambda data, signature=None: bytes(data), raising=False)
    for name in ("String", "UInt16", "Int16"):
        monkeypatch.setattr(dbus, name, lambda x: x, raising=False)
    import ble_advertisement_router as r
    R = r.BleAdvertisementRouter.__new__(r.BleAdvertisementRouter)
    R._mfg_registrations = {}
    R._mac_registrations = {}
    R._pid_registrations = {}
    R._pid_range_registrations = {}
    R._name_prefix_registrations = {}
    R._emitters = {}

    class _Root:
        def update_heartbeat(self):
            pass
    R._root = _Root()
    return R


class _Emitter:
    def __init__(self):
        self.calls = []

    def Advertisement(self, mac, mfg, data, rssi, iface, name):
        self.calls.append((mac, mfg, data, rssi, iface, name))


def test_parse_registers_a_name_prefix_path(router) -> None:
    path = "/ble_advertisements/dbus-power-watchdog/name_prefix/WD_"
    router._parse_registrations("com.victronenergy.dbus-power-watchdog",
                                path, "<node/>")
    assert router._name_prefix_registrations == {"WD_": {path}}
    assert router.get_registered_name_prefixes() == {"WD_"}
    assert router.has_registrations() is True


def test_routes_a_matching_name_to_its_consumer_with_the_name_in_the_signal(router) -> None:
    path = "/ble_advertisements/dbus-power-watchdog/name_prefix/WD_"
    router._name_prefix_registrations = {"WD_": {path}}
    em = _Emitter()
    router._emitters = {path: em}
    ok = router.process_name_advertisement(
        "aabbccddeeff", "WD_E7_00a0508d", -55, "684E054477B0")
    assert ok is True and len(em.calls) == 1
    mac, mfg, data, rssi, iface, name = em.calls[0]
    assert mac == "AA:BB:CC:DD:EE:FF"          # tap mac -> colon upper
    assert name == "WD_E7_00a0508d"            # full name for model disambiguation
    assert iface == "684E054477B0"             # the card that heard it
    assert rssi == -55


def test_does_not_route_a_non_matching_name(router) -> None:
    path = "/ble_advertisements/dbus-power-watchdog/name_prefix/WD_"
    router._name_prefix_registrations = {"WD_": {path}}
    em = _Emitter()
    router._emitters = {path: em}
    assert router.process_name_advertisement(
        "aabbccddeeff", "EasyStart_1234", -55, "684E054477B0") is False
    assert em.calls == []


def test_two_prefixes_route_independently(router) -> None:
    wd = "/ble_advertisements/dpw/name_prefix/WD_"
    pm = "/ble_advertisements/dpw/name_prefix/PM"
    router._name_prefix_registrations = {"WD_": {wd}, "PM": {pm}}
    ewd, epm = _Emitter(), _Emitter()
    router._emitters = {wd: ewd, pm: epm}
    router.process_name_advertisement("aabbccddeeff", "PMD1234", -40, "684E054477B0")
    assert len(epm.calls) == 1 and ewd.calls == []


def test_removal_clears_name_prefix_registrations(router) -> None:
    path = "/ble_advertisements/dbus-power-watchdog/name_prefix/WD_"
    router._name_prefix_registrations = {"WD_": {path}}
    router._notify_registrations_changed = lambda: None
    router._remove_service_registrations("dbus-power-watchdog")
    assert router._name_prefix_registrations == {}
    assert router.has_registrations() is False


def test_sensors_py_feeds_the_router_and_updates_the_tap_prefix_set_in_place() -> None:
    """Source-level: the name path feeds the router, and registrations fold
    into the tap's mutable name-prefix set without a restart."""
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    assert "self._router.process_name_advertisement(" in src
    assert "self._name_prefixes: set" in src
    assert "self._internal_name_prefixes" in src
    # mutated in place (not reassigned) so the running tap sees it
    assert "self._name_prefixes.clear()" in src
    assert "self._name_prefixes.update(" in src
    assert "get_registered_name_prefixes()" in src
