"""dbus-python off the mainloop thread needs a connection with no mainloop.

dbus-python's DBusGMainLoop supports only the DEFAULT main context, so a
normally-constructed connection wires its watches into the MAIN thread's
loop no matter which thread built it.  A worker thread then calls
synchronously on a connection the main thread is dispatching, which
libdbus does not support — on dev-cerbo that produced six SIGABRTs with
"malloc(): unaligned fastbin chunk detected".
"""
from __future__ import annotations

import inspect

import dbus_bus


def test_unattached_constructor_exists_and_is_separate_from_get_bus() -> None:
    # Deliberately NOT folded into get_bus: that cache exists to share
    # mainloop-integrated connections, and its callers want signals.
    assert callable(dbus_bus.get_private_unattached_bus)
    assert dbus_bus.get_private_unattached_bus is not dbus_bus.get_bus


def test_it_opts_out_of_the_main_loop() -> None:
    src = inspect.getsource(dbus_bus.get_private_unattached_bus)
    assert "NULL_MAIN_LOOP" in src
    assert "mainloop=NULL_MAIN_LOOP" in src


def test_it_is_not_cached() -> None:
    # Caching would hand one thread's connection to another, which is the
    # hazard this function exists to avoid.
    src = inspect.getsource(dbus_bus.get_private_unattached_bus)
    assert "_bus_instances" not in src


def test_follower_uses_the_unattached_bus_not_the_cached_one() -> None:
    import bms_link_follower
    # The construction lives in _new_bus so the liveness rebuild can
    # re-use it; the pin is that ALL construction goes through the
    # unattached constructor, wherever it moves within the class.
    src = inspect.getsource(bms_link_follower.DbusBusOps)
    assert "get_private_unattached_bus" in src
    # The specific call, not the substring — the comment above it says
    # "NOT get_bus()", which a looser check would match.
    assert 'get_bus("bms-link-follower")' not in src


def test_follower_can_close_its_connection() -> None:
    # A connection left open leaks an fd per follower restart and counts
    # against max_connections_per_user.
    import bms_link_follower
    assert hasattr(bms_link_follower.DbusBusOps, "close")


def test_follower_closes_when_its_thread_stops() -> None:
    import bms_link_follower
    src = inspect.getsource(bms_link_follower.BmsLinkFollower._run)
    assert "finally" in src and "close()" in src


def test_provisioning_persists_run_on_the_mainloop() -> None:
    """The persist does settings writes and role publishes — GLib-thread
    work.  It used to be marshalled there with GLib.idle_add from a
    provisioning worker THREAD; provisioning now runs in-process through
    AsyncGATTWriter.provision_key, whose completion callback already
    arrives on the GLib thread (ble_async_loop.submit marshals it), so
    the correct shape is a direct call from the done callback and NO
    worker thread left to be wrong on.  What this pins is the absence of
    the thread machinery: no provisioning Thread, no idle_add wrapper.
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    driver = os.path.normpath(os.path.join(
        here, "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
    for name in ("ble_device_smartshunt.py", "ble_device_orion_tr.py",
                 "ble_device_ip22_charger.py"):
        with open(os.path.join(driver, name)) as fh:
            src = fh.read()
        assert "provision_key(" in src, f"{name} should use the writer"
        assert "GLib.idle_add(self._persist_provisioning_result" not in src, \
            f"{name}: the idle_add marshal implies a worker thread came back"
        assert "keyprov" not in src, \
            f"{name}: provisioning worker thread still present"
