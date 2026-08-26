"""Provisioning runs through the writer's single slot, in this process.

It was a subprocess (orion_tr_key_cli), which put two of our own
processes on one device; BlueZ holds one connect attempt per device
(dev->att_io), so mode writes during provisioning failed with
"Operation already in progress".  In-process, the writer's slot IS the
serialisation, and the external_session registry that refereed the two
processes is gone because there is no second process left.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


def _load_real(name):
    spec = importlib.util.spec_from_file_location(
        f"_real_prov_{name}", os.path.join(DRIVER_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gatt():
    return _load_real("orion_tr_gatt")


def _writer(gatt):
    w = gatt.AsyncGATTWriter.__new__(gatt.AsyncGATTWriter)
    w._bus = object()
    w._busy = False
    w._agent = None
    return w


def test_a_busy_slot_rejects_with_none(gatt) -> None:
    # The caller distinguishes "no payload" from "payload"; a busy slot
    # must answer through the callback, not silently.
    w = _writer(gatt)
    w._busy = True
    got = []
    w.provision_key("F2:86:C3:32:4C:D2", 14916, on_done=got.append)
    assert got == [None]


def test_provisioning_takes_the_slot(gatt, monkeypatch) -> None:
    w = _writer(gatt)
    monkeypatch.setattr(gatt.ble_async_loop, "start", lambda: True)
    monkeypatch.setattr(gatt.ble_gatt_dbus, "lookup_device",
                        lambda bus, mac, prefer_adapter=None: (None, None))
    submitted = []
    monkeypatch.setattr(gatt.ble_async_loop, "submit",
                        lambda mk, done: submitted.append((mk, done)) or True)

    w.provision_key("F2:86:C3:32:4C:D2", 14916, on_done=lambda p: None)
    assert w.busy is True, "a mode write arriving now must be rejected"
    assert len(submitted) == 1


def test_completion_frees_the_slot_and_adds_adapter(gatt, monkeypatch) -> None:
    w = _writer(gatt)
    monkeypatch.setattr(gatt.ble_async_loop, "start", lambda: True)
    monkeypatch.setattr(gatt.ble_gatt_dbus, "lookup_device",
                        lambda bus, mac, prefer_adapter=None:
                        ("/org/bluez/hci0/dev_F2_86_C3_32_4C_D2",
                         {"Paired": True}))
    monkeypatch.setattr(gatt.ble_gatt_dbus, "adapter_from_path",
                        lambda path: "hci0")
    holder = {}
    monkeypatch.setattr(gatt.ble_async_loop, "submit",
                        lambda mk, done: holder.update(done=done) or True)
    got = []
    w.provision_key("F2:86:C3:32:4C:D2", 14916, on_done=got.append)

    holder["done"]({"key": "ab" * 16, "firmware": "3.05"}, None)
    assert w.busy is False, "the slot must free for the next mode write"
    assert got and got[0]["key"] == "ab" * 16
    assert got[0]["adapter"] == "hci0", (
        "drivers persist the adapter preference from this field")


def test_failure_frees_the_slot_and_reports_none(gatt, monkeypatch) -> None:
    w = _writer(gatt)
    monkeypatch.setattr(gatt.ble_async_loop, "start", lambda: True)
    monkeypatch.setattr(gatt.ble_gatt_dbus, "lookup_device",
                        lambda bus, mac, prefer_adapter=None: (None, None))
    holder = {}
    monkeypatch.setattr(gatt.ble_async_loop, "submit",
                        lambda mk, done: holder.update(done=done) or True)
    got = []
    w.provision_key("F2:86:C3:32:4C:D2", 14916, on_done=got.append)

    holder["done"](None, RuntimeError("link died"))
    assert w.busy is False
    assert got == [None]


def test_the_registry_is_gone(gatt) -> None:
    # Deleting it was the point: a coordination layer for a collision we
    # no longer create.  Its reappearance would mean a subprocess came
    # back without this history.
    assert not hasattr(gatt, "external_session")
    assert not hasattr(gatt, "_await_external_clear")


def test_the_shared_session_validates_keys() -> None:
    hks = _load_real("hex_key_session")
    assert hks.valid_key_payload(None) is None
    assert hks.valid_key_payload({"key": "abc"}) is None
    assert hks.valid_key_payload({"key": "zz" * 16}) is None
    ok = hks.valid_key_payload({"key": "AB" * 16, "firmware": "3.05"})
    assert ok["key"] == "ab" * 16, "stored keys are lowercase hex"
