# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Unit tests for load_throttle.LoadThrottle.

The throttle's only inputs are ``/proc/loadavg`` and the wall clock.
We control the loadavg file via a tempfile and the callback side
effects via lambdas — no D-Bus, no real /proc, no GLib.
"""

from __future__ import annotations

import builtins
import importlib.util
import logging
import math

import pytest

import load_throttle
from load_throttle import (
    LoadThrottle,
    TRIP_5M,
    TRIP_15M,
    RELEASE_5M,
    RELEASE_15M,
    _DEFAULT_MAX_LOAD_15,
    _derive_thresholds,
    _read_watchdog_max_load_15,
)


# Prod's /etc/watchdog.conf (Venus v3.79, read 2026-11-26), verbatim.
PROD_WATCHDOG_CONF = """\
watchdog-device = /dev/watchdog

log-dir = /var/volatile/log/watchdog
min-memory = 2500
max-load-5 = 10
max-load-15 = 6
repair-binary = /usr/sbin/store_watchdog_error.sh
test-prescaler = 60
retry-timeout = 0
"""

# What the stock config derives to: 15m arm at limit-0.5 / release limit-1.0,
# 5m arm at max-load-5 - 1.0 / release max-load-5 - 2.0.
STOCK = dict(trip_15m=5.5, trip_5m=9.0, release_15m=5.0, release_5m=8.0)


def _write_loadavg(path, *, l1=1.0, l5=2.0, l15=3.0):
    """Write a ``/proc/loadavg``-shaped file at ``path``."""
    path.write_text("%.2f %.2f %.2f 1/100 12345\n" % (l1, l5, l15))


def _make(tmp_path, *, trip_calls=None, release_calls=None, **kwargs):
    """Construct a LoadThrottle pointed at a fake /proc/loadavg under
    ``tmp_path``.  Returns ``(throttle, loadavg_path)``."""
    loadavg = tmp_path / "loadavg"
    _write_loadavg(loadavg)  # initial: safe values

    if trip_calls is None:
        trip_calls = []
    if release_calls is None:
        release_calls = []

    # Pin the stock thresholds rather than the module constants, which
    # depend on whatever /etc/watchdog.conf the test host has (none, on
    # a Mac).
    for k, v in STOCK.items():
        kwargs.setdefault(k, v)
    t = LoadThrottle(
        on_trip=lambda l5, l15: trip_calls.append((l5, l15)),
        on_release=lambda l5, l15: release_calls.append((l5, l15)),
        loadavg_path=str(loadavg),
        **kwargs,
    )
    return t, loadavg


def _module_reading(tmp_path, conf_text):
    """A fresh copy of load_throttle whose import-time read of
    ``/etc/watchdog.conf`` sees ``conf_text`` (or no file, if None).

    This is the path prod actually runs: the module constants are
    derived once at import and every ``LoadThrottle()`` defaults to them.
    """
    conf = tmp_path / "watchdog.conf"
    if conf_text is not None:
        conf.write_text(conf_text)
    real_open = builtins.open

    def fake_open(file, *a, **k):
        if file == "/etc/watchdog.conf":
            file = str(conf)
        return real_open(file, *a, **k)

    spec = importlib.util.spec_from_file_location(
        "load_throttle_under_test", load_throttle.__file__)
    m = importlib.util.module_from_spec(spec)
    builtins.open = fake_open
    try:
        spec.loader.exec_module(m)
    finally:
        builtins.open = real_open
    return m


def _consts(m):
    return (m.TRIP_15M, m.TRIP_5M, m.RELEASE_15M, m.RELEASE_5M)


# ── trip thresholds ────────────────────────────────────────────────────────


class TestTrip:
    def test_below_both_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=4.0, l15=4.0)
        t.tick()
        assert not t.is_throttled
        assert trips == []

    def test_15m_at_threshold_trips(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=STOCK["trip_15m"])
        t.tick()
        assert t.is_throttled
        assert trips == [(2.0, STOCK["trip_15m"])]

    def test_15m_above_threshold_trips(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=STOCK["trip_15m"] + 0.1)
        t.tick()
        assert t.is_throttled
        assert trips and trips[0][1] == pytest.approx(STOCK["trip_15m"] + 0.1)

    def test_5m_at_threshold_trips_even_if_15m_safe(self, tmp_path):
        """The OR condition fires on the 5-min signal alone."""
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=STOCK["trip_5m"], l15=2.0)
        t.tick()
        assert t.is_throttled

    def test_15m_just_below_5_5_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=2.0, l15=5.49)
        t.tick()
        assert not t.is_throttled
        assert trips == []

    def test_5m_just_below_9_0_does_not_trip(self, tmp_path):
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=8.99, l15=2.0)
        t.tick()
        assert not t.is_throttled

    def test_trip_callback_fires_only_once(self, tmp_path):
        """Successive ticks while throttled should not re-fire on_trip."""
        trips: list = []
        t, lf = _make(tmp_path, trip_calls=trips)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        t.tick()
        t.tick()
        assert len(trips) == 1


# ── release thresholds ─────────────────────────────────────────────────────


class TestRelease:
    def test_release_requires_both_below(self, tmp_path):
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        assert t.is_throttled

        # 15-min dropped but 5-min still high → still throttled
        _write_loadavg(lf, l5=8.5, l15=4.0)
        t.tick()
        assert t.is_throttled
        assert releases == []

        # 5-min dropped but 15-min still high → still throttled
        _write_loadavg(lf, l5=2.0, l15=5.5)
        t.tick()
        assert t.is_throttled
        assert releases == []

        # Both below → release
        _write_loadavg(lf, l5=4.99, l15=4.99)
        t.tick()
        assert not t.is_throttled
        assert releases == [(4.99, 4.99)]

    def test_release_at_threshold_does_not_release(self, tmp_path):
        """Release requires strictly less-than, not equal."""
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()

        _write_loadavg(lf, l5=STOCK["release_5m"], l15=STOCK["release_15m"])
        t.tick()
        assert t.is_throttled

    def test_release_callback_fires_only_once(self, tmp_path):
        releases: list = []
        t, lf = _make(tmp_path, release_calls=releases)
        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        _write_loadavg(lf, l5=1.0, l15=1.0)
        t.tick()
        t.tick()
        t.tick()
        assert len(releases) == 1


# ── re-trip after release ──────────────────────────────────────────────────


class TestReTrip:
    def test_can_trip_again_after_release(self, tmp_path):
        trips: list = []
        releases: list = []
        t, lf = _make(tmp_path, trip_calls=trips, release_calls=releases)

        _write_loadavg(lf, l5=7.0, l15=7.0)
        t.tick()
        _write_loadavg(lf, l5=1.0, l15=1.0)
        t.tick()
        _write_loadavg(lf, l5=8.0, l15=8.0)
        t.tick()

        assert t.is_throttled
        assert len(trips) == 2
        assert len(releases) == 1


# ── /proc/loadavg parsing edge cases ───────────────────────────────────────


class TestLoadavgParsing:
    def test_returns_true_even_when_loadavg_unreadable(self, tmp_path, caplog):
        """tick() must keep returning True so the GLib timeout keeps firing."""
        t, _lf = _make(tmp_path)
        t._loadavg_path = str(tmp_path / "does-not-exist")
        with caplog.at_level(logging.WARNING, logger="load_throttle"):
            assert t.tick() is True
        assert any("failed to read" in r.message for r in caplog.records)

    def test_malformed_loadavg_is_handled(self, tmp_path):
        t, lf = _make(tmp_path)
        lf.write_text("garbage no spaces")
        # Should not raise, should not change state
        assert t.tick() is True
        assert not t.is_throttled

    def test_last_load_values_exposed(self, tmp_path):
        t, lf = _make(tmp_path)
        _write_loadavg(lf, l5=3.14, l15=2.71)
        t.tick()
        assert t.last_load_5m == pytest.approx(3.14)
        assert t.last_load_15m == pytest.approx(2.71)


# ── callback errors don't crash tick() ─────────────────────────────────────


class TestCallbackResilience:
    def test_on_trip_raising_does_not_crash_tick(self, tmp_path, caplog):
        def bad(_l5, _l15):
            raise RuntimeError("intentional")

        t = LoadThrottle(
            on_trip=bad,
            loadavg_path=str(tmp_path / "loadavg"),
            **STOCK,
        )
        _write_loadavg(tmp_path / "loadavg", l5=10.0, l15=10.0)
        with caplog.at_level(logging.ERROR, logger="load_throttle"):
            assert t.tick() is True
        # state still updates (we are throttled) even though callback raised
        assert t.is_throttled
        assert any("on_trip callback raised" in r.message for r in caplog.records)

    def test_on_release_raising_does_not_crash_tick(self, tmp_path, caplog):
        def bad(_l5, _l15):
            raise RuntimeError("intentional")

        loadavg = tmp_path / "loadavg"
        _write_loadavg(loadavg, l5=10.0, l15=10.0)
        t = LoadThrottle(
            on_release=bad,
            loadavg_path=str(loadavg),
            **STOCK,
        )
        t.tick()
        _write_loadavg(loadavg, l5=1.0, l15=1.0)
        with caplog.at_level(logging.ERROR, logger="load_throttle"):
            t.tick()
        assert not t.is_throttled


# ── /etc/watchdog.conf parsing & threshold derivation ──────────────────────


class TestWatchdogConfParsing:
    """The whole point of reading the watchdog config is so a sysadmin
    who edits ``max-load-15`` in one place gets matching behaviour out
    of our self-throttle without touching code.  These cover the
    obvious shapes the file can take in the wild."""

    def test_happy_path(self, tmp_path):
        conf = tmp_path / "watchdog.conf"
        conf.write_text("max-load-15 = 8\n")
        assert _read_watchdog_max_load_15(str(conf)) == 8.0

    def test_float_value(self, tmp_path):
        conf = tmp_path / "watchdog.conf"
        conf.write_text("max-load-15 = 7.5\n")
        assert _read_watchdog_max_load_15(str(conf)) == 7.5

    def test_no_spaces_around_equals(self, tmp_path):
        conf = tmp_path / "watchdog.conf"
        conf.write_text("max-load-15=4\n")
        assert _read_watchdog_max_load_15(str(conf)) == 4.0

    def test_comments_and_blank_lines_tolerated(self, tmp_path):
        conf = tmp_path / "watchdog.conf"
        conf.write_text(
            "# Venus OS default watchdog.conf\n"
            "\n"
            "watchdog-device = /dev/watchdog\n"
            "  # indented comment\n"
            "max-load-15 = 6\n"
            "interval = 10\n"
        )
        assert _read_watchdog_max_load_15(str(conf)) == 6.0

    def test_missing_key_falls_back(self, tmp_path):
        conf = tmp_path / "watchdog.conf"
        conf.write_text("interval = 10\nrealtime = yes\n")
        assert _read_watchdog_max_load_15(str(conf)) == _DEFAULT_MAX_LOAD_15

    def test_missing_file_falls_back(self, tmp_path):
        assert _read_watchdog_max_load_15(
            str(tmp_path / "does-not-exist")) == _DEFAULT_MAX_LOAD_15

    def test_malformed_value_falls_back(self, tmp_path):
        """A garbled value shouldn't crash us — fall through to the
        safe default and keep operating."""
        conf = tmp_path / "watchdog.conf"
        conf.write_text("max-load-15 = nope\n")
        assert _read_watchdog_max_load_15(str(conf)) == _DEFAULT_MAX_LOAD_15

    def test_no_equals_sign_skipped(self, tmp_path):
        """Lines without '=' (section markers, stray words) are simply
        ignored, not errored on."""
        conf = tmp_path / "watchdog.conf"
        conf.write_text("[section]\nmax-load-15 = 9\n")
        assert _read_watchdog_max_load_15(str(conf)) == 9.0


class TestDeriveThresholds:
    def test_stock_config(self):
        """Venus ships ``max-load-5 = 10`` and ``max-load-15 = 6``: the
        15-minute arm keeps its old (5.5, 5.0) and the 5-minute arm
        moves to its own limit's (9.0, 8.0)."""
        assert _derive_thresholds(max_load_5=10.0, max_load_15=6.0) == (5.5, 9.0, 5.0, 8.0)

    def test_each_arm_follows_its_own_limit(self):
        assert _derive_thresholds(max_load_5=12.0, max_load_15=8.0) == (7.5, 11.0, 7.0, 10.0)
        assert _derive_thresholds(max_load_5=7.5, max_load_15=6.5) == (6.0, 6.5, 5.5, 5.5)

    def test_5m_arm_is_not_borrowed_from_max_load_15(self):
        """Changing only max-load-15 must not move the 5-minute arm."""
        a = _derive_thresholds(max_load_5=10.0, max_load_15=6.0)
        b = _derive_thresholds(max_load_5=10.0, max_load_15=8.0)
        assert a[1] == b[1] == 9.0 and a[3] == b[3] == 8.0

    def test_no_limit_means_the_arm_is_off(self):
        """The watchdog daemon skips a limit of 0; so do we.  'Off' is
        infinity on both sides: ``x >= inf`` never trips and ``x < inf``
        never blocks a release."""
        t15, t5, r15, r5 = _derive_thresholds(max_load_5=0.0, max_load_15=6.0)
        assert (t15, r15) == (5.5, 5.0)
        assert t5 == math.inf and r5 == math.inf

    def test_module_level_constants_match_derivation(self):
        """The module-level ``TRIP_15M`` etc. should be whatever
        :func:`_derive_thresholds` says given the file we read."""
        m5, m15 = load_throttle._MAX_LOAD_5, load_throttle._MAX_LOAD_15
        expected = _derive_thresholds(max_load_5=m5, max_load_15=m15)
        assert (TRIP_15M, TRIP_5M, RELEASE_15M, RELEASE_5M) == expected


# ── end to end: the module as prod imports it ──────────────────────────────


class TestProdConfig:
    """``/etc/watchdog.conf`` → module constants → ``LoadThrottle()``
    with no arguments, exactly as ``dbus_ble_sensors.py`` builds it."""

    def test_constants_from_prod_conf(self, tmp_path):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        assert _consts(m) == (5.5, 9.0, 5.0, 8.0)

    @pytest.mark.parametrize("l5,l15", [
        (6.5, 5.4),     # the shape of every prod trip since 10-17
        (6.55, 5.45),   # the highest 5m and 15m prod saw at a trip
        (8.99, 5.49),
    ])
    def test_prod_trips_no_longer_fire(self, tmp_path, l5, l15):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        loadavg = tmp_path / "loadavg"
        _write_loadavg(loadavg, l5=l5, l15=l15)
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        t.tick()
        assert not t.is_throttled

    def test_5m_at_9_trips(self, tmp_path, caplog):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        loadavg = tmp_path / "loadavg"
        _write_loadavg(loadavg, l5=9.0, l15=3.0)
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        with caplog.at_level(logging.WARNING):
            t.tick()
        assert t.is_throttled
        # load-forensics tails this prefix (its TRIP_LINE)
        assert any(r.getMessage().startswith("load_throttle: tripped (15m=3.00, 5m=9.00 >= 9.00)")
                   for r in caplog.records)

    def test_15m_at_5_5_still_trips(self, tmp_path):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        loadavg = tmp_path / "loadavg"
        _write_loadavg(loadavg, l5=2.0, l15=5.5)
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        t.tick()
        assert t.is_throttled

    def test_5m_release_hysteresis(self, tmp_path):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        loadavg = tmp_path / "loadavg"
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        _write_loadavg(loadavg, l5=9.5, l15=3.0)
        t.tick()
        assert t.is_throttled
        _write_loadavg(loadavg, l5=8.5, l15=3.0)    # under the trip, above the release
        t.tick()
        assert t.is_throttled
        _write_loadavg(loadavg, l5=8.0, l15=3.0)    # release is strict
        t.tick()
        assert t.is_throttled
        _write_loadavg(loadavg, l5=7.99, l15=3.0)
        t.tick()
        assert not t.is_throttled

    def test_15m_release_hysteresis(self, tmp_path):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        loadavg = tmp_path / "loadavg"
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        _write_loadavg(loadavg, l5=3.0, l15=5.6)
        t.tick()
        assert t.is_throttled
        _write_loadavg(loadavg, l5=3.0, l15=5.0)
        t.tick()
        assert t.is_throttled
        _write_loadavg(loadavg, l5=3.0, l15=4.99)
        t.tick()
        assert not t.is_throttled

    def test_startup_line_names_both_limits(self, tmp_path, caplog):
        m = _module_reading(tmp_path, PROD_WATCHDOG_CONF)
        with caplog.at_level(logging.INFO):
            m.LoadThrottle(loadavg_path=str(tmp_path / "loadavg"))
        msgs = [r.getMessage() for r in caplog.records]
        assert ("load_throttle: thresholds derived from /etc/watchdog.conf "
                "(max-load-5=10, max-load-15=6): "
                "trip 15m>=5.5 or 5m>=9.0, release 15m<5.0 and 5m<8.0") in msgs


class TestNo5mLimit:
    """When the watchdog enforces no 5-minute limit, neither do we."""

    @pytest.mark.parametrize("line", [
        "",                      # absent
        "max-load-5 = 0\n",      # zero: the daemon skips it
        "max-load-5 = lots\n",   # garbage
    ])
    def test_5m_arm_off(self, tmp_path, line):
        m = _module_reading(tmp_path, line + "max-load-15 = 6\n")
        assert (m.TRIP_15M, m.RELEASE_15M) == (5.5, 5.0)
        assert m.TRIP_5M == math.inf and m.RELEASE_5M == math.inf

        loadavg = tmp_path / "loadavg"
        t = m.LoadThrottle(loadavg_path=str(loadavg))
        _write_loadavg(loadavg, l5=50.0, l15=3.0)
        t.tick()
        assert not t.is_throttled, "no 5-minute limit: a 5m spike alone never trips"

        _write_loadavg(loadavg, l5=50.0, l15=5.6)
        t.tick()
        assert t.is_throttled, "the 15-minute arm still works"
        _write_loadavg(loadavg, l5=50.0, l15=4.9)
        t.tick()
        assert not t.is_throttled, "an off 5m arm never blocks a release"

    def test_startup_line_says_the_arm_is_off(self, tmp_path, caplog):
        m = _module_reading(tmp_path, "max-load-15 = 6\n")
        with caplog.at_level(logging.INFO):
            m.LoadThrottle(loadavg_path=str(tmp_path / "loadavg"))
        msgs = [r.getMessage() for r in caplog.records]
        assert ("load_throttle: thresholds derived from /etc/watchdog.conf "
                "(max-load-5 unset, max-load-15=6): "
                "trip 15m>=5.5 (5m arm off), release 15m<5.0") in msgs

    def test_max_load_1_supplies_max_load_5_as_the_daemon_does(self, tmp_path):
        """watchdog's read_config: ``if (maxload1 && !maxload5)
        maxload5 = maxload1 * 3 / 4`` (integer arithmetic)."""
        m = _module_reading(tmp_path, "max-load-1 = 10\nmax-load-15 = 6\n")
        assert m._MAX_LOAD_5 == 7.0
        assert _consts(m) == (5.5, 6.0, 5.0, 5.0)

    def test_missing_file_assumes_the_stock_image(self, tmp_path):
        """No readable config at all: assume what the stock Venus image
        ships, as the 15-minute arm always has."""
        m = _module_reading(tmp_path, None)
        assert _consts(m) == (5.5, 9.0, 5.0, 8.0)


def test_max_load_15_zero_turns_the_15m_arm_off(tmp_path):
    """A 0 limit is "no check" to the daemon.  Taken literally it would
    put our trip at -0.5 and leave the service throttled for good."""
    m = _module_reading(tmp_path, "max-load-5 = 10\nmax-load-15 = 0\n")
    assert m.TRIP_15M == math.inf and m.RELEASE_15M == math.inf
    assert (m.TRIP_5M, m.RELEASE_5M) == (9.0, 8.0)
    loadavg = tmp_path / "loadavg"
    _write_loadavg(loadavg, l5=1.0, l15=1.0)
    t = m.LoadThrottle(loadavg_path=str(loadavg))
    t.tick()
    assert not t.is_throttled
