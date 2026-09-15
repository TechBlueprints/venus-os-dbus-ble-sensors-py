"""The vendored victron_ble parsers must not raise on an off_reason the
enum does not list.

On 2026-09-15 an Orion-TR on prod advertised off_reason = 0xFFFFFFFF,
the "not available" sentinel the same parser already maps to None for
voltage and current.  Upstream 0.9.3 builds the field with
OffReason(value), which raises ValueError and discards the whole record.
ble_device_orion_tr maps a None off_reason to 0 ("no reason") already.
"""
from __future__ import annotations

import os
import struct
import sys

import pytest

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
EXT = os.path.join(SRC, "ext")
for p in (SRC, EXT):
    if p not in sys.path:
        sys.path.insert(0, p)

from victron_ble.devices.base import OffReason  # noqa: E402
from victron_ble.devices.orion_xs import OrionXS  # noqa: E402
from victron_ble.devices.smart_battery_protect import SmartBatteryProtect  # noqa: E402


def _orion_record(off_reason: int) -> bytes:
    # device_state, charger_error, out_v, out_i, in_v, in_i, off_reason
    return struct.pack("<BBHHHHI", 3, 0, 1350, 125, 2650, 70, off_reason)


def test_orion_xs_all_ones_off_reason_is_none_not_an_exception() -> None:
    parsed = OrionXS.parse_decrypted(OrionXS.__new__(OrionXS), _orion_record(0xFFFFFFFF))
    assert parsed["off_reason"] is None
    # the rest of the record survives
    assert parsed["output_voltage"] == 13.5 and parsed["input_voltage"] == 26.5


def test_orion_xs_listed_off_reasons_still_decode() -> None:
    for reason in OffReason:
        parsed = OrionXS.parse_decrypted(OrionXS.__new__(OrionXS), _orion_record(reason.value))
        assert parsed["off_reason"] is reason


def test_orion_xs_unlisted_combination_is_none() -> None:
    # 0x05 = NO_INPUT_POWER | SWITCHED_OFF_REGISTER: a real bitmask the
    # enum cannot name; must not raise
    parsed = OrionXS.parse_decrypted(OrionXS.__new__(OrionXS), _orion_record(0x00000005))
    assert parsed["off_reason"] is None


def test_smart_battery_protect_has_the_same_tolerance() -> None:
    src = open(os.path.join(EXT, "victron_ble", "devices", "smart_battery_protect.py")).read()
    assert "OffReason._value2member_map_" in src
    assert 'OffReason(off_reason),\n' not in src


def test_driver_maps_a_none_off_reason_to_no_reason() -> None:
    src = open(os.path.join(SRC, "ble_device_orion_tr.py")).read()
    assert 'int(off_reason.value) if off_reason is not None else 0' in src
