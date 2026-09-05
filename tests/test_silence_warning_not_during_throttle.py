"""The silence watchdog must not report our own throttle as a fault.

When the load throttle trips it tears the tap down and disables scanning
deliberately.  The resulting quiet is expected, not a failure, and every
re-enable path is itself gated on ``_throttled`` — so a warning saying
"re-enabling passive scan" during a throttle both misreports a
deliberate action and promises something it cannot do.

Prod 2026-09-05: throttle tripped 15:22:50Z, this warned at 15:28:20Z
about 329 s of silence it had caused, and a reader outside the service
took those lines for process restarts.
"""
from __future__ import annotations

import os
import re

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))
SRC_FILE = os.path.join(SRC, "dbus_ble_sensors.py")


def _prune_tick_body() -> str:
    src = open(SRC_FILE).read()
    i = src.index("def _prune_tick")
    return src[i:src.index("\n    def ", i + 10)]


def test_silence_check_is_inside_the_throttle_guard() -> None:
    body = _prune_tick_body()
    guard = body.index("if not self._throttled:")
    silence = body.index("No matching advertisements received")
    assert silence > guard, "silence check must come after the guard opens"
    # Indentation is the real proof of scope: the check must be nested
    # deeper than the guard itself.
    guard_indent = len(body[:guard].rsplit("\n", 1)[-1])
    line_start = body.rfind("if (self._last_tap_rx", 0, silence)
    silence_indent = len(body[:line_start].rsplit("\n", 1)[-1])
    assert silence_indent > guard_indent, (
        f"silence check indent {silence_indent} must be inside the "
        f"throttle guard at indent {guard_indent}")


def test_release_restarts_the_silence_clock() -> None:
    """Otherwise the watchdog fires the instant the throttle lifts."""
    src = open(SRC_FILE).read()
    i = src.index("self._throttled = False")
    after = src[i:i + 1400]
    assert "self._last_tap_rx = time.monotonic()" in after, (
        "throttled time must not be counted as silence on release")
    assert "self._silence_warned = False" in after
