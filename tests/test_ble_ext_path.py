"""Path bootstrap: vendored copies are the fallback, not the default.

The fleet's BLE consumers share one checkout at /data/bcm so that a
placement or drain fix reaches every service on its next restart, and so
that the claims in /run/bt-claims mean the same thing to all of them.
The /data/bcm/python3 shim already puts that stack on sys.path, so when
we run under it we must add nothing — inserting our ext/ copies ahead of
it would silently pin this service to a different sha of the shared
protocol.  A bare clone with no shim still has to work, which is why the
vendored set stays.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

import ble_ext_path


@pytest.fixture(autouse=True)
def _clean_sys_path():
    """Start each test with our ext/ dirs off sys.path.

    Any earlier test that imported an entry point may have installed
    them already, which would make "did install() add anything?"
    trivially false for reasons unrelated to the branch under test.
    """
    saved = list(sys.path)
    sys.path[:] = [p for p in sys.path if ble_ext_path._EXT_DIR not in p]
    try:
        yield
    finally:
        sys.path[:] = saved


def _fresh():
    mod = importlib.reload(ble_ext_path)
    return mod


def test_defers_to_a_stack_already_imported(monkeypatch) -> None:
    mod = _fresh()
    stub = types.ModuleType("bleak_connection_manager")
    stub.__spec__ = None          # what makes find_spec raise ValueError
    monkeypatch.setitem(sys.modules, "bleak_connection_manager", stub)
    before = list(sys.path)

    assert mod.install() is True
    assert sys.path == before, "must not shadow the shim's stack"


def test_defers_to_a_stack_the_shim_put_on_the_path(monkeypatch) -> None:
    mod = _fresh()
    monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a, **k: object() if name == "bleak_connection_manager"
        else None)
    before = list(sys.path)

    assert mod.install() is True
    assert sys.path == before


def test_falls_back_to_vendored_when_nothing_provides_it(monkeypatch) -> None:
    mod = _fresh()
    monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None)
    before = list(sys.path)

    mod.install()
    added = [p for p in sys.path if p not in before]
    assert added, "a bare clone has to get the vendored stack"
    assert all("/ext/" in p or p.endswith("/ext") for p in added)


def test_a_broken_find_spec_degrades_to_vendored(monkeypatch) -> None:
    # The safe direction: shadowing the shim with our own copy beats
    # running with no stack at all.
    mod = _fresh()
    monkeypatch.delitem(sys.modules, "bleak_connection_manager", raising=False)

    def _boom(name, *a, **k):
        raise ValueError("weird interpreter state")

    monkeypatch.setattr(importlib.util, "find_spec", _boom)
    before = list(sys.path)

    mod.install()
    assert [p for p in sys.path if p not in before]
