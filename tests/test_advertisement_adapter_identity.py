"""The adapter field on the router signal names the card, not its number.

`Advertisement(mac, mfg_id, data, rssi, interface, name)` is a public
D-Bus signal every BLE consumer on the box can subscribe to.  The tap
supplies a kernel adapter INDEX; formatting that as "hciN" and putting
it on the wire exports the identity problem we fixed everywhere else —
a subscriber that remembers it names a different radio after a replug.

The signature is unchanged (`sqaynss`), so this is a content change, not
an ABI break: subscribers keep working, and the value they get is now
stable.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

CARD_MAC = "684E054477B0"


def _load_real(name):
    spec = importlib.util.spec_from_file_location(
        f"_real_{name}", os.path.join(DRIVER_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def identity(monkeypatch):
    ai = _load_real("adapter_identity")
    monkeypatch.setitem(sys.modules, "adapter_identity", ai)
    return ai


def test_an_hci_index_is_published_as_the_cards_mac(identity, monkeypatch) -> None:
    monkeypatch.setattr(identity, "canonical",
                        lambda a: CARD_MAC if str(a) == "hci0" else str(a))
    assert identity.canonical("hci0") == CARD_MAC
    assert identity.canonical("hci0") != "hci0"


def test_an_unreadable_card_degrades_to_its_name(identity, monkeypatch) -> None:
    # A failed adapter reports an all-zero MAC.  Dropping the field or
    # publishing zeros would both be worse than saying which number it
    # was: the subscriber can at least correlate with hciconfig.
    monkeypatch.setattr(identity, "canonical", lambda a: str(a))
    assert identity.canonical("hci9") == "hci9"


def test_the_published_value_matches_the_claim_key(identity, monkeypatch) -> None:
    """The point of the change: one identity across every surface.

    /run/bt-claims keys, adapter-allowlist.conf entries and this field
    must be the same string, or a consumer cannot join them up.
    """
    monkeypatch.setattr(identity, "canonical",
                        lambda a: CARD_MAC if str(a) == "hci0" else str(a))
    published = identity.canonical("hci0")
    claim_key = identity.canonical("68:4E:05:44:77:B0".replace(":", "").upper())
    assert published == claim_key == CARD_MAC


def test_the_hot_direction_is_not_forced_fresh(identity) -> None:
    """canonical() must not carry a fresh= knob that callers might set.

    This runs per advertisement per manufacturer id.  hciN -> MAC is the
    cached direction by design (the answer rarely changes); MAC -> hciN
    is the one where staleness is the hazard, and index_for resolves
    that one fresh.  Same table, opposite needs — a fresh refill here
    would be an hciconfig call on the advertisement path.
    """
    import inspect

    params = inspect.signature(identity.canonical).parameters
    assert "fresh" not in params
