"""Scan rotation: exactly one scanning card listens at a time.

Accept-all costs the kernel a USB interrupt and a Bluetooth-worker wakeup
per advertisement the radio delivers, before any filter of ours runs, so
one card open at a time halves the box-level price.  These tests drive
DbusBleSensors._apply_rotation with the HCI layer faked and check who is
told to scan, who is told to stop, and that every path that used to
enable all cards now defers to the rotation.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

mod = pytest.importorskip("dbus_ble_sensors")


class _Svc:
    def get_active_scan(self):
        return False

    def get_continuous_scan(self):
        return False


class _Claims:
    def __init__(self):
        self.held = set()

    def hold(self, key, exclusive=False):
        self.held.add(key)

    def release(self, key):
        self.held.discard(key)

    def release_all(self):
        self.held.clear()


@pytest.fixture
def rig(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(mod.hci_scan_control, "enable_scan",
                        lambda idx, filter_policy=None, scan_type=None: calls.append(("enable", idx)) or True)
    monkeypatch.setattr(mod.hci_scan_control, "disable_passive_scan",
                        lambda idx: calls.append(("disable", idx)) or True)
    monkeypatch.setattr(mod.adapter_identity, "index_for",
                        lambda key: int(key[3:]) if key.startswith("hci") else None)
    monkeypatch.setattr(mod.adapter_identity, "label", lambda key, name: f"{name} ({key})")

    s = mod.DbusBleSensors.__new__(mod.DbusBleSensors)
    s._throttled = False
    s._adapters = {}
    s._scan_active_key = None
    s._rotation_swaps = 0
    s._scan_enabled_adapters = set()
    s._scan_claims = _Claims()
    s._scan_filter_policy = {}
    s._scan_failure_streak = {}
    s._dbus_ble_service = _Svc()
    return s, calls


def _add(s, *keys):
    for k in keys:
        s._adapters[k] = {"name": k, "mac": "00:00:00:00:00:0" + k[-1], "path": "/" + k}


def test_first_apply_puts_one_card_on_air_and_the_rest_idle(rig) -> None:
    s, calls = rig
    _add(s, "hci1", "hci0")
    s._apply_rotation("adapter added")
    assert s._scan_active_key == "hci0"                      # sorted order, first card
    assert calls == [("enable", 0), ("disable", 1)]
    assert s._scan_enabled_adapters == {"hci0"}
    assert s._scan_claims.held == {"hci0", "hci1"}, "idle cards keep their claim"


def test_advance_moves_the_role_and_wraps(rig) -> None:
    s, calls = rig
    _add(s, "hci0", "hci1")
    s._apply_rotation("start")
    calls.clear()
    assert s._rotation_tick() is True
    assert s._scan_active_key == "hci1"
    assert calls == [("disable", 0), ("enable", 1)]
    assert s._scan_enabled_adapters == {"hci1"}
    s._rotation_tick()
    assert s._scan_active_key == "hci0"


def test_apply_without_advance_keeps_the_current_card(rig) -> None:
    s, calls = rig
    _add(s, "hci0", "hci1")
    s._apply_rotation("start")
    s._rotation_tick()
    calls.clear()
    s._apply_rotation("scan lost")
    assert s._scan_active_key == "hci1"
    assert ("enable", 1) in calls and ("enable", 0) not in calls


def test_a_single_card_never_goes_idle(rig) -> None:
    s, calls = rig
    _add(s, "hci0")
    s._apply_rotation("start")
    calls.clear()
    s._rotation_tick()
    s._rotation_tick()
    assert s._scan_active_key == "hci0"
    assert all(c[0] == "enable" for c in calls)


def test_throttled_means_hands_off(rig) -> None:
    s, calls = rig
    _add(s, "hci0", "hci1")
    s._throttled = True
    s._apply_rotation("start")
    s._rotation_tick()
    assert calls == [] and s._scan_active_key is None


def test_removing_the_listening_card_hands_the_role_on(rig) -> None:
    s, calls = rig
    _add(s, "hci0", "hci1")
    s._apply_rotation("start")
    assert s._scan_active_key == "hci0"
    s._adapters.pop("hci0")
    s._scan_enabled_adapters.discard("hci0")
    s._scan_active_key = None
    calls.clear()
    s._apply_rotation("adapter removed")
    assert s._scan_active_key == "hci1" and ("enable", 1) in calls


def test_no_cards_means_no_listener(rig) -> None:
    s, calls = rig
    s._apply_rotation("start")
    assert s._scan_active_key is None and calls == []


def test_every_former_enable_all_path_defers_to_the_rotation() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    # the only direct _start_passive_scan caller is the rotation itself
    body = src[src.index("def _apply_rotation"):]
    body = body[:body.index("\n    def ")]
    assert "self._start_passive_scan(key, quiet=quiet)" in body
    outside = src.replace(body, "")
    assert re.search(r"self\._start_passive_scan\(", outside) is None, \
        "enable-all paths must go through _apply_rotation"
    # the re-enable tick only touches the listening card
    tick = src[src.index("def _scan_reenable_tick"):]
    tick = tick[:tick.index("\n    def ")]
    assert "if key != self._scan_active_key:" in tick
    # the rotation timer is registered with its own interval
    assert "GLib.timeout_add_seconds(_SCAN_ROTATION_INTERVAL_S, self._rotation_tick)" in src
    assert re.search(r"^_SCAN_ROTATION_INTERVAL_S = 60$", src, re.M)
    # throttle release and the prune tick's eager path use it
    rel = src[src.index("def _on_load_released"):]
    rel = rel[:rel.index("\n    def ")]
    assert 'self._apply_rotation("throttle released")' in rel
    assert 'self._apply_rotation("scan lost")' in src
