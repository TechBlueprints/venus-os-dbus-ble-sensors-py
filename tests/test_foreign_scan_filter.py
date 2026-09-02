"""A name-identified advertisement heard via a card we do not scan is
someone else's scan, and must not steer our link placement.

The tap is HCI_CHANNEL_MONITOR: it sees every adapter's traffic and
stamps each report with that adapter's index.  The EasyStart driver
treats that index as "the card that provably heard it" and hands
Adapter1.ConnectDevice that card -- correct when the scan is ours, and
exactly wrong when it is not.

Prod, 2026-09-02 19:40Z: the night watch ran passive RSSI surveys on a
ninth, unconfigured radio.  Its reports reached our tap stamped hci9,
easystart_89fe was handed to ConnectDevice on hci9, and the link came up
outside ble-connect.conf's pool with no warning -- lookup_device's pool
ranking never runs on this path.  A card in nobody's config, and one
that could be assigned to a BMS tomorrow.

The gate is adapter-allowlist membership: the static fact "we scan
here".  Not _scan_enabled_adapters, which the silence-warning and
throttle paths clear transiently while the radio scan is still ours.
"""
from __future__ import annotations

import logging
import os
import re

import pytest

import dbus_ble_sensors as mod


class _Svc:
    def get_continuous_scan(self):
        return False


class _FakeEasyStart:
    calls: list = []

    def __init__(self, identity):
        self.identity = identity
        self.info = {"dev_id": f"microair_{identity}"}

    @staticmethod
    def identity_from_name(name):
        return "easystart_" + name.split("_", 1)[1].lower()

    def handle_name_advertisement(self, mac, adv_name, rssi, address_type,
                                  adapter_index):
        _FakeEasyStart.calls.append((self.identity, adapter_index))


def _sensors(allowed_keys):
    """A DbusBleSensors with only what _process_name_advertisement touches."""
    s = mod.DbusBleSensors.__new__(mod.DbusBleSensors)
    s._foreign_scan_logged = set()
    s._refusal_logged = set()
    s._known_mac = {}
    s._configured_dev_ids = set()
    s._name_device_macs = {}
    s._save_name_device_macs = lambda: None
    s._dbus_ble_service = _Svc()
    # adapter_identity.canonical("hciN") has no backend in tests and
    # degrades to the name itself, so keys here are "hci0", "hci9".
    s._adapter_allowed = lambda key, name: key in allowed_keys
    return s


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr(mod.BleDevice, "NAME_CLASSES",
                        {"EasyStart_": _FakeEasyStart})
    _FakeEasyStart.calls.clear()
    yield


def test_an_advert_from_our_own_card_reaches_the_driver() -> None:
    s = _sensors(allowed_keys={"hci0"})
    dev = _FakeEasyStart("easystart_89fe")
    s._known_mac["easystart_89fe"] = dev

    s._process_name_advertisement("38182bfb9b76", "EasyStart_89FE",
                                  rssi=-60, address_type=0, adapter_index=0)

    assert _FakeEasyStart.calls == [("easystart_89fe", 0)]


def test_an_advert_from_a_foreign_scan_is_ignored(caplog) -> None:
    s = _sensors(allowed_keys={"hci0"})
    dev = _FakeEasyStart("easystart_89fe")
    s._known_mac["easystart_89fe"] = dev

    with caplog.at_level(logging.INFO):
        s._process_name_advertisement("38182bfb9b76", "EasyStart_89FE",
                                      rssi=-60, address_type=0,
                                      adapter_index=9)

    assert _FakeEasyStart.calls == [], (
        "an advert stamped with a card we do not scan on came from "
        "another process's scan; following it puts our link on their card")
    assert s._name_device_macs == {}, "nothing learned from a foreign scan"
    assert any("we do not scan on it" in r.message for r in caplog.records)


def test_the_foreign_card_is_reported_once(caplog) -> None:
    s = _sensors(allowed_keys={"hci0"})
    s._known_mac["easystart_89fe"] = _FakeEasyStart("easystart_89fe")
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            s._process_name_advertisement("38182bfb9b76", "EasyStart_89FE",
                                          -60, 0, adapter_index=9)
    hits = [r for r in caplog.records if "we do not scan on it" in r.message]
    assert len(hits) == 1, "a standing condition, one line per adapter"


def test_a_foreign_advert_does_not_adopt_a_new_device() -> None:
    """The filter must sit before the discovery gate and adoption."""
    s = _sensors(allowed_keys={"hci0"})
    s._process_name_advertisement("38182bfb9b76", "EasyStart_89FE",
                                  -60, 0, adapter_index=9)
    assert s._known_mac == {}
    assert s._refusal_logged == set(), (
        "a foreign advert must not even reach the gate's refusal log")


def test_the_logged_set_is_declared_once_before_use() -> None:
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
               "src", "opt", "victronenergy", "dbus-ble-sensors-py",
               "dbus_ble_sensors.py")).read()
    decls = [m.start() for m in
             re.finditer(r"self\._foreign_scan_logged\s*(:\s*set\s*)?=\s*set\(\)", src)]
    use = src.index("if adapter_key not in self._foreign_scan_logged:")
    assert len(decls) == 1 and decls[0] < use, (
        "declared exactly once, before use — the _configured_macs lesson")
