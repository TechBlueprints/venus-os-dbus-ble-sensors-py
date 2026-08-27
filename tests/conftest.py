"""
Pytest configuration for the BLE-charger test suite.

Tests are deliberately self-contained — they exercise the pure logic
and the mixin behaviours against captured byte fixtures.  They do NOT
require D-Bus, BlueZ, or a live device.  The two driver modules
(``ble_device_ip22_charger``, ``ble_device_orion_tr``) pull in dbus and
GLib at import time, so we provide minimal stub modules so the
shared-helper module (``ble_charger_common``) can import cleanly in a
test environment.

When running the suite:

    cd venus-os-dbus-ble-sensors-py
    PYTHONPATH=src/opt/victronenergy/dbus-ble-sensors-py:tests \\
        python3 -m pytest tests/ -v

Or via the wrapper script ``tests/run.sh`` if you don't want to type
the path.
"""
from __future__ import annotations

import os
import sys
import types

# Make the shared module importable without dragging in dbus/glib.
HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_DIR = os.path.normpath(os.path.join(
    HERE, "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
sys.path.insert(0, DRIVER_DIR)

# Stub out the heavy runtime imports ble_charger_common touches at
# module level (dbus, gi.repository.GLib, orion_tr_gatt).  Tests that
# need real behaviour from these get explicit fakes via fixtures below.

if "dbus" not in sys.modules:
    dbus = types.ModuleType("dbus")
    dbus.SystemBus = lambda: None
    dbus.SessionBus = lambda: None
    dbus.Interface = lambda *a, **kw: None
    dbus.DBusException = Exception

    # The dbus-python scalar/array subclasses ble_gatt_dbus._plain
    # converts away from.  Distinct classes so isinstance() discriminates
    # exactly as it does against the real ones; nothing constructs them.
    for _name in ("ByteArray", "Boolean", "Int16", "Int32", "Int64",
                  "UInt16", "UInt32", "UInt64", "Byte", "Double",
                  "String", "ObjectPath", "Signature", "Array",
                  "Dictionary", "Struct"):
        setattr(dbus, _name, type(_name, (), {}))

    # dbus.service, so modules that define a BlueZ agent (ble_gatt_dbus,
    # and anything importing it) can be imported under the stubs.  The
    # agent is never dispatched in tests; it only has to build.
    dbus_service = types.ModuleType("dbus.service")

    class _StubServiceObject:
        def __init__(self, *a, **kw):
            pass

    def _stub_method(*_a, **_kw):
        def decorate(fn):
            return fn
        return decorate

    dbus_service.Object = _StubServiceObject
    dbus_service.method = _stub_method
    dbus.service = dbus_service

    # dbus.bus, so dbus_bus.py can be imported: it subclasses
    # BusConnection at module scope.
    dbus_bus_mod = types.ModuleType("dbus.bus")

    class _StubBusConnection:
        TYPE_SESSION = 0
        TYPE_SYSTEM = 1

        def __new__(cls, *_a, **_kw):
            return object.__new__(cls)

        def get_is_connected(self):
            return True

        def close(self):
            pass

    dbus_bus_mod.BusConnection = _StubBusConnection
    dbus.bus = dbus_bus_mod

    # dbus.mainloop.glib, imported for its side effect (wiring the GLib
    # main loop) by every module that talks to BlueZ.  A package, not a
    # plain module, so `import dbus.mainloop.glib` resolves.
    dbus_mainloop = types.ModuleType("dbus.mainloop")
    dbus_mainloop.NULL_MAIN_LOOP = object()
    dbus_mainloop_glib = types.ModuleType("dbus.mainloop.glib")

    def _stub_dbus_gmainloop(*_a, **_kw):
        return None

    dbus_mainloop_glib.DBusGMainLoop = _stub_dbus_gmainloop
    dbus_mainloop.glib = dbus_mainloop_glib
    dbus.mainloop = dbus_mainloop

    sys.modules["dbus"] = dbus
    sys.modules["dbus.mainloop"] = dbus_mainloop
    sys.modules["dbus.mainloop.glib"] = dbus_mainloop_glib
    sys.modules["dbus.service"] = dbus_service
    sys.modules["dbus.bus"] = dbus_bus_mod

# vedbus comes from velib_python, which install.sh fetches onto the
# device rather than vendoring here — so it is absent in a checkout.
# Only the two names dbus_settings_service imports are needed; nothing
# in the tests dispatches through them.

if "vedbus" not in sys.modules:
    vedbus = types.ModuleType("vedbus")

    class _StubVeDbusItem:
        def __init__(self, *a, **kw):
            pass

    vedbus.VeDbusItemImport = _StubVeDbusItem
    vedbus.VeDbusItemExport = _StubVeDbusItem
    vedbus.VeDbusService = _StubVeDbusItem
    sys.modules["vedbus"] = vedbus

# velib_python's logger helper, fetched onto the device by install.sh
# rather than vendored — absent in a checkout, same as vedbus.

if "logger" not in sys.modules:
    _logger_mod = types.ModuleType("logger")

    def _setup_logging(*_a, **_kw):
        import logging
        return logging.getLogger()

    _logger_mod.setup_logging = _setup_logging
    sys.modules["logger"] = _logger_mod

if "gi" not in sys.modules:
    gi = types.ModuleType("gi")
    gi_repo = types.ModuleType("gi.repository")

    class _GLibStub:
        # Minimal facade — capture timeout_add invocations so tests
        # can assert scheduling behaviour without a real main loop.
        scheduled: list[tuple[int, object]] = []

        @classmethod
        def timeout_add(cls, ms, fn):
            cls.scheduled.append((ms, fn))
            return 0

        @classmethod
        def timeout_add_seconds(cls, seconds, fn):
            return cls.timeout_add(int(seconds) * 1000, fn)

        @classmethod
        def idle_add(cls, fn, *args):
            cls.scheduled.append((0, fn))
            return 0

    gi_repo.GLib = _GLibStub
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = gi_repo

# ble_role.BleRole is the base every role class inherits from.  Stub it
# ONCE, here, because four test modules used to each install their own
# shape into sys.modules and the last writer won: an arg-less
# ``type("BleRole", (), {})`` left BleRoleBattery inheriting a base whose
# __init__ is object.__init__, so ``BleRoleBattery(config)`` raised
# "object.__init__() takes exactly one argument" — but only when that
# module happened to be imported first, which made it an alphabetical
# accident rather than a reproducible failure.  Modules that want the
# stub now find it already present and leave it alone.
if "ble_role" not in sys.modules:
    sys.modules["ble_role"] = types.ModuleType("ble_role")
if not hasattr(sys.modules["ble_role"], "BleRole"):
    class _BleRoleBase:
        # Accepts the config argument the real base takes; roles call
        # super().__init__(config) unconditionally.
        def __init__(self, config: dict = None):
            self.config = config
            self.info: dict = {}

    sys.modules["ble_role"].BleRole = _BleRoleBase

# orion_tr_gatt provides AsyncGATTWriter — replace with a stub that
# tests can introspect for write_register calls.
if "orion_tr_gatt" not in sys.modules:
    otg = types.ModuleType("orion_tr_gatt")

    class _StubAsyncGATTWriter:
        def __init__(self, *a, **kw):
            self.busy = False
            self.calls: list[dict] = []

        def write_register(self, mac, passkey, register_id, value_bytes,
                           on_done=None):
            self.calls.append({
                "mac": mac,
                "passkey": passkey,
                "register_id": register_id,
                "value_bytes": bytes(value_bytes),
                "on_done": on_done,
            })
            # Default behaviour: report immediate success unless the
            # test wires .next_result = False.
            if on_done is not None:
                on_done(getattr(self, "next_result", True))

        def read_registers(self, mac, passkey, register_ids,
                           extra_writes=None, on_done=None):
            self.calls.append({
                "mac": mac,
                "passkey": passkey,
                "register_ids": list(register_ids),
                "extra_writes": list(extra_writes or []),
                "on_done": on_done,
            })
            if on_done is not None:
                on_done(getattr(self, "next_result", True),
                        getattr(self, "next_read", {}))

    otg.AsyncGATTWriter = _StubAsyncGATTWriter
    sys.modules["orion_tr_gatt"] = otg


# pytest fixtures — real ones, not stubs.
import pytest  # noqa: E402

class FakeRoleService:
    """Drop-in for ``DbusRoleService``-shaped objects in unit tests.

    Behaves as a dict-by-path: ``rs[path] = value`` writes, ``rs[path]``
    reads.  Reads on an unwritten path raise ``KeyError`` so the
    mixin's ``KeyError`` fallback in ``_publish_alarms`` is exercised.
    """

    def __init__(self, initial: dict | None = None):
        self.values: dict[str, object] = dict(initial or {})

    def __setitem__(self, key, value):
        self.values[key] = value

    def __getitem__(self, key):
        return self.values[key]

    def __contains__(self, key):
        return key in self.values

class FakeDbusSettings:
    """In-memory stand-in for ``DbusSettingsService``."""

    def __init__(self, initial: dict | None = None):
        self.values: dict[str, object] = dict(initial or {})
        self.created: list[str] = []

    def set_item(self, path, def_value=None, min_value=0, max_value=0,
                 silent=False, callback=None):
        if path not in self.values:
            self.values[path] = def_value
            self.created.append(path)
        return self  # not a real VeDbusItemImport, but tests don't need it

    def set_value(self, path, value):
        self.values[path] = value

    def try_get_value(self, path):
        return self.values.get(path)

    def get_value(self, path):
        return self.values.get(path)

@pytest.fixture
def fake_role():
    """A pristine FakeRoleService each test."""
    return FakeRoleService()

@pytest.fixture
def fake_settings():
    """A pristine FakeDbusSettings each test."""
    return FakeDbusSettings()

@pytest.fixture
def writer():
    """A fresh stub AsyncGATTWriter each test."""
    from orion_tr_gatt import AsyncGATTWriter
    w = AsyncGATTWriter()
    yield w

@pytest.fixture(autouse=True)
def reset_glib_scheduled():
    """Clear GLib.timeout_add capture between tests."""
    from gi.repository import GLib
    GLib.scheduled.clear()
    yield
    GLib.scheduled.clear()
