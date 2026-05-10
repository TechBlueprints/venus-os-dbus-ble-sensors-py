"""
Pin the ``try/finally`` guard around the ``self.info['dev_id']``
mutation in ``BleDevice._create_indexed_role_service``.

Without it, a single exception during role-service init on a
multi-sensor device (e.g. SeeLevel BTP3 — sensor numbers 0..13
broadcast in turn) triggers a runaway: each subsequent advertisement
reads the already-mutated ``dev_id``, appends another ``_NN``, and
registers a bloated path in ``com.victronenergy.settings``.
"""

import importlib.util
import logging
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))


# Stub D-Bus / vedbus / inner services for off-device import.
def _ensure_stub(name: str, attrs: dict):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_ensure_stub('dbus', {
    'SystemBus': lambda **kw: MagicMock(),
    'SessionBus': lambda **kw: MagicMock(),
    'Interface': lambda *a, **kw: MagicMock(),
    'Bus': type('Bus', (), {}),
    'Int64': int,
    'String': str,
})
_ensure_stub('dbus.bus', {'BusConnection': type('BusConnection', (), {})})
sys.modules['dbus'].bus = sys.modules['dbus.bus']
_ensure_stub('vedbus', {
    'VeDbusService': MagicMock,
    'VeDbusItemImport': MagicMock,
    'VeDbusItemExport': MagicMock,
})
_ensure_stub('settingsdevice', {})
_ensure_stub('dbus_settings_service', {
    'DbusSettingsService': MagicMock,
})
_ensure_stub('dbus_ble_service', {
    'DbusBleService': type('DbusBleService', (), {
        'get': staticmethod(lambda: MagicMock()),
    }),
})
_ensure_stub('dbus_role_service', {
    'DbusRoleService': MagicMock,
})


_ble_device_path = os.path.join(os.path.dirname(__file__), '..', 'ble_device.py')
_spec = importlib.util.spec_from_file_location('_real_ble_device', _ble_device_path)
_real_ble_device = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_ble_device)
BleDevice = _real_ble_device.BleDevice


# Bypass the heavy ``__init__`` of BleDevice — we want a direct test of
# ``_create_indexed_role_service`` and need to control ``self.info``
# without dragging in the full settings / D-Bus init.
def _make_device(dev_id: str = 'seelevel_btp3_00a0508d9569') -> BleDevice:
    dev = BleDevice.__new__(BleDevice)
    dev.info = {
        'dev_id': dev_id,
        'dev_prefix': 'seelevel_btp3',
        'dev_mac': '00a0508d9569',
        'roles': {},
        'settings': [],
        'alarms': [],
        'product_id': 0,
        'product_name': '',
        'device_name': '',
        'firmware_version': '',
        'hardware_version': '',
        'manufacturer_id': 0,
    }
    dev._role_services = {}
    dev._plog = '[test]'
    return dev


class TryFinallyRestoresDevId(unittest.TestCase):

    def test_dev_id_restored_on_role_service_construction_failure(self):
        dev = _make_device()
        original = dev.info['dev_id']

        # Force the role-service constructor to raise mid-init.
        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()  # role class
            with patch.object(_real_ble_device, 'DbusRoleService',
                              side_effect=RuntimeError('AddMatch budget exhausted')):
                result = dev._create_indexed_role_service('tank', 0)

        self.assertIsNone(result, "should return None on failure")
        self.assertEqual(dev.info['dev_id'], original,
            f"dev_id must be restored after exception; got {dev.info['dev_id']!r}")

    def test_dev_id_restored_on_load_settings_failure(self):
        dev = _make_device()
        original = dev.info['dev_id']

        fake_role_service = MagicMock()
        fake_role_service.load_settings.side_effect = KeyError('min')

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              return_value=fake_role_service):
                result = dev._create_indexed_role_service('tank', 1)

        self.assertIsNone(result)
        self.assertEqual(dev.info['dev_id'], original)

    def test_repeated_failures_do_not_compound_corruption(self):
        """The pre-fix bug: each failed creation appends ``_NN`` to the
        already-mutated dev_id.  After 50 failed creations, dev_id grows
        by ~150 chars.  The fix keeps it pinned at the original length."""
        dev = _make_device()
        original = dev.info['dev_id']

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              side_effect=RuntimeError('forced failure')):
                # Simulate the SeeLevel sensor cycle: indices 0, 1, 2, 13 repeating.
                for _ in range(50):
                    for idx in (0, 1, 2, 13):
                        dev._create_indexed_role_service('tank', idx)

        self.assertEqual(dev.info['dev_id'], original,
            f"dev_id grew under repeated failure; got len={len(dev.info['dev_id'])}: "
            f"{dev.info['dev_id'][:80]}...")

    def test_success_path_restores_dev_id(self):
        """Even on the happy path the mutation must be reverted so the
        next index sees the canonical base dev_id."""
        dev = _make_device()
        original = dev.info['dev_id']

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              return_value=MagicMock()):
                result = dev._create_indexed_role_service('tank', 0)

        self.assertIsNotNone(result)
        self.assertEqual(dev.info['dev_id'], original)


if __name__ == '__main__':
    unittest.main()
