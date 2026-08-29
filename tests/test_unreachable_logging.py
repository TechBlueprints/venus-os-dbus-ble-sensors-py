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


# --- dropped-before-discovery vs a real missing characteristic --------
#
# An EasyStart soft starter whose A/C shut off DURING connect produced
# BleakCharacteristicNotFoundError on prod (2026-08-29 02:18:47), with
# bcmv2 recording "disconnect event ... last link traffic: never".  The
# driver logged a WARNING and took an exponential backoff, delaying the
# reconnect for a compressor that had merely stopped.
#
# The same exception type is also how a genuine defect arrives -- a wrong
# UUID, or firmware that dropped a characteristic -- and that must stay
# loud.  Frequency cannot separate them either: this was 1 failure in 41
# sessions, and a wrong UUID would fail all 41.  So the discriminator has
# to be the GATT database, not the exception and not the rate.


class BleakCharacteristicNotFoundError(Exception):
    """Stands in for bleak's class, matched by name."""


class BleakError(Exception):
    """Stands in for bleak's base error, matched by name."""


class _Services:
    def __init__(self, chars):
        self.characteristics = chars


class _Client:
    def __init__(self, chars):
        self.services = _Services(chars)


class _ClientRefusingServices:
    """Some bleak versions raise on .services before discovery."""

    @property
    def services(self):
        raise BleakError("Service Discovery has not been performed yet")


def test_empty_gatt_database_means_the_link_died() -> None:
    exc = BleakCharacteristicNotFoundError("d973f2e1-... was not found!")
    assert ble_gatt_link.dropped_before_discovery(exc, _Client({})) is True


def test_a_resolved_database_missing_the_char_stays_loud() -> None:
    """The real-defect case: discovery worked, the characteristic is gone."""
    exc = BleakCharacteristicNotFoundError("d973f2e1-... was not found!")
    client = _Client({"0000180a-0000-1000-8000-00805f9b34fb": object()})
    assert ble_gatt_link.dropped_before_discovery(exc, client) is False


def test_services_attribute_that_refuses_means_no_database() -> None:
    exc = BleakCharacteristicNotFoundError("nope")
    assert ble_gatt_link.dropped_before_discovery(
        exc, _ClientRefusingServices()) is True


def test_the_bare_discovery_error_needs_no_client() -> None:
    # smartshunt_hex hit this form: bleak refuses the lookup outright.
    exc = BleakError("Service Discovery has not been performed yet")
    assert ble_gatt_link.dropped_before_discovery(exc) is True


def test_without_a_client_we_stay_loud() -> None:
    """Nothing to inspect means no licence to silence a possible defect."""
    exc = BleakCharacteristicNotFoundError("nope")
    assert ble_gatt_link.dropped_before_discovery(exc, None) is False


def test_unrelated_errors_are_not_swallowed() -> None:
    assert ble_gatt_link.dropped_before_discovery(
        TypeError("bad argument"), _Client({})) is False
    assert ble_gatt_link.dropped_before_discovery(
        ValueError("nonsense"), _Client({})) is False


def test_a_wrapped_characteristic_error_is_still_seen() -> None:
    inner = BleakCharacteristicNotFoundError("d973f2e1-... was not found!")
    outer = RuntimeError("session failed")
    outer.__cause__ = inner
    assert ble_gatt_link.dropped_before_discovery(outer, _Client({})) is True


def test_it_does_not_overlap_with_unreachable() -> None:
    """The two classifiers answer different questions and must not blur."""
    exc = BleakCharacteristicNotFoundError("d973f2e1-... was not found!")
    assert ble_gatt_link.unreachable(exc) is False, (
        "a device that answered is not 'unreachable' — keeping these "
        "separate is what stops a real missing-characteristic defect "
        "from being filed as 'switched off'")
