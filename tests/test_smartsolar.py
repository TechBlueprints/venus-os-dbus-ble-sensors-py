"""SmartSolar over advertisements: exact-model detector, IP22-style key recovery, honest paths.

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
    ks = sys.modules["smartsolar_key_settings"]
    ks.get_advertisement_key = lambda *a: None
    for name in ("set_advertisement_key", "get_preferred_adapter", "set_preferred_adapter",
                 "set_firmware_version", "advertisement_key_setting_path"):
        setattr(ks, name, lambda *a, **k: None)
    ks.advertisement_key_setting_path = lambda mac: f"/Settings/Devices/smartsolar_{mac}/AdvertisementKey"
    for name in ("orion_tr_gatt", "orion_tr_pin", "hex_key_session", "dbus"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["orion_tr_gatt"].AsyncGATTWriter = type("AsyncGATTWriter", (), {})
    sys.modules["orion_tr_pin"].resolve_pairing_passkey = lambda *_: 0
    def _valid(p):
        k = str((p or {}).get("key", "")).strip().lower()
        return p if len(k) == 32 and all(c in "0123456789abcdef" for c in k) else None
    sys.modules["hex_key_session"].valid_key_payload = _valid
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
    bcc = sys.modules["ble_charger_common"]
    if not hasattr(bcc, "format_firmware_version"):
        bcc.format_firmware_version = lambda raw: raw
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
    assert not ss.is_smartsolar_manufacturer_data(b"\x10\x02\x57\xa0"), "short frame, but a 100/50"
    assert not ss.is_smartsolar_manufacturer_data(b"\x10\x02\x53")


def test_short_product_id_only_beacon_is_still_ours(ss) -> None:
    """An MPPT with nothing to report drops the payload and beacons its id.

    This MUST match: a False here falls through the dispatcher to
    BleDeviceVictronEnergy, whose failing check blacklists the MAC in
    _ignored_mac for the life of the process.  On prod one such beacon
    arriving first silenced the charger until the next restart.
    """
    assert ss.is_smartsolar_manufacturer_data(b"\x10\x02\x53\xa0")
    dev = ss.BleDeviceSmartSolar.__new__(ss.BleDeviceSmartSolar)
    dev.info = {"dev_mac": "c120d54f7125"}; dev._plog = "x"
    dev._adv_key_hex = None; dev._dbus_settings = None; dev._role_services = {}
    dev._stored_key_invalid = False; dev._last_provision_attempt = 0.0
    dev._provision_attempts = 0; dev._gave_up_logged = False
    # configure() must survive the 4-byte frame, and a short frame must
    # never reach the decoder.
    ss.BleDeviceSmartSolar.check_manufacturer_data(dev, b"\x10\x02\x53\xa0")


class _Writer:
    """Stands in for the module's single-slot GATT writer."""
    def __init__(self): self.calls = []
    def provision_key(self, mac, passkey, on_done, prefer_adapter=None, timeout_s=60.0):
        self.calls.append((mac, passkey, prefer_adapter)); self.on_done = on_done


def _device(ss, monkeypatch, enabled=True):
    dev = ss.BleDeviceSmartSolar.__new__(ss.BleDeviceSmartSolar)
    dev.info = {"dev_mac": "c120d54f7125"}; dev._plog = "c120d54f7125 - SmartSolar:"
    dev._adv_key_hex = None; dev._dbus_settings = object(); dev._role_services = {}
    dev._pairing_passkey = 123456; dev._last_provision_attempt = 0.0
    dev._provision_attempts = 0; dev._stored_key_invalid = False
    dev._gave_up_logged = False; dev._last_full_telemetry_at = 0.0
    w = _Writer(); monkeypatch.setattr(ss, "_gatt", lambda: w); ss._provision_busy = False
    stored = {}
    monkeypatch.setattr(ss, "set_advertisement_key", lambda _s, mac, k: stored.__setitem__(mac, k))
    monkeypatch.setattr(ss, "set_preferred_adapter", lambda _s, mac, a: stored.__setitem__("adapter", a))
    monkeypatch.setattr(ss, "set_firmware_version", lambda _s, mac, v: stored.__setitem__("fw", v))
    monkeypatch.setattr(ss, "get_preferred_adapter", lambda *_: "AA:BB:CC:DD:EE:01")
    svc = sys.modules["dbus_ble_service"].DbusBleService
    monkeypatch.setattr(svc, "is_device_enabled", lambda self, *_: enabled)
    return dev, w, stored


def test_no_key_starts_one_provisioning_session_not_one_per_frame(ss, monkeypatch, caplog) -> None:
    """The IP22 rule: a missing key is fetched over ONE paired HEX session
    (VREG 0xEC65); frames arriving while it runs, or inside the backoff,
    do not start another."""
    dev, w, stored = _device(ss, monkeypatch)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert w.calls == [("C1:20:D5:4F:71:25", 123456, "AA:BB:CC:DD:EE:01")]
    assert sum("provisioning" in r.message for r in caplog.records) == 1
    # A valid payload is persisted key + adapter + firmware, and the key is live.
    w.on_done({"key": "0F" * 16, "adapter": "AA:BB:CC:DD:EE:01", "firmware": "0159"})
    assert stored["c120d54f7125"] == "0f" * 16 and stored["adapter"] == "AA:BB:CC:DD:EE:01"
    assert stored["fw"] == "0159" and dev._adv_key_hex == "0f" * 16
    dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert len(w.calls) == 1, "a device with a key never provisions again"


def test_short_or_empty_payload_is_not_persisted(ss, monkeypatch) -> None:
    dev, w, stored = _device(ss, monkeypatch)
    dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    w.on_done({"key": "abcd"})
    assert stored == {} and dev._adv_key_hex is None, "the 4cbc0900 guard: never persist a short key"
    w.on_done(None)
    assert stored == {}


def test_attempts_are_bounded_then_the_driver_says_so_once(ss, monkeypatch, caplog) -> None:
    """Under the fleet notify policy the HEX path has not yet been proven;
    a session that keeps timing out must not churn a GATT link forever on
    prod.  Bounded attempts per process, one WARNING, then silence."""
    dev, w, stored = _device(ss, monkeypatch)
    t = [1000.0]; monkeypatch.setattr(ss.time, "monotonic", lambda: t[0])
    limit = ss.BleDeviceSmartSolar._PROVISION_MAX_ATTEMPTS
    with caplog.at_level(logging.INFO):
        for _ in range(limit + 3):
            dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
            if getattr(w, "on_done", None):
                w.on_done(None); w.on_done = None
            t[0] += ss.BleDeviceSmartSolar._PROVISION_BACKOFF_SECS + 1
    assert len(w.calls) == limit
    gave_up = [r for r in caplog.records if r.levelno == logging.WARNING and "giving up" in r.message]
    assert len(gave_up) == 1 and "AdvertisementKey" in gave_up[0].message


def test_backoff_keeps_retries_apart(ss, monkeypatch) -> None:
    dev, w, stored = _device(ss, monkeypatch)
    t = [1000.0]; monkeypatch.setattr(ss.time, "monotonic", lambda: t[0])
    dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX)); w.on_done(None)
    t[0] += 10; dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert len(w.calls) == 1, "inside the backoff: no new session"
    t[0] += ss.BleDeviceSmartSolar._PROVISION_BACKOFF_SECS
    dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert len(w.calls) == 2


def test_a_disabled_device_never_opens_a_link(ss, monkeypatch) -> None:
    dev, w, stored = _device(ss, monkeypatch, enabled=False)
    for _ in range(3):
        dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert w.calls == [], "no GATT to a device nobody enabled"


def test_key_mismatch_forgets_the_key_and_reprovisions(ss, monkeypatch) -> None:
    dev, w, stored = _device(ss, monkeypatch)
    dev._adv_key_hex = "0f" * 16
    class _Parser:
        def __init__(self, _k): pass
        def parse(self, _b): raise sys.modules["victron_ble.exceptions"].AdvertisementKeyMismatchError("x")
    monkeypatch.setattr(ss, "detect_device_type", lambda _b: _Parser)
    dev.handle_manufacturer_data(bytes.fromhex(SMARTSOLAR_PORTABLE_HEX))
    assert dev._adv_key_hex is None and dev._stored_key_invalid
    assert len(w.calls) == 1


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
