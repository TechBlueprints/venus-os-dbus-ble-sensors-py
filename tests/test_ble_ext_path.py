"""The stack comes from the shared checkout, and nothing else.

/data/bcm is one checkout of bleak-connection-manager serving every BLE
consumer on the box, which is what makes the claims in /run/bt-claims
mean the same thing to all of them.  This repo used to carry its own
copy as a fallback; it was removed because the fallback was reached
precisely when converging the shared checkout had failed, so a stale one
meant silently running an older stack at the moment the box had just
reported being unhealthy.  Found in exactly that state on prod, five
commits behind, having announced nothing.

So install() adds nothing to sys.path.  Its whole job is to answer
"did the stack arrive", and to say something actionable when it did not.
"""
from __future__ import annotations

import importlib
import sys
import types

import ble_ext_path


def _fresh():
    return importlib.reload(ble_ext_path)


def test_reports_available_when_the_shim_provided_the_stack(monkeypatch) -> None:
    mod = _fresh()
    for name in ("bleak_connection_manager", "bleak"):
        stub = types.ModuleType(name)
        stub.__spec__ = None      # what makes find_spec raise ValueError
        monkeypatch.setitem(sys.modules, name, stub)
    before = list(sys.path)

    assert mod.install() is True
    assert mod.available() is True
    assert sys.path == before, "install() must not touch sys.path any more"


def test_reports_unavailable_and_says_what_to_do(monkeypatch, caplog) -> None:
    mod = _fresh()
    for name in ("bleak_connection_manager", "bleak"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None)

    caplog.set_level("WARNING", logger="ble_ext_path")
    assert mod.install() is False
    assert mod.available() is False
    # A bare "unavailable" sends the reader looking in the wrong repo.
    assert "/data/bcm/python3" in caplog.text
    assert "install.sh" in caplog.text


def test_a_broken_find_spec_reads_as_unavailable(monkeypatch) -> None:
    # Degrade to "no stack" rather than to a wrong claim that one is
    # present: the caller's next move is importing bleak, which would
    # fail anyway, and saying so first is more useful than a traceback.
    mod = _fresh()
    for name in ("bleak_connection_manager", "bleak"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    def _boom(name, *a, **k):
        raise ValueError("weird interpreter state")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    assert mod.install() is False


def test_claims_are_available_without_bleak(monkeypatch) -> None:
    # The point of the separate check: the advertisement scanner
    # publishes claims and never touches bleak, so a box with the claims
    # layer but no bleak still coordinates adapters correctly.
    mod = _fresh()
    stub = types.ModuleType("bleak_connection_manager")
    stub.__spec__ = None
    monkeypatch.setitem(sys.modules, "bleak_connection_manager", stub)
    monkeypatch.delitem(sys.modules, "bleak", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None)

    assert mod.claims_available() is True
    assert mod.install() is False


def test_install_is_idempotent(monkeypatch) -> None:
    mod = _fresh()
    stub = types.ModuleType("bleak_connection_manager")
    stub.__spec__ = None
    monkeypatch.setitem(sys.modules, "bleak_connection_manager", stub)
    monkeypatch.setitem(sys.modules, "bleak", stub)
    assert mod.install() is True
    monkeypatch.delitem(sys.modules, "bleak_connection_manager")
    assert mod.install() is True, "the answer is cached, not re-derived"
