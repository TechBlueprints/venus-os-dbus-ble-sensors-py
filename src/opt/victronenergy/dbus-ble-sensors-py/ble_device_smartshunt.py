"""
Victron SmartShunt / BMV-Smart (BLE manufacturer ``0x02E1``, Instant
Readout record type ``0x02`` BatteryMonitor).

Live battery telemetry arrives as encrypted Victron advertisements —
the same AES-CTR Instant Readout the IP22 uses, parsed by vendored
``victron_ble.devices.BatteryMonitor``.  The 16-byte advertisement key
is device-specific and is read once via a paired GATT session; this
driver reuses :mod:`orion_tr_key_cli` for that provisioning.

Publishes ``com.victronenergy.battery.smartshunt_<mac>`` so gui-v2 and
``dbus-systemcalc-py`` treat the unit like a VE.Direct BMV.

This class is **not** registered in ``BleDevice.DEVICE_CLASSES``:
manufacturer ``0x02E1`` is already claimed by SolarSense.  Dispatch
lives in :mod:`dbus_ble_sensors` next to the Orion-TR / IP22 matchers.
"""
from __future__ import annotations

import logging
import struct
import time
from typing import Any, Dict, Optional


import hex_key_session
from ble_charger_common import (
    ChargerCommonMixin,
    bluez_device_name as _bluez_device_name,
    format_mac_colons as _format_mac_colons,
    serial_from_advertised_name as _serial_from_advertised_name,
)
from ble_device import BleDevice
from dbus_ble_service import DbusBleService
from dbus_settings_service import DbusSettingsService
from orion_tr_pin import resolve_pairing_passkey
from smartshunt_key_settings import (
    advertisement_key_setting_path,
    get_advertisement_key,
    get_firmware_version,
    get_preferred_adapter,
    set_advertisement_key,
    set_firmware_version,
    set_preferred_adapter,
)
import smartshunt_hex
from ve_types import VE_UN8
from victron_ble.devices import detect_device_type  # type: ignore
from victron_ble.exceptions import (  # type: ignore
    AdvertisementKeyMismatchError,
)

logger = logging.getLogger(__name__)

VICTRON_MANUFACTURER_ID = 0x02E1

# Instant Readout record types this family emits.
# 0x02 = BatteryMonitor (SmartShunt / BMV SOC path)
# 0x0D = DcEnergyMeter (same SKU in DC-meter mode)
SMARTSHUNT_RECORD_TYPES = frozenset({0x02, 0x0D})

# Product IDs from the vendored victron_ble model table.
SMARTSHUNT_PRODUCT_IDS = frozenset({
    0xA380, 0xA381, 0xA382, 0xA383,  # BMV-710 / 712 Smart
    0xA389, 0xA38A, 0xA38B,          # SmartShunt 500 / 1000 / 2000
    0xA38C, 0xA38D, 0xA38E,          # SmartShunt IP67
    0xC030, 0xC031, 0xC032,          # SmartShunt IP65
    0xC034,                          # BMV-800 Smart
    0xC035, 0xC036, 0xC037,          # SmartShunt IP65 (alt)
    0xC038,                          # SmartShunt 300A
})

_SMARTSHUNT_PRODUCT_NAMES = {
    0xA380: "BMV-710 Smart",
    0xA381: "BMV-712 Smart",
    0xA382: "BMV-710H Smart",
    0xA383: "BMV-712 Smart",
    0xA389: "SmartShunt 500A/50mV",
    0xA38A: "SmartShunt 1000A/50mV",
    0xA38B: "SmartShunt 2000A/50mV",
    0xA38C: "SmartShunt IP67 500A/50mV",
    0xA38D: "SmartShunt IP67 1000A/50mV",
    0xA38E: "SmartShunt IP67 2000A/50mV",
    0xC030: "SmartShunt IP65 500A/50mV",
    0xC031: "SmartShunt IP65 1000A/50mV",
    0xC032: "SmartShunt IP65 2000A/50mV",
    0xC034: "BMV-800 Smart",
    0xC035: "SmartShunt IP65 500A/50mV",
    0xC036: "SmartShunt IP65 1000A/50mV",
    0xC037: "SmartShunt IP65 2000A/50mV",
    0xC038: "SmartShunt 300A/50mV",
}

# Venus BMV alarm paths for Instant Readout AlarmReason bits.
_ALARM_PATHS = (
    (1, "/Alarms/LowVoltage"),
    (2, "/Alarms/HighVoltage"),
    (4, "/Alarms/LowSoc"),
    (8, "/Alarms/LowStarterVoltage"),
    (16, "/Alarms/HighStarterVoltage"),
    (32, "/Alarms/LowTemperature"),
    (64, "/Alarms/HighTemperature"),
    (128, "/Alarms/MidVoltage"),
    (256, "/Alarms/Overload"),
    (512, "/Alarms/Ripple"),
)

_provision_busy = False


def is_smartshunt_manufacturer_data(manufacturer_data: bytes) -> bool:
    """True when *manufacturer_data* is a SmartShunt / BMV Instant Readout.

    Accepts the 4-byte product-id-only beacon (no record type) and any
    longer frame whose product id is in the family and whose record type
    is BatteryMonitor (``0x02``) or DcEnergyMeter (``0x0D``).
    """
    if len(manufacturer_data) < 4:
        return False
    pid = struct.unpack("<H", manufacturer_data[2:4])[0]
    if pid not in SMARTSHUNT_PRODUCT_IDS:
        return False
    if len(manufacturer_data) >= 5 and manufacturer_data[4] not in SMARTSHUNT_RECORD_TYPES:
        return False
    return True


def _format_firmware_version(raw_hex: Optional[str]) -> Optional[str]:
    if not raw_hex:
        return None
    try:
        blob = bytes.fromhex(raw_hex)
    except ValueError:
        return None

    def _bcd_byte(b: int) -> int:
        return ((b >> 4) & 0xF) * 10 + (b & 0xF)

    def _format_low16(value16: int) -> Optional[str]:
        if value16 in (0, 0xFFFF):
            return None
        major = _bcd_byte((value16 >> 8) & 0xFF)
        minor = _bcd_byte(value16 & 0xFF)
        return f"{major}.{minor:02d}"

    if len(blob) == 2:
        return _format_low16(int.from_bytes(blob, "little")) or raw_hex
    if len(blob) == 4:
        v = int.from_bytes(blob, "little")
        if v in (0, 0xFFFFFFFF):
            return raw_hex
        base = _format_low16(v & 0xFFFF)
        if base is None:
            return raw_hex
        kind = (v >> 24) & 0xF0
        suffix = {0x40: "", 0x50: "~beta", 0xF0: "~dev"}.get(kind, "")
        return base + suffix
    return raw_hex


def _alarm_value(parsed) -> int:
    alarm = parsed.get("alarm")
    if alarm is None:
        return 0
    if hasattr(alarm, "value"):
        try:
            return int(alarm.value)
        except (TypeError, ValueError):
            return 0
    try:
        return int(alarm)
    except (TypeError, ValueError):
        return 0


class BleDeviceSmartShunt(BleDevice):
    """SmartShunt / BMV-Smart driven by encrypted Victron advertisements."""

    SETTINGS_NS_PREFIX = "smartshunt"

    # Honour a short product-id-only beacon as "no telemetry" only after
    # this many seconds without a successful decode — same grace the
    # IP22 uses so interleaved heartbeats don't clear live values.
    _OFF_FRAME_GRACE_S = 30.0

    @staticmethod
    def matches_manufacturer_data(manufacturer_data: bytes) -> bool:
        return is_smartshunt_manufacturer_data(manufacturer_data)

    def __init__(self, dev_mac: str):
        self._adv_key_hex: Optional[str] = None
        self._dbus_settings = DbusSettingsService()
        self._pairing_passkey: int = resolve_pairing_passkey(
            self._dbus_settings)
        self._last_provision_attempt: float = 0.0
        self._stored_key_invalid = False
        self._last_full_telemetry_at: float = 0.0
        self._hex_state: Dict[str, Any] = {}
        self._hex_started = False
        super().__init__(dev_mac)

    def configure(self, manufacturer_data: bytes):
        pid = struct.unpack("<H", manufacturer_data[2:4])[0]
        self._adv_key_hex = get_advertisement_key(self._dbus_settings,
                                                  self.info["dev_mac"])
        # Shadow MANUFACTURER_ID the same way IP22 / Orion do — keep
        # 0x02E1 routable to BleDeviceVictronEnergy for SolarSense.
        self.MANUFACTURER_ID = VICTRON_MANUFACTURER_ID
        adv_name = _bluez_device_name(self.info["dev_mac"])
        product_name = (adv_name
                        or _SMARTSHUNT_PRODUCT_NAMES.get(pid)
                        or "SmartShunt")
        firmware_raw = get_firmware_version(self._dbus_settings,
                                            self.info["dev_mac"])
        firmware_version = _format_firmware_version(firmware_raw) or "1.0.0"
        self.info.update(
            {
                "manufacturer_id": VICTRON_MANUFACTURER_ID,
                "product_id": pid,
                "product_name": product_name,
                "device_name": adv_name or product_name,
                "dev_prefix": "smartshunt",
                "firmware_version": firmware_version,
                "roles": {"battery": {}},
                "regs": [
                    {
                        "name": "_smartshunt_placeholder",
                        "type": VE_UN8,
                        "offset": 0,
                        "roles": [None],
                    }
                ],
                "settings": [],
                "alarms": [],
            }
        )

    def init(self):
        super().init()
        adv_name = _bluez_device_name(self.info["dev_mac"])
        serial = _serial_from_advertised_name(adv_name)
        if serial:
            self.info["serial"] = serial
        if adv_name:
            for role_service in self._role_services.values():
                current = role_service["/CustomName"]
                if not current:
                    self._publish_value(role_service, "/CustomName", adv_name)
                if serial:
                    self._publish_value(role_service, "/Serial", serial)
        self._start_hex()

    def _start_hex(self) -> None:
        if self._hex_started:
            return
        # Never open a GATT session to a device nobody enabled.  This
        # runs from init(), i.e. the moment an advertisement is first
        # decoded — before anything asks whether we want this device.
        # Without the check we connected to every SmartShunt in range,
        # including neighbours' and wired units that need no BLE at all;
        # on the prod gateway one unreachable shunt drove 139 discovery
        # bursts, and those sessions are what crashed bluetoothd.
        if not DbusBleService.get().is_device_enabled(self.info):
            return
        if self._adv_key_hex or get_advertisement_key(
                self._dbus_settings, self.info["dev_mac"]):
            return
        mac = _format_mac_colons(self.info["dev_mac"])
        if smartshunt_hex.start(mac, self._pairing_passkey,
                                self._on_hex_update):
            self._hex_started = True
        else:
            logger.warning("%s: HEX session failed to start", self._plog)

    def _on_hex_update(self, fields: dict) -> None:
        key_hex = fields.get("advertisement_key")
        if key_hex:
            try:
                set_advertisement_key(self._dbus_settings,
                                      self.info["dev_mac"], key_hex)
                self._adv_key_hex = key_hex
                self._stored_key_invalid = False
                logger.info("%s: advertisement key stored from HEX",
                            self._plog)
            except Exception:
                logger.exception("%s: failed to persist HEX advertisement key",
                                 self._plog)
        serial = fields.get("serial")
        if serial:
            self.info["serial"] = serial
        model = fields.get("model_name")
        if model:
            self.info["product_name"] = model
        self._hex_state.update(fields)
        parsed = {
            "voltage": self._hex_state.get("voltage"),
            "current": self._hex_state.get("current"),
            "power": self._hex_state.get("power"),
            "soc": self._hex_state.get("soc"),
            "consumed_ah": self._hex_state.get("consumed_ah"),
            "ttg_s": self._hex_state.get("ttg_s"),
            "temperature": self._hex_state.get("temperature"),
            "aux_voltage": self._hex_state.get("aux_voltage"),
            "alarm": self._hex_state.get("alarm", 0),
            "model_name": self._hex_state.get("model_name"),
        }
        v = parsed.get("voltage")
        i = parsed.get("current")
        if v is not None and i is not None:
            parsed["power"] = round(v * i, 2)
        if v is not None or parsed.get("soc") is not None:
            self._last_full_telemetry_at = time.monotonic()
            self._publish(parsed)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return self.matches_manufacturer_data(manufacturer_data)

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        if not DbusBleService.get().is_device_enabled(self.info):
            return

        if self._stored_key_invalid:
            self._maybe_provision_key()
            return

        key = self._adv_key_hex or get_advertisement_key(
            self._dbus_settings, self.info["dev_mac"])
        if key:
            self._adv_key_hex = key

        if not key:
            return

        if len(manufacturer_data) < 10:
            now = time.monotonic()
            last_full = getattr(self, "_last_full_telemetry_at", 0.0)
            if now - last_full >= self._OFF_FRAME_GRACE_S:
                self._publish_empty_state()
            return

        try:
            parsed = self._decode_advertisement(key, manufacturer_data)
        except AdvertisementKeyMismatchError:
            logger.warning(
                "%s: advertisement decrypt failed (key mismatch) — "
                "re-reading VREG 0xEC65",
                self._plog,
            )
            self._stored_key_invalid = True
            self._adv_key_hex = None
            self._maybe_provision_key()
            return
        except Exception:
            logger.exception("%s: SmartShunt advertisement decode error",
                             self._plog)
            return

        if parsed is None:
            return

        self._last_full_telemetry_at = time.monotonic()
        self._publish(parsed)

    @staticmethod
    def _decode_advertisement(key_hex: str, manufacturer_data: bytes):
        device_cls = detect_device_type(manufacturer_data)
        if device_cls is None:
            return None
        parser = device_cls(key_hex)
        parsed = parser.parse(manufacturer_data)

        model_name = None
        getter = getattr(parsed, "get_model_name", None)
        if callable(getter):
            model_name = getter()
            if model_name and str(model_name).startswith("<Unknown"):
                pid = struct.unpack("<H", manufacturer_data[2:4])[0]
                model_name = _SMARTSHUNT_PRODUCT_NAMES.get(pid, model_name)

        remaining = parsed.get_remaining_mins() if hasattr(
            parsed, "get_remaining_mins") else None
        voltage = parsed.get_voltage() if hasattr(parsed, "get_voltage") else None
        current = parsed.get_current() if hasattr(parsed, "get_current") else None
        soc = parsed.get_soc() if hasattr(parsed, "get_soc") else None
        consumed = (parsed.get_consumed_ah()
                    if hasattr(parsed, "get_consumed_ah") else None)
        temp = (parsed.get_temperature()
                if hasattr(parsed, "get_temperature") else None)
        starter = (parsed.get_starter_voltage()
                   if hasattr(parsed, "get_starter_voltage") else None)
        midpoint = (parsed.get_midpoint_voltage()
                    if hasattr(parsed, "get_midpoint_voltage") else None)
        alarm = parsed.get_alarm() if hasattr(parsed, "get_alarm") else 0

        aux_v = starter if starter is not None else midpoint
        ttg_s = None
        if remaining is not None:
            ttg_s = int(remaining) * 60

        power = None
        if voltage is not None and current is not None:
            power = round(voltage * current, 2)

        return {
            "voltage": voltage,
            "current": current,
            "power": power,
            "soc": soc,
            "consumed_ah": consumed,
            "ttg_s": ttg_s,
            "temperature": temp,
            "aux_voltage": aux_v,
            "alarm": alarm,
            "model_name": model_name,
        }

    # ------------------------------------------------------------------
    # Key provisioning
    # ------------------------------------------------------------------

    _PROVISION_BACKOFF_SECS = 180.0

    def _maybe_provision_key(self) -> None:
        global _provision_busy
        if _provision_busy:
            return
        now = time.monotonic()
        since_last = now - self._last_provision_attempt
        if (self._last_provision_attempt > 0
                and since_last < self._PROVISION_BACKOFF_SECS):
            return

        self._last_provision_attempt = now
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: no advertisement key cached — provisioning in-process "
            "(VREG 0xEC65)",
            self._plog,
        )

        _provision_busy = True
        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])

        def done(payload):
            global _provision_busy
            _provision_busy = False
            payload = hex_key_session.valid_key_payload(payload)
            if not payload:
                logger.warning(
                    "%s: key provisioning did not produce a 16-byte "
                    "key; will retry after backoff", self._plog)
                return
            self._persist_provisioning_result(payload)

        ChargerCommonMixin._gatt_writer().provision_key(
            mac_colon, self._pairing_passkey,
            on_done=done, prefer_adapter=pref_adapter)

    def _persist_provisioning_result(self, payload: Dict[str, Any]) -> None:
        key_hex = payload.get("key")
        if key_hex:
            try:
                set_advertisement_key(self._dbus_settings,
                                      self.info["dev_mac"], key_hex)
                self._adv_key_hex = key_hex
                self._stored_key_invalid = False
                logger.info(
                    "%s: advertisement key stored at %s",
                    self._plog,
                    advertisement_key_setting_path(self.info["dev_mac"]))
            except Exception:
                logger.exception(
                    "%s: failed to persist advertisement key", self._plog)

        firmware_raw = payload.get("firmware")
        if firmware_raw:
            try:
                set_firmware_version(self._dbus_settings,
                                     self.info["dev_mac"], firmware_raw)
                pretty = _format_firmware_version(firmware_raw) or firmware_raw
                self.info["firmware_version"] = pretty
                for role_service in self._role_services.values():
                    try:
                        self._publish_value(role_service,
                                            "/FirmwareVersion", pretty)
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "%s: failed to persist firmware version", self._plog)

        adapter = payload.get("adapter")
        if adapter:
            try:
                set_preferred_adapter(self._dbus_settings,
                                      self.info["dev_mac"], adapter)
            except Exception:
                logger.exception(
                    "%s: failed to store preferred adapter", self._plog)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _publish_empty_state(self) -> None:
        """Clear measurement paths when only the short beacon is seen."""
        if "serial" not in self.info:
            self.info["serial"] = _serial_from_advertised_name(
                _bluez_device_name(self.info["dev_mac"])) or ""

        for role_service in list(self._role_services.values()):
            with role_service:
                self._publish_value(role_service, "/Dc/0/Voltage", None,
                                    sensor_type="voltage")
                self._publish_value(role_service, "/Dc/0/Current", None,
                                    sensor_type="current")
                self._publish_value(role_service, "/Dc/0/Power", None,
                                    sensor_type="power")
                self._publish_value(role_service, "/Dc/0/Temperature", None,
                                    sensor_type="temperature")
                self._publish_value(role_service, "/Dc/1/Voltage", None,
                                    sensor_type="voltage")
                self._publish_value(role_service, "/Soc", None)
                self._publish_value(role_service, "/ConsumedAmphours", None)
                self._publish_value(role_service, "/TimeToGo", None)
                if self.info.get("serial"):
                    self._publish_value(role_service, "/Serial",
                                        self.info["serial"])
                self._publish_alarms(role_service, 0)
            role_service.connect()

    def _publish(self, parsed) -> None:
        if "serial" not in self.info:
            self.info["serial"] = _serial_from_advertised_name(
                _bluez_device_name(self.info["dev_mac"])) or ""

        model = parsed.get("model_name")
        if model:
            self.info["product_name"] = model

        for role_service in list(self._role_services.values()):
            ble_svc = DbusBleService.get()
            if not ble_svc.is_device_role_enabled(
                    self.info, role_service.ble_role.NAME):
                continue

            with role_service:
                v = parsed.get("voltage")
                i = parsed.get("current")
                self._publish_value(role_service, "/Dc/0/Voltage", v,
                                    sensor_type="voltage")
                self._publish_value(role_service, "/Dc/0/Current", i,
                                    sensor_type="current")
                self._publish_value(role_service, "/Dc/0/Power",
                                    parsed.get("power"),
                                    sensor_type="power")
                self._publish_value(role_service, "/Dc/0/Temperature",
                                    parsed.get("temperature"),
                                    sensor_type="temperature")
                self._publish_value(role_service, "/Dc/1/Voltage",
                                    parsed.get("aux_voltage"),
                                    sensor_type="voltage")
                self._publish_value(role_service, "/Soc", parsed.get("soc"))
                self._publish_value(role_service, "/ConsumedAmphours",
                                    parsed.get("consumed_ah"))
                self._publish_value(role_service, "/TimeToGo",
                                    parsed.get("ttg_s"))
                if self.info.get("serial"):
                    self._publish_value(role_service, "/Serial",
                                        self.info["serial"])
                if model:
                    self._publish_value(role_service, "/ProductName", model)
                self._publish_alarms(role_service, _alarm_value(parsed))
            role_service.connect()

    def _publish_alarms(self, role_service, alarm_bits: int) -> None:
        bits = int(alarm_bits or 0)
        for mask, path in _ALARM_PATHS:
            self._publish_value(role_service, path, 2 if bits & mask else 0)
