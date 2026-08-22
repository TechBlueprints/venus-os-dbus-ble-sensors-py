# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""One long-lived asyncio loop for the service's BLE work.

This service's main loop is GLib; bleak's is asyncio.  Rather than pump
one from the other, BLE coroutines run on a single daemon thread that
owns an event loop for the life of the process, and results come back to
the GLib thread through :func:`GLib.idle_add`.

*One* loop, not one per operation.  bleak keeps its BlueZManager — and
underneath it a ``dbus_fast`` MessageBus — as a per-event-loop singleton,
so a loop per GATT write leaks a system-bus connection each time.  On a
Cerbo that is the same failure mode PR #7 fixed for our dbus-python
connections, and it is why the sibling services (dbus-shyion-switch,
dbus-easytouchrv) settled on a persistent loop too.

The catcher is installed by :func:`start` *before* the loop exists, so
every bleak client built on it is already routed through bcmv2.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from gi.repository import GLib

import ble_catcher

logger = logging.getLogger(__name__)

_START_TIMEOUT_S = 5.0

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
_start_failed = False


def _run_loop() -> None:
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    _ready.set()
    try:
        loop.run_forever()
    finally:
        try:
            loop.close()
        except Exception:
            pass


def start() -> bool:
    """Install the catcher and spin up the BLE event loop.

    Returns ``False`` when the vendored stack is missing or the thread
    never came up — callers should then treat GATT as unavailable rather
    than block.  Idempotent and safe to call from any thread.
    """
    global _thread, _start_failed
    with _lock:
        if _loop is not None and _thread is not None and _thread.is_alive():
            return True
        if _start_failed:
            return False

        # Before the loop, and before anything imports bleak's classes.
        if not ble_catcher.install():
            _start_failed = True
            return False

        _ready.clear()
        _thread = threading.Thread(target=_run_loop, name="BleAsyncLoop",
                                   daemon=True)
        _thread.start()
        if not _ready.wait(timeout=_START_TIMEOUT_S):
            logger.error("BLE event loop did not start within %.0fs — "
                         "GATT operations are unavailable", _START_TIMEOUT_S)
            _start_failed = True
            return False
        logger.info("BLE event loop running")
        return True


def available() -> bool:
    """Whether :func:`submit` can currently accept work."""
    return _loop is not None and _thread is not None and _thread.is_alive()


def submit(make_coro, on_done=None) -> bool:
    """Run ``make_coro()`` on the BLE loop, report back on the GLib loop.

    *make_coro* is a zero-argument callable returning a coroutine — a
    factory rather than a coroutine object so nothing is created (and
    then never awaited) when the loop is unavailable.

    *on_done*, if given, is called as ``on_done(result, error)`` on the
    **GLib main thread**: exactly one of the two is ``None``.  Callers
    therefore touch D-Bus and driver state from the thread that owns
    them, never from the BLE loop.

    Returns ``False`` if the work could not be scheduled at all, in which
    case *on_done* is not called.
    """
    if not start():
        return False

    loop = _loop
    if loop is None:
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(make_coro(), loop)
    except Exception:
        logger.exception("failed to schedule BLE coroutine")
        return False

    if on_done is None:
        return True

    def _on_settled(fut) -> None:
        try:
            result, error = fut.result(), None
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            result, error = None, exc

        def _deliver() -> bool:
            try:
                on_done(result, error)
            except Exception:
                logger.exception("BLE completion callback raised")
            return False  # one-shot

        GLib.idle_add(_deliver)

    future.add_done_callback(_on_settled)
    return True


def shutdown() -> None:
    """Stop the loop thread.  Only used by tests and by clean exits."""
    global _loop, _thread
    with _lock:
        loop, thread = _loop, _thread
        _loop, _thread = None, None
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=2.0)
