# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""An adapter is its MAC.  ``hciN`` is only what it is called right now.

The numbering is not an identity.  A USB reset renumbers a card, so does
replugging, and so does a reboot: on dev-cerbo the onboard Broadcom failed
its firmware reset one boot, and the USB dongle that had been ``hci1`` came
up as ``hci0``.  Anything we remembered about "hci1" — that it was
allow-listed, that scanning was enabled on it, how many times enabling had
failed — silently transferred to a different physical radio.

That is not a theoretical failure.  ``adapter-allowlist.conf`` exists to
reserve cards for other BLE services, so getting it backwards means we
scan the card we promised to leave alone, and leave alone the one we were
supposed to use.

So identity is the MAC, and ``hciN`` is resolved from it at the moment we
need it — immediately before an HCI socket call, never cached across one.
This mirrors what bcmv2 does for connections (``Adapters are identified by
MAC: hciN numbering is not an identity``), and deliberately borrows its
resolver rather than growing a second one: :mod:`bleak_connection_manager.claims`
is stdlib-only, already vendored, and already knows the Venus-specific
detail that no adapter exposes a sysfs ``address`` attribute, so the whole
table comes from a single ``hciconfig`` call behind a short-lived cache.

Everything degrades.  With no vendored stack we fall back to treating
``hciN`` as the identity, which is exactly today's behaviour — worse than
MAC binding, better than refusing to scan.
"""
from __future__ import annotations

import logging
import re

import ble_ext_path

logger = logging.getLogger(__name__)

_HCI_RE = re.compile(r"^hci\d+$")
_HEX12_RE = re.compile(r"^[0-9A-F]{12}$")
_MAC_SEPARATORS = ":-. \t_"

_claims = None
_looked_up = False


def _backend():
    """The vendored claims module, or None when the stack is absent."""
    global _claims, _looked_up
    if _looked_up:
        return _claims
    _looked_up = True
    if ble_ext_path.claims_available():
        try:
            from bleak_connection_manager import claims
            _claims = claims
        except Exception:
            logger.exception("bt-claims import failed — adapters will be "
                             "identified by hciN, which renumbering breaks")
    return _claims


def mac_key(value) -> str | None:
    """Any spelling of a MAC → the canonical ``AABBCCDDEEFF``, else None.

    Permissive on separators and case because humans type these into
    config files.  An ``hciN`` name is not a MAC and returns None.
    """
    backend = _backend()
    if backend is not None:
        return backend.mac_key(value)
    text = str(value).strip()
    for sep in _MAC_SEPARATORS:
        text = text.replace(sep, "")
    text = text.upper()
    return text if _HEX12_RE.match(text) else None


def canonical(adapter) -> str:
    """Identity key for an adapter, from a MAC *or* an ``hciN`` name.

    An ``hciN`` whose MAC cannot be read degrades to the name itself:
    coordination is an optimization and must never fail closed because a
    card will not identify itself.
    """
    backend = _backend()
    if backend is not None:
        return backend.adapter_key(adapter)
    key = mac_key(adapter)
    return key if key is not None else str(adapter).strip()


def hci_for(adapter) -> str | None:
    """The ``hciN`` an adapter answers to *right now*, or None if gone."""
    backend = _backend()
    if backend is not None:
        return backend.hci_for(adapter)
    text = str(adapter).strip()
    return text if _HCI_RE.match(text) else None


def index_for(adapter) -> int | None:
    """Current controller index for ``hci_scan_control``, or None.

    Resolve immediately before the socket call.  A stale index is how a
    scan-enable lands on the card another service is using.

    "Immediately" has to be enforced, not merely intended.  The backend
    serves adapter MACs from a 30s TTL cache, so a MAC-named adapter
    would otherwise resolve through a mapping up to a TTL old — and
    naming a card by its MAC is precisely a statement that its number
    may change.  For up to that TTL we could open a raw socket on
    whatever card has since inherited the number, which is the exact
    isolation failure MAC-naming exists to prevent, arriving through
    the mechanism chosen to avoid it.

    So drop the cache first when there is something to resolve.  An
    adapter already written as ``hciN`` has nothing to look up and pays
    nothing; a MAC costs one refill (~11ms on a Cerbo against ~19us
    cached), which is affordable here because this sits on scan-enable
    and accept-list operations, not on the advertisement path.
    """
    if mac_key(adapter) is not None:
        invalidate()
    name = hci_for(adapter)
    if name is None or not _HCI_RE.match(name):
        return None
    return int(name[3:])


def invalidate(adapter=None) -> None:
    """Drop cached MAC lookups — call after anything that resets a card."""
    backend = _backend()
    if backend is not None:
        backend.invalidate_adapter_mac(adapter)


def label(key: str, name: str | None = None) -> str:
    """Human-readable form for logs: ``hci0 (00019540C333)``.

    Logs are read against ``hciconfig`` output and BlueZ paths, so the
    current name still has to be there — but the key is what our state is
    actually keyed by, and printing both is what makes a renumbering
    visible in the log rather than merely confusing.
    """
    name = name or hci_for(key)
    if name and name != key:
        return f"{name} ({key})"
    return str(name or key)


def allowed(entries, key: str, name: str | None = None) -> bool:
    """Whether *key* is permitted by a list of allow-list entries.

    An empty list permits everything — that is the unconfigured default,
    not a lockout.  Entries are matched by identity, so a MAC in any
    spelling names the card itself; a bare ``hciN`` entry is honoured as
    the older spelling, matched both by resolving it and against the name
    BlueZ is currently using, so an existing config keeps working right
    up until the numbering changes under it.
    """
    entries = [e for e in (str(x).strip() for x in entries) if e]
    if not entries:
        return True
    for entry in entries:
        if canonical(entry) == key:
            return True
        if name is not None and entry == name:
            return True
    return False
