"""GATT work in this process must not race our own subprocesses.

Key provisioning and telemetry run as subprocesses (orion_tr_key_cli),
because they need their own asyncio loop and pairing agent.  They
connect to the same device AsyncGATTWriter connects to, and BlueZ will
not hold two connect attempts for one device: the second gets
org.bluez.Error.Failed "Operation already in progress", which surfaces
as "Failed to connect after N attempt(s)" on a write a user is watching.

The charger already declined to spawn telemetry while the writer was
busy; the reverse gate was missing.  Measured on dev-cerbo: both
observed failures began within 3 s of a telemetry spawn, while writes
beginning 5 s or more after one succeeded.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading

MAC = "F2:86:C3:32:4C:D2"

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


def _load_real_gatt():
    """Load the real orion_tr_gatt, past conftest's stub.

    conftest installs a stub module named orion_tr_gatt so the device
    drivers can be imported without dbus/GLib.  A plain import here
    returns that stub, and these tests would silently be examining a
    fake that has none of the behaviour under test.
    """
    spec = importlib.util.spec_from_file_location(
        "_real_orion_tr_gatt", os.path.join(DRIVER_DIR, "orion_tr_gatt.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gatt = _load_real_gatt()


def test_a_session_marks_the_device_busy() -> None:
    assert gatt.external_session_active(MAC) is False
    with gatt.external_session(MAC):
        assert gatt.external_session_active(MAC) is True
    assert gatt.external_session_active(MAC) is False


def test_case_does_not_matter() -> None:
    # Config, D-Bus paths and log lines disagree about MAC case; the
    # registry must not be the place that discovers it.
    with gatt.external_session(MAC.lower()):
        assert gatt.external_session_active(MAC.upper()) is True


def test_sessions_nest_without_early_release() -> None:
    # Provisioning can be followed by telemetry for the same device.
    # A plain boolean would clear on the first exit and let a write in
    # while the second subprocess is still connected.
    with gatt.external_session(MAC):
        with gatt.external_session(MAC):
            assert gatt.external_session_active(MAC) is True
        assert gatt.external_session_active(MAC) is True
    assert gatt.external_session_active(MAC) is False


def test_a_raising_subprocess_still_releases() -> None:
    try:
        with gatt.external_session(MAC):
            raise RuntimeError("subprocess blew up")
    except RuntimeError:
        pass
    assert gatt.external_session_active(MAC) is False, (
        "a stuck entry would block every future write to this device")


def test_other_devices_are_unaffected() -> None:
    with gatt.external_session(MAC):
        assert gatt.external_session_active("AA:BB:CC:DD:EE:FF") is False


def test_the_writer_waits_for_a_live_session() -> None:
    """The wait happens in the coroutine, never on the GLib thread."""
    released = threading.Event()

    async def scenario():
        cm = gatt.external_session(MAC)
        cm.__enter__()

        waiter = asyncio.ensure_future(gatt._await_external_clear(MAC))
        await asyncio.sleep(0.2)
        assert not waiter.done(), "must not proceed while a session is live"

        cm.__exit__(None, None, None)
        released.set()
        await asyncio.wait_for(waiter, timeout=5)

    asyncio.get_event_loop().run_until_complete(scenario())
    assert released.is_set()


def test_no_session_means_no_wait() -> None:
    # The common case must not pay for the guard.
    async def scenario():
        started = asyncio.get_event_loop().time()
        await gatt._await_external_clear("11:22:33:44:55:66")
        return asyncio.get_event_loop().time() - started

    elapsed = asyncio.get_event_loop().run_until_complete(scenario())
    assert elapsed < 0.1, f"clear path should be immediate, took {elapsed:.2f}s"


def test_a_stuck_session_does_not_wait_forever(monkeypatch) -> None:
    # A subprocess that never releases must delay a write, not lose it:
    # proceeding and letting BlueZ refuse is no worse than refusing here.
    monkeypatch.setattr(gatt, "EXTERNAL_SESSION_WAIT_S", 1.0)

    async def scenario():
        with gatt.external_session(MAC):
            started = asyncio.get_event_loop().time()
            await gatt._await_external_clear(MAC)
            return asyncio.get_event_loop().time() - started

    elapsed = asyncio.get_event_loop().run_until_complete(scenario())
    assert 0.9 < elapsed < 2.5, f"should give up near the bound, took {elapsed:.2f}s"
