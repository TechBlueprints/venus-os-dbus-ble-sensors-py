"""Links a previous life left behind are dropped before the tap opens.

svc -t ends this process with os._exit(0); a crash or kill ends it with
nothing.  bleak never sends Disconnect either way, and bluetoothd keeps
the LE link with no client behind it.  A connected peripheral does not
advertise, so the next life cannot hear the device on ANY card.

Prod, 2026-09-02 19:58Z: a restart orphaned easystart_89fe's link on
hci9 -- a card outside both the allowlist and ble-connect.conf.  The new
life logged "silent until its A/C runs" for ~17 minutes while the A/C
ran.  A manual Device1.Disconnect at 20:15:00Z had it re-advertising and
re-linked on a pooled card within ten seconds.

At our own startup we hold nothing, so any link to one of OUR addresses
is stale by construction.  Only our addresses are touched.
"""
from __future__ import annotations

import os
import re

import ble_gatt_dbus

OURS = "38:18:2B:FB:9B:76"
THEIRS = "AB:80:72:54:E0:B4"


class _Obj:
    """A bus object: the root answers GetManagedObjects, devices Disconnect."""
    disconnected: list = []
    raise_for: set = set()

    def __init__(self, path, objects):
        self._path = path
        self._objects = objects

    def GetManagedObjects(self):
        return self._objects

    def Disconnect(self):
        if self._path in _Obj.raise_for:
            raise RuntimeError("org.bluez.Error.NotConnected")
        _Obj.disconnected.append(self._path)


class _Bus:
    def __init__(self, devices):
        self.objects = {
            p: {ble_gatt_dbus.DEVICE_INTERFACE: props} for p, props in devices.items()
        }
        self.get_object_calls = 0

    def get_object(self, name, path, introspect=True):
        self.get_object_calls += 1
        return _Obj(path, self.objects)


def _install(monkeypatch):
    # dbus.Interface(obj, iface) -> obj, so the stub object answers directly.
    monkeypatch.setattr(ble_gatt_dbus.dbus, "Interface", lambda obj, *a, **kw: obj)
    _Obj.disconnected = []
    _Obj.raise_for = set()


def test_our_connected_device_is_dropped_on_any_adapter(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({
        "/org/bluez/hci9/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76"})
    assert _Obj.disconnected == ["/org/bluez/hci9/dev_38_18_2B_FB_9B_76"], (
        "hci9 is outside the pool and the allowlist; the sweep must not care")
    assert got == [("/org/bluez/hci9/dev_38_18_2B_FB_9B_76", OURS)]


def test_address_spelling_does_not_matter(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({"/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True}})
    ble_gatt_dbus.disconnect_stale_links(bus, {"38:18:2B:FB:9B:76"})
    assert len(_Obj.disconnected) == 1


def test_not_connected_and_not_ours_are_left_alone(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({
        "/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": False},
        "/org/bluez/hci4/dev_AB_80_72_54_E0_B4": {"Address": THEIRS, "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76"})
    assert _Obj.disconnected == [], (
        "a disconnected device needs nothing; another consumer's live "
        "link (serialbattery's pack) is never ours to drop")
    assert got == []


def test_one_failure_does_not_stop_the_sweep(monkeypatch) -> None:
    _install(monkeypatch)
    _Obj.raise_for = {"/org/bluez/hci1/dev_38_18_2B_FB_9B_76"}
    bus = _Bus({
        "/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True},
        "/org/bluez/hci9/dev_38_18_2B_FA_24_FA": {"Address": "38:18:2B:FA:24:FA", "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76", "38182bfa24fa"})
    assert [p for p, _ in got] == ["/org/bluez/hci9/dev_38_18_2B_FA_24_FA"]


def test_nothing_configured_means_no_bus_traffic(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({})
    assert ble_gatt_dbus.disconnect_stale_links(bus, set()) == []
    assert bus.get_object_calls == 0


def test_bluez_unreachable_does_not_raise(monkeypatch) -> None:
    _install(monkeypatch)

    class _Dead:
        def get_object(self, *a, **kw):
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

    assert ble_gatt_dbus.disconnect_stale_links(_Dead(), {"38182bfb9b76"}) == [], (
        "a startup sweep must never stop the service from starting")


def test_start_sweeps_before_the_tap_opens() -> None:
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
               "src", "opt", "victronenergy", "dbus-ble-sensors-py",
               "dbus_ble_sensors.py")).read()
    start = src.index("    def start(self):")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "disconnect_stale_links(" in body, "start() must run the sweep"
    assert body.index("disconnect_stale_links(") < body.index("self._start_tap()"), (
        "the sweep must run before the tap opens, so the freed device is "
        "heard on our own cards from the first advertisement")
    assert "self._configured_macs" in body and "self._name_device_macs" in body, (
        "both address stores feed the sweep: HEX devices and EasyStarts alike")
