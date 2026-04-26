"""
Victron Blue Smart IP22 charger (BLE manufacturer ``0x02E1``, product
IDs ``0xA330``–``0xA33F``).

The IP22 publishes live charger telemetry as encrypted Victron
advertisements, and accepts a ``DEVICE_MODE`` write (VREG ``0x0200``)
over GATT for on/off control — the same protocol already used by the
Orion-TR driver in this service.  The 16-byte advertisement key is
device-specific and must be read once via a paired GATT session; this
driver reuses :mod:`orion_tr_key_cli` to perform that provisioning.

This file mirrors the structure of :mod:`ble_device_orion_tr` but
publishes under a single ``charger`` role so the device appears on
gui-v2's *DC Sources* page alongside the VE.Direct Phoenix Smart IP43
charger reference design.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import struct
import subprocess
import threading

from gi.repository import GLib
import time
from typing import Any, Callable, Dict, Optional

import dbus

from ble_device import BleDevice
from dbus_ble_service import DbusBleService
from dbus_role_service import DbusRoleService
from dbus_settings_service import DbusSettingsService
from victron_ble.devices import detect_device_type  # type: ignore
from victron_ble.exceptions import (  # type: ignore
    AdvertisementKeyMismatchError,
)

from orion_tr_gatt import AsyncGATTWriter
from orion_tr_pin import resolve_pairing_passkey
from ip22_key_settings import (
    advertisement_key_setting_path,
    get_advertisement_key,
    get_firmware_version,
    get_preferred_adapter,
    set_advertisement_key,
    set_firmware_version,
    set_preferred_adapter,
)
from scan_control import pause_scanning, resume_scanning
from ve_types import VE_UN8

logger = logging.getLogger(__name__)

VICTRON_MANUFACTURER_ID = 0x02E1
IP22_PRODUCT_ID_MIN = 0xA330
IP22_PRODUCT_ID_MAX = 0xA33F

# IP22 (BSC firmware 3.65) does NOT implement VREG 0x0200 (Device mode) or
# 0x0202 (Remote control mask) — both return the "unknown register" error
# (ack code 1) on this firmware.  The only verified-writable real-control
# register is 0xEDF0 (Battery max current, u16 LE in 0.1A units).  Both
# the open-source pvtex/Victron_BlueSmart_IP22 and wasn-eu/Victron_BlueSmart_IP22
# drivers control the charger by manipulating only this register; the
# device clamps writes below ~7.5A to the minimum hardware limit.
#
# /Mode on/off via the Orion-TR-style 0x0200 write is NOT viable here.
VREG_BATTERY_MAX_CURRENT = 0xEDF0   # u16 LE, 0.1 A
VREG_BATTERY_TYPE        = 0xEDF1   # u8;  0xFF = USER (unlocks voltage writes)
VREG_FLOAT_VOLTAGE       = 0xEDF6   # u16 LE, 0.01 V
VREG_ABSORPTION_VOLTAGE  = 0xEDF7   # u16 LE, 0.01 V
VREG_DEVICE_MODE = 0x0200    # legacy alias retained for back-compat in code paths

# DVCC dedupe thresholds.  DVCC pushes /Link/ChargeVoltage and
# /Link/ChargeCurrent on every cycle (~once per minute even when idle);
# a GATT write costs ~3-5 s of pause-scan + connect + write + disconnect,
# so we only round-trip a write when the request meaningfully differs
# from what we last pushed.  Tolerances match what the IP22 itself can
# resolve: 0.1 A and 0.01 V.
_CHARGE_CURRENT_DEADBAND_A = 0.1
_CHARGE_VOLTAGE_DEADBAND_V = 0.05

# IP22 battery-type sentinel that unlocks user-defined absorption/float
# voltage writes via 0xEDF7 / 0xEDF6.  Probed live: writes to 0xEDF7
# return code 02 (param error) until 0xEDF1 == 0xFF.
BATTERY_TYPE_USER = 0xFF

# Known IP22 / Blue Smart model spec strings by product id.  Used when the
# vendored ``victron_ble`` package's table doesn't cover a given SKU.
_IP22_PRODUCT_NAMES = {
    0xA330: "Blue Smart IP22 Charger 12|30 (1)",
    0xA331: "Blue Smart IP22 Charger 12|30 (3)",
    0xA332: "Blue Smart IP22 Charger 24|16 (1)",
    0xA333: "Blue Smart IP22 Charger 24|16 (3)",
    0xA334: "Blue Smart IP22 Charger 12|15 (1)",
    0xA335: "Blue Smart IP22 Charger 12|20 (1)",
    0xA336: "Blue Smart IP22 Charger 12|20 (3)",
    0xA337: "Blue Smart IP22 Charger 24|8 (1)",
    0xA338: "Blue Smart IP22 Charger 12|15 (3)",
    0xA339: "Blue Smart IP22 Charger 24|12 (1)",
    0xA33A: "Blue Smart IP22 Charger 24|12 (3)",
    0xA33B: "Blue Smart IP22 Charger 12|10 (1)",
}

_gatt_writer: Optional[AsyncGATTWriter] = None
_provision_lock = threading.Lock()
_provision_busy = False

_KEY_CLI_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "orion_tr_key_cli.py")

def is_ip22_charger_manufacturer_data(manufacturer_data: bytes) -> bool:
    # The IP22 drops its encrypted payload when powered off and advertises
    # a short "product-id only" frame, so accept any length >= 4 as long as
    # the product id is in the IP22 range.  Frames with a full encrypted
    # payload additionally carry mode byte ``0x08`` (AcCharger).
    if len(manufacturer_data) < 4:
        return False
    pid = struct.unpack("<H", manufacturer_data[2:4])[0]
    if not (IP22_PRODUCT_ID_MIN <= pid <= IP22_PRODUCT_ID_MAX):
        return False
    if len(manufacturer_data) >= 5 and manufacturer_data[4] != 0x08:
        return False
    return True

def _shared_bus() -> dbus.Bus:
    return (
        dbus.SessionBus()
        if "DBUS_SESSION_BUS_ADDRESS" in os.environ
        else dbus.SystemBus()
    )

def _gatt() -> AsyncGATTWriter:
    global _gatt_writer
    if _gatt_writer is None:
        _gatt_writer = AsyncGATTWriter(_shared_bus())
    return _gatt_writer

def _run_key_cli(mac: str, passkey: int,
                 timeout_s: float = 60.0,
                 preferred_adapter: Optional[str] = None,
                 ) -> Optional[Dict[str, Any]]:
    cmd = [
        "python3", _KEY_CLI_PATH,
        mac,
        "--passkey", str(passkey),
        "--timeout", str(int(timeout_s)),
    ]
    if preferred_adapter:
        cmd.extend(["--preferred-adapter", preferred_adapter])
    logger.info("Spawning key-provisioner subprocess: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 20.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ip22 key-provisioner subprocess timed out for %s", mac)
        return None
    except Exception:
        logger.exception("failed to spawn ip22 key-provisioner subprocess")
        return None

    if result.returncode != 0:
        logger.warning("ip22 key-provisioner exited %d: %s",
                       result.returncode, (result.stderr or "").strip())
        return None

    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw)
    except Exception:
        logger.warning("ip22 key-provisioner non-JSON output: %r", raw)
        return None

    key = str(payload.get("key", "")).strip().lower()
    if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
        logger.warning("ip22 key-provisioner returned invalid key: %r", key)
        return None
    payload["key"] = key
    return payload

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
        v = int.from_bytes(blob, "little")
        s = _format_low16(v)
        if s:
            return s
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

def _format_mac_colons(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    return ":".join(s[i : i + 2] for i in range(0, 12, 2)).upper()

def _bluez_device_name(dev_mac: str) -> Optional[str]:
    mac_suffix = "/dev_" + _format_mac_colons(dev_mac).replace(":", "_")
    try:
        bus = _shared_bus()
        om = dbus.Interface(
            bus.get_object("org.bluez", "/", introspect=False),
            "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
        for path in objects:
            if not str(path).endswith(mac_suffix):
                continue
            if "org.bluez.Device1" not in objects[path]:
                continue
            obj = bus.get_object("org.bluez", path, introspect=False)
            props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
            for prop in ("Name", "Alias"):
                try:
                    val = str(props.Get("org.bluez.Device1", prop))
                except dbus.DBusException:
                    continue
                if val:
                    return val
    except Exception:
        return None
    return None

class BleDeviceIP22Charger(BleDevice):
    """Blue Smart IP22 charger driven by encrypted Victron advertisements."""

    # Some IP22 firmwares interleave a 4-byte "product-id only" beacon
    # alongside the encrypted telemetry advertisement.  When the unit is
    # genuinely off it sends only the short beacon, but a running unit can
    # still emit it occasionally — so honour the short-frame "off" reading
    # only after this many seconds without a successful telemetry decode.
    _OFF_FRAME_GRACE_S = 30.0

    @staticmethod
    def matches_manufacturer_data(manufacturer_data: bytes) -> bool:
        return is_ip22_charger_manufacturer_data(manufacturer_data)

    def __init__(self, dev_mac: str):
        self._adv_key_hex: Optional[str] = None
        self._dbus_settings = DbusSettingsService()
        self._pairing_passkey: int = resolve_pairing_passkey(
            self._dbus_settings)
        self._mode_busy = False
        self._last_provision_attempt: float = 0.0
        self._stored_key_invalid = False
        self._last_daily_refresh_date: Optional[str] = None
        self._last_full_telemetry_at: float = 0.0
        # Last value we successfully pushed to the device for each writable
        # DVCC link path.  Used to dedupe GATT writes when DVCC re-publishes
        # the same setpoint on every cycle.
        self._last_pushed_charge_current_a: Optional[float] = None
        self._last_pushed_charge_voltage_v: Optional[float] = None
        # Cached battery-type byte; if it isn't already USER (0xFF) the
        # absorption-voltage write at 0xEDF7 will be rejected with code 02.
        self._battery_type_is_user: Optional[bool] = None
        # GATT write queue.  DVCC pushes /Link/ChargeCurrent and
        # /Link/ChargeVoltage in quick succession; the shared
        # AsyncGATTWriter has a single _busy flag and rejects concurrent
        # writes, so we coalesce per-VREG requests in this slot map and
        # drain them serially via _kick_pending_writes().  Mapping is
        # ``vreg_id -> (value_bytes, on_complete)``; later writes to the
        # same VREG before drain replace the slot — DVCC only cares about
        # the most recent setpoint anyway.
        self._pending_writes: dict[int, tuple[bytes, Optional[Callable]]] = {}
        self._pending_drain_scheduled = False
        super().__init__(dev_mac)

    def configure(self, manufacturer_data: bytes):
        pid = struct.unpack("<H", manufacturer_data[2:4])[0]
        self._adv_key_hex = get_advertisement_key(self._dbus_settings,
                                                  self.info["dev_mac"])
        # Shadow MANUFACTURER_ID the same way Orion-TR does — keep 0x02E1
        # routable to BleDeviceVictronEnergy for SolarSense while still
        # satisfying the base class's per-instance check.
        self.MANUFACTURER_ID = VICTRON_MANUFACTURER_ID
        adv_name = _bluez_device_name(self.info["dev_mac"])
        product_name = (adv_name
                        or _IP22_PRODUCT_NAMES.get(pid)
                        or "Blue Smart IP22 Charger")
        device_name_base = adv_name or "IP22"
        firmware_raw = get_firmware_version(self._dbus_settings,
                                            self.info["dev_mac"])
        firmware_version = _format_firmware_version(firmware_raw) or "1.0.0"
        self.info.update(
            {
                "manufacturer_id": VICTRON_MANUFACTURER_ID,
                "product_id": pid,
                "product_name": product_name,
                "device_name": device_name_base,
                "dev_prefix": "ip22",
                "firmware_version": firmware_version,
                "roles": {"charger": {}},
                "regs": [
                    {
                        "name": "_ip22_placeholder",
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
        if adv_name:
            for role_service in self._role_services.values():
                current = role_service["/CustomName"]
                if not current:
                    role_service["/CustomName"] = adv_name

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
            self._maybe_provision_key()
            return

        # Short "off" frame: just the product-id prefix, no encrypted
        # payload.  Some IP22 firmwares interleave short beacons with full
        # telemetry advertisements as a power-saving feature even while the
        # charger is running, so do not treat a short frame as authoritative
        # off-state if a full telemetry frame arrived recently — let the
        # most recent decoded telemetry stand.  Only honour the short frame
        # as "off" once the IP22 has gone quiet for ``_OFF_FRAME_GRACE_S``.
        if len(manufacturer_data) < 10:
            now = time.monotonic()
            last_full = getattr(self, "_last_full_telemetry_at", 0.0)
            if now - last_full >= self._OFF_FRAME_GRACE_S:
                self._publish_off_state()
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
            logger.exception("%s: IP22 advertisement decode error",
                             self._plog)
            return

        if parsed is None:
            return

        self._last_full_telemetry_at = time.monotonic()
        self._publish(parsed)
        self._maybe_daily_refresh()

    @staticmethod
    def _decode_advertisement(key_hex: str, manufacturer_data: bytes):
        device_cls = detect_device_type(manufacturer_data)
        if device_cls is None:
            return None
        parser = device_cls(key_hex)
        parsed = parser.parse(manufacturer_data)

        charge_state = parsed.get_charge_state()
        charger_error = parsed.get_charger_error()

        model_name = parsed.get_model_name()
        if model_name and model_name.startswith("<Unknown"):
            pid = struct.unpack("<H", manufacturer_data[2:4])[0]
            model_name = _IP22_PRODUCT_NAMES.get(pid, model_name)

        return {
            "device_state": (int(charge_state.value)
                             if charge_state is not None else 0),
            "charger_error": (int(charger_error.value)
                              if charger_error is not None else 0),
            "output_voltage1": parsed.get_output_voltage1(),
            "output_voltage2": parsed.get_output_voltage2(),
            "output_voltage3": parsed.get_output_voltage3(),
            "output_current1": parsed.get_output_current1(),
            "output_current2": parsed.get_output_current2(),
            "output_current3": parsed.get_output_current3(),
            "temperature": parsed.get_temperature(),
            "ac_current": parsed.get_ac_current(),
            "model_name": model_name,
        }

    # ------------------------------------------------------------------
    # Key provisioning lifecycle (mirrors orion_tr_key_cli pipeline)
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
            "%s: no advertisement key cached — spawning subprocess to "
            "read VREG 0xEC65",
            self._plog,
        )

        pause_scanning("ip22 key provisioning")
        _provision_busy = True

        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])

        def worker():
            global _provision_busy
            try:
                with _provision_lock:
                    payload = _run_key_cli(mac_colon,
                                           self._pairing_passkey,
                                           preferred_adapter=pref_adapter)
                if not payload:
                    logger.warning(
                        "%s: key provisioning did not produce a 16-byte "
                        "key; will retry after backoff", self._plog)
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("ip22 key provisioning")

        threading.Thread(
            target=worker, name=f"ip22-keyprov-{mac_colon}",
            daemon=True).start()

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
                    advertisement_key_setting_path(
                        self.info["dev_mac"]))
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
                        role_service["/FirmwareVersion"] = pretty
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "%s: failed to persist firmware version", self._plog)

        hw_version = payload.get("hardware_version")
        if hw_version:
            try:
                self.info["hardware_version"] = hw_version
                for role_service in self._role_services.values():
                    try:
                        role_service["/HardwareVersion"] = hw_version
                    except Exception:
                        pass
            except Exception:
                logger.exception(
                    "%s: failed to set hardware version", self._plog)

        adapter = payload.get("adapter")
        if adapter:
            try:
                set_preferred_adapter(self._dbus_settings,
                                      self.info["dev_mac"], adapter)
            except Exception:
                logger.exception(
                    "%s: failed to store preferred adapter", self._plog)

    # ------------------------------------------------------------------
    # Daily early-morning refresh
    # ------------------------------------------------------------------

    _DAILY_REFRESH_HOUR_MIN = 3
    _DAILY_REFRESH_HOUR_MAX = 5

    def _maybe_daily_refresh(self) -> None:
        global _provision_busy
        if not self._adv_key_hex:
            return
        if _provision_busy:
            return
        now = datetime.datetime.now()
        if not (self._DAILY_REFRESH_HOUR_MIN <= now.hour
                <= self._DAILY_REFRESH_HOUR_MAX):
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_daily_refresh_date == today:
            return

        self._last_daily_refresh_date = today
        mac_colon = _format_mac_colons(self.info["dev_mac"])
        logger.info(
            "%s: daily morning refresh — reading firmware via GATT",
            self._plog)

        pref_adapter = get_preferred_adapter(self._dbus_settings,
                                             self.info["dev_mac"])
        pause_scanning("ip22 daily refresh")
        _provision_busy = True

        def worker():
            global _provision_busy
            try:
                with _provision_lock:
                    payload = _run_key_cli(mac_colon,
                                           self._pairing_passkey,
                                           preferred_adapter=pref_adapter)
                if not payload:
                    return
                self._persist_provisioning_result(payload)
            finally:
                _provision_busy = False
                resume_scanning("ip22 daily refresh")

        threading.Thread(
            target=worker, name=f"ip22-daily-{mac_colon}",
            daemon=True).start()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _publish_off_state(self) -> None:
        """Publish a minimal snapshot when the device is advertising the
        short power-off frame (no encrypted payload)."""
        for role_service in list(self._role_services.values()):
            role_service["/State"] = 0
            role_service["/Dc/0/Current"] = 0.0
            role_service["/Dc/0/Power"] = 0.0
            if not self._mode_busy:
                role_service["/Mode"] = 4
            role_service.connect()

    def _publish(self, parsed) -> None:
        for role_service in list(self._role_services.values()):
            ble_svc = DbusBleService.get()
            if not ble_svc.is_device_role_enabled(
                    self.info, role_service.ble_role.NAME):
                continue

            st = int(parsed["device_state"])
            v1 = parsed.get("output_voltage1")
            i1 = parsed.get("output_current1")
            if v1 is not None:
                role_service["/Dc/0/Voltage"] = v1
            if i1 is not None:
                role_service["/Dc/0/Current"] = i1
            if v1 is not None and i1 is not None:
                role_service["/Dc/0/Power"] = round(v1 * i1, 2)

            for idx, out in enumerate(("2", "3")):
                vk = f"output_voltage{out}"
                ik = f"output_current{out}"
                role_service[f"/Dc/{idx + 1}/Voltage"] = parsed.get(vk)
                role_service[f"/Dc/{idx + 1}/Current"] = parsed.get(ik)

            role_service["/Dc/0/Temperature"] = parsed.get("temperature")
            role_service["/Ac/In/L1/I"] = parsed.get("ac_current")

            model = parsed.get("model_name")
            if model and not model.startswith("<Unknown"):
                role_service["/ProductName"] = model
            role_service["/ProductId"] = self.info["product_id"]
            role_service["/State"] = st
            role_service["/ErrorCode"] = int(parsed["charger_error"])

            # NrOfOutputs — any non-None out2/out3 bumps it up
            outputs = 1
            if parsed.get("output_voltage2") is not None:
                outputs = 2
            if parsed.get("output_voltage3") is not None:
                outputs = 3
            role_service["/NrOfOutputs"] = outputs

            if not self._mode_busy:
                role_service["/Mode"] = 4 if st == 0 else 1

            role_service.connect()

    # ------------------------------------------------------------------
    # /Mode write (GATT)
    # ------------------------------------------------------------------

    def _ip22_on_mode_write(self,
                            role_service: DbusRoleService,
                            value: int) -> bool:
        # Disabled: the IP22 BLE firmware does not implement VREG 0x0200.
        # See docs/IP22-INTEGRATION.md for the register-probe matrix.
        return False

    # ------------------------------------------------------------------
    # /Settings/ChargeCurrentLimit write (GATT) — VREG 0xEDF0, u16 LE in 0.1A
    # ------------------------------------------------------------------

    def _ip22_on_charge_current_limit_write(self,
                                            role_service: DbusRoleService,
                                            value_amps) -> bool:
        # value_amps is the requested current in amps (float-ish).  IP22
        # encodes this as u16 LE, scale 0.1A.  The device clamps writes
        # below the model minimum (~7.5A on the 12/30 model).
        try:
            tenths = int(round(float(value_amps) * 10))
        except (TypeError, ValueError):
            return False
        if tenths < 0 or tenths > 0xFFFF:
            return False
        writer = _gatt()
        if writer.busy:
            logger.warning("%s: GATT writer busy", self._plog)
            return False

        mac = _format_mac_colons(self.info["dev_mac"])
        pause_scanning("ip22 /Settings/ChargeCurrentLimit write")

        def on_done(success: bool):
            try:
                if not success:
                    logger.error("%s: GATT ChargeCurrentLimit write failed",
                                 self._plog)
            finally:
                resume_scanning("ip22 /Settings/ChargeCurrentLimit write")

        # u16 little-endian in 0.1A units
        value_bytes = bytes([tenths & 0xFF, (tenths >> 8) & 0xFF])
        writer.write_register(
            mac,
            self._pairing_passkey,
            VREG_BATTERY_MAX_CURRENT,
            value_bytes,
            on_done=on_done,
        )
        return True

    # ------------------------------------------------------------------
    # DVCC integration — /Link/* writes pushed by dbus-systemcalc-py
    # ------------------------------------------------------------------
    #
    # dbus-systemcalc-py pushes target setpoints onto a charger service via
    # /Link/ChargeCurrent and /Link/ChargeVoltage and expects them to take
    # effect on the hardware.  For IP22 we map:
    #
    #   /Link/ChargeCurrent  -> VREG 0xEDF0 (Battery max current,    0.1 A)
    #   /Link/ChargeVoltage  -> VREG 0xEDF7 (Absorption voltage,     0.01 V)
    #
    # The other DVCC inputs (/Link/TemperatureSense, /Link/VoltageSense,
    # /Link/BatteryCurrent, /Link/NetworkMode, /Settings/BmsPresent) are
    # accepted on the role but only stored — IP22 firmware 3.65 has no
    # writable VREG that consumes external sense or BMS-presence data, so
    # we surface them on D-Bus for systemcalc to read back without trying
    # to push them onto the wire.

    # ------------------------------------------------------------------
    # Per-device GATT write queue — collapses DVCC bursts and serialises
    # writes against the single-slot AsyncGATTWriter.
    # ------------------------------------------------------------------

    _PENDING_DRAIN_INTERVAL_MS = 1500

    def _enqueue_write(self, vreg: int, value_bytes: bytes,
                       on_complete: Optional[Callable] = None) -> None:
        # Newer write to the same VREG supersedes any pending one.  This
        # is exactly what we want for DVCC-style continuous setpoints.
        self._pending_writes[vreg] = (value_bytes, on_complete)
        self._kick_pending_writes()

    def _kick_pending_writes(self) -> None:
        if not self._pending_writes:
            return
        writer = _gatt()
        if writer.busy:
            self._schedule_drain()
            return

        # Pop one slot and fire the underlying GATT write.  on_done
        # reschedules the drain so the next pending VREG goes out next.
        vreg, (value_bytes, on_complete) = next(
            iter(self._pending_writes.items()))
        del self._pending_writes[vreg]

        mac = _format_mac_colons(self.info["dev_mac"])
        pause_scanning(f"ip22 GATT write 0x{vreg:04X}")

        def on_done(success: bool):
            try:
                if not success:
                    logger.error("%s: GATT write 0x%04X failed",
                                 self._plog, vreg)
                if on_complete is not None:
                    try:
                        on_complete(success)
                    except Exception:
                        logger.exception(
                            "%s: pending-write completion callback failed",
                            self._plog)
            finally:
                resume_scanning(f"ip22 GATT write 0x{vreg:04X}")
                self._schedule_drain()

        writer.write_register(
            mac, self._pairing_passkey,
            vreg, value_bytes,
            on_done=on_done,
        )

    def _schedule_drain(self) -> None:
        if self._pending_drain_scheduled or not self._pending_writes:
            return
        self._pending_drain_scheduled = True

        def _on_drain_tick():
            self._pending_drain_scheduled = False
            try:
                self._kick_pending_writes()
            except Exception:
                logger.exception("%s: pending-write drain tick failed",
                                 self._plog)
            return False  # one-shot

        GLib.timeout_add(self._PENDING_DRAIN_INTERVAL_MS, _on_drain_tick)

    # ------------------------------------------------------------------
    # /Link/ChargeCurrent — DVCC target current → VREG 0xEDF0
    # ------------------------------------------------------------------

    def _ip22_on_link_charge_current_write(self,
                                            role_service: DbusRoleService,
                                            value_amps) -> bool:
        try:
            new_a = float(value_amps)
        except (TypeError, ValueError):
            return False
        if new_a < 0 or new_a > 1000:
            return False
        last = self._last_pushed_charge_current_a
        if last is not None and abs(new_a - last) < _CHARGE_CURRENT_DEADBAND_A:
            return True
        tenths = int(round(new_a * 10))
        if tenths < 0 or tenths > 0xFFFF:
            return False
        value_bytes = bytes([tenths & 0xFF, (tenths >> 8) & 0xFF])

        def on_done(success: bool):
            if success:
                self._last_pushed_charge_current_a = new_a

        self._enqueue_write(VREG_BATTERY_MAX_CURRENT, value_bytes,
                            on_complete=on_done)
        return True

    # ------------------------------------------------------------------
    # /Link/ChargeVoltage — DVCC target voltage → VREG 0xEDF7
    # ------------------------------------------------------------------

    def _ip22_on_link_charge_voltage_write(self,
                                            role_service: DbusRoleService,
                                            value_volts) -> bool:
        try:
            new_v = float(value_volts)
        except (TypeError, ValueError):
            return False
        if new_v <= 0 or new_v > 80:
            return False
        last = self._last_pushed_charge_voltage_v
        if last is not None and abs(new_v - last) < _CHARGE_VOLTAGE_DEADBAND_V:
            return True
        centivolts = int(round(new_v * 100))
        if centivolts < 0 or centivolts > 0xFFFF:
            return False
        value_bytes = bytes([centivolts & 0xFF, (centivolts >> 8) & 0xFF])

        # 0xEDF7 only accepts writes once the battery-type sentinel
        # 0xEDF1 == 0xFF (USER) has been set; queue that first whenever we
        # haven't confirmed the cache.  The drain runs serialised so the
        # USER write completes before the voltage write goes out.
        if self._battery_type_is_user is not True:
            def on_user_set(success: bool):
                if success:
                    self._battery_type_is_user = True
                else:
                    logger.error(
                        "%s: GATT BatteryType=USER write failed",
                        self._plog)
            self._enqueue_write(
                VREG_BATTERY_TYPE, bytes([BATTERY_TYPE_USER]),
                on_complete=on_user_set,
            )

        def on_voltage_set(success: bool):
            if success:
                self._last_pushed_charge_voltage_v = new_v

        self._enqueue_write(VREG_ABSORPTION_VOLTAGE, value_bytes,
                            on_complete=on_voltage_set)
        return True

    @staticmethod
    def _ip22_on_link_passive_write(_role_service: DbusRoleService,
                                    _value) -> bool:
        # Generic "store on D-Bus, no GATT" handler used for
        # /Link/TemperatureSense, /Link/VoltageSense, /Link/BatteryCurrent,
        # /Link/NetworkMode and /Settings/BmsPresent.  Returning True
        # signals the D-Bus layer to commit the value on the path so DVCC
        # can read it back; we don't push it to the IP22 over GATT because
        # this firmware exposes no consumer VREG for the sense inputs.
        return True
