"""Every notify we acquire must be released before the link drops.

We ask bleak for the fd-based notify path — `bluez={"use_start_notify":
False}` — because on Venus StartNotify plus PropertiesChanged delivers
empty payloads for these characteristics once the link is SMP-paired.
That makes us the only consumer on the box that calls AcquireNotify.

BlueZ 5.72 stores the notify client into chrc->notify_io->data without
taking a reference (fixed upstream in 5.84/5.86; Venus ships 5.72), so
an acquire still outstanding when the link goes away leaves a dangling
pointer that detonates 30-120 s later during temporary-device cleanup —
far from anything that names this process.  Root-caused by the
bcmv2-crash-analysis session against nine prod bluetoothd crashes.

Before this, we never called stop_notify at all: every session ended
with acquires held, so the planting was our common case rather than an
edge.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

import pytest

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

MODULES = ["orion_tr_gatt", "smartshunt_hex", "orion_tr_key_cli"]


def _load_real(name):
    spec = importlib.util.spec_from_file_location(
        f"_real_notify_{name}", os.path.join(DRIVER_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Services:
    """Two of the three modules gate on the characteristic existing."""

    @staticmethod
    def get_characteristic(char):
        return object()


class _Client:
    def __init__(self, fail_on=(), connected=True):
        self.started = []
        self.stopped = []
        self.services = _Services()
        self.is_connected = connected
        self._fail_on = set(fail_on)

    async def start_notify(self, char, callback, **kw):
        # The acquire path is what we must ask for; record which we got.
        self.started.append((char, kw.get("bluez", {})))

    async def stop_notify(self, char):
        if char in self._fail_on:
            raise RuntimeError("link already gone")
        self.stopped.append(char)


@pytest.fixture(params=MODULES)
def mod(request):
    return _load_real(request.param)


def test_every_module_can_release(mod) -> None:
    assert hasattr(mod, "_stop_notify_all"), (
        f"{mod.__name__} acquires notifies and must be able to release them")


def test_subscribing_records_the_acquire(mod) -> None:
    client = _Client()
    acquired: list = []

    asyncio.get_event_loop().run_until_complete(
        mod._start_notify(client, "char-a", lambda *_: None, acquired))

    assert acquired == ["char-a"], (
        "an unrecorded acquire is one teardown will not release")
    assert client.started[0][1].get("use_start_notify") is False, (
        "we must still be asking for the acquire path")


def test_releasing_empties_the_list(mod) -> None:
    client = _Client()
    acquired = ["a", "b", "c"]

    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, acquired, True))

    assert sorted(client.stopped) == ["a", "b", "c"]
    assert acquired == [], "a leftover entry would be released twice"


def test_a_dead_link_does_not_stop_the_rest(mod) -> None:
    # The common teardown case: the link is already gone, so every
    # stop_notify fails.  It must still drain, and must not raise —
    # raising here would mask the caller's own exception.
    client = _Client(fail_on={"b"})
    acquired = ["a", "b", "c"]

    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, acquired, True))

    assert acquired == []
    assert sorted(client.stopped) == ["a", "c"]


def test_releasing_nothing_is_harmless(mod) -> None:
    client = _Client()
    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, [], True))
    assert client.stopped == []


def test_a_failed_session_is_not_released(mod) -> None:
    """The guard that stopped prod crashing every ~60 s.

    Releasing a notify on a link BlueZ has already torn down is the
    "notify client already freed" precondition for the 5.72 UAF, and it
    buys nothing — a dead link has no notify to stop.  Without this,
    every FAILING session walked notify_io_destroy deliberately: six of
    six bluetoothd SIGSEGVs on prod landed within 0-1 s of a session
    drop, and crashes-per-failed-session went from ~0.01 to ~0.15 while
    session volume actually fell.
    """
    client = _Client()
    acquired = ["a", "b"]

    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, acquired, False))

    assert client.stopped == [], "must not touch a torn-down link"
    assert acquired == [], "but must still forget them"


def test_a_live_link_is_still_released(mod) -> None:
    # The hygiene case this was always aimed at: a clean session end,
    # where the acquire is real and BlueZ is still holding it.
    client = _Client(connected=True)
    acquired = ["a", "b"]

    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, acquired, True))

    assert sorted(client.stopped) == ["a", "b"]
    assert acquired == []


def test_the_guard_does_not_consult_is_connected(mod) -> None:
    """It must not, and this pins it.

    The first version of this guard skipped the release when
    client.is_connected was False.  Prod kept crashing at the same rate,
    because is_connected reads BlueZ's CACHED Connected property — the
    signal that lies on a phantom connection, which is precisely the
    kind of session that fails.  A client reporting itself connected
    while the link is gone would sail straight through that check.

    The condition is now whether the session completed, which the
    caller knows for certain.
    """
    class _Trap:
        def __init__(self):
            self.stopped = []
            self.services = _Services()

        @property
        def is_connected(self):
            raise AssertionError(
                "the guard must not depend on a property that can lie")

        async def stop_notify(self, char):
            self.stopped.append(char)

    client = _Trap()
    acquired = ["a"]
    asyncio.get_event_loop().run_until_complete(
        mod._stop_notify_all(client, acquired, False))
    assert client.stopped == []
    assert acquired == []
