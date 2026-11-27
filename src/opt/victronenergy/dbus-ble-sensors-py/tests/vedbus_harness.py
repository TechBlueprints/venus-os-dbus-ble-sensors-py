"""Real vedbus over a recording fake bus, for counting the signals we emit.

Off-device tests stub ``vedbus`` out entirely (conftest.py), so nothing in
the suite exercised vedbus's own batching, and a test fake that counted
"one signal per batch" or "one per path" was only ever a restatement of
what its author believed.  This module loads the real ``vedbus.py`` (a
copy of the one installed on dev-cerbo, ``tests/fixtures/velib_python/``,
md5 76438c1ea75b0b012d4cc7b23a03e310) against a fake ``dbus`` in which a
``@dbus.service.signal`` method records its call.  In dbus-python, calling
a method decorated with ``@dbus.service.signal`` is what emits the signal,
so the record is the list of signals the process would have put on the bus.

``make_role_service`` returns the real ``DbusRoleService`` (loaded from
disk, past conftest's stub) wrapping a real ``VeDbusService``.
"""
import copy
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_VEDBUS_PATH = os.path.join(_HERE, 'fixtures', 'velib_python', 'vedbus.py')
_DRS_PATH = os.path.join(_HERE, '..', 'dbus_role_service.py')

# Every signal emitted by any harness object, in order:
# (signal name, object path, payload).
EMITTED = []


def _signal(*_args, **_kwargs):
    def wrap(fn):
        def emit(self, *args):
            # Copied: dbus-python serializes the payload at the emit, and
            # vedbus clears its ``changes`` dict straight after.
            EMITTED.append((fn.__name__, self.__dbus_object_path__,
                            copy.deepcopy(args[0]) if args else None))
            return fn(self, *args)
        emit.__name__ = fn.__name__
        return emit
    return wrap


def _method(*_args, **_kwargs):
    return lambda fn: fn


class _Object:
    def __init__(self, conn=None, object_path=None, *args, **kwargs):
        self.__dbus_object_path__ = object_path

    def remove_from_connection(self):
        pass


class _BusName:
    def __init__(self, name, bus, do_not_queue=False):
        self.name = name

    def __del__(self):
        pass


class FakeBus:
    """Stands in for the BusConnection; vedbus only hands it to objects."""


def _load_vedbus():
    fake_dbus = types.ModuleType('dbus')
    fake_service = types.ModuleType('dbus.service')
    fake_service.Object = _Object
    fake_service.BusName = _BusName
    fake_service.signal = _signal
    fake_service.method = _method
    fake_dbus.service = fake_service
    fake_dbus.Byte = type('Byte', (int,), {})
    fake_dbus.SessionBus = fake_dbus.SystemBus = lambda *a, **k: FakeBus()
    fake_ve_utils = types.ModuleType('ve_utils')
    # Identity wrap: the payload stays comparable plain Python.
    fake_ve_utils.wrap_dbus_value = lambda v: v
    fake_ve_utils.unwrap_dbus_value = lambda v: v

    swapped = {n: sys.modules.get(n) for n in ('dbus', 'dbus.service', 've_utils')}
    sys.modules.update({'dbus': fake_dbus, 'dbus.service': fake_service,
                        've_utils': fake_ve_utils})
    try:
        spec = importlib.util.spec_from_file_location('_harness_vedbus', _VEDBUS_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for name, prev in swapped.items():
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
    return mod


vedbus = _load_vedbus()


def _ensure_names(module_name, **names):
    """Give a (possibly conftest-stubbed) module the names our import needs."""
    mod = sys.modules.get(module_name)
    if mod is None:
        mod = sys.modules[module_name] = types.ModuleType(module_name)
    for key, value in names.items():
        if not hasattr(mod, key):
            setattr(mod, key, value)


def _load_role_service_module():
    # Placeholders only: make_role_service never constructs a settings
    # service or asks the BLE service anything.
    _ensure_names('dbus_settings_service',
                  DbusSettingsService=type('DbusSettingsService', (), {}))
    _ensure_names('dbus_ble_service',
                  DbusBleService=type('DbusBleService', (), {'get': staticmethod(lambda: None)}))
    _ensure_names('vedbus', VeDbusService=type('VeDbusService', (), {}),
                  VeDbusItemImport=type('VeDbusItemImport', (), {}),
                  VeDbusItemExport=type('VeDbusItemExport', (), {}))
    spec = importlib.util.spec_from_file_location('_harness_dbus_role_service', _DRS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The module imported conftest's stub VeDbusService; point it at the real one.
    mod.VeDbusService = vedbus.VeDbusService
    return mod


dbus_role_service = _load_role_service_module()
DbusRoleService = dbus_role_service.DbusRoleService


def make_role_service(ble_device, ble_role, dev_id='test_000000000000', connected=True):
    """A real DbusRoleService on a real VeDbusService, minus the bus and settings.

    Built with ``__new__`` and ``_init_dbus_service`` so no settings
    daemon or bus name is needed.  ``connected=True`` makes ``connect()``
    a no-op, as it is for every advertisement after the first.
    """
    svc = DbusRoleService.__new__(DbusRoleService)
    svc._ble_device = ble_device
    svc.ble_role = ble_role
    svc._dev_id = dev_id
    svc._dbus_id = f"{dev_id}/{ble_role.NAME}"
    svc._service_name = f"com.victronenergy.{ble_role.NAME}.{dev_id}"
    svc._bus = FakeBus()
    svc._dbus_settings = None
    svc._dbus_iface = None
    svc._connected = connected
    svc._init_dbus_service()
    if connected:
        # As connect() leaves it: the bus name is held.
        svc._dbus_service.register()
    return svc


def items_changed():
    """The ItemsChanged payloads emitted so far: one dict of paths each."""
    return [payload for name, _path, payload in EMITTED if name == 'ItemsChanged']


def signal_count():
    return len(EMITTED)


def reset():
    EMITTED.clear()
