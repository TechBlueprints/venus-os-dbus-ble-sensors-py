"""
Behaviour tests for SmartShunt publish paths and the battery role surface.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


class _CapturingService:
    def __init__(self):
        self.paths: dict[str, dict] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def add_path(self, path, value, writeable=False, onchangecallback=None):
        self.paths[path] = {
            "initial": value,
            "writeable": writeable,
            "onchangecallback": onchangecallback,
        }


class _FakeRoleService:
    def __init__(self, ble_role_name="battery"):
        self.values: dict[str, object] = {}
        self.ble_role = types.SimpleNamespace(NAME=ble_role_name)
        self.connected = False

    def __setitem__(self, key, value):
        self.values[key] = value

    def __getitem__(self, key):
        return self.values[key]

    def __contains__(self, key):
        return key in self.values

    def connect(self):
        self.connected = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(scope="module")
def role_module():
    if "ble_role" not in sys.modules:
        ble_role = types.ModuleType("ble_role")
    else:
        ble_role = sys.modules["ble_role"]
    if not hasattr(ble_role, "BleRole"):
        class _BleRoleBase:
            def __init__(self, config=None):
                self.info = {}
        ble_role.BleRole = _BleRoleBase
        sys.modules["ble_role"] = ble_role
    return importlib.import_module("ble_role_battery")


def test_battery_role_publishes_bmv_paths(role_module):
    role = role_module.BleRoleBattery()
    svc = _CapturingService()
    rs = types.SimpleNamespace(_dbus_service=svc, _ble_device=None)
    role.init(rs)
    for required in ("/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power",
                     "/Soc", "/ConsumedAmphours", "/TimeToGo",
                     "/Alarms/LowVoltage", "/Alarms/LowSoc"):
        assert required in svc.paths, f"missing required path {required}"


@pytest.fixture(scope="module")
def smartshunt_module():
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

    def _stub_publish_value(self, role_service, path, value, **_kw):
        role_service[path] = value
        return True

    sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
        "MANUFACTURER_ID": None,
        "DEVICE_CLASSES": {},
        "info": {},
        "_publish_value": _stub_publish_value,
    })
    sys.modules["dbus_role_service"].DbusRoleService = type(
        "DbusRoleService", (), {})

    class _StubBleSvc:
        @staticmethod
        def get():
            return _StubBleSvc()

        def is_device_role_enabled(self, _info, _name):
            return True

        def is_device_enabled(self, _info):
            return True

    sys.modules["dbus_ble_service"].DbusBleService = _StubBleSvc
    existing = sys.modules.get("ble_device_smartshunt")
    if existing is not None:
        existing.DbusBleService = _StubBleSvc
        existing.BleDeviceSmartShunt._publish_value = _stub_publish_value
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

    return importlib.import_module("ble_device_smartshunt")


def _make_device(mod):
    device = mod.BleDeviceSmartShunt.__new__(mod.BleDeviceSmartShunt)
    device.info = {
        "dev_mac": "df1b3b4e0541",
        "product_id": 0xA38A,
        "serial": "HQ2234CT7MN",
    }
    device._plog = "df1b3b4e0541 - SmartShunt:"
    device._role_services = {"battery": _FakeRoleService()}
    device._adv_key_hex = "00" * 16
    device._stored_key_invalid = False
    device._last_full_telemetry_at = 0.0
    return device


def test_publish_writes_battery_paths(smartshunt_module):
    device = _make_device(smartshunt_module)
    device._publish({
        "voltage": 13.21,
        "current": -2.5,
        "power": -33.02,
        "soc": 87.3,
        "consumed_ah": -12.4,
        "ttg_s": 3600,
        "temperature": 22.0,
        "aux_voltage": 12.6,
        "alarm": 0,
        "model_name": "SmartShunt 1000A/50mV",
    })
    rs = device._role_services["battery"]
    assert rs.values["/Dc/0/Voltage"] == 13.21
    assert rs.values["/Dc/0/Current"] == -2.5
    assert rs.values["/Dc/0/Power"] == -33.02
    assert rs.values["/Soc"] == 87.3
    assert rs.values["/ConsumedAmphours"] == -12.4
    assert rs.values["/TimeToGo"] == 3600
    assert rs.values["/Dc/1/Voltage"] == 12.6
    assert rs.values["/Serial"] == "HQ2234CT7MN"
    assert rs.values["/Alarms/LowVoltage"] == 0
    assert rs.connected


def test_publish_empty_clears_measurements(smartshunt_module):
    device = _make_device(smartshunt_module)
    device._publish_empty_state()
    rs = device._role_services["battery"]
    assert rs.values["/Dc/0/Voltage"] is None
    assert rs.values["/Soc"] is None
    assert rs.values["/ConsumedAmphours"] is None
    assert rs.values["/Alarms/LowVoltage"] == 0


def test_publish_alarms_low_voltage_and_low_soc(smartshunt_module):
    device = _make_device(smartshunt_module)
    device._publish({
        "voltage": 11.2,
        "current": 0.0,
        "power": 0.0,
        "soc": 8.0,
        "consumed_ah": -90.0,
        "ttg_s": None,
        "temperature": None,
        "aux_voltage": None,
        "alarm": 1 | 4,  # LOW_VOLTAGE | LOW_SOC
        "model_name": None,
    })
    rs = device._role_services["battery"]
    assert rs.values["/Alarms/LowVoltage"] == 2
    assert rs.values["/Alarms/LowSoc"] == 2
    assert rs.values["/Alarms/HighVoltage"] == 0


def test_vedirect_hex_get_ec65_checksum():
    from victron_vreg import encode_vedirect_hex_get
    assert encode_vedirect_hex_get(0xEC65) == b":765ECFD\n"
    assert encode_vedirect_hex_get(0x0100) == b":700014D\n"
