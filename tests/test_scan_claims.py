"""Soft bt-claims published for the adapters we scan on.

The claims are how the rest of the box learns which cards we are scanning
on.  They must never be able to break scanning itself, so every failure
mode here has to degrade to "no claim" rather than raise.
"""
from __future__ import annotations

import ble_catcher
import scan_claims
from scan_claims import ScanClaims


class _FakeManager:
    def __init__(self, fail_on=(), hard_taken=()):
        self.held: dict[str, object] = {}
        self.released: list[str] = []
        self.fail_on = set(fail_on)
        # adapters whose exclusive scan claim another process already holds
        self.hard_taken = set(hard_taken)
        self.kinds: dict[str, str] = {}

    def claim_soft(self, adapter, qualifier=None):
        if adapter in self.fail_on:
            raise OSError("claim directory unwritable")
        claim = object()
        self.held[adapter] = (claim, qualifier)
        self.kinds[adapter] = "soft"
        return claim

    def claim_hard(self, adapter):
        if adapter in self.hard_taken:
            return None
        claim = object()
        self.held[adapter] = (claim, None)
        self.kinds[adapter] = "hard"
        return claim

    def release(self, claim):
        for adapter, (held, _q) in list(self.held.items()):
            if held is claim:
                del self.held[adapter]
                self.released.append(adapter)


def _with_manager(monkeypatch, manager):
    monkeypatch.setattr(ble_catcher, "claim_manager", lambda: manager)


def test_hold_and_release_round_trip(monkeypatch) -> None:
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()

    claims.hold("hci0")
    claims.hold("hci1")
    assert claims.held() == ["hci0", "hci1"]
    assert set(manager.held) == {"hci0", "hci1"}

    claims.release("hci0")
    assert claims.held() == ["hci1"]
    assert manager.released == ["hci0"]


def test_claims_are_qualified_as_scan(monkeypatch) -> None:
    # The qualifier keeps these distinct from the connection claims the
    # catcher writes for the same owner.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0")
    _claim, qualifier = manager.held["hci0"]
    assert qualifier == scan_claims.QUALIFIER == "scan"


def test_hold_is_idempotent(monkeypatch) -> None:
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0")
    first = manager.held["hci0"][0]
    claims.hold("hci0")
    assert manager.held["hci0"][0] is first


def test_release_all_drops_every_claim(monkeypatch) -> None:
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    for adapter in ("hci0", "hci1", "hci2"):
        claims.hold(adapter)
    claims.release_all()
    assert claims.held() == []
    assert sorted(manager.released) == ["hci0", "hci1", "hci2"]


def test_missing_claims_layer_is_a_no_op(monkeypatch) -> None:
    # No vendored stack: scanning must carry on uncoordinated.
    monkeypatch.setattr(ble_catcher, "claim_manager", lambda: None)
    claims = ScanClaims()
    claims.hold("hci0")
    claims.release("hci0")
    claims.release_all()
    assert claims.held() == []


def test_claim_failure_does_not_propagate(monkeypatch) -> None:
    manager = _FakeManager(fail_on={"hci0"})
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0")
    assert claims.held() == []
    # A different adapter still works afterwards.
    claims.hold("hci1")
    assert claims.held() == ["hci1"]


def test_release_of_unheld_adapter_is_harmless(monkeypatch) -> None:
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.release("hci9")
    assert manager.released == []


def test_passive_scanning_claims_softly(monkeypatch) -> None:
    # A passive listen transmits nothing and genuinely shares the card.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=False)
    assert claims.kind("hci0") == "soft"
    assert manager.kinds["hci0"] == "soft"
    assert manager.held["hci0"][1] == "scan"


def test_active_scanning_takes_the_exclusive_claim(monkeypatch) -> None:
    # An active scanner transmits SCAN_REQs and holds the channel; that is
    # exactly what the hard claim announces.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=True)
    assert claims.kind("hci0") == "hard"
    assert manager.kinds["hci0"] == "hard"


def test_flipping_the_toggle_swaps_the_claim_kind(monkeypatch) -> None:
    # The file on disk must say what we are doing now, not what we were.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=False)
    claims.hold("hci0", exclusive=True)
    assert claims.kind("hci0") == "hard"
    assert manager.kinds["hci0"] == "hard"
    assert manager.released == ["hci0"]      # the soft one was given up
    claims.hold("hci0", exclusive=False)
    assert claims.kind("hci0") == "soft"


def test_same_kind_twice_is_a_no_op(monkeypatch) -> None:
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=True)
    first = manager.held["hci0"][0]
    claims.hold("hci0", exclusive=True)
    assert manager.held["hci0"][0] is first
    assert manager.released == []


def test_contested_hard_claim_falls_back_to_soft(monkeypatch) -> None:
    # Another live scanner holds it.  We are still on this radio, so
    # register as occupancy rather than disappearing from the directory.
    manager = _FakeManager(hard_taken={"hci0"})
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=True)
    assert claims.kind("hci0") == "soft"
    assert manager.kinds["hci0"] == "soft"
    assert claims.held() == ["hci0"]
