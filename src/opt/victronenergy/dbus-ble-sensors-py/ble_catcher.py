# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Route every GATT connection in this process through bcmv2.

``bleak-connection-manager`` v2 rebinds ``bleak.BleakClient`` process-wide
to a claim-aware wrapper: adapter selection ranks the cards by what other
live processes on this Cerbo are doing with them (the ``/run/bt-claims``
convention), pinned devices walk their preference list failure-driven, and
capped adapters gate on established-link slots.  Installing it here is what
lets our charger writes coexist with dbus-serialbattery's BMS links instead
of fighting them for the same radio.

``wrap_scanner=True``, because this process *does* scan actively — once,
rarely, and only where it has no choice: when BlueZ has never seen a device
there is nothing to connect to until a discovery finds it
(:func:`ble_gatt_link.resolve`).  Routing that through the catcher's
scanner is what makes it take the adapter's hard ``hciN.scan`` claim, rank
away from cards other processes are already scanning on, and release when
it stops.  An active scan nobody else can see is exactly the thing this
project has spent effort not doing to other services.

Wrapping is safe here precisely because that is the *only* bleak scanner in
the process: the advertisement path is the raw HCI monitor tap, driven
straight over an HCI socket, and not a bleak scanner at all.

``scan_to_score=False`` all the same.  That option buys RSSI-based
placement by running short active sweeps of its own on a 10s-every-300s
cadence — a second scanner competing with the tap for the same radios, to
produce placement data we can get for free.  The tap already scans on
every adapter we are allowed to use, so there is nothing an extra sweep
would tell us.  Placement stays least-used: occupancy and failure history.

What the catcher cannot supply is the *pairing* half: BlueZ needs an
``org.bluez.Agent1`` to answer the Victron passkey, and bleak registers
none.  :mod:`orion_tr_gatt` and :mod:`orion_tr_key_cli` keep their own
dbus-python agents for that.

Optional deployment config, ``/data/apps/dbus-ble-sensors-py/ble-connect.conf``::

    # Adapters usable for GATT.  Empty (or no file) means every adapter
    # the kernel exposes is a candidate, ranked by live claims.
    #   hci1                       pool entry
    #   AA:BB:CC:DD:EE:FF@hci1     pin that device to that adapter
    #                              (repeat the MAC for a preference list)
    adapters = hci1 hci2
    # Established-link capacity, for dongles with an undocumented limit
    # (CSR ~5, Broadcom ~7 are the field starting points).  Opt-in;
    # uncapped adapters are never slot-gated.
    link_caps = hci1:5

This is *not* ``adapter-allowlist.conf``.  That file reserves adapters away
from the advertisement scanner; this one bounds where GATT links may be
placed.  They are separate on purpose: a card reserved from our scanning is
usually the best card to connect on.
"""
from __future__ import annotations

import logging
import os

import adapter_identity
import ble_ext_path

logger = logging.getLogger(__name__)

CONFIG_PATH = "/data/apps/dbus-ble-sensors-py/ble-connect.conf"

# Names this process's claims in /run/bt-claims.  bcmv2 appends the pid.
CLAIM_OWNER = "dbus-ble-sensors-py"

_installed = False
_claim_manager = None


def _read_config(path: str = CONFIG_PATH) -> dict:
    """Parse the ``key = value`` config file.  Missing file → empty."""
    values: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip().lower()] = value.strip()
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("failed to read %s — continuing unconfigured", path)
    return values


def catcher_options(path: str = CONFIG_PATH) -> tuple[list[str], dict[str, int]]:
    """``(adapters, link_caps)`` for :func:`install`, from config."""
    cfg = _read_config(path)
    adapters = cfg.get("adapters", "").replace(",", " ").split()
    link_caps: dict[str, int] = {}
    for entry in cfg.get("link_caps", "").replace(",", " ").split():
        adapter, _, cap = entry.partition(":")
        if adapter and cap.isdigit():
            link_caps[adapter] = int(cap)
    return adapters, link_caps


def link_adapter_names() -> set[str]:
    """Current ``hciN`` names of the adapters GATT links may be placed on.

    An empty set means unconfigured — every adapter is a candidate, which
    is bcmv2's own default and must stay the behaviour when no operator
    constraint exists.

    This exists because the pool was only ever half-enforced.  bcmv2 ranks
    *its* placement against these entries, but a device already known to
    BlueZ is resolved to an object path first, and that path names the
    adapter the link will use.  On dev the IP22's stored
    ``PreferredAdapter`` was ``00019540C333`` — the pack's link radio,
    learned back when it was the card that worked — while
    ble-connect.conf named ``00:01:95:24:24:CC``.  The stored hint won,
    and every IP22 link landed on the radio the config exists to keep
    clear.

    Resolved fresh: a MAC-named card is one whose number changes.
    """
    entries, _ = catcher_options()
    names: set[str] = set()
    for entry in entries:
        # "MAC@hciX" pins a device to an adapter; the adapter half is
        # still a legitimate link target.  A bare entry is a pool adapter.
        _, sep, adapter = entry.rpartition("@")
        adapter = adapter if sep else entry
        if not adapter:
            continue
        try:
            name = adapter_identity.hci_for(adapter)
        except Exception:
            continue
        if name:
            names.add(name)
    return names


def install(owner: str = CLAIM_OWNER, extra_adapters=()) -> bool:
    """Install the bcmv2 catcher.  Returns False if the stack is absent.

    *extra_adapters* are entries prepended to the configured ones, in
    bcmv2's raw syntax.  The key provisioner uses it to turn its
    ``--preferred-adapter`` into a pin (``MAC@hciX``), which is what that
    flag always meant: try the card that worked last time first.

    Must run before anything imports ``bleak``'s classes by value — our
    GATT modules resolve ``bleak.BleakClient`` at call time precisely so
    ordering cannot silently regress.  Idempotent.
    """
    global _installed
    if _installed:
        return True
    if not ble_ext_path.install():
        return False

    try:
        from bleak_connection_manager import install_bleak_catcher
    except Exception:
        logger.exception("bleak-connection-manager import failed — "
                         "GATT operations are unavailable")
        return False

    adapters, link_caps = catcher_options()
    adapters = list(extra_adapters) + adapters
    try:
        install_bleak_catcher(
            owner,
            adapters=adapters,
            link_caps=link_caps,
            # Our one active scan is the device-resolution fallback, and
            # it should claim like everyone else's.  See the module
            # docstring for why the recurring sweeps stay off.
            wrap_scanner=True,
            scan_to_score=False,
        )
    except Exception:
        logger.exception("bleak catcher install failed — "
                         "GATT operations are unavailable")
        return False

    logger.info("bcmv2 catcher installed (adapters=%s link_caps=%s); "
                "advertisement scanning stays on the HCI tap",
                adapters or "all", link_caps or "none")
    _installed = True
    return True


def installed() -> bool:
    return _installed


def claim_manager():
    """Shared :class:`ClaimManager` for claims we publish ourselves.

    The catcher keeps its own manager for connection and scan claims; this
    one is for the advertisement tap, which coordinates through the same
    directory without routing its traffic through bleak at all.  Returns
    ``None`` when the claims layer is unavailable.
    """
    global _claim_manager
    if _claim_manager is not None:
        return _claim_manager
    if not ble_ext_path.claims_available():
        return None
    try:
        from bleak_connection_manager.claims import ClaimManager
    except Exception:
        logger.exception("bt-claims import failed — adapter coordination off")
        return None
    # Same owner convention as the catcher: name plus pid, so a restart
    # race between the old process's unreaped claims and the new one's is
    # visible rather than merged.
    _claim_manager = ClaimManager(owner=f"{CLAIM_OWNER}-{os.getpid()}")
    return _claim_manager
