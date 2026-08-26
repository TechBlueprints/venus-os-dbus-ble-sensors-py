"""Repeated settings reads must not mint a D-Bus match rule each time.

`bus.get_object()` on a well-known name installs a NameOwnerChanged
watch keyed to the resolved unique name, so that the proxy can notice
its service vanishing.  That rule belongs to the CONNECTION, not to the
Python object, so it outlives the import and counts against
max_match_rules_per_connection (1024 by default).

createsignal=False suppresses the PropertiesChanged rule and does
nothing about the name-owner one — which is exactly why a function that
carefully passed createsignal=False still leaked.  Measured on the prod
Cerbo: 435 identical copies of

    type='signal',interface='org.freedesktop.DBus',
    member='NameOwnerChanged',arg0=':1.77'

on one connection in ~40 minutes of process life, one every ~5.5s.
"""
from __future__ import annotations

import sys
import types

import pytest


class _Proxy:
    def __init__(self, value, exists=True):
        self._value = value
        self._exists = exists

    def GetValue(self):
        if not self._exists:
            raise RuntimeError("no such path")
        return self._value


class _Import:
    """Counts constructions — one per path is the budget."""

    constructed: list = []

    def __init__(self, bus, service, path, eventCallback=None,
                 createsignal=True, initialValue=None):
        type(self).constructed.append(path)
        self.path = path
        self._value = _VALUES.get(path)
        self.exists = path in _VALUES

    def get_value(self):
        return self._value


_VALUES = {"/Settings/Devices/x/Present": 7}


@pytest.fixture
def settings(monkeypatch):
    _Import.constructed = []
    vedbus = types.ModuleType("vedbus")
    vedbus.VeDbusItemImport = _Import
    vedbus.VeDbusItemExport = object
    monkeypatch.setitem(sys.modules, "vedbus", vedbus)

    import importlib
    import dbus_settings_service
    mod = importlib.reload(dbus_settings_service)

    svc = mod.DbusSettingsService.__new__(mod.DbusSettingsService)
    svc._bus = object()
    svc._paths = {}
    return svc


def test_repeated_reads_construct_one_import(settings) -> None:
    for _ in range(50):
        assert settings.try_get_value("/Settings/Devices/x/Present") == 7

    assert _Import.constructed.count("/Settings/Devices/x/Present") <= 2, (
        f"one import per path is the budget; got "
        f"{_Import.constructed.count('/Settings/Devices/x/Present')} — each "
        f"one is a match rule against a ceiling of 1024")


def test_a_missing_path_also_stops_constructing(settings) -> None:
    # The polling case that actually leaked on prod: a setting that is
    # not there yet, read every few seconds forever.
    for _ in range(50):
        assert settings.try_get_value("/Settings/Devices/x/Absent") is None

    assert _Import.constructed.count("/Settings/Devices/x/Absent") <= 2


def test_distinct_paths_still_get_their_own(settings) -> None:
    settings.try_get_value("/Settings/Devices/x/Present")
    settings.try_get_value("/Settings/Devices/x/Absent")
    assert set(_Import.constructed) == {
        "/Settings/Devices/x/Present", "/Settings/Devices/x/Absent"}
