"""Steady-state log volume: the operator asked for less, measured first.

Prod over 94 h ran 40 lines/h.  Half of it was two things: the four-line
registration chorus per device per restart (and bcm deploys restart this
service several times a day), and four lines per A/C cycle from the
EasyStart driver.  A third source — the SmartShunt "Service Discovery
has not been performed yet" traceback — had been ~30% of the log before
the discovery gate; it is at zero now but the code path is still there.

These pin the levels, because the defect is "which level did the author
pick" and nothing at runtime observes that.
"""
from __future__ import annotations

import logging
import os
import re

import pytest

import log_filters
import smartshunt_hex

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


def _read(name: str) -> str:
    return open(os.path.join(SRC, name)).read()


# --- vedbus: root-logger INFO chatter, filtered by module --------------

def _record(module: str, level: int) -> logging.LogRecord:
    r = logging.LogRecord("root", level, "/x/%s.py" % module, 1, "m", (), None)
    r.module = module
    return r


def test_vedbus_info_is_dropped_but_its_warnings_pass() -> None:
    f = log_filters.QuietVedbusFilter()
    assert f.filter(_record("vedbus", logging.INFO)) is False
    assert f.filter(_record("vedbus", logging.WARNING)) is True
    assert f.filter(_record("vedbus", logging.ERROR)) is True


def test_other_modules_are_untouched() -> None:
    f = log_filters.QuietVedbusFilter()
    assert f.filter(_record("dbus_role_service", logging.INFO)) is True
    assert f.filter(_record("ble_device_easystart", logging.DEBUG)) is True


def test_install_is_idempotent_and_skipped_in_debug() -> None:
    root = logging.getLogger()
    h = logging.StreamHandler()
    root.addHandler(h)
    try:
        assert log_filters.install(debug=True) == 0
        assert not any(isinstance(x, log_filters.QuietVedbusFilter) for x in h.filters)
        assert log_filters.install(debug=False) >= 1
        n = sum(isinstance(x, log_filters.QuietVedbusFilter) for x in h.filters)
        log_filters.install(debug=False)
        assert sum(isinstance(x, log_filters.QuietVedbusFilter) for x in h.filters) == n, (
            "a second install must not stack a second filter")
    finally:
        root.removeHandler(h)


def test_main_installs_the_filter_after_setup_logging() -> None:
    src = _read("dbus_ble_sensors.py")
    a = src.index("setup_logging(args.debug)")
    b = src.index("log_filters.install(args.debug)")
    assert a < b, "filters attach to the handlers setup_logging creates"


# --- registration: one INFO line per device, the rest DEBUG -------------

def test_registration_chorus_is_one_info_line() -> None:
    sensors = _read("dbus_ble_sensors.py")
    role = _read("dbus_role_service.py")
    assert re.search(r'logging\.debug\(f"\{dev_mac\}: initializing device with class', sensors)
    assert re.search(r'logging\.debug\(f"\{identity\}: initializing name-identified', sensors)
    assert re.search(r'logging\.debug\(f"\{self\._ble_device\._plog\} vrm instance', role)
    assert re.search(r'logging\.info\(f"\{self\._ble_device\._plog\} registered ', role), (
        "the single INFO line lives at registration and carries the instance")
    assert "registering {self._service_name!r} dbus service on bus" not in role, (
        "the bus object repr said nothing a reader could use")


# --- EasyStart: two INFO lines per A/C cycle, not four ------------------

def test_easystart_attempt_and_offline_are_debug() -> None:
    src = _read("ble_device_easystart.py")
    assert re.search(r'logging\.debug\(f"\{self\._plog\} starting GATT session', src)
    assert re.search(r'logging\.debug\(f"\{self\._plog\} offline — publishing 0 W', src)
    # The success signal and the outcome stay at INFO.
    assert re.search(r'logging\.info\(f"\{self\._plog\} live telemetry flowing', src)
    assert re.search(r'logging\.info\(f"\{self\._plog\} link dropped mid-session', src)


# --- SmartShunt: a discovery drop is one INFO line, throttled -----------

class BleakError(Exception):
    """Stands in for bleak's base error, matched by name."""


def test_discovery_drop_is_one_info_line_then_throttled(caplog) -> None:
    smartshunt_hex._unreachable_state.clear()
    exc = BleakError("Service Discovery has not been performed yet")
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            smartshunt_hex._note_dropped_before_discovery("AA:BB", exc)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1, "one line per window, not per attempt"
    assert "dropped before service discovery" in infos[0].message
    assert not any(r.exc_info for r in caplog.records), "no traceback"


def test_run_forever_classifies_the_drop_before_the_traceback_branch() -> None:
    src = _read("smartshunt_hex.py")
    i = src.index("if ble_gatt_link.unreachable(exc):")
    j = src.index("elif ble_gatt_link.dropped_before_discovery(exc):")
    k = src.index('logger.exception("SmartShunt HEX session dropped for %s", mac)')
    assert i < j < k, (
        "the discovery-drop branch must sit between unreachable and the "
        "catch-all traceback, or the 229-traceback flood can recur")


def test_a_success_clears_the_shared_throttle_state() -> None:
    smartshunt_hex._unreachable_state.clear()
    smartshunt_hex._note_dropped_before_discovery("CC:DD", BleakError("x"))
    assert "CC:DD" in smartshunt_hex._unreachable_state
    smartshunt_hex._note_reachable("CC:DD")
    assert "CC:DD" not in smartshunt_hex._unreachable_state
