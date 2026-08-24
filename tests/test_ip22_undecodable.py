"""An advertisement we cannot fully parse is dropped, and said once.

Instant Readout has no authentication tag, and the library's only
integrity check is a single byte, so a frame can decrypt "successfully"
and still contain a field outside its enum.  On dev-cerbo one IP22
produced "108 is not a valid OperationMode" nineteen times, each with a
full traceback.

Two properties matter and are easy to get wrong in opposite directions:
the frame must be dropped ENTIRELY — publishing the fields that did parse
would put unvalidated values on D-Bus where nothing downstream could tell
they were suspect — and the condition must be reported once per window,
not once per advertisement.
"""
from __future__ import annotations

import logging
import os


def _driver_source() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(
        here, "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py",
        "ble_device_ip22_charger.py"))
    with open(path) as fh:
        return fh.read()


def test_value_error_is_caught_before_the_generic_handler() -> None:
    src = _driver_source()
    assert "except ValueError as exc:" in src
    # Ordering matters: a bare `except Exception` first would swallow it
    # and keep logging tracebacks.
    assert src.index("except ValueError as exc:") < src.index(
        'logger.exception("%s: IP22 advertisement decode error"')


def test_the_frame_is_dropped_not_partially_published() -> None:
    src = _driver_source()
    handler = src[src.index("except ValueError as exc:"):]
    handler = handler[:handler.index("except Exception:")]
    assert "return" in handler
    assert "_publish" not in handler


def test_it_reports_via_the_throttle_not_logger_exception() -> None:
    src = _driver_source()
    handler = src[src.index("except ValueError as exc:"):]
    handler = handler[:handler.index("except Exception:")]
    assert "_note_undecodable" in handler
    assert "logger.exception" not in handler


class _Subject:
    """Minimal stand-in carrying just what the throttle touches."""

    _UNDECODABLE_LOG_INTERVAL_S = 1800.0
    _plog = "ip22-test"

    def __init__(self) -> None:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.normpath(os.path.join(
            here, "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py",
            "ble_device_ip22_charger.py"))
        spec = importlib.util.spec_from_file_location("_ip22_probe", path)
        # Bind the unbound function without importing the whole driver.
        src = open(path).read()
        start = src.index("    def _note_undecodable")
        end = src.index("    def _maybe_hex_telemetry")
        body = "import time, logging\nlogger = logging.getLogger('ip22-test')\n"
        body += "class _Host:\n" + src[start:end]
        namespace: dict = {}
        exec(compile(body, path, "exec"), namespace)
        self._host_cls = namespace["_Host"]

    def make(self):
        host = self._host_cls()
        host._plog = "ip22-test"
        host._UNDECODABLE_LOG_INTERVAL_S = 1800.0
        return host


def test_repeated_identical_failures_log_once(caplog) -> None:
    host = _Subject().make()
    with caplog.at_level(logging.WARNING):
        for _ in range(19):
            host._note_undecodable(ValueError("108 is not a valid OperationMode"))
    assert len(caplog.records) == 1


def test_a_different_message_is_reported_immediately(caplog) -> None:
    # A new failure mode must not be hidden by a window opened for
    # another one.
    host = _Subject().make()
    with caplog.at_level(logging.WARNING):
        host._note_undecodable(ValueError("108 is not a valid OperationMode"))
        host._note_undecodable(ValueError("41 is not a valid ChargerError"))
    assert len(caplog.records) == 2
