# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Make our advertisement scanning visible to the other BLE services.

The advertisement path deliberately does **not** go through bcmv2: it
drives the controller over a raw HCI socket and reads results off the
monitor channel, so BlueZ never materialises a ``Device1`` per advertiser.
But "not routed through the connection manager" should not mean
"invisible".  Every other service here coordinates through the
``/run/bt-claims`` file convention, and a card we are scanning on is a
card whose links are measurably less stable — that is what
``adapter-allowlist.conf`` exists to work around by hand.

So we publish a **soft** claim (``hciN.use.<owner>.scan``) per adapter we
have scanning enabled on, released when the adapter goes away or the load
throttle stops scanning.

Soft rather than hard, because passive scanning genuinely does coexist
with other traffic: the claim is a fact to rank on, not a reservation, and
a hard ``hciN.scan`` would push every bcmv2 consumer off cards they can
legitimately use.  If ``/Settings/BleSensors/ActiveScan`` is ever turned
on, that reasoning weakens — ``hciN.scan`` means precisely "I am actively
scanning here, use another card" — and switching to :meth:`claim_hard` is
a one-line change.

We never yield a card on someone else's claim either — this is
one-directional by design, an announcement rather than a negotiation.

The nice emergent property: our own GATT links go through bcmv2, which
ranks by occupancy, so a charger write naturally prefers a card we are
*not* scanning on.
"""
from __future__ import annotations

import logging

import ble_catcher

logger = logging.getLogger(__name__)

# Distinguishes these from the connection claims the catcher writes for
# the same owner, so `ls /run/bt-claims` stays readable.
QUALIFIER = "scan"


class ScanClaims:
    """Soft claims tracking which adapters we have scanning enabled on.

    Every method degrades to a no-op when the claims layer is unavailable
    (no vendored stack, unwritable ``/run``): coordination is an
    optimization here, never a precondition for scanning.
    """

    def __init__(self) -> None:
        self._claims: dict[str, object] = {}
        self._warned = False

    def _manager(self):
        manager = ble_catcher.claim_manager()
        if manager is None and not self._warned:
            self._warned = True
            logger.info("bt-claims unavailable — our scanning will not be "
                        "announced to other BLE services")
        return manager

    def hold(self, adapter: str) -> None:
        """Claim *adapter*.  Idempotent; the manager heartbeats for us."""
        if adapter in self._claims:
            return
        manager = self._manager()
        if manager is None:
            return
        try:
            claim = manager.claim_soft(adapter, qualifier=QUALIFIER)
        except Exception:
            logger.exception("%s: failed to claim", adapter)
            return
        if claim is not None:
            self._claims[adapter] = claim
            logger.debug("%s: scan claim held", adapter)

    def release(self, adapter: str) -> None:
        """Drop our claim on *adapter*, if we hold one."""
        claim = self._claims.pop(adapter, None)
        if claim is None:
            return
        manager = self._manager()
        if manager is None:
            return
        try:
            manager.release(claim)
            logger.debug("%s: scan claim released", adapter)
        except Exception:
            logger.exception("%s: failed to release claim", adapter)

    def release_all(self) -> None:
        """Drop every claim — the load throttle stopping all scanning."""
        for adapter in list(self._claims):
            self.release(adapter)

    def held(self) -> list[str]:
        return sorted(self._claims)
