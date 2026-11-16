"""MPPT PV-side VREG decoding and SmartShunt-class instance parsing.

Wire values are what a SmartSolar MPPT 75/15 returned on prod
2026-09-05, alongside its live advertisement and the GUI.
"""
from __future__ import annotations

import importlib.util
import os
import sys

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


def _vreg():
    spec = importlib.util.spec_from_file_location(
        "_real_vreg_solar", os.path.join(DRIVER_DIR, "victron_vreg.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pv_registers_decode_in_protocol_units() -> None:
    v = _vreg()
    assert v.decode_solarcharger_vreg(0xEDBB, bytes.fromhex("7e06")) == {"pv_voltage": 16.62}
    assert v.decode_solarcharger_vreg(0xEDBC, bytes.fromhex("a23e0000")) == {"pv_power": 160.34}
    assert v.decode_solarcharger_vreg(0xEDB3, b"\x02") == {"mpp_operation_mode": 2}
    assert v.decode_solarcharger_vreg(0xEDD3, bytes.fromhex("29000000")) == {"yield_today_kwh": 0.41}
    assert v.decode_solarcharger_vreg(0xEDBD, bytes.fromhex("7100")) == {"pv_current": 11.3}


def test_not_available_sentinels_decode_to_nothing() -> None:
    v = _vreg()
    assert v.decode_solarcharger_vreg(0xEDBB, b"\xff\xff") == {}
    assert v.decode_solarcharger_vreg(0xEDBC, b"\xff\xff\xff\xff") == {}
    assert v.decode_solarcharger_vreg(0xEDB3, b"") == {}
    assert v.decode_solarcharger_vreg(0xED8D, bytes.fromhex("5f05")) == {}, "not a PV register"


def test_getdevices_instances_come_from_the_push_and_default_when_absent() -> None:
    v = _vreg()
    # Captured: 02 9F 00 00 01 00 03 01 FF -> instances 0, 1, 3 (pairs of (instance, extra)).
    assert v.parse_device_list_instances([bytes.fromhex("029f000001000301ff")]) == [0, 1, 3]
    assert v.parse_device_list_instances([b"\x08\x03\x19\xed\x8d\x42\x5b\x05"]) == [0]
    assert v.parse_device_list_instances([], default=(0, 3)) == [0, 3]
