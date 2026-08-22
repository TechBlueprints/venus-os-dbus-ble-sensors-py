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

**The claim kind follows what we are actually doing**, because that is the
only thing that makes it true:

* **Passive scanning takes a soft claim** (``<MAC>.use.<owner>.scan``).
  A passive scanner listens and transmits nothing; it genuinely coexists
  with other traffic, so the claim is a fact to rank on, not a
  reservation.  A hard claim here would push every bcmv2 consumer off
  cards they can legitimately share — and ours is a permanent listen, not
  a short scan activity, so it would push them off forever.
* **Active scanning takes the hard claim** (``<MAC>.scan``, exclusive).
  An active scanner transmits a SCAN_REQ at every advertiser it hears and
  holds the channel for the response.  That is precisely what the hard
  claim means — "I am actively scanning here, use another card" — and
  announcing it as merely soft would let a second scanner land on the
  same radio believing it was free.

If the hard claim cannot be taken (another live process is already
scanning that card) we fall back to a soft one rather than going silent:
we are still using the radio, and everyone else should still see it.  We
never yield a card on someone else's claim — this is one-directional by
design, an announcement rather than a negotiation.

The nice emergent property: our own GATT links go through bcmv2, which
ranks by occupancy, so a charger write naturally prefers a card we are
*not* scanning on.  Our own hard claim does not push our own connections
away — bcmv2 compares the claim's pid against its own.
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
        # adapter key -> (kind, claim), kind being "hard" or "soft"
        self._claims: dict[str, tuple[str, object]] = {}
        self._warned = False
        self._downgraded: set[str] = set()

    def _manager(self):
        manager = ble_catcher.claim_manager()
        if manager is None and not self._warned:
            self._warned = True
            logger.info("bt-claims unavailable — our scanning will not be "
                        "announced to other BLE services")
        return manager

    def hold(self, adapter: str, exclusive: bool = False) -> None:
        """Claim *adapter*, hard when *exclusive* (i.e. actively scanning).

        Idempotent for an unchanged kind; the manager heartbeats for us.
        A change of kind — the ActiveScan toggle being flipped — releases
        the old claim and takes the other, so the file on disk always says
        what we are currently doing.
        """
        want = "hard" if exclusive else "soft"
        held = self._claims.get(adapter)
        if held is not None and held[0] == want:
            return
        manager = self._manager()
        if manager is None:
            return
        if held is not None:
            self.release(adapter)

        kind, claim = want, None
        try:
            if exclusive:
                claim = manager.claim_hard(adapter)
                if claim is None:
                    # Someone else is scanning this card.  Say so once, and
                    # register as occupancy rather than disappearing: we are
                    # still on this radio either way.
                    kind = "soft"
                    claim = manager.claim_soft(adapter, qualifier=QUALIFIER)
                    if adapter not in self._downgraded:
                        self._downgraded.add(adapter)
                        logger.info(
                            "%s: another process holds the scan claim — "
                            "claiming softly instead", adapter)
                else:
                    self._downgraded.discard(adapter)
            else:
                claim = manager.claim_soft(adapter, qualifier=QUALIFIER)
        except Exception:
            logger.exception("%s: failed to claim", adapter)
            return
        if claim is not None:
            self._claims[adapter] = (kind, claim)
            logger.debug("%s: %s scan claim held", adapter, kind)

    def release(self, adapter: str) -> None:
        """Drop our claim on *adapter*, if we hold one."""
        held = self._claims.pop(adapter, None)
        if held is None:
            return
        _kind, claim = held
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

    def kind(self, adapter: str) -> 'str | None':
        """``"hard"``, ``"soft"``, or None if we hold no claim."""
        held = self._claims.get(adapter)
        return held[0] if held else None
