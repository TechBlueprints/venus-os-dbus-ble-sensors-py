"""
SmartShunt advertisement-filter tests against constructed byte streams.

The full ``victron_ble`` decode chain isn't unit-tested here.  This
module covers the structural path the driver uses to decide whether an
advertisement belongs to a SmartShunt / BMV-Smart:

  - is_smartshunt_manufacturer_data() — length / product-id / record-type
"""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(scope="module")
def smartshunt_module():
    """Import ble_device_smartshunt with stubbed dependencies."""
    if "vedbus" not in sys.modules:
        vedbus = types.ModuleType("vedbus")
        vedbus.VeDbusItemImport = type("VeDbusItemImport", (), {})
        vedbus.VeDbusItemExport = type("VeDbusItemExport", (), {})
        vedbus.VeDbusService = type("VeDbusService", (), {})
        sys.modules["vedbus"] = vedbus

    if "dbus_bus" not in sys.modules:
        dbus_bus = types.ModuleType("dbus_bus")
        dbus_bus.get_bus = lambda _name: types.SimpleNamespace(
            list_names=lambda: [_name])
        sys.modules["dbus_bus"] = dbus_bus

    if "smartshunt_hex" not in sys.modules:
        sh = types.ModuleType("smartshunt_hex")
        sh.start = lambda *a, **k: False
        sys.modules["smartshunt_hex"] = sh

    for name in ("dbus_ble_service", "dbus_role_service", "ble_device",
                 "ble_role", "smartshunt_key_settings", "orion_tr_pin",
                 "dbus_settings_service"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None,
        "DEVICE_CLASSES": {},
        "info": {},
    })
    sys.modules["dbus_role_service"].DbusRoleService = type(
        "DbusRoleService", (), {})
    sys.modules["dbus_ble_service"].DbusBleService = type(
        "DbusBleService", (), {})
    sys.modules["dbus_settings_service"].DbusSettingsService = type(
        "DbusSettingsService", (), {"__init__": lambda self: None})
    for fn in ("advertisement_key_setting_path", "get_advertisement_key",
               "get_firmware_version", "get_preferred_adapter",
               "set_advertisement_key", "set_firmware_version",
               "set_preferred_adapter"):
        setattr(sys.modules["smartshunt_key_settings"], fn,
                lambda *a, **kw: None)
    sys.modules["orion_tr_pin"].resolve_pairing_passkey = lambda _s: 14916

    if "ve_types" not in sys.modules:
        vt = types.ModuleType("ve_types")
        vt.VE_UN8 = int
        sys.modules["ve_types"] = vt

    try:
        import victron_ble  # noqa: F401
        from victron_ble.devices import detect_device_type  # noqa: F401
    except Exception:
        vb = types.ModuleType("victron_ble")
        vb_devices = types.ModuleType("victron_ble.devices")
        vb_devices.detect_device_type = lambda _b: None
        vb_exc = types.ModuleType("victron_ble.exceptions")
        vb_exc.AdvertisementKeyMismatchError = type(
            "AdvertisementKeyMismatchError", (Exception,), {})
        sys.modules["victron_ble"] = vb
        sys.modules["victron_ble.devices"] = vb_devices
        sys.modules["victron_ble.exceptions"] = vb_exc

    import importlib
    return importlib.import_module("ble_device_smartshunt")


def test_short_beacon_passes(smartshunt_module):
    # 0xA38A SmartShunt 1000A, product-id only.
    assert smartshunt_module.is_smartshunt_manufacturer_data(
        bytes.fromhex("10008aa3"))


def test_full_telemetry_battery_monitor_passes(smartshunt_module):
    # record-type 0x02 BatteryMonitor + dummy payload
    data = bytes.fromhex("10008aa302") + bytes(16)
    assert smartshunt_module.is_smartshunt_manufacturer_data(data)


def test_dc_energy_meter_record_passes(smartshunt_module):
    data = bytes.fromhex("10008aa30d") + bytes(16)
    assert smartshunt_module.is_smartshunt_manufacturer_data(data)


def test_too_short_rejected(smartshunt_module):
    assert not smartshunt_module.is_smartshunt_manufacturer_data(
        b"\x10\x00\x8a")
    assert not smartshunt_module.is_smartshunt_manufacturer_data(b"")


def test_ip22_payload_rejected(smartshunt_module):
    # 0xA330 IP22, record 0x08
    data = bytes.fromhex("100030a308") + bytes(16)
    assert not smartshunt_module.is_smartshunt_manufacturer_data(data)


def test_orion_payload_rejected(smartshunt_module):
    # 0xA3C9 Orion-TR
    data = bytes.fromhex("1000c9a304") + bytes(16)
    assert not smartshunt_module.is_smartshunt_manufacturer_data(data)


def test_wrong_record_type_rejected(smartshunt_module):
    # SmartShunt product id but AcCharger record type
    data = bytes.fromhex("10008aa308abcdef")
    assert not smartshunt_module.is_smartshunt_manufacturer_data(data)


@pytest.mark.parametrize("pid_le", [
    "89a3",  # 0xA389 500A
    "8aa3",  # 0xA38A 1000A
    "8ba3",  # 0xA38B 2000A
    "8da3",  # 0xA38D IP67 1000A
    "31c0",  # 0xC031 IP65 1000A
    "36c0",  # 0xC036 IP65 1000A
    "38c0",  # 0xC038 300A
    "81a3",  # 0xA381 BMV-712
])
def test_known_product_ids_pass(smartshunt_module, pid_le):
    assert smartshunt_module.is_smartshunt_manufacturer_data(
        bytes.fromhex("1000" + pid_le))


def test_unknown_product_id_rejected(smartshunt_module):
    # 0xA330 is IP22
    assert not smartshunt_module.is_smartshunt_manufacturer_data(
        bytes.fromhex("100030a3"))
