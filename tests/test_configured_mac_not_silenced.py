"""A device we hold settings for must survive a frame nothing can parse.

Silencing a MAC adds it to ``_tap_ignored_macs``, which has no TTL: the
tap stops delivering that address for the life of the process.  Correct
for a stranger, destructive for our own gear.

Victron chargers interleave two manufacturer records.  Only one is the
Instant Readout frame whose bytes 2-3 are a product id; the other is
claimed by no detector, falls through to the 0x02E1 fallback class, and
fails its check.  Prod 2026-09-03: the SmartSolar was heard at 20:03:01,
silenced on such a frame, and stayed dark across two restarts.
"""
from __future__ import annotations

import dbus_ble_sensors as mod


def _sensors(configured):
    obj = mod.DbusBleSensors.__new__(mod.DbusBleSensors)
    obj._configured_macs = set(configured)
    return obj


def test_a_configured_device_is_never_silenced() -> None:
    s = _sensors({"c120d54f7125"})
    assert s._should_silence_mac("c120d54f7125", routed=False) is False, (
        "our own charger must get the next frame, not be dropped for the "
        "life of the process")


def test_a_stranger_is_still_silenced() -> None:
    s = _sensors({"c120d54f7125"})
    assert s._should_silence_mac("aabbccddeeff", routed=False) is True, (
        "silencing unparseable strangers is the whole point of the set")


def test_anything_the_router_wanted_is_never_silenced() -> None:
    s = _sensors(set())
    assert s._should_silence_mac("aabbccddeeff", routed=True) is False
