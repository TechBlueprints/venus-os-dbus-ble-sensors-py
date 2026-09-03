"""
Victron SmartSolar MPPT charger over BLE advertisements.

The unit broadcasts encrypted Instant Readout frames (manufacturer 0x02E1,
mode byte 0x01 = solar charger).  With its 32-hex Instant Readout key in
settings, every frame decodes to battery V/A, PV power, today's yield,
load current, charge state and error code — the same AES-CTR scheme and
the same vendored ``victron_ble`` library the IP22 driver uses, with the
``SolarCharger`` parser instead of ``AcCharger``.

Key recovery works the IP22 way.  A device that is enabled but has no
key gets ONE paired HEX session (VREG 0xEC65 through the shared
single-slot writer, same passkey resolution as the Orion/IP22 drivers);
the key, the firmware string and the adapter that succeeded are
persisted under ``/Settings/Devices/smartsolar_<mac>/`` and the link is
dropped.  That is the only GATT this driver ever does: telemetry stays
on advertisements, there are no writes, and a device nobody enabled is
never connected to.

Deliberate limits of this first version, both for caution:

* **Bounded provisioning.**  The IP22 retries a failed key read every
  backoff window for the life of the process.  Under the fleet notify
  policy the HEX path is not yet proven, so a session that keeps timing
  out would churn a GATT link on prod indefinitely; this driver stops
  after ``_PROVISION_MAX_ATTEMPTS`` per process, says so once, and waits
  for a hand-set ``AdvertisementKey`` or a restart.
* **Product 0xA053 only** (SmartSolar Charger MPPT 75/15).  The box also
  has two SmartSolar 100/50s (0xA057) that are already wired over
  VE.Direct.  The discovery gate would keep them out while discovery is
  off, but "cannot match a 100/50 regardless of any setting" is the
  property asked for, so the detector is narrowed and can be widened
  later by adding to ACCEPTED_PRODUCT_IDS.
"""
from __future__ import annotations

import logging
import os
import struct
import time
from typing import Any, Dict, Optional

import dbus

import hex_key_session
from ble_charger_common import (
    ChargerCommonMixin,
    format_firmware_version,
    serial_from_advertised_name,
)
from ble_device import BleDevice
from dbus_ble_service import DbusBleService
from dbus_settings_service import DbusSettingsService
from orion_tr_gatt import AsyncGATTWriter
from orion_tr_pin import resolve_pairing_passkey
from smartsolar_key_settings import (
    advertisement_key_setting_path,
    get_advertisement_key,
    get_preferred_adapter,
    set_advertisement_key,
    set_firmware_version,
    set_preferred_adapter,
)
from ve_types import VE_UN8
from victron_ble.devices import detect_device_type  # type: ignore
from victron_ble.exceptions import AdvertisementKeyMismatchError  # type: ignore

logger = logging.getLogger(__name__)

VICTRON_MANUFACTURER_ID = 0x02E1
SOLAR_CHARGER_MODE = 0x01

# Names from victron_ble's own model table, kept here for the product
# name without importing its internals.
SMARTSOLAR_PRODUCT_NAMES = {
    0xA042: "BlueSolar Charger MPPT 75/15",
    0xA053: "SmartSolar Charger MPPT 75/15",
    0xA054: "SmartSolar Charger MPPT 75/10",
    0xA055: "SmartSolar Charger MPPT 100/15",
    0xA056: "SmartSolar Charger MPPT 100/30",
    0xA057: "SmartSolar Charger MPPT 100/50",
}
# What this driver will actually adopt.  See the module docstring.
ACCEPTED_PRODUCT_IDS = frozenset({0xA053})

# One writer for the family, one provisioning session in flight at a
# time — the same shape as the IP22 module.
_gatt_writer: Optional[AsyncGATTWriter] = None
_provision_busy = False


def _shared_bus() -> dbus.Bus:
    return (dbus.SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus())


def _gatt() -> AsyncGATTWriter:
    global _gatt_writer
    if _gatt_writer is None:
        _gatt_writer = AsyncGATTWriter(_shared_bus())
    return _gatt_writer


def _format_mac_colons(dev_mac: str) -> str:
    s = dev_mac.replace(":", "").upper()
    return ":".join(s[i:i + 2] for i in range(0, 12, 2))


def is_smartsolar_manufacturer_data(manufacturer_data: bytes) -> bool:
    """Structural gate: product id in the accepted set, and solar mode.

    Like the IP22, an MPPT that has nothing to report drops the encrypted
    payload and advertises a short "product-id only" frame, so anything
    from length 4 up is ours if the product id matches; the mode byte is
    only checked once the frame is long enough to carry one.

    Requiring the full frame here is not merely a missed reading.  A
    frame this returns False for falls through the dispatcher to
    BleDeviceVictronEnergy, whose own check then fails, and THAT puts the
    MAC in ``_ignored_mac`` for the life of the process — so one short
    beacon arriving first silences the device until the next restart.
    Prod did exactly that on 2026-09-03: heard at 20:03:01, ignored, and
    never adopted again.
    """
    if len(manufacturer_data) < 4:
        return False
    pid = struct.unpack("<H", manufacturer_data[2:4])[0]
    if pid not in ACCEPTED_PRODUCT_IDS:
        return False
    if len(manufacturer_data) >= 5 and manufacturer_data[4] != SOLAR_CHARGER_MODE:
        return False
    return True


class BleDeviceSmartSolar(ChargerCommonMixin, BleDevice):
    """SmartSolar MPPT driven by encrypted Victron advertisements."""

    SETTINGS_NS_PREFIX = "smartsolar"
    PERSISTED_SETTING_SUFFIXES_TO_PATHS: Dict[str, str] = {}
    _PROVISION_BACKOFF_SECS = 180.0
    _PROVISION_MAX_ATTEMPTS = 5

    @staticmethod
    def matches_manufacturer_data(manufacturer_data: bytes) -> bool:
        return is_smartsolar_manufacturer_data(manufacturer_data)

    def __init__(self, dev_mac: str):
        self._adv_key_hex: Optional[str] = None
        self._dbus_settings = DbusSettingsService()
        self._pairing_passkey: int = resolve_pairing_passkey(
            self._dbus_settings)
        self._last_provision_attempt: float = 0.0
        self._provision_attempts: int = 0
        self._stored_key_invalid = False
        self._gave_up_logged = False
        self._last_full_telemetry_at: float = 0.0
        self._init_charger_common()
        super().__init__(dev_mac)

    @staticmethod
    def _gatt_writer() -> AsyncGATTWriter:
        return _gatt()

    def configure(self, manufacturer_data: bytes):
        pid = struct.unpack("<H", manufacturer_data[2:4])[0]
        self._adv_key_hex = get_advertisement_key(self._dbus_settings,
                                                  self.info["dev_mac"])
        product_name = SMARTSOLAR_PRODUCT_NAMES.get(pid, f"SmartSolar 0x{pid:04X}")
        self.MANUFACTURER_ID = VICTRON_MANUFACTURER_ID
        self.info.update({
            "manufacturer_id": VICTRON_MANUFACTURER_ID,
            "product_id": pid,
            "product_name": product_name,
            "device_name": "SmartSolar",
            "dev_prefix": "smartsolar",
            "roles": {"solarcharger": {}},
            "regs": [{"name": "_smartsolar_placeholder", "type": VE_UN8,
                      "offset": 0, "roles": [None]}],
            "settings": [],
            "alarms": [],
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return is_smartsolar_manufacturer_data(manufacturer_data)

    # ------------------------------------------------------------------
    # Advertisements
    # ------------------------------------------------------------------

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        if not DbusBleService.get().is_device_enabled(self.info):
            return
        key = self._adv_key_hex or (
            None if self._stored_key_invalid else get_advertisement_key(
                self._dbus_settings, self.info["dev_mac"]))
        if not key:
            self._maybe_provision_key()
            return
        self._adv_key_hex = key
        if len(manufacturer_data) < 10:
            return   # short beacon, no payload
        try:
            parsed = self._decode_advertisement(key, manufacturer_data)
        except AdvertisementKeyMismatchError:
            logger.warning("%s: advertisement decrypt failed (key mismatch) — "
                           "re-reading VREG 0xEC65", self._plog)
            self._stored_key_invalid = True
            self._adv_key_hex = None
            self._maybe_provision_key()
            return
        except ValueError as exc:
            logger.debug("%s: advertisement undecodable: %s", self._plog, exc)
            return
        except Exception:
            logger.exception("%s: SmartSolar advertisement decode error",
                             self._plog)
            return
        if parsed is None:
            return
        self._last_full_telemetry_at = time.monotonic()
        self._publish(parsed)

    # ------------------------------------------------------------------
    # Key provisioning (mirrors ble_device_ip22_charger, bounded)
    # ------------------------------------------------------------------

    def _maybe_provision_key(self) -> None:
        global _provision_busy
        if _provision_busy:
            return
        if self._provision_attempts >= self._PROVISION_MAX_ATTEMPTS:
            if not self._gave_up_logged:
                self._gave_up_logged = True
                logger.warning(
                    "%s: %d key-provisioning sessions failed — giving up "
                    "until restart; set %s by hand from VictronConnect "
                    "(Product info → Instant readout details) if this "
                    "persists", self._plog, self._provision_attempts,
                    advertisement_key_setting_path(self.info["dev_mac"]))
            return
        now = time.monotonic()
        if (self._last_provision_attempt > 0
                and now - self._last_provision_attempt
                < self._PROVISION_BACKOFF_SECS):
            return
        self._last_provision_attempt = now
        self._provision_attempts += 1
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: no advertisement key cached — provisioning in-process "
            "(VREG 0xEC65, attempt %d/%d)", self._plog,
            self._provision_attempts, self._PROVISION_MAX_ATTEMPTS)
        _provision_busy = True
        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])

        def done(payload):
            global _provision_busy
            _provision_busy = False
            payload = hex_key_session.valid_key_payload(payload)
            if not payload:
                logger.warning(
                    "%s: key provisioning did not produce a 16-byte key "
                    "(attempt %d/%d); will retry after backoff", self._plog,
                    self._provision_attempts, self._PROVISION_MAX_ATTEMPTS)
                return
            self._provision_attempts = 0
            self._persist_provisioning_result(payload)

        self._gatt_writer().provision_key(
            mac_colon, self._pairing_passkey, on_done=done,
            prefer_adapter=pref_adapter)

    def _persist_provisioning_result(self, payload: Dict[str, Any]) -> None:
        key_hex = str(payload.get("key") or "").strip().lower()
        if key_hex:
            try:
                set_advertisement_key(self._dbus_settings,
                                      self.info["dev_mac"], key_hex)
                self._adv_key_hex = key_hex
                self._stored_key_invalid = False
                logger.info("%s: advertisement key stored at %s", self._plog,
                            advertisement_key_setting_path(self.info["dev_mac"]))
            except Exception:
                logger.exception("%s: failed to persist advertisement key",
                                 self._plog)
        firmware_raw = payload.get("firmware")
        if firmware_raw:
            try:
                set_firmware_version(self._dbus_settings,
                                     self.info["dev_mac"], firmware_raw)
                pretty = format_firmware_version(firmware_raw) or firmware_raw
                self.info["firmware_version"] = pretty
                for role_service in self._role_services.values():
                    try:
                        self._publish_value(role_service, "/FirmwareVersion",
                                            pretty)
                    except Exception:
                        pass
            except Exception:
                logger.exception("%s: failed to persist firmware version",
                                 self._plog)
        hw_version = payload.get("hardware_version")
        if hw_version:
            self.info["hardware_version"] = hw_version
            for role_service in self._role_services.values():
                try:
                    self._publish_value(role_service, "/HardwareVersion",
                                        hw_version)
                except Exception:
                    pass
        adapter = payload.get("adapter")
        if adapter:
            try:
                set_preferred_adapter(self._dbus_settings,
                                      self.info["dev_mac"], adapter)
            except Exception:
                logger.exception("%s: failed to store preferred adapter",
                                 self._plog)

    @staticmethod
    def _decode_advertisement(key_hex: str, manufacturer_data: bytes) -> Optional[dict]:
        device_cls = detect_device_type(manufacturer_data)
        if device_cls is None:
            return None
        parsed = device_cls(key_hex).parse(manufacturer_data)
        charge_state = parsed.get_charge_state()
        charger_error = parsed.get_charger_error()
        yield_wh = parsed.get_yield_today()
        return {
            "device_state": int(charge_state.value) if charge_state is not None else 0,
            "charger_error": int(charger_error.value) if charger_error is not None else 0,
            "battery_voltage": parsed.get_battery_voltage(),
            "battery_current": parsed.get_battery_charging_current(),
            "yield_today_kwh": (round(yield_wh / 1000.0, 3) if yield_wh is not None else None),
            "solar_power": parsed.get_solar_power(),
            "load_current": parsed.get_external_device_load(),
            "model_name": parsed.get_model_name(),
        }

    def _publish(self, parsed: Dict[str, Any]) -> None:
        for role_service in list(self._role_services.values()):
            if not DbusBleService.get().is_device_role_enabled(
                    self.info, role_service.ble_role.NAME):
                continue
            with role_service:
                v = parsed.get("battery_voltage")
                i = parsed.get("battery_current")
                p = parsed.get("solar_power")
                load_i = parsed.get("load_current")
                self._publish_value(role_service, "/Dc/0/Voltage", v,
                                    sensor_type="charger_voltage")
                self._publish_value(role_service, "/Dc/0/Current", i,
                                    sensor_type="charger_current")
                self._publish_value(role_service, "/Pv/Power", p, sensor_type="power")
                self._publish_value(role_service, "/Yield/Power", p, sensor_type="power")
                self._publish_value(role_service, "/History/Daily/0/Yield",
                                    parsed.get("yield_today_kwh"))
                self._publish_value(role_service, "/Load/I", load_i,
                                    sensor_type="current")
                self._publish_value(role_service, "/Load/State",
                                    (1 if load_i else 0) if load_i is not None else None)
                self._publish_value(role_service, "/State", int(parsed["device_state"]))
                self._publish_value(role_service, "/ErrorCode", int(parsed["charger_error"]))
                if "serial" not in self.info:
                    self.info["serial"] = serial_from_advertised_name(
                        self.info.get("adv_name")) or ""
                if self.info.get("serial"):
                    self._publish_value(role_service, "/Serial", self.info["serial"])
        self._tick_history(int(parsed["device_state"]), parsed.get("battery_current"))
