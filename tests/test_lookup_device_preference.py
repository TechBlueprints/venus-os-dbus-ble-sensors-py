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


# --- The configured GATT pool is a constraint, not a preference -------
#
# ble-connect.conf bounds which adapters GATT links may be placed on.
# bcmv2 ranks its own placement against that list, but a device BlueZ
# already knows is resolved to an object path first, and the path names
# the adapter the link will use -- so the pool was only half-enforced.
#
# Observed on dev: the IP22's stored PreferredAdapter was 00019540C333,
# the pack's link radio, learned back when it was the card that worked.
# ble-connect.conf named 00:01:95:24:24:CC.  The hint won, and every
# IP22 link landed on the radio the config exists to keep clear -- the
# exact sharing that file's comments exist to prevent.

POOL_HCI = CARD_HCI        # the one card ble-connect.conf permits
EXCLUDED_HCI = OTHER_HCI   # e.g. the pack's link radio


@pytest.fixture
def _pool(monkeypatch):
    import ble_catcher
    monkeypatch.setattr(ble_catcher, "link_adapter_names",
                        lambda: {POOL_HCI})


def test_a_preference_outside_the_pool_is_ignored(_pool) -> None:
    """A learned hint must not defeat an operator constraint."""
    bus = _bus_with({
        f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
        f"/org/bluez/{POOL_HCI}{SUFFIX}": {"Paired": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(
        bus, MAC, prefer_adapter=EXCLUDED_HCI)
    assert path == f"/org/bluez/{POOL_HCI}{SUFFIX}", (
        "PreferredAdapter named a card outside ble-connect.conf's pool; "
        "the pooled candidate must win anyway")


def test_pool_outranks_connected_and_bonded(_pool) -> None:
    """Dropping the bad preference alone is not enough.

    With no preference at all, the old ranking fell through to
    connected-then-bonded -- which is precisely the excluded card, since
    that is where the device bonded.
    """
    bus = _bus_with({
        f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
        f"/org/bluez/{POOL_HCI}{SUFFIX}": {},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC)
    assert path == f"/org/bluez/{POOL_HCI}{SUFFIX}", (
        "a Connected+Paired candidate on an excluded card must not beat "
        "an unbonded candidate on a permitted one")


def test_an_unconfigured_pool_changes_nothing(monkeypatch) -> None:
    """No ble-connect.conf means every adapter is a candidate."""
    import ble_catcher
    monkeypatch.setattr(ble_catcher, "link_adapter_names", lambda: set())
    bus = _bus_with({
        f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
        f"/org/bluez/{CARD_HCI}{SUFFIX}": {"Paired": True},
    })
    path, _ = ble_gatt_dbus.lookup_device(bus, MAC, prefer_adapter=CARD)
    assert path == f"/org/bluez/{CARD_HCI}{SUFFIX}"


def test_only_out_of_pool_candidate_is_still_used(_pool, caplog) -> None:
    """Rank, do not filter.

    If BlueZ knows the device only on an excluded adapter, refusing
    would take a working device off the bus to honour a preference about
    which radio it uses.  It must still be reported.
    """
    bus = _bus_with({
        f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
    })
    ble_gatt_dbus._warned_out_of_pool.discard(MAC)
    with caplog.at_level("WARNING"):
        path, _ = ble_gatt_dbus.lookup_device(bus, MAC)
    assert path == f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}"
    assert any("outside the configured GATT pool" in r.message
               for r in caplog.records), (
        "falling outside the pool must be visible, not silent")


def test_the_out_of_pool_warning_is_logged_once_per_device(_pool, caplog) -> None:
    """It is a standing condition, not an event.

    It holds for every connect until the config changes or the device
    bonds on a pooled card.  Unthrottled it fired on every telemetry
    cycle -- observed three times in one minute on dev -- which is the
    same steady-state accumulation that made the discovery gate's
    refusal line worth fixing.
    """
    ble_gatt_dbus._warned_out_of_pool.discard(MAC)
    bus = _bus_with({
        f"/org/bluez/{EXCLUDED_HCI}{SUFFIX}": {"Paired": True, "Connected": True},
    })
    with caplog.at_level("WARNING"):
        for _ in range(5):
            ble_gatt_dbus.lookup_device(bus, MAC)
    hits = [r for r in caplog.records
            if "outside the configured GATT pool" in r.message]
    assert len(hits) == 1, (
        f"expected one warning across five lookups, got {len(hits)}")
