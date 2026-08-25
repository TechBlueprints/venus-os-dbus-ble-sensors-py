"""Adapter identity: a card is its MAC, hciN is only its current name.

The failure these guard against is silent and physical.  dev-cerbo's
onboard Broadcom failed its firmware reset one boot and the USB dongle
that had been hci1 came up as hci0; anything keyed by name — most
damagingly adapter-allowlist.conf, whose whole job is reserving a card
for the BMS — then pointed at the wrong radio.
"""
from __future__ import annotations

import adapter_identity as ai


def test_mac_key_accepts_every_spelling_humans_write() -> None:
    canonical = "00019540C333"
    for spelling in ("00:01:95:40:C3:33", "00-01-95-40-c3-33",
                     "0001.9540.c333", "00 01 95 40 c3 33",
                     "00_01_95_40_C3_33", "00019540c333"):
        assert ai.mac_key(spelling) == canonical, spelling


def test_mac_key_rejects_non_macs() -> None:
    # An hciN name is not a MAC; the caller decides what that means.
    assert ai.mac_key("hci0") is None
    assert ai.mac_key("") is None
    assert ai.mac_key("00:01:95:40:C3") is None      # too short
    assert ai.mac_key("zz:01:95:40:c3:33") is None   # not hex


def test_canonical_passes_macs_through_and_keeps_unknown_names() -> None:
    assert ai.canonical("00:01:95:40:C3:33") == "00019540C333"
    # No such adapter on a test host: degrade to the name rather than
    # failing closed, which would mean refusing to scan at all.
    assert ai.canonical("hci7") == "hci7"


def test_index_for_reads_the_current_number() -> None:
    assert ai.index_for("hci0") == 0
    assert ai.index_for("hci11") == 11
    # A MAC that resolves to no present adapter has no index.
    assert ai.index_for("00:01:95:40:C3:33") is None


def test_label_shows_both_name_and_identity() -> None:
    assert ai.label("00019540C333", "hci0") == "hci0 (00019540C333)"
    # Degraded identity: don't print "hci0 (hci0)".
    assert ai.label("hci0", "hci0") == "hci0"


def test_empty_allowlist_permits_everything() -> None:
    # Unconfigured is the default, not a lockout.
    assert ai.allowed([], "00019540C333", "hci0") is True
    assert ai.allowed(["", "   "], "00019540C333", "hci0") is True


def test_allowlist_matches_by_mac_in_any_spelling() -> None:
    for spelling in ("00:01:95:40:C3:33", "00-01-95-40-c3-33", "00019540c333"):
        assert ai.allowed([spelling], "00019540C333", "hci0") is True


def test_allowlist_denies_a_different_card() -> None:
    assert ai.allowed(["11:22:33:44:55:66"], "00019540C333", "hci0") is False


def test_allowlist_still_honours_legacy_hci_entries() -> None:
    # Existing configs keep working — against the name BlueZ is using now.
    assert ai.allowed(["hci0"], "00019540C333", "hci0") is True
    assert ai.allowed(["hci1"], "00019540C333", "hci0") is False


def test_a_mac_entry_survives_renumbering() -> None:
    # The point of the whole change: same card, new number, still allowed.
    entries = ["00:01:95:40:C3:33"]
    assert ai.allowed(entries, "00019540C333", "hci1") is True
    assert ai.allowed(entries, "00019540C333", "hci0") is True


def _plain_mac_key(value):
    """What a backend's mac_key does, without asking the backend.

    ai.mac_key delegates to the backend, so a stub that calls it back
    recurses forever.
    """
    text = str(value).strip().replace(":", "").replace("-", "").upper()
    return text if len(text) == 12 and all(
        c in "0123456789ABCDEF" for c in text) else None


def test_a_modern_backend_is_still_invalidated_first(monkeypatch) -> None:
    """One call site that is correct on every backend generation.

    Invalidating before a backend that already resolves fresh is free —
    it clears an empty cache, and the single refill happens inside
    hci_for either way (measured on a Cerbo: 10.1ms for hci_for alone,
    8.7ms for invalidate-then-call).  Paying nothing for a branch we do
    not have to test twice is the better trade.
    """
    calls = []

    class _Backend:
        def mac_key(self, value):
            return _plain_mac_key(value)

        def hci_for(self, adapter, fresh=True):
            calls.append(("hci_for", fresh))
            return "hci3"

        def invalidate_adapter_mac(self, adapter=None):
            calls.append(("invalidate", adapter))

    monkeypatch.setattr(ai, "_backend", lambda: _Backend())

    assert ai.index_for("00:01:95:40:C3:33") == 3
    assert calls == [("invalidate", None), ("hci_for", True)], (
        f"invalidate first, then resolve freshly: {calls}")


def test_an_older_backend_gets_the_same_guarantee(monkeypatch) -> None:
    """Falling back to a cached answer here is the failure, not a
    degradation.

    An installer whose /data/bcm convergence failed runs the vendored
    copy, which may predate the fresh-by-default contract.  index_for
    feeds raw HCI socket calls, so a stale number means programming a
    scan onto whatever card inherited it.
    """
    calls = []

    class _OldBackend:
        def mac_key(self, value):
            return _plain_mac_key(value)

        def hci_for(self, adapter):          # no `fresh` parameter
            calls.append(("hci_for", None))
            return "hci3"

        def invalidate_adapter_mac(self, adapter=None):
            calls.append(("invalidate", adapter))

    monkeypatch.setattr(ai, "_backend", lambda: _OldBackend())

    assert ai.index_for("00:01:95:40:C3:33") == 3
    assert calls[0] == ("invalidate", None), (
        f"must force the refill the backend will not: {calls}")


def test_a_log_label_does_not_pay_for_a_refill(monkeypatch) -> None:
    # These run per adapter per scan cycle.  A name a few seconds stale
    # is a cosmetic wrong; an hciconfig call per log line is a real cost.
    seen = []

    class _Backend:
        def mac_key(self, value):
            return _plain_mac_key(value)

        def adapter_key(self, adapter):
            return str(adapter)

        def hci_for(self, adapter, fresh=True):
            seen.append(fresh)
            return "hci3"

    monkeypatch.setattr(ai, "_backend", lambda: _Backend())

    ai.label("00019540C333")
    assert seen == [False], f"a label must not force a refill: {seen}"


def test_an_hci_named_adapter_pays_nothing(monkeypatch) -> None:
    # Nothing to resolve, so no refill: the cost lands only where the
    # staleness could.
    calls = []

    class _Backend:
        def mac_key(self, value):
            return _plain_mac_key(value)

        def hci_for(self, adapter, fresh=True):
            return str(adapter)

        def invalidate_adapter_mac(self, adapter=None):
            calls.append(adapter)

    monkeypatch.setattr(ai, "_backend", lambda: _Backend())

    assert ai.index_for("hci7") == 7
    assert calls == [], "an hciN entry has no cached mapping to be stale"


def test_a_backend_without_invalidation_does_not_crash(monkeypatch) -> None:
    # Nothing to force, so nothing to force.  Degrading to "no guarantee
    # available" beats an AttributeError on the scan-enable path.
    class _Ancient:
        def mac_key(self, value):
            return _plain_mac_key(value)

        def hci_for(self, adapter):
            return "hci3"

    monkeypatch.setattr(ai, "_backend", lambda: _Ancient())
    assert ai.index_for("00:01:95:40:C3:33") == 3
