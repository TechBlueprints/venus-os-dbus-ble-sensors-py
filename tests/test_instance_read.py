"""The SmartShunt-class HEX read shares the writer's single slot.

A SmartSolar MPPT does not answer instance-0 GetValues (error 1 for every
register, measured 2026-09-05), so it gets its own session shape:
GetDevices, subscribe every instance, harvest Pushes.  What these tests
pin is the slot discipline and the callback contract, which the driver's
PV poll relies on.
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
        f"_real_inst_{name}", os.path.join(DRIVER_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gatt():
    return _load_real("orion_tr_gatt")


def _writer(gatt):
    w = gatt.AsyncGATTWriter.__new__(gatt.AsyncGATTWriter)
    w._bus = object(); w._busy = False; w._agent = None
    return w


def test_a_busy_slot_rejects_through_the_callback(gatt) -> None:
    w = _writer(gatt); w._busy = True
    got = []
    w.read_instance_pushes("C1:20:D5:4F:71:25", 14916, [0xEDBB],
                           on_done=lambda ok, vals, adapter: got.append((ok, vals, adapter)))
    assert got == [(False, {}, None)]


def test_completion_frees_the_slot_and_names_the_adapter(gatt, monkeypatch) -> None:
    w = _writer(gatt)
    monkeypatch.setattr(gatt.ble_async_loop, "start", lambda: True)
    seen = {}
    def lookup(bus, mac, prefer_adapter=None):
        seen["prefer"] = prefer_adapter
        return ("/org/bluez/hci1/dev_C1_20_D5_4F_71_25", {"Paired": True})
    monkeypatch.setattr(gatt.ble_gatt_dbus, "lookup_device", lookup)
    holder = {}
    monkeypatch.setattr(gatt.ble_async_loop, "submit",
                        lambda mk, done: holder.update(done=done) or True)
    got = []
    w.read_instance_pushes("c1:20:d5:4f:71:25", 14916, [0xEDBB, 0xEDB3],
                           on_done=lambda *a: got.append(a),
                           prefer_adapter="000195CC32F7")
    assert w.busy is True
    assert seen["prefer"] == "000195CC32F7", "the driver's stored card steers the lookup"
    holder["done"]({0xEDBB: b"\x7e\x06"}, None)
    assert w.busy is False
    assert got == [(True, {0xEDBB: b"\x7e\x06"}, "hci1")]


def test_unreachable_is_reported_quietly_and_frees_the_slot(gatt, monkeypatch, caplog) -> None:
    w = _writer(gatt)
    monkeypatch.setattr(gatt.ble_async_loop, "start", lambda: True)
    monkeypatch.setattr(gatt.ble_gatt_dbus, "lookup_device",
                        lambda bus, mac, prefer_adapter=None:
                        ("/org/bluez/hci9/dev_C1_20_D5_4F_71_25", {"Paired": True}))
    holder = {}
    monkeypatch.setattr(gatt.ble_async_loop, "submit",
                        lambda mk, done: holder.update(done=done) or True)
    got = []
    w.read_instance_pushes("C1:20:D5:4F:71:25", 14916, [0xEDBB], on_done=lambda *a: got.append(a))
    # bleak-retry-connector's "not found" is what a card that cannot hear
    # the unit produces (hci9 on prod, 2026-09-05).
    exc = type("BleakNotFoundError", (Exception,), {})("Failed to connect after 3 attempt(s)")
    with caplog.at_level("WARNING"):
        holder["done"](None, exc)
    assert w.busy is False
    assert got == [(False, {}, "hci9")]
    assert any("unreachable via hci9" in r.getMessage() for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_the_session_speaks_the_smartshunt_dialect() -> None:
    """Source-level: subscribe the listed instances, ask in the definite form."""
    src = open(os.path.join(DRIVER_DIR, "orion_tr_gatt.py")).read()
    body = src[src.index("async def _perform_instance_read"):src.index("async def _perform_provision")]
    assert "encode_get_devices()" in body
    assert "parse_device_list_instances(collector.frames)" in body
    assert "encode_subscribe_instance(inst)" in body
    assert "definite=True" in body, "instance-0 indefinite reads are exactly what this unit ignores"
    assert "client.pair()" in body, "a device unknown to the card still gets bonded"
    assert "_puk_pin" not in body, "measured: PV registers come back without PIN auth"
