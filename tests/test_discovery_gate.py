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
  discovery OFF  -> adopt nothing new
Configured devices keep working either way.

Cleaning up what accumulated BEFORE this gate existed is a one-off
operational job, not product behaviour: once nothing new is adopted,
there is nothing for a permanent sweeper to do.  Prod's 63 stale
entries were removed by hand.
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
    raise RuntimeError(
        "dbus_ble_sensors needs Python 3.10+ (dataclass slots) and the "
        "device runs 3.12 — run ./tests/run.sh, which picks a matching "
        "interpreter.  This used to skip, which meant a green suite "
        "covered none of this module.")


class _Svc:
    """Stands in for DbusBleService."""

    def __init__(self, enabled=(), settings=(), stale=()):
        self._enabled = set(enabled)
        self.settings_keys = list(settings)
        self._stale = list(stale)
        self.purged = []
        self.continuous = False

    def unenabled_device_ids(self):
        return list(self._stale)

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


def test_the_gate_uses_the_any_role_enabled_rule() -> None:
    """A device counts as wanted if ANY of its roles is on.

    Reading only the first role's flag is what made an earlier census
    call the Tech Cabinet Ruuvi disabled when its temperature role was
    live and only movement was off — the same adjacent-predicate shape
    that has cost this project repeatedly.
    """
    import dbus_ble_service
    import inspect
    src = inspect.getsource(dbus_ble_service.DbusBleService.is_device_enabled)
    assert "for role_name in device_info['roles']" in src
    assert "return True" in src, "any role enabled means the device is wanted"


def test_configured_macs_survives_construction() -> None:
    """The loaded set must not be re-initialised after it is filled.

    It was: __init__ loaded 20 MACs from stored settings and then, forty
    lines later, assigned an empty set over them.  The gate then treated
    every configured device as a stranger and prod adopted NOTHING —
    four Orion-TRs, six Ruuvis and a Mopeka all logged "not adopting"
    while their settings sat right there.

    Pinned as source order because the failure is an ordering bug that
    a mocked constructor cannot reproduce.
    """
    import os
    import re

    src = open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", "opt",
        "victronenergy", "dbus-ble-sensors-py", "dbus_ble_sensors.py")).read()

    assigns = [m.start() for m in
               re.finditer(r"self\._configured_macs\s*(:\s*set\s*)?=\s*set\(\)", src)]
    load = src.index("self._configured_macs = self._dbus_ble_service.configured_macs()")
    assert len(assigns) == 1, (
        f"expected exactly one empty-set initialisation, found {len(assigns)}")
    assert assigns[0] < load, (
        "the empty-set initialisation must come BEFORE the load, or it "
        "wipes the configured devices and the gate rejects everything")
