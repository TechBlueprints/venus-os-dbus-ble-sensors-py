"""Expected "device not answering" must not cost a stack trace per attempt.

A sensor that is switched off, out of range, or simply not near an
adapter we are allowed to scan on fails identically on every reconnect.
smartshunt_hex retries every 5s, so logging a full traceback each time
wrote ~700 B/s onto the eMMC on prod — multilog rotating every ~20s,
roughly 57 MiB/day — for a condition whose stack never varies.
"""
from __future__ import annotations

import logging

import ble_gatt_link
import smartshunt_hex


class BleakNotFoundError(Exception):
    """Stands in for bleak-retry-connector's class, matched by name."""


class BleakDeviceNotFoundError(Exception):
    """Stands in for bleak's own class, matched by name."""


def test_our_own_resolution_failure_is_expected() -> None:
    assert ble_gatt_link.unreachable(
        ble_gatt_link.DeviceNotFound("AA: not known to BlueZ")) is True


def test_bleak_not_found_is_expected_by_name() -> None:
    # Matched by type name so we need not import from either library.
    assert ble_gatt_link.unreachable(BleakNotFoundError("nope")) is True
    assert ble_gatt_link.unreachable(BleakDeviceNotFoundError("nope")) is True


def test_wrapped_cause_is_still_recognised() -> None:
    # establish_connection raises BleakNotFoundError *from* an inner error,
    # and callers may wrap it again; look through __cause__/__context__.
    inner = BleakNotFoundError("device not found")
    outer = RuntimeError("session failed")
    outer.__cause__ = inner
    assert ble_gatt_link.unreachable(outer) is True


def test_a_real_bug_keeps_its_traceback() -> None:
    # Anything not in the expected set must stay loud.
    assert ble_gatt_link.unreachable(TypeError("bad argument")) is False
    assert ble_gatt_link.unreachable(ValueError("nonsense")) is False


def _reset() -> None:
    smartshunt_hex._unreachable_state.clear()


def test_first_failure_is_reported_once(caplog) -> None:
    _reset()
    with caplog.at_level(logging.WARNING):
        smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
    assert len(caplog.records) == 1
    assert "unreachable" in caplog.records[0].message


def test_repeats_within_the_window_are_suppressed(caplog) -> None:
    _reset()
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            smartshunt_hex._note_unreachable("AA:BB",
                                             BleakNotFoundError("gone"))
    # 50 attempts, one line — the whole point.
    assert len(caplog.records) == 1


def test_a_changed_message_is_new_news(caplog) -> None:
    _reset()
    with caplog.at_level(logging.WARNING):
        smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
        smartshunt_hex._note_unreachable("AA:BB",
                                         BleakNotFoundError("different"))
    assert len(caplog.records) == 2


def test_each_mac_is_tracked_separately(caplog) -> None:
    _reset()
    with caplog.at_level(logging.WARNING):
        smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
        smartshunt_hex._note_unreachable("CC:DD", BleakNotFoundError("gone"))
    assert len(caplog.records) == 2


def test_suppressed_count_is_reported_when_the_window_reopens(caplog) -> None:
    _reset()
    smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
    for _ in range(9):
        smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
    # Pretend the window elapsed rather than sleeping through it.
    last, suppressed, message = smartshunt_hex._unreachable_state["AA:BB"]
    smartshunt_hex._unreachable_state["AA:BB"] = (
        last - smartshunt_hex._UNREACHABLE_LOG_INTERVAL_S - 1,
        suppressed, message)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
    assert len(caplog.records) == 1
    assert "9 further attempt" in caplog.records[0].message


def test_recovery_clears_the_state() -> None:
    _reset()
    smartshunt_hex._note_unreachable("AA:BB", BleakNotFoundError("gone"))
    assert "AA:BB" in smartshunt_hex._unreachable_state
    smartshunt_hex._note_reachable("AA:BB")
    assert "AA:BB" not in smartshunt_hex._unreachable_state
