"""A fully-disabled device's frames stop at the tap; its presence does not.

Prod 2026-09-06: 25.8 of 50.9 accepted advertisements per second (51 %)
belonged to devices with every role disabled -- 23.9/s from two disabled
SmartShunts alone.  Each crossed to the main loop via GLib.idle_add,
was dispatched, and was dropped by the enabled check.

The skip must sit AFTER the presence write and must NOT use the tap's
ignore set: _tap_seen_macs is what _prune_tick uses to refresh a known
device's TTL, so a MAC hidden from it expires and the device vanishes
from the GUI together with its settings entry.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types

import dbus_ble_sensors as mod

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))


class _Svc:
    def __init__(self, enabled_macs):
        self.enabled_macs = set(enabled_macs)
    def is_device_enabled(self, info):
        return info["dev_mac"] in self.enabled_macs


def _sensors(enabled_macs=(), known=()):
    s = mod.DbusBleSensors.__new__(mod.DbusBleSensors)
    s._tap_disabled_macs = set()
    s._dbus_ble_service = _Svc(enabled_macs)
    s._known_mac = mod.DatedDict(ttl=3600)
    for mac in known:
        s._known_mac[mac] = types.SimpleNamespace(info={"dev_mac": mac})
    return s


def _dev(mac):
    return types.SimpleNamespace(info={"dev_mac": mac})


def test_adoption_seeds_only_fully_disabled_devices() -> None:
    s = _sensors(enabled_macs={"aaaaaaaaaaaa"})
    s._refresh_tap_disabled(_dev("aaaaaaaaaaaa"))
    s._refresh_tap_disabled(_dev("bbbbbbbbbbbb"))
    assert s._tap_disabled_macs == {"bbbbbbbbbbbb"}


def test_an_enable_flip_forwards_again_and_a_disable_flip_stops() -> None:
    s = _sensors(enabled_macs=set())
    s._refresh_tap_disabled(_dev("bbbbbbbbbbbb"))
    assert "bbbbbbbbbbbb" in s._tap_disabled_macs
    s._dbus_ble_service.enabled_macs.add("bbbbbbbbbbbb")
    s._refresh_tap_disabled(_dev("bbbbbbbbbbbb"))
    assert "bbbbbbbbbbbb" not in s._tap_disabled_macs
    s._dbus_ble_service.enabled_macs.clear()
    s._refresh_tap_disabled(_dev("bbbbbbbbbbbb"))
    assert "bbbbbbbbbbbb" in s._tap_disabled_macs


def test_name_identified_devices_are_never_added() -> None:
    """They are keyed by identity, not MAC, and served by the name path."""
    s = _sensors()
    s._refresh_tap_disabled(types.SimpleNamespace(info={"dev_id": "microair_easystart_89fe"}))
    assert s._tap_disabled_macs == set()


def test_unreadable_enabled_state_fails_open_to_forwarding() -> None:
    s = _sensors()
    s._dbus_ble_service = types.SimpleNamespace(
        is_device_enabled=lambda info: (_ for _ in ()).throw(RuntimeError("bus")))
    s._refresh_tap_disabled(_dev("cccccccccccc"))
    assert s._tap_disabled_macs == set(), "a read failure must not silence a device"


def test_reconcile_drops_a_mac_whose_device_expired() -> None:
    """Otherwise a returning device's first frame is skipped at the tap
    and it can never be re-adopted."""
    s = _sensors(known={"dddddddddddd"})
    s._tap_disabled_macs = {"dddddddddddd", "eeeeeeeeeeee"}
    s._tap_disabled_macs.intersection_update(s._known_mac.keys())
    assert s._tap_disabled_macs == {"dddddddddddd"}


def test_reconcile_uses_keys_which_does_not_refresh_ttl() -> None:
    """The reconcile must not extend a device's life as a side effect.

    keys() and iteration read the store without touching expiry.
    Membership (``mac in d``) is different: DatedDict.__contains__
    deliberately calls the refreshing getter, so a reconcile written with
    ``in`` would keep every disabled device alive forever.  That is why
    _prune_tick uses intersection_update(keys()) and nothing else.
    """
    d = mod.DatedDict(ttl=100)
    d["k"] = 1
    _, exp0 = d._store["k"]
    list(d.keys()); list(iter(d))
    _, exp1 = d._store["k"]
    assert exp1 == exp0, "keys()/iteration must not refresh"
    "k" in d
    _, exp2 = d._store["k"]
    assert exp2 >= exp1, "membership refreshes -- documented, and avoided by the reconcile"
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    i = src.index("self._tap_disabled_macs.intersection_update")
    assert "self._known_mac.keys()" in src[i:i+80], "reconcile must use keys(), not membership"


def test_tap_skip_sits_after_presence_and_before_the_main_loop_hop() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    body = src[src.index("def _on_advertisement"):src.index("def _tap_thread")]
    presence = body.index("tap_seen[mac] = now")
    skip = body.index("if mac in tap_disabled:")
    name_hop = body.index("GLib.idle_add(self._glib_process_name_tap")
    mfg_hop = body.index("GLib.idle_add(self._glib_process_tap")
    assert presence < skip, "presence must be recorded before the skip (TTL retention)"
    assert name_hop < skip, "name-identified presence path must be unaffected"
    assert skip < mfg_hop, "the skip must precede the manufacturer-data hop"
    assert "_tap_ignored_macs" not in body[skip:skip+200], "must not use the ignore set"


def test_service_fans_the_flip_out_and_isolates_failures() -> None:
    spec = importlib.util.spec_from_file_location(
        "_dbus_ble_service_for_fanout", os.path.join(SRC, "dbus_ble_service.py"))
    before = dict(sys.modules)
    try:
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        svc = m.DbusBleService.__new__(m.DbusBleService)
        svc._enabled_changed_callbacks = []
        got = []
        svc.register_enabled_changed_callback(lambda dev: (_ for _ in ()).throw(RuntimeError("boom")))
        svc.register_enabled_changed_callback(got.append)
        dev = object()
        svc.notify_device_enabled_changed(dev)
        assert got == [dev], "a failing listener must not stop the next one"
    finally:
        for n in [n for n in sys.modules if n not in before]:
            del sys.modules[n]
        sys.modules.update(before)
