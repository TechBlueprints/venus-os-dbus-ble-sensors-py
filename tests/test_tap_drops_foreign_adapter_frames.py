"""The tap drops frames from adapters we do not scan, before parsing them.

The HCI monitor channel (HCI_DEV_NONE) delivers every card's traffic.  When
another service runs an active discovery scan on a card we do not own, its
flood of stranger advertisements reaches our tap and, without this, is fully
parsed (~67 us/report on the Cerbo) only to be dropped at the manufacturer
filter — the shape of the 2026-09-11 04:25Z single-actor CPU burst
(sensors-py 26.6 % of a core for <=60 s while easytouch was scanning).
The adapter-index early-drop discards such a frame in ~1 us.
"""
from __future__ import annotations

import os
import re
import struct
import sys

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import hci_advertisement_tap as tap  # noqa: E402


def _legacy_frame(adapter_idx: int, mac_le: bytes = bytes.fromhex("25714fd520c1")):
    """One LE legacy advertising report (Victron mfg 0x02E1) on *adapter_idx*."""
    ad = bytes([0x02, 0x01, 0x06, 0x17, 0xFF, 0xE1, 0x02]) + bytes(20)
    rep = bytes([0x00, 0x01]) + mac_le + bytes([len(ad)]) + ad + bytes([0xC8])
    hci = bytes([0x3E, 1 + 1 + len(rep), 0x02, 0x01]) + rep
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(hci)) + hci


def test_no_restriction_parses_every_adapter() -> None:
    for idx in (0, 1, 5, 9):
        assert tap.parse_monitor_frame(_legacy_frame(idx)) != []
        assert tap.parse_monitor_frame(_legacy_frame(idx), allowed_adapters=None) != []
        assert tap.parse_monitor_frame(_legacy_frame(idx), allowed_adapters=set()) != []


def test_allowed_adapter_is_parsed_foreign_is_dropped() -> None:
    allowed = {0, 1}
    assert tap.parse_monitor_frame(_legacy_frame(1), allowed_adapters=allowed) != []
    assert tap.parse_monitor_frame(_legacy_frame(0), allowed_adapters=allowed) != []
    # a card we do not scan (easytouch's discovery card) is dropped
    assert tap.parse_monitor_frame(_legacy_frame(5), allowed_adapters=allowed) == []
    assert tap.parse_monitor_frame(_legacy_frame(9), allowed_adapters=allowed) == []


def test_drop_happens_before_body_parsing() -> None:
    """A truncated/garbage body on a foreign adapter must still just drop,
    never raise — proof the adapter check precedes report parsing."""
    frame = _legacy_frame(5)[:8]  # header only, no report body
    # header says adapter 5; with 5 not allowed it returns [] on the index,
    # not on a body-length error.
    assert tap.parse_monitor_frame(frame, allowed_adapters={1}) == []


def test_run_tap_loop_forwards_allowed_adapters() -> None:
    src = open(os.path.join(SRC, "hci_advertisement_tap.py")).read()
    assert "allowed_adapters: 'set[int] | None' = None" in src
    # whitespace-insensitive: the call wraps across lines
    assert re.search(
        r"parse_monitor_frame\(raw,\s*mfg_filter,\s*ignored_macs,\s*"
        r"name_prefixes,\s*allowed_adapters,\s*known_macs\)", src)


def test_sensors_py_maintains_and_passes_the_index_set() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    assert "self._scan_adapter_indices: set[int] = set()" in src
    assert "def _refresh_scan_adapter_indices(self)" in src
    assert "allowed_adapters=self._scan_adapter_indices" in src
    # rebuilt in place (never reassigned) so the running tap sees it
    assert "self._scan_adapter_indices.clear()" in src
    assert "self._scan_adapter_indices.update(indices)" in src
