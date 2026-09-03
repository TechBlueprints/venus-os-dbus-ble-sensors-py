"""SmartSolar over advertisements: exact-model detector, no GATT, honest paths.

Captured on prod hci9 on 2026-09-03: the Portable (0xA053, MPPT 75/15)
and the two hardwired VE.Direct 100/50s (0xA057) that must never be
adopted.  The detector is narrowed to 0xA053 on purpose -- "cannot match
a 100/50 regardless of any setting" was the property asked for.
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import sys
import types

import pytest

from fixtures.captured_advertisements import (
    IP22_FULL_TELEMETRY_HEX_SAMPLES,
    SMARTSOLAR_HARDWIRED_100_50_HEX,
    SMARTSOLAR_PORTABLE_HEX,
)

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))


@pytest.fixture(scope="module")
def ss():
    for name in ("vedbus", "dbus_bus", "dbus_ble_service", "dbus_role_service",
                 "ble_device", "ble_role", "smartsolar_key_settings",
                 "dbus_settings_service", "ve_types"):
        sys.modules.setdefault(name, types.ModuleType(name))
    def _stub_publish_value(self, role_service, path, value, **_kw):
        role_service[path] = value
        return True
    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None, "DEVICE_CLASSES": {}, "info": {},
        "_publish_value": _stub_publish_value})
    class _Svc:
        @staticmethod
        def get(): return _Svc()
        def is_device_role_enabled(self, *_): return True
        def is_device_enabled(self, *_): return True
    sys.modules["dbus_ble_service"].DbusBleService = _Svc
    sys.modules["dbus_settings_service"].DbusSettingsService = type(
        "DbusSettingsService", (), {"__init__": lambda self: None})
    sys.modules["smartsolar_key_settings"].get_advertisement_key = lambda *a: None
    sys.modules["ve_types"].VE_UN8 = int
    try:
        from victron_ble.devices import detect_device_type  # noqa: F401
    except Exception:
        vb = types.ModuleType("victron_ble"); vbd = types.ModuleType("victron_ble.devices")
        vbd.detect_device_type = lambda _b: None
        vbe = types.ModuleType("victron_ble.exceptions")
        vbe.AdvertisementKeyMismatchError = type("AdvertisementKeyMismatchError", (Exception,), {})
        sys.modules["victron_ble"] = vb; sys.modules["victron_ble.devices"] = vbd
        sys.modules["victron_ble.exceptions"] = vbe
    if "ble_charger_common" not in sys.modules:
        bcc = types.ModuleType("ble_charger_common")
        bcc.ChargerCommonMixin = type("ChargerCommonMixin", (), {
            "_init_charger_common": lambda self: None,
            "_tick_history": lambda self, *a, **k: None})
        bcc.serial_from_advertised_name = lambda n: None
        sys.modules["ble_charger_common"] = bcc
    sys.modules.pop("ble_device_smartsolar", None)
    return importlib.import_module("ble_device_smartsolar")


def test_detector_accepts_the_portable_and_only_it(ss) -> None:
    assert ss.is_smartsolar_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    for hexs in SMARTSOLAR_HARDWIRED_100_50_HEX:
        assert not ss.is_smartsolar_manufacturer_data(bytes.fromhex(hexs)), (
            "a hardwired MPPT 100/50 (0xA057) must never match, whatever the gate does")


def test_detector_rejects_other_victron_families(ss) -> None:
    for hexs in IP22_FULL_TELEMETRY_HEX_SAMPLES[:2]:
        assert not ss.is_smartsolar_manufacturer_data(bytes.fromhex(hexs))
    frame = bytearray(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX)); frame[4] = 0x08
    assert not ss.is_smartsolar_manufacturer_data(bytes(frame)), "0xA053 but not solar mode"
    assert not ss.is_smartsolar_manufacturer_data(b"\x10\x02\x53\xa0")


def test_no_key_means_no_gatt_and_one_line(ss, caplog) -> None:
    dev = ss.BleDeviceSmartSolar.__new__(ss.BleDeviceSmartSolar)
    dev.info = {"dev_mac": "c120d54f7125"}; dev._plog = "c120d54f7125 - SmartSolar:"
    dev._adv_key_hex = None; dev._dbus_settings = None; dev._no_key_logged_at = 0.0
    dev._role_services = {}
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    lines = [r for r in caplog.records if "no Instant Readout key" in r.message]
    assert len(lines) == 1
    # Scan CODE only: the docstring legitimately says what the driver does
    # not do, and "provision" appears there.  Identifiers are the evidence.
    src = open(os.path.join(SRC, "ble_device_smartsolar.py")).read()
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#.*", "", code)
    for forbidden in ("_maybe_provision_key", "provision_session", "AsyncGATTWriter",
                      "hex_key_session", "orion_tr_gatt", "start_notify",
                      "ble_gatt_link", "lookup_device"):
        assert forbidden not in code, f"this driver must not carry a GATT path: {forbidden}"


class _Role:
    def __init__(self): self.values = {}; self.ble_role = types.SimpleNamespace(NAME="solarcharger")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __setitem__(self, k, v): self.values[k] = v
    def __getitem__(self, k): return self.values[k]


def test_publish_maps_the_solar_record_honestly(ss) -> None:
    dev = ss.BleDeviceSmartSolar.__new__(ss.BleDeviceSmartSolar)
    dev.info = {"dev_mac": "c120d54f7125", "adv_name": "SmartSolar Portable"}
    dev._plog = "x"; role = _Role(); dev._role_services = {"solarcharger": role}
    dev._tick_history = lambda *a, **k: None
    dev._publish({"device_state": 3, "charger_error": 0, "battery_voltage": 13.42,
                  "battery_current": 4.3, "yield_today_kwh": 0.31, "solar_power": 62,
                  "load_current": 0.0, "model_name": "SmartSolar Charger MPPT 75/15"})
    v = role.values
    assert v["/Dc/0/Voltage"] == 13.42 and v["/Dc/0/Current"] == 4.3
    assert v["/Pv/Power"] == 62 and v["/Yield/Power"] == 62
    assert v["/History/Daily/0/Yield"] == 0.31
    assert v["/Load/I"] == 0.0 and v["/Load/State"] == 0
    assert v["/State"] == 3 and v["/ErrorCode"] == 0
    assert "/Serial" not in v, "custom name carries no HQ serial; do not fabricate one"
    assert "/Pv/V" not in v and "/MppOperationMode" not in v, "HEX-only fields stay untouched (None from the role)"


def test_role_declares_what_systemcalc_reads() -> None:
    src = open(os.path.join(SRC, "ble_role_solarcharger.py")).read()
    for p in ("/Pv/Power", "/Yield/Power", "/Load/I", "/Dc/0/Voltage", "/Dc/0/Current",
              "/State", "/ErrorCode", "/History/Daily/0/Yield", "/NrOfTrackers"):
        assert f'"{p}"' in src, p
    assert 'NAME = "solarcharger"' in src


def test_routing_prefers_the_exact_detector_and_prefix_is_owned() -> None:
    sensors = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    assert "is_smartsolar_manufacturer_data(man_data)" in sensors
    assert sensors.index("is_smartsolar_manufacturer_data(man_data)") > sensors.index(
        "is_smartshunt_manufacturer_data(man_data)")
    svc = open(os.path.join(SRC, "dbus_ble_service.py")).read()
    assert '"smartsolar"' in svc.split("OWNED_PREFIXES")[1].split(")")[0]
