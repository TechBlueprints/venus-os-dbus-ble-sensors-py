# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Micro-Air EasyStart soft starter — connect-and-poll BLE driver.

The EasyStart is unlike every other device in this service:

* **Identified by advertised name, not address.**  The device's identity
  — its dev_id, its settings, its D-Bus service — is derived from the
  advertised name (``EasyStart_XXXX``); the MAC heard in the latest
  advertisement is only used to open the next connection.  Address
  rotation is reported by one community source but is unconfirmed here
  (the field units hold fixed public addresses), so a cached address is
  a usable optimisation that must survive going stale — never identity.
  See ``docs/EASYSTART-PROTOCOL.md``.
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

import adapter_identity
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
# off; the next advertisement is what says it is back, but the unit
# also advertises while refusing connections (observed on prod: the
# idle unit adopts fine, then fails the connect instantly), so failures
# back off exponentially up to the cap — otherwise an idle A/C draws a
# connect attempt every 30 s all night.
SESSION_COOLDOWN_S = 30.0
SESSION_COOLDOWN_MAX_S = 600.0

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
    # dev_id prefix for stored settings (dev_prefix + '_' + identity
    # prefix).  The scan-policy code uses this to recognise that a
    # configured rotating-MAC device exists, which rules out controller
    # accept-list filtering.
    DEV_ID_PREFIXES = ('microair_easystart',)

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
        self._address_type: int = 1
        # The CARD that heard the device, as its identity (MAC), never
        # as the hciN it happened to answer to at the time — see
        # :meth:`_resolve_without_scanning`.
        self._adapter_key: 'str | None' = None
        self._session_active = False
        self._stop_session = False
        self._deleted = False
        self._cooldown_until = 0.0
        self._failure_streak = 0
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

    def handle_name_advertisement(self, mac: str, adv_name: str, rssi: int,
                                  address_type: int = 1,
                                  adapter_index: int = 0):
        """Called on the GLib thread for each (rate-limited) name match.

        *mac* is colon-separated uppercase — the address to connect to
        right now.  *address_type* and *adapter_index* come from the same
        HCI report, and together they let the connect skip discovery
        entirely: BlueZ is handed the address, its type, and the card
        that provably heard it (range is 1-2 m, so the card that heard
        the advertisement is the card that can reach it).

        The adapter index is converted to the card's identity (its MAC)
        here and resolved back to a number only at the moment of the
        connect — ``hciN`` is what a card is called right now, not what
        it is.  See :meth:`_resolve_without_scanning`.
        """
        self._adv_name = adv_name
        self._current_mac = mac
        self._address_type = address_type
        self._adapter_key = adapter_identity.canonical(f"hci{adapter_index}")

        if not DbusBleService.get().is_device_enabled(self.info):
            logging.debug(f"{self._plog} seen but not enabled, skipping")
            return

        role_service = self._role_services.get('acload')
        if role_service is None:
            return

        # Register the D-Bus service on first sighting (mirrors what
        # handle_manufacturer_data does for advertisement sensors).
        role_service.connect()
        # RSSI wobbles a couple of dBm on every advertisement; deadband
        # it so presence updates don't become the service's noisiest path.
        self._publish_value(role_service, '/Rssi', rssi, deadband=4)

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
        # An error AFTER telemetry was flowing is the compressor stopping
        # mid-poll — the ordinary way a session ends (observed on prod as
        # a GATT protocol error on the in-flight write).  Only errors
        # from sessions that never produced data count toward backoff.
        was_flowing = self._reachable is True
        # The compressor stopping DURING connect ends the link before
        # service discovery finishes, so the characteristic lookup runs
        # against an empty GATT database.  Physically identical to the
        # mid-session drop above, only earlier — so it must not count
        # toward backoff either.  Observed on prod 2026-08-29 02:18:47,
        # where it cost a WARNING and an exponential backoff that delayed
        # picking the unit back up.
        dropped_early = bool(getattr(self, "_dropped_before_discovery", False))
        if error is None or was_flowing or dropped_early:
            self._failure_streak = 0
            cooldown = SESSION_COOLDOWN_S
        else:
            self._failure_streak += 1
            cooldown = min(
                SESSION_COOLDOWN_S * (2 ** min(self._failure_streak - 1, 5)),
                SESSION_COOLDOWN_MAX_S)
        self._cooldown_until = time.monotonic() + cooldown
        if error is None:
            logging.info(f"{self._plog} session ended")
        elif was_flowing:
            logging.info(f"{self._plog} link dropped mid-session "
                         f"(A/C stopping is the usual cause): {error!r}")
        elif dropped_early:
            logging.info(f"{self._plog} link dropped before service "
                         f"discovery (A/C stopping during connect is the "
                         f"usual cause): {error!r}")
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

    async def _resolve_without_scanning(self, address: str):
        """Build a connectable ``BLEDevice`` with zero discovery.

        The stock fallback (``ble_gatt_link.resolve`` →
        ``find_device_by_address``) needs an active scan, and this very
        process holds the scan claim on every adapter for its passive
        tap — the wrapped scanner queues behind our own claim and times
        out (observed on prod: ScanSlotWaitTimeout after 30 s, every
        session).  We do not need a scan: the advertisement that
        triggered this session carried the address, its type, and the
        adapter that heard it.  ``Adapter1.ConnectDevice`` (experimental
        BlueZ API; bluetoothd runs with -E on this platform, the service
        run script enforces it) creates the ``Device1`` object from
        exactly those and opens the link on that adapter.

        The adapter is named by its MAC and resolved to an ``hciN``
        **here**, immediately before the call, never carried as a number
        from when the advertisement arrived.  A replug or USB reset
        renumbers cards, and a stale number would aim ConnectDevice at a
        different radio — one that likely cannot hear a device with 1-2 m
        of range, and that another service may have been promised.  Same
        rule the scan path follows; see :mod:`adapter_identity`.
        """
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        from dbus_fast import Message, Variant
        from dbus_fast.constants import MessageType

        index = adapter_identity.index_for(self._adapter_key)
        if index is None:
            # The card that heard it is gone.  Unreachable *through this
            # route*, not a defect: the next advertisement arrives on
            # whichever radio is alive and carries that card's identity.
            raise ble_gatt_link.DeviceNotFound(
                f"{address}: adapter "
                f"{adapter_identity.label(self._adapter_key)} that heard "
                f"this device is no longer present")

        manager = await get_global_bluez_manager()
        bus = manager._bus
        adapter_path = f"/org/bluez/hci{index}"
        dev_path = f"{adapter_path}/dev_{address.upper().replace(':', '_')}"
        type_str = 'public' if self._address_type == 0 else 'random'

        reply = await bus.call(Message(
            destination='org.bluez', path=adapter_path,
            interface='org.bluez.Adapter1', member='ConnectDevice',
            signature='a{sv}',
            body=[{'Address': Variant('s', address),
                   'AddressType': Variant('s', type_str)}]))
        if reply.message_type == MessageType.ERROR:
            # AlreadyExists: the object survived a previous session on
            # this adapter — connect to it below like any known device.
            if reply.error_name != 'org.bluez.Error.AlreadyExists':
                raise ble_gatt_link.DeviceNotFound(
                    f"{address}: ConnectDevice on "
                    f"{adapter_identity.label(self._adapter_key)} "
                    f"failed: {reply.error_name} {reply.body!r}")
        elif reply.body:
            dev_path = str(reply.body[0])

        return ble_gatt_link.make_ble_device(
            address, dev_path, {'Alias': self._adv_name or address})

    async def _run_session(self, address: str):
        self._dropped_before_discovery = False
        device = await asyncio.wait_for(
            self._resolve_without_scanning(address),
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
                    logger.info("%s: read %r timed out after %.0fs "
                                "(%d bytes buffered)", self._adv_name,
                                command, READ_TIMEOUT_S, reassembler.length)
                    return None
                if outcome[0] is not True:
                    logger.info("%s: read %r transfer failed "
                                "(%d bytes buffered)", self._adv_name,
                                command, reassembler.length)
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
                    if raw is not None:
                        logger.info("%s: live block undecodable: %d bytes, "
                                    "%s", self._adv_name, len(raw),
                                    raw[:24].hex())
                    failures += 1
                    if failures >= MAX_POLL_FAILURES:
                        logger.info("%s: %d consecutive failed polls — "
                                    "ending session", self._adv_name, failures)
                        break
                else:
                    failures = 0
                    GLib.idle_add(self._publish_live_glib, live)
                await asyncio.sleep(proto.POLL_INTERVAL_S)
        except Exception as exc:
            # Classified here, not in _on_session_done, because the
            # discriminator is the client's own GATT database and the
            # callback never sees the client.
            #
            # This handler sits on the path of EVERY session, while the
            # condition it detects is rare (1 in 41 on prod).  So it must
            # not be able to make things worse than the error it is
            # describing: if the classifier throws, that exception would
            # replace the real one and every session would fail in a new
            # way.  Swallow it and stay loud instead — the caller's
            # WARNING is the correct fallback when we cannot tell.
            #
            # Exception, not BaseException: a cancelled session is a
            # shutdown, not a diagnosis, and has no business running
            # classification on its way out.
            try:
                self._dropped_before_discovery = (
                    ble_gatt_link.dropped_before_discovery(exc, client))
            except Exception:
                logging.exception(
                    f"{self._plog} could not classify session error; "
                    f"reporting it unclassified")
                self._dropped_before_discovery = False
            raise
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
            # The device reports current in 0.1 A steps and a running
            # compressor wanders one step either way on most polls, so
            # rounded-equality dedup passes nearly every write.  A
            # deadband above one device step (and its 120 V multiple on
            # the derived power) keeps the steady state silent while a
            # real load change still publishes within one poll.
            pub(role_service, '/Ac/L1/Current', current, deadband=0.3)
            pub(role_service, '/Ac/Current', current, deadband=0.3)
            # Derived, no power factor — the voltage path says which
            # nominal value produced it.
            pub(role_service, '/Ac/L1/Power', power, deadband=30.0)
            pub(role_service, '/Ac/Power', power, deadband=30.0)
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
            # Same deadbands as the live path so each path's dedup cache
            # compares like with like across on/off transitions.
            pub(role_service, '/Ac/L1/Current', 0.0, deadband=0.3)
            pub(role_service, '/Ac/Current', 0.0, deadband=0.3)
            pub(role_service, '/Ac/L1/Power', 0.0, deadband=30.0)
            pub(role_service, '/Ac/Power', 0.0, deadband=30.0)
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
