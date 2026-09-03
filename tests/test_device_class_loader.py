"""The class loader must not call a detector-routed class an error.

The 0x02E1 Victron family (Orion-TR, IP22, SmartShunt, SmartSolar) is
picked by payload in the dispatcher and sets MANUFACTURER_ID per
instance in configure().  At startup the loader used to log an ERROR
per such class ("invalid MANUFACTURER_ID: None"), four lines per restart
that once sent an investigation the wrong way.  A class with neither a
manufacturer id nor a detector is still an error.
"""
from __future__ import annotations

import logging
import os
import textwrap

from ble_device import BleDevice


def _write(tmp_path, name, body):
    (tmp_path / f"ble_device_{name}.py").write_text(textwrap.dedent(body))


def test_detector_routed_class_is_debug_not_error(tmp_path, caplog) -> None:
    _write(tmp_path, "fake_detector", """
        from ble_device import BleDevice
        class BleDeviceFakeDetector(BleDevice):
            @staticmethod
            def matches_manufacturer_data(data: bytes) -> bool:
                return False
    """)
    _write(tmp_path, "fake_broken", """
        from ble_device import BleDevice
        class BleDeviceFakeBroken(BleDevice):
            pass
    """)
    before = dict(BleDevice.DEVICE_CLASSES)
    with caplog.at_level(logging.DEBUG):
        BleDevice.load_classes(os.path.join(str(tmp_path), "dbus_ble_sensors.py"))
    errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert not any("fake_detector" in m for m in errors), errors
    assert any("fake_broken" in m and "invalid MANUFACTURER_ID" in m for m in errors), errors
    debugs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("fake_detector" in m and "detector-routed" in m for m in debugs)
    assert BleDevice.DEVICE_CLASSES == before, "neither fake registers by manufacturer id"
