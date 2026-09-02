# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The two BlueZ jobs bleak cannot do for us, over dbus-python.

Both are deliberately kept on the GLib main thread — dbus-python and
bleak's ``dbus_fast`` connection are separate clients of the same bus,
and keeping each on its own thread is what makes the pairing handshake
work: the BLE loop thread blocks awaiting ``Device1.Pair()`` while the
GLib thread dispatches the passkey request back to BlueZ.

1. **Device lookup.**  Which ``Device1`` objects BlueZ already has, so a
   connect does not have to scan to find one.  See :mod:`ble_gatt_link`
   for why avoiding that scan is the whole point.
2. **The pairing agent.**  Victron peripherals want a passkey, BlueZ
   wants an ``org.bluez.Agent1`` to supply it, and bleak registers none.
   Without an agent, BlueZ auto-pairs with the wrong IO capability
   (``DisplayOnly``) and firmwares that require an authenticated link
   reject the write with "Invalid parameters".
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import dbus
import dbus.service
from gi.repository import GLib

import adapter_identity
import ble_catcher

logger = logging.getLogger(__name__)

AGENT_INTERFACE = "org.bluez.Agent1"
DEVICE_INTERFACE = "org.bluez.Device1"

# KeyboardDisplay, not DisplayOnly: the capability Victron's SMP exchange
# expects from a host that can supply a passkey.
AGENT_CAPABILITY = "KeyboardDisplay"


def _plain(value):
    """Convert dbus-python types to plain Python.

    The property dict crosses into bleak, which is a ``dbus_fast``
    consumer and has no reason to understand dbus-python's subclasses.
    """
    if isinstance(value, dbus.ByteArray):
        return bytes(value)
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, (dbus.Int16, dbus.Int32, dbus.Int64, dbus.UInt16,
                          dbus.UInt32, dbus.UInt64, dbus.Byte)):
        return int(value)
    if isinstance(value, dbus.Double):
        return float(value)
    if isinstance(value, dbus.String) or isinstance(value, dbus.ObjectPath):
        return str(value)
    if isinstance(value, dbus.Dictionary) or isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (dbus.Array, list, tuple)):
        return [_plain(v) for v in value]
    return value


# Devices already reported as linking outside the configured pool.
# Module scope so it survives per-call and per-writer lifetimes; the
# condition is standing, so one line per device per process is the whole
# of its information content.
_warned_out_of_pool: set[str] = set()


def disconnect_stale_links(bus, addresses) -> list:
    """Disconnect links to OUR devices that a previous life left behind.

    Returns ``[(path, address), ...]`` for each link dropped.

    A ``svc -t`` ends this process with ``os._exit(0)``; a crash or a
    kill ends it with nothing at all.  Either way bleak never sends
    Disconnect, and bluetoothd keeps the LE link up with no client
    behind it.  A connected peripheral does not advertise, so the next
    life cannot hear the device on ANY card — it logs "silent until its
    A/C runs" while the A/C is running.  Prod, 2026-09-02: easystart_89fe
    dark for ~17 minutes after a restart, connected on hci9 to a process
    that no longer existed.

    Runs at startup, before the tap opens.  At that moment this process
    holds nothing, so any link to one of *addresses* is stale by
    construction — there is no live-claim check to get wrong.  Every
    adapter is swept, not just the pool: the orphan above was on a card
    outside both the allowlist and ble-connect.conf.  Only our own
    addresses are touched; another consumer's link is never ours to
    drop.

    Best effort throughout: a failure to reach BlueZ, or to disconnect
    one device, must not stop the service from starting.
    """
    wanted = {str(a).replace(":", "").lower() for a in addresses if a}
    if not wanted:
        return []
    try:
        om = dbus.Interface(
            bus.get_object("org.bluez", "/", introspect=False),
            "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
    except Exception:
        logger.exception("stale-link sweep: BlueZ object lookup failed; "
                         "starting without it")
        return []

    dropped = []
    for path, interfaces in objects.items():
        path = str(path)
        if DEVICE_INTERFACE not in interfaces:
            continue
        props = _plain(interfaces[DEVICE_INTERFACE])
        if not props.get("Connected"):
            continue
        address = str(props.get("Address", ""))
        if address.replace(":", "").lower() not in wanted:
            continue
        try:
            dbus.Interface(bus.get_object("org.bluez", path, introspect=False),
                           DEVICE_INTERFACE).Disconnect()
        except Exception as exc:
            logger.warning("stale-link sweep: could not disconnect %s at %s: %s",
                           address, path, exc)
            continue
        adapter = path.rsplit("/dev_", 1)[0]
        logger.info("stale link from a previous life: disconnected %s on %s "
                    "so it can advertise again", address, adapter)
        dropped.append((path, address))
    return dropped


def lookup_device(bus, mac: str,
                  prefer_adapter: str | None = None,
                  ) -> tuple[str | None, dict | None]:
    """Find the ``Device1`` BlueZ holds for *mac*, if any.

    Returns ``(path, props)`` or ``(None, None)``.  A bonded device keeps
    its object on the adapter it bonded to, so for an already-provisioned
    charger this is the whole of device resolution — no radio involved.

    When several adapters know the device, *prefer_adapter* wins if it is
    among them (the caller remembering what worked last time), then the
    connected one, then the bonded one.  That is the adapter the link will
    actually use, and handing bcmv2 the wrong path would put the claims on
    the wrong card.

    *prefer_adapter* is a card MAC (what ``get_preferred_adapter``
    stores) or a legacy ``hciN``.  It is resolved to the number the card
    answers to *now*, immediately before the path comparison, because a
    stored preference outlives reboots and replugs while hciN numbering
    does not.  A preference that cannot be resolved — the card is absent
    — is dropped rather than matched literally: ranking is a preference,
    not a filter, so the lookup falls through to connected-then-bonded
    instead of returning nothing.
    """
    suffix = "/dev_" + mac.upper().replace(":", "_")
    try:
        om = dbus.Interface(
            bus.get_object("org.bluez", "/", introspect=False),
            "org.freedesktop.DBus.ObjectManager")
        objects = om.GetManagedObjects()
    except Exception:
        logger.exception("%s: BlueZ object lookup failed", mac)
        return None, None

    candidates = []
    for path, interfaces in objects.items():
        path = str(path)
        if not path.endswith(suffix) or DEVICE_INTERFACE not in interfaces:
            continue
        props = _plain(interfaces[DEVICE_INTERFACE])
        candidates.append((path, props))

    if not candidates:
        return None, None

    # Resolve, do not interpolate.  BlueZ paths are /org/bluez/hciN/...,
    # so dropping a stored MAC straight into the string produces a
    # prefix that matches nothing and a preference that silently does
    # nothing at all — the failure looks exactly like having no
    # preference, which is why it would never be noticed.
    name = adapter_identity.hci_for(prefer_adapter) if prefer_adapter else None

    # The configured GATT pool is a constraint; a stored preference is a
    # learned hint.  A hint must never defeat a constraint — that is how
    # the IP22 kept linking on the pack's radio: its PreferredAdapter had
    # recorded that card back when it was the one that worked, and the
    # ble-connect.conf pool naming a different card was never consulted,
    # because the preference picked the BlueZ path outright and the path
    # decides the adapter.
    try:
        pool = ble_catcher.link_adapter_names()
    except Exception:
        logger.exception("%s: could not read the GATT adapter pool; "
                         "placing without it", mac)
        pool = set()

    if pool and name and name not in pool:
        logger.info("%s: ignoring preferred adapter %s — not in the "
                    "configured GATT pool (%s)",
                    mac, prefer_adapter, ", ".join(sorted(pool)))
        name = None

    wanted = f"/org/bluez/{name}/" if name else None

    def in_pool(path: str) -> bool:
        # No pool configured means every adapter is permitted.
        if not pool:
            return True
        return any(path.startswith(f"/org/bluez/{n}/") for n in pool)

    def rank(item):
        _path, props = item
        # Pool membership outranks everything: dropping the out-of-pool
        # preference alone was not enough, because the connected/bonded
        # fallback lands on whichever card the device last bonded to,
        # which is the very card the operator excluded.
        return (0 if in_pool(_path) else 1,
                0 if wanted and _path.startswith(wanted) else 1,
                0 if props.get("Connected") else 1,
                0 if props.get("Paired") else 1,
                _path)

    path, props = sorted(candidates, key=rank)[0]

    # Ranking, not filtering.  If BlueZ knows this device only on an
    # adapter outside the pool, refusing would take a working device off
    # the bus to honour a preference about which radio it uses — the
    # wrong trade.  But it must not pass silently: the operator asked for
    # links on specific cards and this one is not on them.
    if pool and not in_pool(path) and mac not in _warned_out_of_pool:
        # Once per device per process.  This is a standing condition, not
        # an event: it holds for every connect until someone changes the
        # config or bonds the device on a pooled card.  Logged unthrottled
        # it fired on every telemetry cycle — roughly every 30s per
        # device — which is the same steady-state accumulation that made
        # the gate's refusal line worth fixing.
        _warned_out_of_pool.add(mac)
        logger.warning("%s: no pooled adapter knows this device; linking on "
                       "%s, outside the configured GATT pool (%s). "
                       "Bond it on a pooled card or widen ble-connect.conf; "
                       "further occurrences are not logged.",
                       mac, path.rsplit("/", 1)[0], ", ".join(sorted(pool)))
    return path, props


class _PairingAgent(dbus.service.Object):
    """BlueZ pairing agent that answers with the Victron passkey."""

    def __init__(self, bus, path, passkey):
        super().__init__(bus, path)
        self._passkey = passkey

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        logger.info("Pairing agent: providing passkey for %s", device)
        return dbus.UInt32(self._passkey)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


class PairingAgent:
    """Register/unregister a passkey agent for one pairing attempt.

    Scoped to the attempt rather than the process: while registered we are
    BlueZ's *default* agent, which means every pairing on the box comes to
    us, and we only know one passkey.
    """

    def __init__(self, bus, passkey: int, tag: str):
        self._bus = bus
        self._passkey = int(passkey)
        self._path = "/org/victronenergy/dbus_ble_sensors/agent/%s_%d" % (
            tag.replace(":", "").replace("/", ""), os.getpid())
        self._agent = None
        self._manager = None

    @property
    def path(self) -> str:
        return self._path

    def register(self) -> bool:
        try:
            self._agent = _PairingAgent(self._bus, self._path, self._passkey)
            self._manager = dbus.Interface(
                self._bus.get_object("org.bluez", "/org/bluez",
                                     introspect=False),
                "org.bluez.AgentManager1")
            path = dbus.ObjectPath(self._path)
            try:
                self._manager.UnregisterAgent(path)
            except dbus.DBusException:
                pass  # not registered — the normal case
            self._manager.RegisterAgent(path, AGENT_CAPABILITY)
            self._manager.RequestDefaultAgent(path)
            logger.debug("pairing agent registered at %s", self._path)
            return True
        except Exception:
            logger.exception("failed to register pairing agent — pairing "
                             "will fall back to BlueZ's default handling")
            return False

    def unregister(self) -> None:
        if self._manager is not None:
            try:
                self._manager.UnregisterAgent(dbus.ObjectPath(self._path))
            except Exception:
                pass
            self._manager = None
        if self._agent is not None:
            try:
                self._agent.remove_from_connection()
            except Exception:
                pass
            self._agent = None

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, *exc):
        self.unregister()
        return False


def adapter_from_path(path: str | None) -> str | None:
    """``/org/bluez/hci1/dev_AA_BB_…`` → ``hci1``."""
    match = re.match(r"/org/bluez/(hci\d+)/", str(path or ""))
    return match.group(1) if match else None


async def pump_default_context(stop: asyncio.Event) -> None:
    """Dispatch dbus-python traffic from inside an asyncio program.

    The pairing agent is a dbus-python object served by the default GLib
    main context.  In the service that context is already running, but the
    standalone tools (:mod:`orion_tr_key_cli`, ``scripts/probe_charger_vregs``)
    are asyncio programs with no GLib loop of their own — so they run this
    as a background task, and iterating the context non-blockingly between
    asyncio ticks is what lets BlueZ's passkey request reach the agent
    while ``client.pair()`` is being awaited.

    Never call this from the service: two things iterating one context is
    a way to dispatch a callback twice.
    """
    context = GLib.MainContext.default()
    while not stop.is_set():
        while context.pending():
            context.iteration(False)
        await asyncio.sleep(0.01)
