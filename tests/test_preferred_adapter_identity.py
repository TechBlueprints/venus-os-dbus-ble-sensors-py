"""A persisted preferred adapter must name the card, not its number.

This value lives in com.victronenergy.settings, which survives reboots
and replugs.  hciN numbering does not: a USB reset renumbers cards, so a
stored "hci0" can come to mean a different radio than the one that
actually worked.  At that point the setting sends the device to the
wrong card — the exact isolation failure MAC identity exists to prevent,
arriving through a setting meant to help.

Observed on the prod Cerbo before this was fixed:

    INFO:orion_tr_key_settings:Stored preferred adapter hci0 for Orion-TR fb8d9fa69893
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

DRIVER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

MODULES = ["orion_tr_key_settings", "smartshunt_key_settings", "smartsolar_key_settings",
           "ip22_key_settings"]

MAC = "AA:BB:CC:DD:EE:FF"
CARD_KEY = "00019540C333"


class _Settings:
    """Records what actually reached the settings store."""

    def __init__(self, initial=None):
        self.stored = dict(initial or {})

    def try_get_value(self, path):
        return self.stored.get(path)

    def set_item(self, path, value, *a, **kw):
        self.stored[path] = value

    def set_value(self, path, value):
        self.stored[path] = value


def _load_real(name):
    """Load the real module from disk under a private name.

    Several other test modules install stubs called
    ``orion_tr_key_settings`` and friends into sys.modules, and those
    persist for the rest of the session — so a plain import here returns
    whichever stub ran first, and this test would silently be examining
    a fake.
    """
    spec = importlib.util.spec_from_file_location(
        f"_real_{name}", os.path.join(DRIVER_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=MODULES)
def mod(request, monkeypatch):
    # Patch adapter_identity itself, not an attribute of the module
    # under test.  Doing it this way means the test still *runs* against
    # a version that never imports adapter_identity — so the failure is
    # a wrong stored value, which is the defect, rather than a missing
    # attribute, which is only a symptom of the fix being absent.
    identity = _load_real("adapter_identity")
    monkeypatch.setitem(sys.modules, "adapter_identity", identity)
    # Stand in for the backend: hci0 is the card whose MAC is CARD_KEY.
    monkeypatch.setattr(
        identity, "canonical",
        lambda adapter: CARD_KEY if str(adapter) in ("hci0", CARD_KEY)
        else str(adapter))
    return _load_real(request.param)


def test_an_hci_name_is_stored_as_the_cards_mac(mod) -> None:
    settings = _Settings()
    mod.set_preferred_adapter(settings, MAC, "hci0")

    assert CARD_KEY in settings.stored.values()
    assert "hci0" not in settings.stored.values(), (
        "a number outlives the numbering that gave it meaning")


def test_a_legacy_stored_number_is_rejected_not_upgraded(mod) -> None:
    """This reverses an earlier decision, deliberately.

    The old behaviour upgraded a stored "hci0" to the MAC of whatever
    card answered to hci0 at read time, to avoid a settings migration.
    That resolution is unsound: the number was written at an unknown past
    moment, so resolving it now can name a card that was never the one
    meant — and the result is then MAC-shaped, so nothing downstream can
    tell a laundered guess from a real preference.

    This file's own docstring named that hazard while the test below
    enshrined it.  Observed on dev: hci0 named three different physical
    cards within one hour (00:01:95:24:24:CC, 00:01:95:0C:31:BA,
    8A:88:4B:E3:48:F7).

    No preference is the honest answer.  Placement falls through to the
    configured pool and connected-then-bonded ranking, and the next
    successful connect rewrites the setting as a MAC — self-healing,
    which is what the migration was being avoided for in the first place.
    """
    path = mod.preferred_adapter_setting_path(MAC)
    settings = _Settings({path: "hci0"})

    assert mod.get_preferred_adapter(settings, MAC) is None


def test_an_unresolvable_legacy_name_is_also_rejected(mod) -> None:
    # Previously passed through unchanged, on the reasoning that the card
    # might return.  But "hci9" does not name a card that can return — it
    # names a slot, and whatever lands in that slot later is unrelated to
    # whatever was there when this was written.
    path = mod.preferred_adapter_setting_path(MAC)
    settings = _Settings({path: "hci9"})

    assert mod.get_preferred_adapter(settings, MAC) is None


def test_nothing_stored_stays_nothing(mod) -> None:
    assert mod.get_preferred_adapter(_Settings(), MAC) is None


def test_an_empty_adapter_is_not_stored(mod) -> None:
    settings = _Settings()
    mod.set_preferred_adapter(settings, MAC, "  ")
    assert settings.stored == {}


@pytest.mark.parametrize("stored", ["hci0", "HCI0", "hci1", "hci12"])
def test_every_hci_spelling_is_rejected(mod, stored) -> None:
    """Case and number are incidental; the form is the problem.

    Found by the night watch: of three stored hints on dev, two were
    hciN-form. hciN is assigned by probe order, so the value does not
    identify hardware at all — resolving it now yields whichever card
    enumerated first this boot.
    """
    path = mod.preferred_adapter_setting_path(MAC)
    assert mod.get_preferred_adapter(_Settings({path: stored}), MAC) is None


def test_a_mac_preference_is_untouched(mod) -> None:
    """The rejection must not cost us the case the setting exists for."""
    path = mod.preferred_adapter_setting_path(MAC)
    got = mod.get_preferred_adapter(_Settings({path: CARD_KEY}), MAC)
    assert got == CARD_KEY


def test_a_blank_preference_is_no_preference(mod) -> None:
    path = mod.preferred_adapter_setting_path(MAC)
    assert mod.get_preferred_adapter(_Settings({path: "   "}), MAC) is None
