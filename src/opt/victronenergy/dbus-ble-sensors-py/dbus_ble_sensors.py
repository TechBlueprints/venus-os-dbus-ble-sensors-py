#!/usr/bin/env python3
import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
import logging
from logging.handlers import RotatingFileHandler
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from argparse import ArgumentParser
from ble_device import BleDevice
from ble_device_orion_tr import BleDeviceOrionTR, is_orion_tr_manufacturer_data
from ble_device_ip22_charger import (
    BleDeviceIP22Charger,
    is_ip22_charger_manufacturer_data,
)
from ble_device_smartshunt import (
    BleDeviceSmartShunt,
    is_smartshunt_manufacturer_data,
)
from ble_role import BleRole
from dbus_bus import get_bus
from dbus_ble_service import DbusBleService
from gi.repository import GLib
from logger import setup_logging
import log_filters
from collections.abc import MutableMapping
import json
import threading
import time
from conf import IGNORED_DEVICES_TIMEOUT, DEVICE_SERVICES_TIMEOUT, PROCESS_VERSION
from hci_advertisement_tap import (
    create_tap_socket, run_tap_loop, TappedAdvertisement,
)
from ble_advertisement_router import BleAdvertisementRouter
from sensor_rounding import SensorRoundingPolicy
from sensor_publisher import SensorPublisher
from load_throttle import LoadThrottle
import adapter_identity
import hci_scan_control
import platform_notifications
from scan_claims import ScanClaims

ADV_LOG_QUIET_PERIOD = 1800
SILENCE_WARNING_SECONDS = 300

# Name-identified devices (EasyStart): their advertisement is a presence
# signal, not a data payload, so forwarding every one to the GLib thread
# buys nothing.  One per MAC per this interval is plenty.
NAME_ADV_MIN_INTERVAL = 5.0

# Name-identified devices ride in the accept lists by their LAST-HEARD
# address.  Running the radios accept-all for them instead cost real
# CPU (measured on prod: ~23% of a core parsing the neighbour firehose
# on both adapters, 15-minute load ~4.0 against the 5.5 throttle trip).
# The field units hold a fixed Espressif-OUI public address — the
# community rotation report is unconfirmed on this hardware — so
# there is no periodic wide listening; if an address ever does rotate
# while the unit is unheard, the device goes silent until a restart's
# grace window relearns it (or Continuous scanning is re-enabled), and
# that is the accepted trade until rotation is actually observed.
#
# After a restart no current address is known; listen wide until one
# is heard, but never longer than this.
NAME_STARTUP_GRACE_S = 600.0
# Byte-level identical-advertisement re-forward interval comes from the
# SensorRoundingPolicy setting at /Settings/SensorRounding/HeartbeatSeconds
# so this and the publish-level dedup in SensorPublisher share one knob.
from man_id import MAN_NAMES

SNIF_LOGGER = logging.getLogger("sniffer")
SNIF_LOGGER.propagate = False

# How often (seconds) to re-issue the HCI scan-enable commands on each
# adapter.  Other things on the system (notably ``shyion-switch`` doing
# active scans via bleak) can reset the controller's scan parameters
# back to active or disable scanning entirely.  Re-issuing every minute
# keeps us in passive mode with a worst-case 60 s gap.  See the
# ``hci_scan_control`` module docstring for the full rationale.
_SCAN_REENABLE_INTERVAL_S = 60

# Where we persist the ``{mac: address_type}`` cache.  Sits on the
# ``/data`` partition so it survives reboots — without it, the first
# scan_reenable tick after a service restart would see an empty cache
# and we'd have to bounce the user back through accept-all mode to
# rediscover our own configured devices.
_KNOWN_MAC_TYPES_PATH = '/data/conf/dbus-ble-sensors-py-known-mac-types.json'
# Last-heard address per name-identified device (EasyStart), persisted
# because the accept lists need it from the first second after a
# restart: the units can stay silent for hours (a compressor that is
# off may not advertise), so the startup grace alone cannot be relied
# on to relearn them, and a unit missing from the accept list is never
# heard again.  Field units hold fixed public addresses, so a stored
# value staying valid is the expected case, not a hope.
_NAME_DEVICE_MACS_PATH = '/data/conf/dbus-ble-sensors-py-name-device-macs.json'


def _load_name_device_macs_static() -> 'dict[str, tuple[str, int]]':
    """Read the persisted ``{identity: [mac, address_type]}`` map.

    Empty on any failure — then the startup grace is the (best-effort)
    fallback for learning the addresses fresh.
    """
    try:
        with open(_NAME_DEVICE_MACS_PATH, 'r') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        out = {}
        for identity, entry in raw.items():
            if not isinstance(identity, str) or not identity:
                continue
            try:
                mac, addr_type = entry
            except (TypeError, ValueError):
                continue
            if not isinstance(mac, str) or len(mac) != 12:
                continue
            try:
                addr_type = int(addr_type)
            except (TypeError, ValueError):
                continue
            if addr_type not in (0, 1):
                continue
            out[identity] = (mac.lower(), addr_type)
        return out
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception(f"Failed to read {_NAME_DEVICE_MACS_PATH!r}, "
                          "starting fresh")
        return {}


# Adapter identity lives in adapter_identity: a card is its MAC, and the
# hci<N> index is resolved from that immediately before each HCI socket
# call.  There is deliberately no name-to-index helper here any more —
# one was how a remembered "hci1" could end up driving whichever card
# happened to hold that number later.


def _load_known_mac_types_static() -> 'dict[str, int]':
    """Read the persisted ``{mac: address_type}`` cache from disk.

    Returns an empty dict on any failure — the cache is a performance
    aid for ``Continuous scanning OFF`` mode, not a correctness
    requirement.  Cold-start with an empty cache means the controller
    sees no devices in accept-list mode; the user can switch
    ``ContinuousScan`` back on to repopulate, then off again.
    """
    try:
        with open(_KNOWN_MAC_TYPES_PATH, 'r') as f:
            raw = json.load(f)
        # Reject anything that doesn't look right rather than crashing
        # the service init on a corrupt file.
        if not isinstance(raw, dict):
            return {}
        out = {}
        for k, v in raw.items():
            if not isinstance(k, str) or len(k) != 12:
                continue
            try:
                v_int = int(v)
            except (TypeError, ValueError):
                continue
            if v_int not in (0, 1):
                continue
            out[k.lower()] = v_int
        return out
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception(f"Failed to read {_KNOWN_MAC_TYPES_PATH!r}, starting fresh")
        return {}


class DbusBleSensors(object):
    """
    Main class for the D-bus BLE Sensors python service.
    Extends base C service 'dbus-ble-sensors' to allow community integration of any BLE sensors.

    BLE advertisements are received via an HCI monitor channel tap — a passive
    read-only socket that sees ALL HCI traffic between the host and every
    Bluetooth controller (the same mechanism btmon uses).

    To make the controller actually scan, we register an AdvertisementMonitor1
    with BlueZ on each adapter.  This triggers *passive* scanning — the
    controller listens for advertisements without sending SCAN_REQ packets —
    which coexists cleanly with other services that need active scanning and
    GATT connections (e.g. power-watchdog, shyion-switch via bleak).

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/
    - https://github.com/victronenergy/node-red-contrib-victron/blob/master/src/nodes/victron-virtual.js
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/ruuvi-salon.json
    """

    def __init__(self):
        self._dbus: dbus.bus.BusConnection = get_bus("org.bluez")
        self._dbus_ble_service = DbusBleService()
        # Wire the GUI ``Continuous scanning`` toggle directly into our
        # scan-mode re-apply path.  Without this we'd still pick up
        # changes via the 60 s _scan_reenable_tick, but a GUI flip
        # would have up-to-60 s latency before the filter policy
        # actually changed on the controller.  Event-driven cuts that
        # to milliseconds.  The polling tick stays as a backstop for
        # the non-toggle reasons we need to re-apply scan params
        # (shyion-switch's bleak resetting scan policy during active
        # discovery, etc.).
        # Strangers we have already announced a refusal for.  The first
        # refusal per MAC is the audit record — it is how we prove the
        # gate actually turns strangers away.  Repeats carry no new
        # information: a neighbour's charger advertises forever, so even
        # throttled to one line per MAC per 30 minutes this grew ~46
        # lines/hour on prod across 23 neighbours, indefinitely.
        #
        # Declared here and never re-initialised later — the same
        # ordering mistake that silently emptied _configured_macs.
        self._refusal_logged: set = set()

        # MACs with stored settings: our own configured gear.  Declared
        # here and filled immediately below — NOT re-initialised later,
        # which is what silently emptied it and made the gate reject
        # every configured device on prod.
        self._configured_macs: set = set()

        # Devices we have kept before, read from stored settings.  This
        # is what lets discovery stay OFF as the normal state: our own
        # gear is already configured and keeps working, while anything
        # new is ignored until someone turns discovery on to add it.
        try:
            self._configured_macs = self._dbus_ble_service.configured_macs()
            logging.info("%d device(s) already configured in settings",
                         len(self._configured_macs))
        except Exception:
            logging.exception("could not load configured devices; "
                              "treating all as new")

        self._dbus_ble_service.register_continuous_scan_callback(
            self._on_continuous_scan_changed)
        # Passive vs active scanning re-applies through the same path:
        # both are scan-parameter changes, and both should take effect
        # on the GUI toggle rather than at the next 60 s tick.
        self._dbus_ble_service.register_active_scan_callback(
            self._on_continuous_scan_changed)

        # Settings-backed rounding policy + dedup/heartbeat publisher.
        # Constructed once here so every device driver inherits the same
        # policy via the singleton accessors (.get()).  Settings are
        # auto-created with sane defaults on first run.
        self._rounding_policy = SensorRoundingPolicy(
            self._dbus_ble_service.settings)
        self._publisher = SensorPublisher(self._rounding_policy)

        # Adapters we know about, keyed by identity (MAC, colons
        # stripped) rather than by hci<N> — the number is not stable
        # across a reset, a replug, or a reboot.  Each value carries the
        # name and path BlueZ most recently used for that card.
        self._adapters: dict[str, dict] = {}

        self._known_mac = DatedDict(ttl=DEVICE_SERVICES_TIMEOUT)
        self._ignored_mac = DatedDict(ttl=IGNORED_DEVICES_TIMEOUT)
        self._last_adv_seen: dict[str, float] = {}

        BleRole.load_classes(os.path.abspath(__file__))
        BleDevice.load_classes(os.path.abspath(__file__))

        self._internal_mfg_ids: frozenset[int] = frozenset(BleDevice.DEVICE_CLASSES.keys())
        self._known_mfg_ids: set[int] = set(self._internal_mfg_ids)

        # Name-identified devices (no manufacturer data; e.g. EasyStart).
        # The tap decodes and forwards local names only for these
        # prefixes; everything else's name is never even parsed.
        self._name_prefixes: tuple = tuple(BleDevice.NAME_CLASSES.keys())
        self._last_name_adv: dict[str, float] = {}
        self._name_accept_all_logged: bool = False
        # identity -> (tap_mac, address_type): the current address of
        # each name-identified device, refreshed on every matched
        # advertisement and injected into every adapter's accept list
        # so the device stays hearable at accept-list cost.  Loaded
        # from disk so a restart does not depend on the units speaking
        # during the grace — they can be silent for hours.
        self._name_device_macs: dict = _load_name_device_macs_static()
        if self._name_device_macs:
            logging.info("%d name-device address(es) loaded from cache",
                         len(self._name_device_macs))
        self._started_at: float = time.monotonic()
        # Full dev_ids with stored settings — the discovery gate for
        # name-identified devices, whose dev_id contains no MAC for
        # _configured_macs to find.
        self._configured_dev_ids: set = set()
        try:
            self._configured_dev_ids = \
                self._dbus_ble_service.configured_dev_ids()
        except Exception:
            logging.exception("could not load configured dev_ids")
        self._last_mfg_data: dict[str, tuple[bytes, float]] = {}
        self._tap_seen_macs: dict[str, float] = {}
        self._tap_ignored_macs: set[str] = set()
        self._last_tap_rx: float = 0.0
        self._silence_warned: bool = False
        self._tap_thread: threading.Thread | None = None
        self._tap_stop = threading.Event()
        # Adapters we've enabled scanning on, by identity key.
        # Replaces the old ``_registered_adapters`` from when we drove
        # scanning by registering a bluez AdvertisementMonitor.
        self._scan_enabled_adapters: set[str] = set()
        # Soft bt-claims announcing those adapters to the other BLE
        # services sharing these radios.  The claims layer is stdlib-only,
        # so this costs nothing at startup — unlike the bcmv2 catcher,
        # which pulls in bleak and is therefore installed lazily, on the
        # first GATT write (see ble_async_loop.start).
        self._scan_claims = ScanClaims()
        # Filter policy currently applied on each adapter — used by the
        # 60 s re-enable tick to decide whether to re-apply accept-list
        # mode (and rebuild the list) or just refresh the wide scan.
        self._scan_filter_policy: dict[str, int] = {}
        # Per-adapter count of consecutive scan-enable failures.  Used
        # only to throttle the "passive scan enable failed" warning to
        # the first occurrence in a streak (plus one every hour) — on
        # multi-controller systems it's normal for another driver
        # (bluez's own background scan for connection management, the
        # Victron VeSmart bridge, etc.) to own one adapter, leaving us
        # the other.  The HCI tap is bound to ``HCI_DEV_NONE`` so it
        # collects ads from every controller regardless of which one
        # we drive, so a failing secondary is rarely a problem in
        # practice.
        self._scan_failure_streak: dict[str, int] = {}
        # adapter key -> controller accept-list capacity (entries), read
        # once per adapter.  None means the read failed and we fall back
        # to handing that card the whole list.
        self._accept_list_size: dict[str, 'int | None'] = {}
        # Last shortfall we warned about, so a permanent overflow does not
        # log on every re-apply tick.
        self._accept_list_warned: 'tuple[int, int] | None' = None
        # Persistent cache mapping canonical MAC → BLE address type
        # (0 public / 1 random).  Populated by the HCI tap as it sees
        # ads from devices we recognise, persisted to
        # ``_KNOWN_MAC_TYPES_PATH``, used to build the controller's
        # Filter Accept List in accept-list-only mode.  See
        # :meth:`_load_known_mac_types` for bootstrap behaviour.
        self._mac_address_types: dict[str, int] = _load_known_mac_types_static()
        self._mac_address_types_dirty: bool = False

        self._router = BleAdvertisementRouter(
            self._dbus,
            version=PROCESS_VERSION,
            on_registrations_changed=self._on_registrations_changed,
        )

        # Load-driven self-throttle: pauses the HCI tap + BlueZ
        # AdvertisementMonitor registration when sustained load gets
        # close to the /etc/watchdog.conf trip threshold.  See
        # load_throttle.py for the state machine.  The active GUI
        # notification handle (if any) lives here so we can clear it
        # on release.
        self._throttle = LoadThrottle(
            on_trip=self._on_load_trip,
            on_release=self._on_load_released,
        )
        self._throttle_notification: platform_notifications.PlatformNotification | None = None
        self._throttled: bool = False

        self._list_adapters()

    def _list_adapters(self):
        self._dbus.add_signal_receiver(
            self._on_interfaces_added,
            dbus_interface='org.freedesktop.DBus.ObjectManager',
            signal_name='InterfacesAdded'
        )
        self._dbus.add_signal_receiver(
            self._on_interfaces_removed,
            dbus_interface='org.freedesktop.DBus.ObjectManager',
            signal_name='InterfacesRemoved'
        )

        object_manager = dbus.Interface(
            self._dbus.get_object('org.bluez', '/'),
            'org.freedesktop.DBus.ObjectManager'
        )
        objects = object_manager.GetManagedObjects()
        for path, ifaces in objects.items():
            self._on_interfaces_added(path, ifaces)

    # Optional adapter allow-list: if this file exists and is non-empty,
    # scanning runs ONLY on the adapters listed in it (one per line,
    # '#' comments allowed).  Absent or empty file = all adapters
    # (historical behavior).  Lets installations reserve specific
    # adapters for other BLE services (e.g. battery BMS connections)
    # that continuous scanning would destabilize.
    #
    # Entries are adapter MACs, in any spelling.  hciX names still work,
    # but they name a number rather than a card: cards get renumbered by
    # a reset, a replug, or a reboot, and a reservation that follows the
    # number instead of the card protects the wrong radio.
    ADAPTER_ALLOWLIST_PATH = '/data/apps/dbus-ble-sensors-py/adapter-allowlist.conf'

    def _adapter_name(self, key: str) -> 'str | None':
        """The hci<N> BlueZ last used for this card, for log lines only."""
        record = self._adapters.get(key)
        return record['name'] if record else None

    def _adapter_allowed(self, key, name):
        """Whether this adapter may be scanned on.

        Entries are matched by *identity*: a MAC in any spelling names the
        card itself, and an ``hci<N>`` entry is accepted as the older
        spelling.  Matching by name alone is what made this file dangerous
        — reserve ``hci1`` for a BMS, let the cards renumber, and the
        reservation now protects the wrong radio while we scan the one we
        promised to leave alone.
        """
        try:
            with open(self.ADAPTER_ALLOWLIST_PATH) as f:
                entries = [ln.split('#', 1)[0].strip() for ln in f]
        except OSError:
            return True
        return adapter_identity.allowed(entries, key, name)

    def _on_interfaces_added(self, path, interfaces):
        if not str(path).startswith('/org/bluez'):
            return
        name = path.split('/')[-1]
        if 'org.bluez.Adapter1' in interfaces:
            # The MAC has to be read before the allow-list check now, since
            # that is what the entries are matched against.  BlueZ hands it
            # to us here, so this costs nothing extra and is authoritative
            # — no hciconfig, no sysfs (which Venus does not populate).
            adapter = self._dbus.get_object('org.bluez', path)
            props = dbus.Interface(adapter, 'org.freedesktop.DBus.Properties')
            mac = str(props.Get('org.bluez.Adapter1', 'Address'))
            key = adapter_identity.mac_key(mac) or adapter_identity.canonical(name)
            if not self._adapter_allowed(key, name):
                logging.info(f"{name} ({key}): skipping adapter "
                             f"(not in adapter-allowlist.conf)")
                return
            logging.info(f"{name}: adding adapter, path={path!r}, address={mac!r}")
            known = self._adapters.get(key)
            if known is not None and known['name'] != name:
                # Same card, new number.  Everything we know about it is
                # keyed by identity, so this is a rename, not a new card.
                logging.info(f"{key}: renumbered {known['name']} -> {name}")
                adapter_identity.invalidate()
            if known is None or known['name'] != name:
                self._adapters[key] = {'name': name, 'mac': mac,
                                       'path': str(path)}
                self._dbus_ble_service.add_ble_adapter(name, mac)
                self._start_passive_scan(key)

    def _on_interfaces_removed(self, path, interfaces):
        if not str(path).startswith('/org/bluez'):
            return
        name = path.split('/')[-1]
        if 'org.bluez.Adapter1' in interfaces:
            key = next((k for k, rec in self._adapters.items()
                        if rec['name'] == name), None)
            if key is None:
                # never added (e.g. excluded by adapter-allowlist.conf) —
                # removing would raise and kill the signal handler
                return
            self._dbus_ble_service.remove_ble_adapter(name)
            self._adapters.pop(key, None)
            # Best-effort: turn off the controller's scanner before
            # bluez tears the adapter down.  If the adapter is already
            # gone the HCI socket open will fail; we swallow that.  Use
            # the name BlueZ just gave us: the card is on its way out, so
            # resolving the identity afresh would only fail.
            try:
                if name.startswith('hci') and name[3:].isdigit():
                    hci_scan_control.disable_passive_scan(int(name[3:]))
            except Exception:
                pass
            self._scan_enabled_adapters.discard(key)
            self._scan_claims.release(key)
            adapter_identity.invalidate()
            logging.info(f"{name} ({key}): adapter removed")

    def _save_known_mac_types(self) -> None:
        """Persist ``self._mac_address_types`` to disk.

        Idempotent and cheap; called when the dirty flag is set.  Uses
        an atomic write (temp file + rename) so a crash mid-flush
        doesn't leave a truncated file.
        """
        if not self._mac_address_types_dirty:
            return
        try:
            tmp = _KNOWN_MAC_TYPES_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self._mac_address_types, f)
            os.replace(tmp, _KNOWN_MAC_TYPES_PATH)
            self._mac_address_types_dirty = False
        except Exception:
            logging.exception(f"Failed to persist {_KNOWN_MAC_TYPES_PATH!r}")

    def _save_name_device_macs(self) -> None:
        """Persist the name-device address map (atomic, best-effort).

        Written on change rather than on a dirty-flag tick: it changes
        at most once per address rotation or new unit, so eagerness
        costs nothing and a crash cannot lose the only copy.
        """
        try:
            tmp = _NAME_DEVICE_MACS_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({identity: list(entry) for identity, entry
                           in self._name_device_macs.items()}, f)
            os.replace(tmp, _NAME_DEVICE_MACS_PATH)
        except Exception:
            logging.exception(f"Failed to persist {_NAME_DEVICE_MACS_PATH!r}")

    def _on_continuous_scan_changed(self, new_value: bool) -> None:
        """Called by DbusBleService when ``/Settings/BleSensors/ContinuousScan``
        changes (GUI toggle, settings-restore, anywhere).

        Re-applies the filter policy on every known adapter
        immediately, so the new mode takes effect within milliseconds
        instead of waiting for the next polling tick.  Skipped while
        ``_throttled`` is True — the load throttle will re-apply
        whichever policy is current at release time.
        """
        if self._throttled:
            logging.info(
                "ContinuousScan changed to %r while throttled; will apply "
                "on next throttle release", new_value)
            return
        logging.info("ContinuousScan changed to %r — re-applying scan policy", new_value)
        # Defer the actual re-apply to the periodic tick implementation
        # so all the per-adapter loop / failure-streak / policy-diff
        # logic stays in one place.
        self._scan_reenable_tick()

    def _desired_filter_policy(self) -> int:
        """Return the controller filter policy that matches the current
        ``/Settings/BleSensors/ContinuousScan`` setting.

        ON  (default) → ``FILTER_POLICY_ACCEPT_ALL`` — the controller
                        passes every advertisement up, just like before
                        the accept-list refactor.
        OFF           → ``FILTER_POLICY_ACCEPT_LIST_ONLY`` — the
                        controller drops advertisements whose MAC
                        isn't in the accept list we apply alongside.
        """
        try:
            return (hci_scan_control.FILTER_POLICY_ACCEPT_ALL
                    if self._dbus_ble_service.get_continuous_scan()
                    else hci_scan_control.FILTER_POLICY_ACCEPT_LIST_ONLY)
        except Exception:
            # Service init might not have populated the setting yet —
            # err on the safe side so we don't accidentally hide every
            # configured device.
            return hci_scan_control.FILTER_POLICY_ACCEPT_ALL

    def _desired_scan_type(self) -> int:
        """Passive unless ``/Settings/BleSensors/ActiveScan`` is set.

        Passive is the default because active scanning transmits a
        SCAN_REQ at every advertiser in range and holds the channel for
        the reply — measurably harder on the BMS links sharing these
        radios.  The toggle exists for devices whose payload only arrives
        in the SCAN_RSP; see ``hci_scan_control.DEFAULT_SCAN_TYPE``.

        Errs passive on any failure: the neighbourly mode is the safe one
        to fall back to.
        """
        try:
            if self._dbus_ble_service.get_active_scan():
                return hci_scan_control.SCAN_TYPE_ACTIVE
        except Exception:
            logging.exception("could not read ActiveScan setting — "
                              "staying passive")
        return hci_scan_control.SCAN_TYPE_PASSIVE

    def _start_passive_scan(self, key: str) -> None:
        """Issue HCI commands to put the adapter into passive scan mode.

        Replaces the previous BlueZ ``RegisterMonitor`` flow.  The
        controller starts scanning, advertisement reports flow through
        the kernel HCI socket, our ``HCI monitor tap`` reads them on
        ``HCI_CHANNEL_MONITOR``, and BlueZ stays completely uninvolved
        — no Device1 objects get created, no PropertiesChanged signals
        get emitted, and dbus-daemon's heap stays flat.

        Bluez's GATT path is unaffected: the rare times we (or
        shyion-switch) need a GATT session, ``bleak.connect(mac)``
        calls ``Adapter1.ConnectDevice`` which creates the bluez
        Device1 entry on demand, and bluez evicts it after disconnect.

        Honors the ``/Settings/BleSensors/ContinuousScan`` setting via
        :meth:`_desired_filter_policy`.  When ``Continuous scanning``
        is OFF, we apply the accept list as part of the same HCI
        socket open so the scan-disabled window stays minimal.
        """
        # Resolved here, immediately before the socket call, and never
        # stored: between one scan-enable and the next this card may have
        # been renumbered out from under us.
        idx = adapter_identity.index_for(key)
        label = adapter_identity.label(key, self._adapter_name(key))
        if idx is None:
            logging.warning(f"{label}: no current hci<N> for this adapter, "
                            f"cannot enable scan")
            return
        policy = self._desired_filter_policy()
        was_enabled = key in self._scan_enabled_adapters
        prev_policy = self._scan_filter_policy.get(key)
        ok = self._apply_scan_policy(key, idx, policy)
        if ok:
            self._scan_enabled_adapters.add(key)
            self._scan_claims.hold(key, exclusive=True)
            self._scan_filter_policy[key] = policy
            self._scan_failure_streak[key] = 0
            # Only log the "scan enabled" line on a real transition —
            # first enable, change of policy, or recovery from a
            # failure streak.  Steady-state re-applies stay quiet at
            # debug; otherwise this fires every ``_scan_reenable_tick``.
            if not was_enabled or prev_policy != policy:
                policy_label = self._policy_label(policy)
                mode = ("active" if self._desired_scan_type()
                        == hci_scan_control.SCAN_TYPE_ACTIVE else "passive")
                logging.info(f"{label}: {mode} scan enabled via HCI socket ({policy_label})")
            else:
                logging.debug(f"{label}: scan re-applied")
        else:
            streak = self._scan_failure_streak.get(key, 0) + 1
            self._scan_failure_streak[key] = streak
            # Loud on first failure of a streak so the user notices in
            # logs; quiet during the retry storm; loud again once an
            # hour so persistent failures aren't completely silent.
            if streak == 1 or streak % 60 == 0:
                logging.warning(
                    f"{label}: scan enable failed "
                    f"(streak={streak}); will retry on next periodic tick. "
                    "Most common cause on a Cerbo: this adapter is in LE "
                    "advertising mode (broadcasting the Cerbo as a "
                    "peripheral so the VRM app can find it), which "
                    "prevents scan-parameter changes at the controller "
                    "level.  The HCI tap is bound to HCI_DEV_NONE so it "
                    "still receives ads from whichever adapter IS "
                    "scanning, so this is usually harmless.  See "
                    "`btmgmt --index N info` for the adapter's current "
                    "settings — a `discoverable` entry indicates this is "
                    "the advertising adapter.")
            else:
                logging.debug(
                    f"{label}: scan enable failed (streak={streak})")

    def _accept_list_capacity(self, key: str) -> 'int | None':
        """Controller accept-list size for an adapter, read once and cached."""
        if key in self._accept_list_size:
            return self._accept_list_size[key]
        idx = adapter_identity.index_for(key)
        size = None
        if idx is not None:
            try:
                size = hci_scan_control.read_accept_list_size(idx)
            except Exception:
                logging.exception("%s: accept-list size read failed",
                                  adapter_identity.label(key))
        if not size:
            size = None
        self._accept_list_size[key] = size
        return size

    def _accept_list_for(self, key: str, devices: list) -> list:
        """The slice of *devices* this adapter should watch for.

        Falls back to the whole list if any adapter's capacity is unknown
        — that is exactly the historical behaviour, and it is better to
        overlap than to leave a device assigned to no card at all.
        """
        keys = sorted(self._adapters)
        if key not in keys:
            return devices
        capacities = {k: self._accept_list_capacity(k) for k in keys}
        if any(capacities[k] is None for k in keys):
            return devices
        slices = hci_scan_control.accept_list_slices(
            keys, capacities, len(devices))
        covered = sum(count for _off, count in slices.values())
        if covered < len(devices):
            state = (len(devices), covered)
            if self._accept_list_warned != state:
                self._accept_list_warned = state
                detail = ", ".join(
                    f"{adapter_identity.label(k)}={capacities[k]}" for k in keys)
                logging.warning(
                    f"accept-list capacity exceeded: {len(devices)} known "
                    f"devices, room for {covered} across the adapters we scan "
                    f"on ({detail}).  {len(devices) - covered} device(s) will "
                    "not be heard while Continuous scanning is OFF — turn it "
                    "on, or scan on another adapter, or prune the cache.")
        else:
            self._accept_list_warned = None
        offset, count = slices[key]
        return devices[offset:offset + count]

    def _policy_label(self, policy: int) -> str:
        """Human label for what a policy request actually puts on the radio.

        An accept-list request lands as accept-all when a configured
        name-identified device exists (see ``_apply_scan_policy``); the
        label must describe the radio, not the request, or the log
        claims a filter that is not in force.
        """
        if policy == hci_scan_control.FILTER_POLICY_ACCEPT_ALL:
            return "accept-all"
        if self._has_configured_name_devices():
            return "accept-list-only (+name-device addresses)"
        return "accept-list-only"

    def _name_grace_active(self) -> bool:
        """Whether the post-restart wide-listening grace is in force.

        After a restart no current address is known for any name device;
        idle units advertise, so listening wide briefly converges in
        seconds.  Once an address is learned (or the grace expires) the
        radios stay accept-list — there is no periodic wide listening,
        by choice: the field units hold fixed public addresses, and the
        rotation insurance is deferred until rotation is ever observed.
        """
        return (not self._name_device_macs
                and time.monotonic() - self._started_at
                < NAME_STARTUP_GRACE_S)

    def _has_configured_name_devices(self) -> bool:
        """Whether any name-identified (rotating-MAC) device is configured.

        Matches stored dev_ids against the DEV_ID_PREFIXES each
        name-identified device class declares.  Devices adopted during
        this run are included — their dev_id is added to
        ``_configured_dev_ids`` at adoption.
        """
        prefixes = tuple(
            prefix
            for cls in BleDevice.NAME_CLASSES.values()
            for prefix in getattr(cls, 'DEV_ID_PREFIXES', ()))
        if not prefixes:
            return False
        return any(dev_id.startswith(prefixes)
                   for dev_id in self._configured_dev_ids)

    def _apply_scan_policy(self, key: str, adapter_index: int,
                           policy: int) -> bool:
        """Apply a scan filter policy on the given adapter.

        ``FILTER_POLICY_ACCEPT_ALL`` is just the plain enable; the
        accept list is irrelevant.  ``FILTER_POLICY_ACCEPT_LIST_ONLY``
        rebuilds the accept list from our persisted MAC cache and
        applies it atomically with the policy change.

        If accept-list mode is requested but the cache is empty, we
        log a warning and fall back to ``FILTER_POLICY_ACCEPT_ALL``
        rather than leave the controller refusing every advertisement
        — the user can always run with ``ContinuousScan = ON`` long
        enough to populate the cache.
        """
        if policy == hci_scan_control.FILTER_POLICY_ACCEPT_LIST_ONLY:
            # Name-identified devices (EasyStart) ride in the accept
            # list by their last-heard address, injected on every
            # adapter below.  After a restart no address is known yet,
            # so listen wide until one is heard (bounded grace).
            # ContinuousScan OFF still closes the ADOPTION gate
            # throughout.  See NAME_STARTUP_GRACE_S for why there is
            # deliberately no periodic wide listening beyond the grace.
            named = self._has_configured_name_devices()
            if named and self._name_grace_active():
                logging.debug(
                    "%s: startup grace — listening wide until a "
                    "name-device address is learned",
                    adapter_identity.label(key))
                return hci_scan_control.enable_scan(
                    adapter_index,
                    filter_policy=hci_scan_control.FILTER_POLICY_ACCEPT_ALL,
                    scan_type=self._desired_scan_type(),
                )
            if named and not self._name_accept_all_logged:
                self._name_accept_all_logged = True
                logging.info(
                    "configured name-identified device(s): accept list "
                    "includes their last-heard address on every adapter; "
                    "discovery/adoption stays off per ContinuousScan")
            devices = sorted(self._mac_address_types.items())
            if not devices and not self._name_device_macs:
                logging.warning(
                    f"hci{adapter_index}: accept-list mode requested but cache "
                    "is empty — falling back to accept-all.  Re-enable "
                    "Continuous Scanning briefly to populate."
                )
                return hci_scan_control.enable_scan(
                    adapter_index,
                    filter_policy=hci_scan_control.FILTER_POLICY_ACCEPT_ALL,
                    scan_type=self._desired_scan_type(),
                )
            mine = self._accept_list_for(key, devices)
            if named and self._name_device_macs:
                # Name-device addresses go on EVERY adapter, after the
                # capacity slicing: a 1-2 m device sliced onto the far
                # radio would never be heard at all.
                have = {mac for mac, _t in mine}
                mine = mine + sorted(
                    entry for entry in self._name_device_macs.values()
                    if entry[0] not in have)
            logging.debug("%s: accept list %d of %d known devices",
                          adapter_identity.label(key), len(mine), len(devices))
            return hci_scan_control.apply_accept_list(
                adapter_index, mine, scan_type=self._desired_scan_type())
        return hci_scan_control.enable_scan(
            adapter_index, filter_policy=policy,
            scan_type=self._desired_scan_type())

    def _scan_reenable_tick(self) -> bool:
        """Re-issue the scan-enable HCI commands on every known adapter.

        Returns True so the GLib timer keeps firing.

        Other services on the system (notably ``shyion-switch``) can
        reset the controller's scan parameters when they do their own
        active discovery.  Re-issuing the disable→params→enable
        sequence brings us back to passive mode within at most one
        tick.  When scanning is already in the requested state, the
        controller returns Command Disallowed (0x0C) on the disable
        step and the parameter/enable steps proceed normally.

        This tick also:
          * Detects ``ContinuousScan`` setting flips and switches
            filter policy accordingly (so the GUI toggle has effect
            within at most one tick).
          * Flushes the persistent ``mac_address_types`` cache to
            disk if it's been updated since the last flush.

        Skipped while ``_throttled`` is True — the load-throttle
        explicitly disabled scanning, the throttle release path will
        re-enable when load drops.
        """
        if self._throttled:
            return True
        desired = self._desired_filter_policy()
        for key in list(self._adapters):
            idx = adapter_identity.index_for(key)
            if idx is None:
                continue
            current = self._scan_filter_policy.get(key)
            if current != desired:
                # Policy change — go through the full apply path so
                # accept-list rebuild + scan-params update happen as
                # one transaction.
                if self._apply_scan_policy(key, idx, desired):
                    self._scan_enabled_adapters.add(key)
                    self._scan_claims.hold(key, exclusive=True)
                    self._scan_filter_policy[key] = desired
                    logging.info(f"{adapter_identity.label(key, self._adapter_name(key))}: "
                                 f"scan filter policy switched to "
                                 f"{self._policy_label(desired)}")
            else:
                # Steady-state re-issue using the policy we already
                # have.  For accept-list mode, also re-apply the list
                # in case it changed (new devices learned via the tap).
                if self._apply_scan_policy(key, idx, desired):
                    self._scan_enabled_adapters.add(key)
                    self._scan_claims.hold(key, exclusive=True)
        # Flush persisted cache once per tick if anything changed.
        self._save_known_mac_types()
        return True

    def _process_advertisement(self, dev_mac: str, manufacturer_data: dict[int, bytes],
                               adapter_index: int = 0, rssi: int = 0,
                               address_type: int = 0):
        """Process a single BLE advertisement (called on the GLib main thread).

        Each (mfg_id, data) pair is offered to both the internal device class
        system and the external advertisement router.  A MAC is only added to
        the ignore list when *neither* system is interested.
        """
        if dev_mac in self._ignored_mac:
            if dev_mac not in self._known_mac:
                return
            del self._ignored_mac[dev_mac]
            logging.debug(f"{dev_mac}: recovered known device from ignored list")

        # Publish the CARD, not its number.  The tap hands us a kernel
        # adapter index; formatting that as "hciN" and putting it on a
        # D-Bus signal hands every subscriber a value that means a
        # different radio after a replug — the identity problem we fixed
        # everywhere else, exported.
        #
        # This is the cached direction on purpose: hciN -> MAC is a hot
        # lookup whose answer rarely changes (30 s TTL in the backend,
        # ~19us), unlike MAC -> hciN, where staleness is the hazard and
        # index_for resolves fresh.  Same table, opposite needs.
        #
        # A card whose MAC cannot be read degrades to its hciN name
        # rather than dropping the field.
        adapter_name = adapter_identity.canonical(f"hci{adapter_index}")

        for man_id, man_data in manufacturer_data.items():
            routed = self._router.process_advertisement(
                dev_mac, man_id, man_data, rssi, adapter_name)

            if dev_mac not in self._known_mac:
                self.snif_data(man_id, man_data)

                # Victron manufacturer id 0x02E1: Orion-TR, IP22, SmartShunt, or SolarSense
                if man_id == 0x02E1 and is_orion_tr_manufacturer_data(man_data):
                    device_class = BleDeviceOrionTR
                elif man_id == 0x02E1 and is_ip22_charger_manufacturer_data(man_data):
                    device_class = BleDeviceIP22Charger
                elif man_id == 0x02E1 and is_smartshunt_manufacturer_data(man_data):
                    device_class = BleDeviceSmartShunt
                else:
                    device_class = BleDevice.DEVICE_CLASSES.get(man_id, None)
                if device_class is None:
                    if not routed:
                        now = time.monotonic()
                        if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                            logging.info(f"{dev_mac}: ignoring manufacturer {man_id:#06x}, no device class")
                        self._last_adv_seen[dev_mac] = now
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue

                # Discovery gate.  With Continuous scanning OFF we adopt
                # nothing new: an unknown MAC is ignored rather than
                # turned into a device object.  Devices we have
                # configured before are unaffected — they are in
                # ``_configured_macs`` from their stored settings, so
                # turning discovery off never blinds us to our own gear.
                #
                # Without this, a neighbour's charger or shunt became a
                # full device object on first sight, and (before the
                # session gate below) that object immediately opened a
                # GATT connection to hardware nobody had enabled.  On
                # the prod gateway that produced 59 disabled entries and
                # 139 discovery bursts for one unreachable shunt.
                if (dev_mac not in self._configured_macs
                        and not self._dbus_ble_service.get_continuous_scan()):
                    now = time.monotonic()
                    if dev_mac not in self._refusal_logged:
                        self._refusal_logged.add(dev_mac)
                        logging.info(
                            f"{dev_mac}: not adopting — discovery is off "
                            f"and this device has no stored settings")
                    elif now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                        logging.debug(
                            f"{dev_mac}: still not adopting — discovery "
                            f"is off and this device has no stored settings")
                    self._last_adv_seen[dev_mac] = now
                    self._ignored_mac[dev_mac] = True
                    self._tap_ignored_macs.add(dev_mac)
                    continue

                # One INFO line per device is emitted at registration, in
                # DbusRoleService.connect, carrying the instance.  This
                # earlier step is DEBUG: four lines per device per restart
                # was ~30% of prod output over 94 h.
                logging.debug(f"{dev_mac}: initializing device with class {device_class}")
                try:
                    dev_instance = device_class(dev_mac)
                    if not dev_instance.check_manufacturer_data(man_data):
                        logging.info(
                            f"{dev_mac}: manufacturer data check failed for "
                            f"{device_class.__name__}, ignoring")
                        if not routed:
                            self._ignored_mac[dev_mac] = True
                            self._tap_ignored_macs.add(dev_mac)
                        continue
                    dev_instance.configure(man_data)
                    dev_instance.init()
                    self._known_mac[dev_mac] = dev_instance
                    self._configured_macs.add(dev_mac)
                    # Newly-configured device — remember its BLE
                    # address type so we can put it in the controller's
                    # accept list when ``Continuous scanning`` is OFF.
                    # Address type is fixed for a given peripheral
                    # (random-static or public), so we only need to
                    # record it once.
                    if address_type in (0, 1):
                        prev = self._mac_address_types.get(dev_mac)
                        if prev != address_type:
                            self._mac_address_types[dev_mac] = address_type
                            self._mac_address_types_dirty = True
                except ValueError as exc:
                    logging.info(f"{dev_mac}: device configuration invalid for "
                                 f"{device_class.__name__}: {exc}")
                    if not routed:
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue
                except Exception:
                    logging.exception(f"{dev_mac}: unexpected error during device initialization")
                    if not routed:
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue
            else:
                dev_instance = self._known_mac[dev_mac]

            now = time.monotonic()
            # One INFO line per device per quiet period; everything in
            # between goes to debug.  A device advertises every second or
            # two, so anything unconditional here is a log flood.
            verbose = (now - self._last_adv_seen.get(dev_mac, 0)
                       >= ADV_LOG_QUIET_PERIOD)
            if verbose:
                logging.info(f"{dev_mac}: received manufacturer data: {man_data!r}")
            else:
                logging.debug(f"{dev_mac}: received manufacturer data: {man_data!r}")
            self._last_adv_seen[dev_mac] = now
            if dev_instance.check_manufacturer_data(man_data):
                dev_instance.handle_manufacturer_data(man_data)
            elif verbose:
                logging.info(f"{dev_mac}: ignoring manufacturer data due to data check")
            else:
                logging.debug(f"{dev_mac}: ignoring manufacturer data due to data check")

    def _glib_process_tap(self, adv: TappedAdvertisement):
        """GLib idle callback — bridges from tap thread to main thread."""
        try:
            self._process_advertisement(adv.mac, adv.manufacturer_data,
                                        adv.adapter_index, adv.rssi,
                                        address_type=adv.address_type)
        except Exception:
            logging.exception(f"Error processing tap advertisement from {adv.mac}")
        return False

    def _glib_process_name_tap(self, adv: TappedAdvertisement):
        """GLib idle callback for name-identified advertisements."""
        try:
            self._process_name_advertisement(adv.mac, adv.local_name,
                                             adv.rssi, adv.address_type,
                                             adv.adapter_index)
        except Exception:
            logging.exception(
                f"Error processing name advertisement from {adv.mac}")
        return False

    def _process_name_advertisement(self, tap_mac: str, adv_name: str,
                                    rssi: int, address_type: int = 1,
                                    adapter_index: int = 0):
        """Route a name-identified advertisement (GLib main thread).

        These devices (Micro-Air EasyStart) rotate their advertised MAC,
        so the device store is keyed by an identity derived from the
        advertised *name*; the MAC heard right now is handed to the
        driver purely as the address for its next connection.  Nothing
        here touches the accept-list MAC cache — persisting a rotating
        MAC would fill it with dead entries.
        """
        device_class = None
        for prefix, cls in BleDevice.NAME_CLASSES.items():
            if adv_name.startswith(prefix):
                device_class = cls
                break
        if device_class is None:
            return

        identity = device_class.identity_from_name(adv_name)
        mac = ':'.join(tap_mac[i:i + 2] for i in range(0, 12, 2)).upper()

        dev_instance = self._known_mac.get(identity)
        if dev_instance is None:
            # Same discovery gate as the manufacturer path: with
            # Continuous scanning OFF we adopt nothing new.  Configured
            # name devices have a stored dev_id ending in the identity.
            configured = any(dev_id == identity or
                             dev_id.endswith('_' + identity)
                             for dev_id in self._configured_dev_ids)
            if (not configured
                    and not self._dbus_ble_service.get_continuous_scan()):
                if identity not in self._refusal_logged:
                    self._refusal_logged.add(identity)
                    logging.info(
                        f"{identity}: not adopting — discovery is off "
                        f"and this device has no stored settings")
                return

            logging.debug(f"{identity}: initializing name-identified device "
                          f"with class {device_class} (currently at {mac})")
            try:
                dev_instance = device_class(identity)
                dev_instance.configure(b'')
                dev_instance.init()
            except Exception:
                logging.exception(
                    f"{identity}: unexpected error during device "
                    f"initialization")
                return
            self._known_mac[identity] = dev_instance
            # Its settings exist now, so scan-policy decisions made
            # before the next restart must see it as configured.
            self._configured_dev_ids.add(dev_instance.info['dev_id'])

        # Track the device's current address for the accept lists —
        # refreshed on every matched advertisement, persisted on change
        # so the next restart hears the unit from its first second.
        entry = (tap_mac, address_type if address_type in (0, 1) else 1)
        if self._name_device_macs.get(identity) != entry:
            self._name_device_macs[identity] = entry
            self._save_name_device_macs()

        try:
            dev_instance.handle_name_advertisement(mac, adv_name, rssi,
                                                   address_type,
                                                   adapter_index)
        except Exception:
            logging.exception(f"{identity}: error handling advertisement")

    def _start_tap(self):
        """Start the HCI monitor tap in a background thread.

        The tap uses HCI_CHANNEL_MONITOR which sees ALL adapters (bound to
        HCI_DEV_NONE) — no need to wait for D-Bus adapter enumeration.
        """
        try:
            tap_sock = create_tap_socket()
        except OSError as exc:
            logging.error(f"Cannot open HCI monitor socket: {exc}")
            logging.error("No advertisement source available — service cannot function")
            return

        known_mfg_ids = self._known_mfg_ids
        last_mfg_data = self._last_mfg_data
        tap_seen = self._tap_seen_macs

        def _on_advertisement(adv: TappedAdvertisement):
            if not adv.manufacturer_data and not adv.local_name:
                return
            now = time.monotonic()
            self._last_tap_rx = now
            self._silence_warned = False
            mac = adv.mac
            tap_seen[mac] = now
            if adv.local_name is not None:
                # Presence signal for a name-identified device — no
                # payload to dedup, so rate-limit per MAC instead.
                if now - self._last_name_adv.get(mac, 0.0) \
                        >= NAME_ADV_MIN_INTERVAL:
                    self._last_name_adv[mac] = now
                    GLib.idle_add(self._glib_process_name_tap, adv)
            for mfg_id in adv.manufacturer_data:
                raw = adv.manufacturer_data[mfg_id]
                prev = last_mfg_data.get(mac)
                if prev is not None:
                    prev_data, prev_ts = prev
                    hb = self._rounding_policy.heartbeat_seconds
                    if prev_data == raw and (hb <= 0 or now - prev_ts < hb):
                        return
                last_mfg_data[mac] = (raw, now)
                GLib.idle_add(self._glib_process_tap, adv)
                return

        def _tap_thread():
            try:
                run_tap_loop(tap_sock, _on_advertisement, self._tap_stop,
                             mfg_filter=known_mfg_ids,
                             ignored_macs=self._tap_ignored_macs,
                             name_prefixes=self._name_prefixes or None)
            except Exception:
                logging.exception("HCI monitor tap thread died")

        self._tap_stop.clear()
        t = threading.Thread(target=_tap_thread, daemon=True, name="hci-monitor-tap")
        t.start()
        self._tap_thread = t
        self._last_tap_rx = time.monotonic()
        logging.info("HCI monitor tap started")

    def _restore_name_devices(self) -> None:
        """Recreate configured name-identified devices at startup.

        A device normally springs into existence on its first
        advertisement.  That is wrong for a device whose normal state is
        silence: an EasyStart is unpowered whenever its A/C is off, so
        after a restart it would be missing from the GUI — no acload
        service, no settings entry, nothing to rename — until the A/C
        happened to run, which can be many hours.  It looks broken while
        being perfectly healthy.

        Everything needed to rebuild it is already persisted: the
        settings say which units are configured, and the address cache
        says where each one was last heard.  So rebuild them here and
        publish the off-state, exactly as a session ending would.
        """
        if not BleDevice.NAME_CLASSES:
            return
        for dev_id in sorted(self._configured_dev_ids):
            device_class = None
            for cls in BleDevice.NAME_CLASSES.values():
                prefixes = tuple(getattr(cls, 'DEV_ID_PREFIXES', ()))
                if prefixes and dev_id.startswith(prefixes):
                    device_class = cls
                    break
            if device_class is None:
                continue
            # dev_id is f"{dev_prefix}_{identity}"; recover the identity.
            parts = dev_id.split('_', 1)
            if len(parts) != 2:
                continue
            identity = parts[1]
            if identity in self._known_mac:
                continue
            try:
                dev_instance = device_class(identity)
                dev_instance.configure(b'')
                dev_instance.init()
                self._known_mac[identity] = dev_instance
                logging.info(
                    f"{identity}: restored from stored settings "
                    f"(silent until its A/C runs)")
                offline = getattr(dev_instance, '_publish_offline', None)
                if offline is not None:
                    offline()
            except Exception:
                logging.exception(
                    f"{identity}: could not restore from stored settings")

    def start(self):
        """Start the service: open the tap immediately, begin pruning timer."""
        self._restore_name_devices()
        self._start_tap()
        self._router.start()
        GLib.timeout_add_seconds(30, self._prune_tick)
        # Tick the load throttle every 30s on the GLib mainloop.
        # ``LoadThrottle.tick`` always returns True so the timer
        # persists for the life of the process.
        GLib.timeout_add_seconds(30, self._throttle.tick)
        # Periodic recovery of passive scan: re-issue the HCI
        # disable→params→enable sequence in case another service did
        # an active discovery and reset our scan parameters.  Worst-
        # case recovery latency = _SCAN_REENABLE_INTERVAL_S.
        GLib.timeout_add_seconds(_SCAN_REENABLE_INTERVAL_S, self._scan_reenable_tick)
        # Bridge the active BMS's charge limits onto local charger roles'
        # /Link paths - systemcalc's DVCC does not drive
        # com.victronenergy.charger services. Runs on its own thread, NOT a
        # GLib timer: see bms_link_follower.py for the mainloop-deadlock
        # rationale.
        from bms_link_follower import BmsLinkFollower

        self._bms_link_follower = BmsLinkFollower()
        self._bms_link_follower.start()

    # ── Load-driven throttle ──────────────────────────────────────────────

    def _stop_passive_scan_all(self) -> None:
        """Disable LE scanning on every adapter we'd enabled.

        Called from the load-throttle trip path so the controller
        stops draining radio + CPU during high-load conditions.  The
        re-enable tick is also gated on ``_throttled`` so it won't
        fight the disable.
        """
        for key in list(self._scan_enabled_adapters):
            idx = adapter_identity.index_for(key)
            if idx is None:
                continue
            if hci_scan_control.disable_passive_scan(idx):
                logging.info(f"{adapter_identity.label(key, self._adapter_name(key))}: "
                             f"scan disabled (throttle)")
        self._scan_enabled_adapters.clear()
        # We are off the air: stop telling everyone else these cards are
        # busy, so a BMS link can be placed on one while we sit out.
        self._scan_claims.release_all()

    def _on_load_trip(self, load_5m: float, load_15m: float) -> None:
        """Called by LoadThrottle when load crosses the trip threshold.

        Stops the HCI tap thread (releases its CPU + closes the kernel
        socket), disables the controller's LE scan via HCI commands,
        and pushes a warning notification to the GUI.
        """
        self._throttled = True

        # Stop the HCI tap thread.  ``run_tap_loop`` checks the stop
        # event between recvs and returns cleanly; the socket closes
        # when the thread exits.
        self._tap_stop.set()
        # _prune_tick will not restart the tap while _throttled is True
        # (see the change in _prune_tick below).
        self._tap_thread = None

        # Tell the controller to stop scanning.
        self._stop_passive_scan_all()

        # Surface a warning notification to the Cerbo GUI.
        try:
            self._throttle_notification = platform_notifications.inject(
                self._dbus,
                type_id=platform_notifications.TYPE_WARNING,
                device_name="BLE Sensors",
                description="High system load — BLE updates paused",
            )
            self._throttle_notification.activate()
        except Exception:
            logging.exception("Failed to publish throttle warning notification")
            self._throttle_notification = None

    def _on_load_released(self, load_5m: float, load_15m: float) -> None:
        """Called by LoadThrottle when load drops back below the release.

        Restarts the HCI tap, re-enables passive scanning on every
        known adapter, and dismisses the GUI notification (it stays in
        the history list for later review).
        """
        self._throttled = False

        # Restart the tap.  _start_tap re-clears the event and spawns
        # a fresh daemon thread.
        self._start_tap()

        # Eagerly re-enable scanning on each adapter; the periodic
        # _scan_reenable_tick would also pick this up, but doing it
        # here minimises the recovery gap.
        for key in list(self._adapters):
            self._start_passive_scan(key)

        if self._throttle_notification is not None:
            try:
                self._throttle_notification.dismiss()
            except Exception:
                logging.exception("Failed to dismiss throttle notification")
            self._throttle_notification = None

    def _on_registrations_changed(self):
        """Called by the router when external registrations change.

        Mutates the tap manufacturer-ID filter in place (the tap thread holds
        a reference to the same set object) and clears MACs from the
        suppression lists when a new MAC-level registration matches them.
        """
        external_ids = self._router.get_registered_mfg_ids()
        new_ids = self._internal_mfg_ids | external_ids
        self._known_mfg_ids.update(new_ids)
        stale = self._known_mfg_ids - new_ids
        if stale:
            self._known_mfg_ids.difference_update(stale)
        logging.info("Tap mfg filter updated: %d IDs (%d internal + %d external)",
                     len(self._known_mfg_ids), len(self._internal_mfg_ids),
                     len(external_ids))

        registered_macs = self._router.get_registered_macs()
        if not registered_macs:
            return

        to_unsuppress: list[str] = []
        for mac in list(self._ignored_mac):
            if mac in registered_macs:
                to_unsuppress.append(mac)

        for mac in to_unsuppress:
            del self._ignored_mac[mac]
            self._tap_ignored_macs.discard(mac)
            self._last_mfg_data.pop(mac, None)

        if to_unsuppress:
            logging.info("Unsuppressed %d MAC(s) due to new MAC registrations", len(to_unsuppress))

    def _prune_tick(self):
        """GLib timer callback — prune caches, check tap health."""
        # Refresh TTLs for devices the tap thread has seen since last tick,
        # even if their data was deduplicated and not forwarded to _process_advertisement.
        seen = self._tap_seen_macs
        for mac in list(seen):
            if mac in self._known_mac:
                _ = self._known_mac[mac]  # __getitem__ refreshes TTL

        # A device holding a live GATT session (EasyStart) stops
        # advertising while connected, so the tap-driven refresh above
        # never fires for it.  Peek without refreshing, then touch only
        # the busy ones — a blanket items() walk would refresh every TTL
        # and break expiry entirely.
        # Devices whose normal state is silence (EasyStart: unpowered
        # whenever its A/C is off) must also survive, or they vanish
        # from the GUI — service AND settings entry — on a healthy box.
        # Gated on having stored settings so a stranger adopted during a
        # discovery window can still age out.
        for key, (value, _ts) in list(self._known_mac._store.items()):
            busy = getattr(value, 'is_busy', None)
            if busy is not None and busy():
                _ = self._known_mac[key]
                continue
            survives = getattr(value, 'survives_silence', None)
            if survives is None or not survives():
                continue
            dev_id = (value.info or {}).get('dev_id')
            if dev_id and dev_id in self._configured_dev_ids:
                _ = self._known_mac[key]

        self._known_mac.prune()
        self._ignored_mac.prune()

        # Sync tap-level MAC filter: remove entries that expired from
        # _ignored_mac or were promoted to _known_mac.
        stale_ignored = [
            mac for mac in self._tap_ignored_macs
            if mac not in self._ignored_mac or mac in self._known_mac
        ]
        for mac in stale_ignored:
            self._tap_ignored_macs.discard(mac)

        now = time.monotonic()

        # Prune stale entries from dedup and log-throttle dicts
        stale_macs = [
            mac for mac, ts in self._last_adv_seen.items()
            if now - ts > DEVICE_SERVICES_TIMEOUT
        ]
        for mac in stale_macs:
            self._last_adv_seen.pop(mac, None)
            self._last_mfg_data.pop(mac, None)

        stale_names = [
            mac for mac, ts in self._last_name_adv.items()
            if now - ts > DEVICE_SERVICES_TIMEOUT
        ]
        for mac in stale_names:
            self._last_name_adv.pop(mac, None)

        # Tap thread watchdog: restart if it died.  Skip while the
        # load throttle has us paused — the throttle deliberately
        # tore the tap down, and will re-start it on release.
        if not self._throttled:
            if self._tap_thread is not None and not self._tap_thread.is_alive():
                logging.warning("HCI monitor tap thread is dead — restarting")
                self._tap_thread = None
                self._start_tap()

            # Re-enable passive scan on any adapter that lost it.  The
            # periodic _scan_reenable_tick covers this on a 60 s
            # cadence; this is the eager path for the more frequent
            # _prune_tick (30 s).
            for key in list(self._adapters):
                if key not in self._scan_enabled_adapters:
                    self._start_passive_scan(key)

        # Silence detection: force a scan re-enable if no ads for 5 min
        if self._last_tap_rx > 0 and now - self._last_tap_rx > SILENCE_WARNING_SECONDS:
            if not self._silence_warned:
                logging.warning(
                    f"No matching advertisements received for "
                    f"{int(now - self._last_tap_rx)}s — re-enabling passive scan")
                # Drop our cached "scan is enabled" markers so the
                # next _prune_tick / _scan_reenable_tick re-issues the
                # HCI commands.
                self._scan_enabled_adapters.clear()
                self._silence_warned = True

        return True

    def snif_data(self, man_id: int, man_data: bytes):
        man_name = MAN_NAMES.get(man_id, hex(man_id).upper())
        SNIF_LOGGER.info(f"{man_name!r}: {man_data!r}")

class DatedDict(MutableMapping):
    """
    Dict keeping timestamps for each entries so that older ones can be purged.
    Refreshes timestamp on read. Manual pruning required.
    """

    def __init__(self, ttl):
        self.ttl = ttl
        self._store = {}

    def _now(self): return time.monotonic()

    def __setitem__(self, key, value):
        self._store[key] = (value, self._now() + self.ttl)

    def __getitem__(self, key):
        value, _ = self._store[key]
        self._store[key] = (value, self._now() + self.ttl)
        return value

    def __delitem__(self, key):
        del self._store[key]

    def __iter__(self):
        return iter(self._store.keys())

    def __len__(self):
        return len(self._store)

    def __contains__(self, key):
        contains = key in self._store
        if contains:
            self[key]
        return contains

    def prune(self):
        now = self._now()
        for key in list(self._store.keys()):
            value, expire_time = self._store[key]
            if expire_time <= now:
                if getattr(value, 'delete', None):
                    value.delete()
                del self._store[key]

    def keys(self):
        return self._store.keys()

def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('--version', '-v', action='version', version=PROCESS_VERSION)
    parser.add_argument('--debug', '-d', help='Turn on debug logging', default=False, action='store_true')
    parser.add_argument('--snif', '-s', help='Turn on advertising data sniffer', default=False, action='store_true')
    args = parser.parse_args()

    setup_logging(args.debug)
    # vedbus registers every service at INFO on the root logger; see
    # log_filters for why that is filtered rather than re-levelled.
    log_filters.install(args.debug)

    if args.snif:
        handler = RotatingFileHandler(
            "/var/log/dbus-ble-sensors-py/sniffer.log",
            maxBytes=512 * 1024,
            backupCount=0,
            encoding="utf-8",
            delay=True
        )
        handler.setFormatter(logging.Formatter(fmt='%(message)s'))
        SNIF_LOGGER.addHandler(handler)

    # Immediate exit on SIGTERM so the OS closes all file descriptors and
    # the D-Bus daemon detects the disconnect cleanly.
    import signal
    signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))

    DBusGMainLoop(set_as_default=True)

    service = DbusBleSensors()
    service.start()

    logging.info('Starting service')
    GLib.MainLoop().run()

if __name__ == "__main__":
    main()
