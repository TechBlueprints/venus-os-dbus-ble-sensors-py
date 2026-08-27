"""Discovery off means adopt nothing new, and keep nothing unwanted.

The GUI has "Enable" and "Continuous scanning".  Continuous scanning is
the discovery switch, and it was wired to exactly one thing — the
controller's filter policy — so with it OFF we still adopted every
Victron device in range, built a device object, wrote settings for it,
and (worse) opened a GATT session to it from init().

On the prod gateway that produced 59 disabled device entries from
neighbours' hardware and 139 discovery bursts for one unreachable
SmartShunt, and those sessions are what crashed bluetoothd.

Intended semantics, now implemented:
  discovery ON   -> adopt new devices, create them DISABLED, do not connect
  discovery OFF  -> adopt nothing new; sweep everything nobody enabled,
                    including its D-Bus objects and stored settings
Configured devices keep working either way.
"""
from __future__ import annotations

import pytest

# dbus_ble_sensors uses dataclass(slots=True), which needs Python 3.10+.
# The device runs 3.12; this checkout may not.  Skip rather than fail,
# and note it, so a green local run is never mistaken for coverage of
# this module.
import sys

# dbus_ble_sensors uses dataclass(slots=True), which needs Python 3.10+.
# The device runs 3.12; this checkout may not.  Skip rather than fail,
# and say why, so a green local run is never mistaken for coverage of
# this module.
if sys.version_info < (3, 10):
    pytest.skip("dbus_ble_sensors needs Python 3.10+ (dataclass slots); "
                "device runs 3.12", allow_module_level=True)


class _Svc:
    """Stands in for DbusBleService."""

    def __init__(self, enabled=(), settings=()):
        self._enabled = set(enabled)
        self.settings_keys = list(settings)
        self.purged = []
        self.continuous = False

    def get_continuous_scan(self):
        return self.continuous

    def is_device_enabled(self, info):
        return info["dev_id"] in self._enabled

    def purge_device_settings(self, dev_id):
        self.purged.append(dev_id)
        self.settings_keys = [k for k in self.settings_keys
                              if k.split("/", 1)[0] != dev_id]


class _Device:
    def __init__(self, dev_id, mac):
        self.info = {"dev_id": dev_id, "dev_mac": mac}
        self.deleted = False

    def delete(self):
        self.deleted = True


def _sensors(svc, devices):
    """A DbusBleSensors with just enough wired for the sweep."""
    import dbus_ble_sensors as mod
    obj = mod.DbusBleSensors.__new__(mod.DbusBleSensors)
    obj._dbus_ble_service = svc
    obj._known_mac = dict(devices)
    obj._configured_macs = set(devices)
    obj._throttled = False
    return obj


def test_enabled_devices_survive_the_sweep() -> None:
    svc = _Svc(enabled={"orion_tr_aabb"}, settings=["orion_tr_aabb/charger/Enabled"])
    dev = _Device("orion_tr_aabb", "aabb")
    s = _sensors(svc, {"aabb": dev})

    s._purge_unenabled_devices()

    assert dev.deleted is False
    assert "aabb" in s._known_mac
    assert svc.purged == [], "an enabled device must never be purged"


def test_unenabled_devices_are_deleted_with_their_settings() -> None:
    svc = _Svc(enabled=set(), settings=["smartshunt_c39b/battery/Enabled",
                                        "smartshunt_c39b/battery/CustomName"])
    dev = _Device("smartshunt_c39b", "c39b")
    s = _sensors(svc, {"c39b": dev})

    s._purge_unenabled_devices()

    assert dev.deleted is True, "the device object must go"
    assert "c39b" not in s._known_mac, "and its place in the store"
    assert "c39b" not in s._configured_macs, (
        "and it must look like a stranger again, or the discovery gate "
        "would keep re-adopting it")
    assert svc.purged == ["smartshunt_c39b"], (
        "stored settings must go too — detaching the proxy leaves the "
        "entry behind, which is what accumulated 59 of them")
    assert svc.settings_keys == []


def test_a_mixed_set_purges_only_the_unwanted() -> None:
    svc = _Svc(enabled={"orion_tr_aabb"})
    keep, drop = _Device("orion_tr_aabb", "aabb"), _Device("ip22_ccdd", "ccdd")
    s = _sensors(svc, {"aabb": keep, "ccdd": drop})

    s._purge_unenabled_devices()

    assert keep.deleted is False and drop.deleted is True
    assert set(s._known_mac) == {"aabb"}


def test_a_device_whose_state_cannot_be_read_is_kept() -> None:
    # Deleting on an error would throw away configured gear over a
    # transient settings failure.  Keep, and say so.
    class _Broken(_Svc):
        def is_device_enabled(self, info):
            raise RuntimeError("settings unavailable")

    svc = _Broken()
    dev = _Device("orion_tr_aabb", "aabb")
    s = _sensors(svc, {"aabb": dev})

    s._purge_unenabled_devices()
    assert dev.deleted is False
    assert "aabb" in s._known_mac


def test_delete_failure_still_removes_it_from_the_store() -> None:
    # Otherwise a device that raises on delete is purged forever after,
    # once per toggle.
    class _Stubborn(_Device):
        def delete(self):
            raise RuntimeError("teardown failed")

    svc = _Svc()
    dev = _Stubborn("ip22_ccdd", "ccdd")
    s = _sensors(svc, {"ccdd": dev})

    s._purge_unenabled_devices()
    assert "ccdd" not in s._known_mac
    assert svc.purged == ["ip22_ccdd"]
