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
