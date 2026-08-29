# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Micro-Air EasyStart soft starter — connect-and-poll BLE driver.

The EasyStart is unlike every other device in this service:

* **Identified by advertised name, not address.**  The advertised MAC
  rotates (observed changing within hours), so the device's identity —
  its dev_id, its settings, its D-Bus service — is derived from the
  advertised name (``EasyStart_XXXX``).  The MAC heard in the latest
  advertisement is only used to open the next connection.
* **No advertisement telemetry.**  The advertisement is a presence
  signal; all data comes over a GATT connection (bcmv2-routed, same
  adapter pool as every other link this service opens).
* **Reachable only while the A/C compressor runs.**  Absence of the
  device is the normal off state, not an error, and is never retried
  aggressively or logged as a fault.
* **Exclusive connection.**  While we hold the link the vendor phone
  app cannot connect, and vice versa.  Disabling the device in the GUI
  releases the link within one poll interval.

**Read-only by design.**  The session sends exactly two commands —
``ReadLive`` and ``ReadEEP`` — as literal byte constants from
:mod:`easystart_protocol`.  The device's single write characteristic
also carries settings writes and firmware-update blocks, distinguished
only by content, so no code path here may construct command bytes from
variables.  See ``docs/EASYSTART-PROTOCOL.md``.

Published as ``com.victronenergy.acload`` with the standard AC paths.
Real power is not reported by the device; it is derived as current ×
nominal voltage (a setting, default 120 V) with no power factor, which
is why the voltage path publishes the nominal value it used.
"""
from __future__ import annotations

import asyncio
import logging
import time

from gi.repository import GLib

import ble_async_loop
import ble_gatt_link
import easystart_protocol as proto
from ble_device import BleDevice
from dbus_ble_service import DbusBleService
from ve_types import VE_SN32

logger = logging.getLogger(__name__)

# How long one whole read (command write → terminator) may take.  The
# live block is one chunk; the config block is ~1100 bytes of chunks.
READ_TIMEOUT_S = 15.0

# Consecutive failed polls before the session is torn down.  The device
# dropping the link mid-poll is normal (compressor stopped); this only
# bounds a link that is up but not answering.
MAX_POLL_FAILURES = 3

# Wait between sessions.  A session usually ends because the A/C shut
# off; the next advertisement is what says it is back, but if the unit
# keeps advertising while refusing connections this stops us hammering
# it — and monopolising the radio — with back-to-back attempts.
SESSION_COOLDOWN_S = 30.0

# Overall bound on one session-establishment attempt (resolve, which may
# include a bounded discovery, plus connect with retries).
CONNECT_OVERALL_TIMEOUT_S = 60.0


class BleDeviceEasyStart(BleDevice):
    """Driver for the Micro-Air EasyStart (Flex / 364 with Bluetooth)."""

    # Routing is by advertised name; the manufacturer id is required by
    # the base class's configuration check but never used for dispatch.
    MANUFACTURER_ID = -1
    CUSTOM_PARSING = True
    ADV_NAME_PREFIXES = (proto.ADV_NAME_PREFIX,)

    @staticmethod
    def identity_from_name(adv_name: str) -> str:
        """Stable identity for D-Bus ids, derived from the advertised name.

        ``EasyStart_7F3A`` → ``easystart_7f3a``; the bare ``EasyStart_``
        variant (no unit suffix) → ``easystart``.
        """
        return adv_name.lower().rstrip('_')

    def __init__(self, identity: str):
        super().__init__(identity)
        self._adv_name: str = ''
        self._current_mac: 'str | None' = None
        self._session_active = False
        self._stop_session = False
        self._deleted = False
        self._cooldown_until = 0.0
        self._config_published = False
        self._reachable: 'bool | None' = None

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_id': 0xB051,  # invented, no registry exists
            'product_name': 'Micro-Air EasyStart',
            'device_name': 'EasyStart soft starter',
            'dev_prefix': 'microair',
            'roles': {'acload': {}},
            'regs': [],
            'settings': [
                {
                    # Voltage used to derive power (device reports only
                    # current).  120 V default — RV split-phase leg.
                    'name': 'NominalVoltage',
                    'props': {
                        'type': VE_SN32,
                        'def': 120,
                        'min': 90,
                        'max': 250,
                    },
                },
            ],
        })

    def is_busy(self) -> bool:
        """Whether a GATT session is live.

        A connected EasyStart stops advertising, so the tap-driven TTL
        refresh never fires during a long A/C run; the main service uses
        this to keep the device out of the prune.
        """
        return self._session_active

    # ------------------------------------------------------------------
    # Advertisement handling (GLib thread)
    # ------------------------------------------------------------------

    def handle_name_advertisement(self, mac: str, adv_name: str, rssi: int):
        """Called on the GLib thread for each (rate-limited) name match.

        *mac* is colon-separated uppercase — the address to connect to
        right now, valid only until the device rotates it.
        """
        self._adv_name = adv_name
        self._current_mac = mac

        if not DbusBleService.get().is_device_enabled(self.info):
            logging.debug(f"{self._plog} seen but not enabled, skipping")
            return

        role_service = self._role_services.get('acload')
        if role_service is None:
            return

        # Register the D-Bus service on first sighting (mirrors what
        # handle_manufacturer_data does for advertisement sensors).
        role_service.connect()
        self._publish_value(role_service, '/Rssi', rssi)

        if self._session_active:
            return
        now = time.monotonic()
        if now < self._cooldown_until:
            return
        self._start_session(mac)

    def _start_session(self, mac: str):
        if not ble_async_loop.available() and not ble_async_loop.start():
            logging.warning(f"{self._plog} BLE loop unavailable, cannot connect")
            return
        self._session_active = True
        self._stop_session = False
        self._config_published = False
        logging.info(f"{self._plog} starting GATT session to {mac}")
        ok = ble_async_loop.submit(
            lambda: self._run_session(mac),
            on_done=self._on_session_done,
        )
        if not ok:
            self._session_active = False

    def _on_session_done(self, result, error):
        """Session ended (GLib thread) — normal for the A/C shutting off."""
        self._session_active = False
        self._cooldown_until = time.monotonic() + SESSION_COOLDOWN_S
        if error is None:
            logging.info(f"{self._plog} session ended")
        elif ble_gatt_link.unreachable(error):
            # Off, out of range, or the phone app holds the exclusive
            # link.  Expected steady state — one debug line, no trace.
            logging.debug(f"{self._plog} not reachable: {error}")
        elif isinstance(error, asyncio.TimeoutError):
            logging.info(f"{self._plog} session timed out")
        else:
            logging.warning(f"{self._plog} session failed: {error!r}")
        self._publish_offline()

    # ------------------------------------------------------------------
    # GATT session (BLE loop thread — no dbus from here)
    # ------------------------------------------------------------------

    async def _run_session(self, address: str):
        device = await asyncio.wait_for(
            ble_gatt_link.resolve(address),
            timeout=CONNECT_OVERALL_TIMEOUT_S)
        client = await ble_gatt_link.connect(device, self._adv_name or address)
        try:
            reassembler = proto.Reassembler()
            done = asyncio.Event()
            outcome: list = [None]

            def on_notify(_char, payload: bytearray):
                if done.is_set():
                    return
                result = reassembler.feed(bytes(payload))
                if result is not None:
                    outcome[0] = result
                    done.set()

            await client.start_notify(proto.NOTIFY_CHAR_UUID, on_notify)

            async def read_block(command: bytes) -> 'bytes | None':
                # Reset BEFORE the write — the device may begin
                # streaming before write_gatt_char returns.
                reassembler.reset()
                outcome[0] = None
                done.clear()
                await client.write_gatt_char(proto.WRITE_CHAR_UUID, command,
                                             response=True)
                try:
                    await asyncio.wait_for(done.wait(), timeout=READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    return None
                if outcome[0] is not True:
                    return None
                return reassembler.buffer

            # Configuration once per session; its failure does not fail
            # the session — live telemetry is the point.
            config = None
            raw = await read_block(proto.CMD_READ_EEP)
            if raw is not None:
                config = proto.decode_config(raw)
                if config is None:
                    logger.info("%s: config block truncated (%d bytes) — "
                                "settings not decoded", self._adv_name, len(raw))
            if config is not None:
                GLib.idle_add(self._publish_config_glib, config)

            failures = 0
            while not self._stop_session:
                raw = await read_block(proto.CMD_READ_LIVE)
                live = proto.decode_live(raw) if raw is not None else None
                if live is None:
                    failures += 1
                    if failures >= MAX_POLL_FAILURES:
                        logger.info("%s: %d consecutive failed polls — "
                                    "ending session", self._adv_name, failures)
                        break
                else:
                    failures = 0
                    GLib.idle_add(self._publish_live_glib, live)
                await asyncio.sleep(proto.POLL_INTERVAL_S)
        finally:
            await ble_gatt_link.disconnect(client)

    # ------------------------------------------------------------------
    # Publishing (GLib thread)
    # ------------------------------------------------------------------

    def _publish_target(self):
        """Role service to publish to, or None (deleted / disabled)."""
        if self._deleted:
            return None
        role_service = self._role_services.get('acload')
        if role_service is None:
            return None
        if not DbusBleService.get().is_device_enabled(self.info):
            # User disabled the device mid-session: release the link.
            self._stop_session = True
            return None
        return role_service

    def _nominal_voltage(self, role_service) -> float:
        value = role_service['/NominalVoltage']
        try:
            voltage = float(value)
        except (TypeError, ValueError):
            voltage = 120.0
        return voltage if voltage > 0 else 120.0

    def _publish_live_glib(self, live: dict) -> bool:
        role_service = self._publish_target()
        if role_service is None:
            return False
        voltage = self._nominal_voltage(role_service)
        current = live['current']
        power = current * voltage
        with role_service:
            pub = self._publish_value
            pub(role_service, '/Ac/L1/Current', current,
                sensor_type='current')
            pub(role_service, '/Ac/Current', current, sensor_type='current')
            # Derived, no power factor — the voltage path says which
            # nominal value produced it.
            pub(role_service, '/Ac/L1/Power', power, override=0)
            pub(role_service, '/Ac/Power', power, override=0)
            pub(role_service, '/Ac/L1/Voltage', voltage, override=0)
            if live['frequency'] is not None:
                pub(role_service, '/Ac/L1/Frequency', live['frequency'],
                    override=1)
            pub(role_service, '/EasyStart/Reachable', 1)
            pub(role_service, '/EasyStart/State', live['state'])
            pub(role_service, '/EasyStart/StateName', live['state_name'])
            pub(role_service, '/EasyStart/LearnedStarts',
                live['learned_starts'])
            pub(role_service, '/EasyStart/PeakStartCurrent',
                live['peak_current'], sensor_type='current')
            pub(role_service, '/EasyStart/ScptRemainingSeconds',
                live['scpt_remaining'])
            pub(role_service, '/EasyStart/TotalStarts', live['total_starts'])
            pub(role_service, '/EasyStart/TotalFaults', live['total_faults'])
            pub(role_service, '/Alarms/Fault', 2 if live['fault'] else 0)
        if self._reachable is not True:
            self._reachable = True
            logging.info(f"{self._plog} live telemetry flowing "
                         f"({current:.1f} A, state {live['state_name']!r})")
        return False  # one-shot idle callback

    def _publish_config_glib(self, config: dict) -> bool:
        role_service = self._publish_target()
        if role_service is None:
            return False
        with role_service:
            pub = self._publish_value
            pub(role_service, '/FirmwareVersion',
                str(config['firmware_version']))
            pub(role_service, '/EasyStart/StartupMask', config['smask'])
            pub(role_service, '/EasyStart/FaultMask', config['fmask'])
            pub(role_service, '/EasyStart/ScptDelayMinutes',
                config['scpt_delay_setting'])
            disarmed = config['fmask_disarmed']
            pub(role_service, '/EasyStart/ProtectionsDisarmed',
                ', '.join(disarmed) if disarmed else '')
            # A disarmed protective cutout is a warning the user may not
            # know about (a previous owner/installer turned it off).
            pub(role_service, '/Alarms/ProtectionDisarmed',
                1 if disarmed else 0)
        if disarmed:
            logging.warning(f"{self._plog} fault protections disarmed on "
                            f"the unit: {', '.join(disarmed)}")
        return False

    def _publish_offline(self):
        """A/C off (or link lost): the load is genuinely ~0, say so."""
        role_service = self._role_services.get('acload')
        if role_service is None or self._deleted:
            return
        if not role_service.is_connected():
            return
        with role_service:
            pub = self._publish_value
            pub(role_service, '/Ac/L1/Current', 0.0, sensor_type='current')
            pub(role_service, '/Ac/Current', 0.0, sensor_type='current')
            pub(role_service, '/Ac/L1/Power', 0.0, override=0)
            pub(role_service, '/Ac/Power', 0.0, override=0)
            pub(role_service, '/EasyStart/Reachable', 0)
        if self._reachable is not False:
            self._reachable = False
            logging.info(f"{self._plog} offline — publishing 0 W "
                         "(A/C not running is the normal off state)")

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        # Name-identified device: nothing arrives through the
        # manufacturer-data path.
        pass

    def delete(self):
        self._deleted = True
        self._stop_session = True
        super().delete()
