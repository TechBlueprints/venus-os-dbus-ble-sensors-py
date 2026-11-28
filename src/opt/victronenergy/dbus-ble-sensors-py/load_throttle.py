# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""
Load-driven self-throttle for the BLE-sensors service.

The Cerbo runs ``/usr/sbin/watchdog -c /etc/watchdog.conf``, which
forces a hardware reset when a load average exceeds its configured
limit.  Venus ships ``max-load-5 = 10`` and ``max-load-15 = 6`` (and no
``max-load-1``).  On a busy gateway with many BLE devices in range plus
user-installed Python services, the 15-minute limit can come close.

This module monitors ``/proc/loadavg`` and asks the owning service to
**voluntarily suspend** its busiest BLE work (the HCI advertisement
tap + BlueZ passive scan registration) when the load gets dangerous,
then resume when it recovers.

State machine, each arm derived from the watchdog's own limit for
that window (see :func:`_derive_thresholds`).  With the stock config:

    trip:    15-min >= 5.5  OR  5-min >= 9.0
    release: 15-min <  5.0  AND  5-min <  8.0

An arm whose limit the watchdog does not enforce (unset or 0) is off:
it never trips and never holds a release.

A single log line is emitted on trip (none on release; the GUI
notification gives the visible signal both ways).

Used by ``dbus_ble_sensors.py``, and its four threshold constants by
``load-forensics``.  Pure stdlib — no D-Bus, no GLib, no I/O outside
``open('/proc/loadavg')`` and one read of ``/etc/watchdog.conf`` at
import.  The owning service is expected to call
:meth:`LoadThrottle.tick` periodically (e.g. from
``GLib.timeout_add_seconds(30, ...)``).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Optional


_WATCHDOG_CONF = "/etc/watchdog.conf"
_LOADAVG_PATH = "/proc/loadavg"

# Fallbacks when /etc/watchdog.conf cannot be read at all: the values
# Victron ships in the stock Venus OS image (v3.79 at the time of
# writing).  A file that IS readable but leaves a key out is taken at
# its word instead — see _watchdog_limits().
_DEFAULT_MAX_LOAD_5 = 10.0
_DEFAULT_MAX_LOAD_15 = 6.0

# An arm that is off.  ``x >= OFF`` is never true (never trips) and
# ``x < OFF`` always is (never blocks a release), so the state machine
# needs no special case.
OFF = math.inf

_logger = logging.getLogger(__name__)


def _read_watchdog_conf(path: str) -> Optional[dict[str, str]]:
    """Raw ``key -> value`` strings from a watchdog config file, or
    ``None`` if it cannot be read.  The daemon's own one-option-per-line
    ``key = value`` syntax; blank lines, ``#`` comments and lines with
    no ``=`` are skipped.  A repeated key keeps its first value."""
    conf: dict[str, str] = {}
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                conf.setdefault(key.strip(), val.strip())
    except OSError:
        return None
    return conf


def _as_limit(raw: Optional[str]) -> Optional[float]:
    """A limit as a float, or ``None`` if absent or unparseable."""
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _watchdog_limits(path: str = _WATCHDOG_CONF) -> tuple[float, float, str]:
    """Return ``(max_load_5, max_load_15, provenance)`` as the watchdog
    daemon will enforce them.  A limit ``<= 0`` means no limit.

    What the daemon does (watchdog upstream source, read 2026-11-26 from
    the SourceForge ``master`` tree; prod runs 5.16, whose tag was not
    checked separately, and the man page says the same):

      * ``maxload1/5/15`` default to 0, and ``check_load()`` tests each
        one as ``maxloadN > 0 && avgN > maxloadN`` — so an unset or 0
        limit is simply not checked.  This is why a missing
        ``max-load-5`` turns our 5-minute arm OFF rather than borrowing
        ``max-load-15``: the box will not reboot on the 5-minute figure.
      * ``read_config()`` fills a missing (or 0) ``max-load-5`` with
        ``max-load-1 * 3 / 4`` and a missing ``max-load-15`` with
        ``max-load-1 / 2``, in integer arithmetic, when ``max-load-1``
        is set.  Mirrored here; prod sets no ``max-load-1``.
      * The comparison is on ``atoi()`` of ``/proc/loadavg``, i.e. the
        truncated integer, and strict: ``max-load-5 = 10`` reboots at a
        5-minute average of 11.00, not 10.00.  Our margins below are
        taken from the configured number, so they are at least as wide
        as they read.

    Our own departures, on the safe side: an unparseable value is
    treated like an absent one; an unreadable file assumes the stock
    image (:data:`_DEFAULT_MAX_LOAD_5`, :data:`_DEFAULT_MAX_LOAD_15`);
    and a readable file with no ``max-load-15`` (and no ``max-load-1``)
    still gets the stock 15-minute limit, as this module always did —
    the 15-minute arm is the reboot guard that has mattered.
    """
    conf = _read_watchdog_conf(path)
    if conf is None:
        return (_DEFAULT_MAX_LOAD_5, _DEFAULT_MAX_LOAD_15,
                "%s unreadable; stock max-load-5=%g, max-load-15=%g"
                % (path, _DEFAULT_MAX_LOAD_5, _DEFAULT_MAX_LOAD_15))

    m1 = _as_limit(conf.get('max-load-1'))
    m1_int = int(m1) if m1 is not None and m1 > 0 else 0

    m5 = _as_limit(conf.get('max-load-5'))
    if not m5 and m1_int:
        m5 = float(m1_int * 3 // 4)
        note5 = "max-load-5=%g (3/4 of max-load-1)" % m5
    elif m5 is None:
        m5 = 0.0
        note5 = ("max-load-5 unparseable" if 'max-load-5' in conf
                 else "max-load-5 unset")
    else:
        note5 = "max-load-5=%g" % m5

    m15 = _as_limit(conf.get('max-load-15'))
    if not m15 and m1_int:
        m15 = float(m1_int // 2)
        note15 = "max-load-15=%g (1/2 of max-load-1)" % m15
    elif m15 is None:
        m15 = _DEFAULT_MAX_LOAD_15
        note15 = "max-load-15 %s, stock %g assumed" % (
            "unparseable" if 'max-load-15' in conf else "unset", m15)
    else:
        note15 = "max-load-15=%g" % m15

    return m5, m15, "%s, %s" % (note5, note15)


def _read_watchdog_max_load_15(path: str = _WATCHDOG_CONF) -> float:
    """The 15-minute limit :func:`_watchdog_limits` settles on."""
    return _watchdog_limits(path)[1]


def _read_watchdog_max_load_5(path: str = _WATCHDOG_CONF) -> float:
    """The 5-minute limit :func:`_watchdog_limits` settles on (0 = none)."""
    return _watchdog_limits(path)[0]


def _derive_thresholds(*, max_load_5: float,
                       max_load_15: float) -> tuple[float, float, float, float]:
    """Return ``(trip_15m, trip_5m, release_15m, release_5m)``.

    Each arm follows the watchdog's own limit for its window:

      * trip_15m    = max_load_15 - 0.5  — get out of the way 0.5 before
                                            the watchdog itself fires.
      * release_15m = max_load_15 - 1.0  — hysteresis.
      * trip_5m     = max_load_5 - 1.0   — the 5-minute average swings
                                            harder, so a wider margin.
      * release_5m  = max_load_5 - 2.0   — hysteresis.

    A limit ``<= 0`` (the watchdog checks nothing) makes that arm
    :data:`OFF` on both sides.

    With the stock ``max-load-5 = 10``, ``max-load-15 = 6`` this returns
    ``(5.5, 9.0, 5.0, 8.0)``.  Until 2026-11 the 5-minute arm was taken
    from ``max-load-15`` instead (trip 6.0, release 5.0), four points
    under the limit the watchdog actually enforces: on prod it fired
    129 times from 10-17 to 11-26, every one on the 5-minute reading
    alone with the 15-minute never above 5.45.
    """
    if max_load_15 > 0:
        trip_15m, release_15m = max_load_15 - 0.5, max_load_15 - 1.0
    else:
        trip_15m = release_15m = OFF
    if max_load_5 > 0:
        trip_5m, release_5m = max_load_5 - 1.0, max_load_5 - 2.0
    else:
        trip_5m = release_5m = OFF
    return trip_15m, trip_5m, release_15m, release_5m


# Read once at import.  /etc/watchdog.conf changes require restarting
# the watchdog daemon to take effect anyway, so a service restart on
# our side is the natural moment to re-read.  load-forensics imports
# the four constants, so it must restart too to pick up a change.
#
# We intentionally do NOT log here — load_throttle is imported before
# the service has called setup_logging(), so an import-time log line
# would be silently dropped.  The first :class:`LoadThrottle` instance
# emits the same diagnostic from its constructor instead, by which
# point logging has been configured.
_MAX_LOAD_5, _MAX_LOAD_15, _LIMITS_SOURCE = _watchdog_limits()
TRIP_15M, TRIP_5M, RELEASE_15M, RELEASE_5M = _derive_thresholds(
    max_load_5=_MAX_LOAD_5, max_load_15=_MAX_LOAD_15)


def _describe(trip_15m: float, trip_5m: float,
              release_15m: float, release_5m: float) -> str:
    """``trip ... , release ...`` for a log line; an off arm says so."""
    trips, releases = [], []
    for name, trip, rel in (("15m", trip_15m, release_15m),
                            ("5m", trip_5m, release_5m)):
        if trip == OFF:
            continue
        trips.append("%s>=%.1f" % (name, trip))
        releases.append("%s<%.1f" % (name, rel))
    off = [n for n, t in (("15m", trip_15m), ("5m", trip_5m)) if t == OFF]
    text = "trip " + (" or ".join(trips) if trips else "never")
    if off:
        text += " (%s arm off)" % " and ".join(off)
    if releases:
        text += ", release " + " and ".join(releases)
    return text


class LoadThrottle:
    """Tracks ``/proc/loadavg`` and signals when sustained high load
    crosses a trip threshold.

    The owning service registers two callbacks via the constructor —
    ``on_trip`` is invoked once when the throttle becomes active,
    ``on_release`` once when it becomes inactive.  Both are called
    synchronously from :meth:`tick` on the same thread (the GLib
    mainloop in practice), so they don't need to be thread-safe.

    The ``loadavg_path`` parameter exists for tests; production code
    should leave it at the default.
    """

    def __init__(
        self,
        *,
        on_trip: Callable[[float, float], None] | None = None,
        on_release: Callable[[float, float], None] | None = None,
        trip_15m: float = TRIP_15M,
        trip_5m: float = TRIP_5M,
        release_15m: float = RELEASE_15M,
        release_5m: float = RELEASE_5M,
        loadavg_path: str = _LOADAVG_PATH,
    ) -> None:
        self._on_trip = on_trip
        self._on_release = on_release
        self._trip_15m = trip_15m
        self._trip_5m = trip_5m
        self._release_15m = release_15m
        self._release_5m = release_5m
        self._loadavg_path = loadavg_path
        self._throttled: bool = False
        self._last_load_5m: Optional[float] = None
        self._last_load_15m: Optional[float] = None
        # Logging is configured by the time the service actually
        # constructs us (unlike at module-import time, where the
        # derivation happens silently — see _MAX_LOAD_15 above).
        mine = (trip_15m, trip_5m, release_15m, release_5m)
        if mine == (TRIP_15M, TRIP_5M, RELEASE_15M, RELEASE_5M):
            source = "derived from %s (%s)" % (_WATCHDOG_CONF, _LIMITS_SOURCE)
        else:
            source = "set by caller"
        _logger.info("load_throttle: thresholds %s: %s",
                     source, _describe(*mine))

    @property
    def is_throttled(self) -> bool:
        return self._throttled

    @property
    def last_load_5m(self) -> Optional[float]:
        """Most recent 5-min load avg read by :meth:`tick`."""
        return self._last_load_5m

    @property
    def last_load_15m(self) -> Optional[float]:
        """Most recent 15-min load avg read by :meth:`tick`."""
        return self._last_load_15m

    def _read_loadavg(self) -> tuple[float, float] | None:
        """Return ``(load_5m, load_15m)``.  ``None`` on read failure."""
        try:
            with open(self._loadavg_path, "r") as f:
                parts = f.read().split()
            return float(parts[1]), float(parts[2])
        except (OSError, ValueError, IndexError) as exc:
            _logger.warning("load_throttle: failed to read %s: %s",
                            self._loadavg_path, exc)
            return None

    def tick(self) -> bool:
        """Check the current load and fire callbacks on state change.

        Always returns ``True`` so it can be used directly as a
        ``GLib.timeout_add_seconds`` callback.
        """
        reading = self._read_loadavg()
        if reading is None:
            return True
        load_5m, load_15m = reading
        self._last_load_5m = load_5m
        self._last_load_15m = load_15m

        if not self._throttled:
            if load_15m >= self._trip_15m or load_5m >= self._trip_5m:
                self._throttled = True
                _logger.warning(
                    "load_throttle: tripped (15m=%.2f%s, 5m=%.2f%s) "
                    "— suspending BLE advertisement intake",
                    load_15m, " >= %.2f" % self._trip_15m if load_15m >= self._trip_15m else "",
                    load_5m, " >= %.2f" % self._trip_5m if load_5m >= self._trip_5m else "",
                )
                if self._on_trip is not None:
                    try:
                        self._on_trip(load_5m, load_15m)
                    except Exception:
                        _logger.exception(
                            "load_throttle: on_trip callback raised")
        else:
            if load_15m < self._release_15m and load_5m < self._release_5m:
                self._throttled = False
                # No log on release (per spec — the GUI notification
                # going away is the visible signal).
                if self._on_release is not None:
                    try:
                        self._on_release(load_5m, load_15m)
                    except Exception:
                        _logger.exception(
                            "load_throttle: on_release callback raised")

        return True
