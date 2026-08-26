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


def test_a_soft_claim_is_still_available(monkeypatch) -> None:
    # The kind is still a parameter, because the downgrade path uses it:
    # when another live process already holds the hard claim we register
    # softly rather than going silent.  What changed is that the service
    # never *asks* for soft any more — see the policy test below.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=False)
    assert claims.kind("hci0") == "soft"
    assert manager.kinds["hci0"] == "soft"
    assert manager.held["hci0"][1] == "scan"


def test_scanning_takes_the_exclusive_claim(monkeypatch) -> None:
    # Scanning is exclusive use of a card, passive or active: the filter
    # policy we program is applied to every user of that radio, and a
    # card carrying a permanent scan is not one another service can
    # reliably discover on.
    manager = _FakeManager()
    _with_manager(monkeypatch, manager)
    claims = ScanClaims()
    claims.hold("hci0", exclusive=True)
    assert claims.kind("hci0") == "hard"
    assert manager.kinds["hci0"] == "hard"


def test_changing_kind_swaps_the_claim(monkeypatch) -> None:
    # The file on disk must say what we are doing now, not what we were.
    # Reachable via the downgrade path rather than the ActiveScan toggle,
    # which no longer selects a claim kind.
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


def test_the_service_always_asks_for_an_exclusive_claim() -> None:
    """Policy pin: scanning is exclusive, whatever ActiveScan says.

    This used to depend on the scan type, on the reasoning that a
    passive listen "genuinely coexists".  It does not: our passive scan
    reprograms the controller's filter policy to accept-list-only every
    60 s, discarding advertisements for every user of that radio, not
    just us — dbus-shyion-switch lost seven relays to exactly that.

    Pinned as source rather than behaviour because the call sites are
    inside the scan-enable path, which needs a live HCI socket to run.
    """
    import os
    import re

    src = open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "opt",
        "victronenergy", "dbus-ble-sensors-py", "dbus_ble_sensors.py")).read()

    calls = re.findall(r"_scan_claims\.hold\([^)]*\)", src)
    assert calls, "expected the service to publish scan claims"
    for call in calls:
        assert "exclusive=True" in call, (
            f"scanning must claim exclusively: {call}")
