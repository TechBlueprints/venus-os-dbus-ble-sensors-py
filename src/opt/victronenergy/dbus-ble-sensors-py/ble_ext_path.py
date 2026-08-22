# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Put the vendored BLE connection stack on ``sys.path``.

Every entry point that reaches a device over GATT — the service itself,
``orion_tr_key_cli``, ``scripts/probe_charger_vregs.py`` — calls
:func:`install` before importing anything that touches ``bleak``.

Two Venus OS specifics make the ordering load-bearing:

* Venus OS ships **no** ``bleak`` at all (checked on v3.72 / Python
  3.12), so the whole stack comes from ``ext/`` submodules.
* Venus OS *does* ship ``python3-dbus-fast`` **2.21.1**, while current
  bleak needs ``dbus-fast >= 4`` (it imports ``dbus_fast.annotations``).
  ``bleak-connection-manager/ext`` carries a vendored dbus-fast 5.x for
  exactly this reason, and it has to land **ahead of** site-packages or
  a fresh deploy dies with ``No module named 'dbus_fast.annotations'``.

Advertisement scanning does not go through here — that stays on the raw
HCI monitor tap (:mod:`hci_scan_control`, :mod:`hci_advertisement_tap`),
which drives the controller directly rather than through BlueZ, so no
``Device1`` object is created per advertiser seen.
"""
from __future__ import annotations

import logging
import os
import sys

_logger = logging.getLogger(__name__)

_EXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")

# Order within the list is irrelevant (disjoint top-level packages); what
# matters is that all of them precede site-packages.
_SUBDIRS = (
    os.path.join(_EXT_DIR, "bleak-connection-manager", "src"),
    os.path.join(_EXT_DIR, "bleak-connection-manager", "ext"),
    os.path.join(_EXT_DIR, "bleak-retry-connector", "src"),
    os.path.join(_EXT_DIR, "bluetooth-adapters", "src"),
    os.path.join(_EXT_DIR, "aiooui", "src"),
    os.path.join(_EXT_DIR, "bleak"),
)

# The subset without which there is no connection layer at all.  A
# partially-fetched install (submodule fetch failed, someone copied the
# tree without `git submodule update`) is reported once and then treated
# as "no GATT" — advertisement-driven sensors keep working.
_REQUIRED = (
    os.path.join(_EXT_DIR, "bleak", "bleak"),
    os.path.join(_EXT_DIR, "bleak-connection-manager", "src",
                 "bleak_connection_manager"),
)

# The claims layer alone: stdlib-only, no bleak.  Adapter coordination
# (:mod:`bt_claims` file convention under /run/bt-claims) has to keep
# working even on an install whose bleak submodule never arrived, since
# the passive scanner publishes its claims through it.
_CLAIMS_PKG = os.path.join(_EXT_DIR, "bleak-connection-manager", "src",
                           "bleak_connection_manager")

_installed: bool | None = None


def install() -> bool:
    """Prepend the vendored BLE stack to ``sys.path``.

    Paths are added for whatever is actually present, so a tree carrying
    only ``bleak-connection-manager`` still gets a working claims layer.
    Returns ``True`` only when the *full* stack (bleak included) is there
    — i.e. when GATT is possible.  Idempotent.
    """
    global _installed
    if _installed is not None:
        return _installed

    for sub in _SUBDIRS:
        if os.path.isdir(sub) and sub not in sys.path:
            sys.path.insert(0, sub)

    missing = [p for p in _REQUIRED if not os.path.isdir(p)]
    if missing:
        _logger.warning(
            "BLE connection stack incomplete — missing %s.  "
            "Run 'git submodule update --init --recursive' in the install "
            "directory (or re-run install.sh).  Advertisement-driven "
            "sensors are unaffected; GATT writes and key provisioning "
            "are not possible until this is fixed.",
            ", ".join(os.path.relpath(p, _EXT_DIR) for p in missing))
        _installed = False
        return False

    _installed = True
    return True


def available() -> bool:
    """Whether :func:`install` found a full (bleak-capable) stack."""
    return bool(_installed)


def claims_available() -> bool:
    """Whether the stdlib-only bt-claims layer can be imported.

    True on installs with no bleak, which is the point: the advertisement
    scanner publishes claims and it has no bleak in its path at all.
    """
    install()
    return os.path.isdir(_CLAIMS_PKG)
