# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Check that the BLE connection stack is importable.

Every entry point that reaches a device over GATT — the service itself,
``orion_tr_key_cli``, ``scripts/probe_charger_vregs.py`` — calls
:func:`install` before importing anything that touches ``bleak``.

The stack comes from **``/data/bcm``**, a single checkout of
``bleak-connection-manager`` shared by every BLE consumer on the box.
``install.sh`` converges it and the run scripts exec through its
``python3`` shim, which puts it on ``sys.path`` before we get control.
So there is nothing for this module to add to ``sys.path`` any more; its
job is to answer whether the stack arrived, and to say something useful
when it did not.

This repo used to carry its own copy of that stack as git submodules,
as a fallback for a bare clone.  Two things made the fallback worse than
its absence:

* Adapter placement and drain cooperation are a protocol *between*
  services — the claims in ``/run/bt-claims`` only mean anything if
  every consumer agrees on them.  A private copy is a private opinion
  about a shared protocol.
* The fallback was reached precisely when converging the shared checkout
  had just failed, so a stale copy meant the box silently dropped to an
  older stack at the exact moment it had reported being unhealthy.  It
  was found in that state on the prod Cerbo, five commits behind, having
  announced nothing.

Advertisement scanning does not go through here at all — that stays on
the raw HCI monitor tap (:mod:`hci_scan_control`,
:mod:`hci_advertisement_tap`), which drives the controller directly
rather than through BlueZ, so no ``Device1`` object is created per
advertiser seen.
"""
from __future__ import annotations

import logging
import sys

_logger = logging.getLogger(__name__)

_SHIM = "/data/bcm/python3"

_installed: bool | None = None


def _importable(name: str) -> bool:
    if name in sys.modules:
        # Cheap, and it is what lets test stubs win: a ModuleType with
        # __spec__ = None makes find_spec raise ValueError below.
        return True
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def install() -> bool:
    """Whether the BLE connection stack is importable.  Idempotent.

    Named ``install`` for its callers' sake: it used to put vendored
    copies on ``sys.path`` and every entry point calls it before
    importing bleak.  It no longer installs anything.
    """
    global _installed
    if _installed is not None:
        return _installed

    _installed = _importable("bleak_connection_manager") and _importable("bleak")
    if not _installed:
        _logger.warning(
            "BLE connection stack unavailable — could not import "
            "bleak_connection_manager and bleak.  This service runs under "
            "%s, which puts the shared checkout at /data/bcm on the path; "
            "re-run install.sh to converge it.  Advertisement-driven "
            "sensors are unaffected; GATT writes and key provisioning "
            "cannot run until this is fixed.", _SHIM)
    return _installed


def available() -> bool:
    """Whether :func:`install` found a full (bleak-capable) stack."""
    return bool(_installed)


def claims_available() -> bool:
    """Whether the stdlib-only bt-claims layer can be imported.

    Deliberately separate from :func:`install`: this is true on a box
    with no bleak, which is the point — the advertisement scanner
    publishes its claims through the claims layer and never touches
    bleak at all.
    """
    return _importable("bleak_connection_manager")
