"""Phase 2 of the passive-scan plan: the hardware accept list is retired.

The controller's accept list was 25 entries on one of prod's cards, was
full, silently dropped the devices past its end, needed per-card slicing
and name-device address injection, and coupled ``ContinuousScan`` to the
radio.  The radio is now always accept-all; filtering lives where it has
no capacity limit -- the kernel BPF adapter filter and the tap's pre-walk
MAC gate -- and ``ContinuousScan`` means only "adopt something new".
"""
from __future__ import annotations

import os
import re

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


def _src(name: str) -> str:
    return open(os.path.join(SRC, name)).read()


def test_the_radio_policy_is_always_accept_all() -> None:
    s = _src("dbus_ble_sensors.py")
    body = s[s.index("def _desired_filter_policy"):]
    body = body[:body.index("\n    def ")]
    assert "return hci_scan_control.FILTER_POLICY_ACCEPT_ALL" in body
    assert "FILTER_POLICY_ACCEPT_LIST_ONLY" not in body
    assert "get_continuous_scan" not in body, "ContinuousScan no longer steers the radio"


def test_apply_scan_policy_has_no_accept_list_branch() -> None:
    s = _src("dbus_ble_sensors.py")
    body = s[s.index("def _apply_scan_policy"):]
    body = body[:body.index("\n    def ")]
    assert "apply_accept_list" not in body
    assert "ACCEPT_LIST_ONLY" not in body
    assert "enable_scan(" in body


def test_the_accept_list_machinery_is_gone_from_both_modules() -> None:
    svc, ctl = _src("dbus_ble_sensors.py"), _src("hci_scan_control.py")
    for name in ("_accept_list_for", "_accept_list_capacity", "_name_grace_active",
                 "_has_configured_name_devices", "NAME_STARTUP_GRACE_S",
                 "_name_accept_all_logged"):
        assert name not in svc, name
    for name in ("def apply_accept_list", "def accept_list_slices",
                 "def read_accept_list_size", "_OCF_LE_READ_ACCEPT_LIST_SIZE"):
        assert name not in ctl, name


def test_continuous_scan_still_gates_adoption() -> None:
    """The setting kept exactly one meaning: whether to ADOPT something new."""
    s = _src("dbus_ble_sensors.py")
    assert s.count("get_continuous_scan()") >= 2   # mfg path + name path adoption gates
    assert "not adopting" in s


def test_the_mac_gate_is_plumbed_and_refreshed_at_every_site() -> None:
    s = _src("dbus_ble_sensors.py")
    assert "self._tap_known_macs: set[str] = set()" in s
    assert "known_macs=self._tap_known_macs" in s
    # refreshed on: ContinuousScan flip, adoption, name-address learned,
    # router registration change, and seeded at tap start
    assert s.count("self._refresh_tap_known_macs()") == 5
    # in place, never reassigned (the tap thread holds the object)
    body = s[s.index("def _refresh_tap_known_macs"):]
    body = body[:body.index("\n    def ")]
    assert "self._tap_known_macs.clear()" in body and "self._tap_known_macs.update(desired)" in body
    assert re.search(r"self\._tap_known_macs\s*=\s*", body) is None


def test_the_gate_fails_open_and_opens_for_mfg_id_consumers() -> None:
    s = _src("dbus_ble_sensors.py")
    body = s[s.index("def _refresh_tap_known_macs"):]
    body = body[:body.index("\n    def ")]
    # cannot read the setting -> open (never gate blind)
    assert "except Exception:\n            open_ = True" in body
    # an external mfg-id registration wants strangers -> open
    assert "get_registered_mfg_ids()" in body
    # the known set is the union we act on
    for src in ("self._configured_macs", "self._name_device_macs.values()", "get_registered_macs()"):
        assert src in body, src
