"""A stored adapter preference is a MAC, and must be resolved to use it.

get_preferred_adapter stores the card's own MAC, because the setting
outlives reboots and replugs while hciN numbering does not.  BlueZ
object paths are /org/bluez/hciN/dev_..., so a MAC dropped straight
into that string produces a prefix matching nothing — a preference that
silently does nothing, indistinguishable from having no preference at
all.  That is why it needs a test rather than a reading.
"""
from __future__ import annotations

import sys
import types

import pytest

import ble_gatt_dbus

MAC = "F0:C6:DC:C8:74:7A"
SUFFIX = "/dev_F0_C6_DC_C8_74_7A"
CARD = "684E054477B0"          # the preferred card's own MAC
CARD_HCI = "hci0"              # what it answers to right now
OTHER_HCI = "hci2"


class _Objects(dict):
    pass


def _bus_with(paths):
    """A stub bus whose ObjectManager reports *paths* as Device1s."""
    objects = {
        p: {ble_gatt_dbus.DEVICE_INTERFACE: props} for p, props in paths.items()
    }

    class _OM:
        def GetManagedObjects(self):
            return objects

    class _Bus:
        def get_object(self, *a, **kw):
            return object()

    ble_gatt_dbus.dbus.Interface = lambda *a, **kw: _OM()
    return _Bus()


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    """hci0 is the card whose MAC is CARD; nothing else resolves.

    Patched on adapter_identity itself rather than as an attribute of
    the module under test, so these tests still *run* against a version
    that never imports it — the failure is then a wrong path chosen,
    which is the defect, rather than a missing attribute, which would
    only say the fix was absent.
    """
    import adapter_identity

    monkeypatch.setattr(
        adapter_identity, "hci_for",
        lambda adapter, fresh=True: (
            CARD_HCI if str(adapter) in (CARD, CARD_HCI) else None))


def test_a_mac_preference_selects_that_card(monkeypatch) -> None:
    bus = _bus_with({
        f"/org/bluez/{OTHER_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
        f"/org/bluez/{CARD_HCI}{SUFFIX}": {"Paired": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter=CARD)
    assert path == f"/org/bluez/{CARD_HCI}{SUFFIX}", (
        "the MAC must resolve to its current hciN and win the ranking, "
        "even over a Connected candidate on another card")


def test_a_legacy_hci_preference_still_works(monkeypatch) -> None:
    bus = _bus_with({
        f"/org/bluez/{OTHER_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
        f"/org/bluez/{CARD_HCI}{SUFFIX}": {"Paired": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter=CARD_HCI)
    assert path == f"/org/bluez/{CARD_HCI}{SUFFIX}"


def test_an_unresolvable_preference_falls_through(monkeypatch) -> None:
    # The card is absent right now.  Ranking is a preference, not a
    # filter, so we still return the best available candidate rather
    # than nothing.
    bus = _bus_with({
        f"/org/bluez/{OTHER_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter="AABBCCDDEEFF")
    assert path == f"/org/bluez/{OTHER_HCI}{SUFFIX}"


def test_no_preference_prefers_connected_then_bonded(monkeypatch) -> None:
    bus = _bus_with({
        f"/org/bluez/{CARD_HCI}{SUFFIX}": {"Paired": True},
        f"/org/bluez/{OTHER_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC)
    assert path == f"/org/bluez/{OTHER_HCI}{SUFFIX}"
