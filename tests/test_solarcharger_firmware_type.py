"""A solarcharger's /FirmwareVersion must never be a str.

systemcalc's DVCC delegate does ``v & 0xFF0000`` on it
(delegates/dvcc.py has_externalcontrol_support).  A str raises
TypeError there, and systemcalc dies in a restart loop -- taking DVCC
down for the WHOLE system, not just this device.

Prod 2026-09-03: registering this service dropped both hardwired MPPTs
from NetworkMode 13 to 0 and Exterior lost BMS control, because
BleDevice defaults info['firmware_version'] to the string "1.0.0" and
no previous role had ever exposed that path to systemcalc.

None is the documented "not known" case; systemcalc returns True for it
rather than warning.  An int is fine.  A str is not.
"""
from __future__ import annotations

import os
import re

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                   "opt", "victronenergy", "dbus-ble-sensors-py")


def test_smartsolar_declares_firmware_version_none() -> None:
    src = open(os.path.join(os.path.normpath(SRC), "ble_device_smartsolar.py")).read()
    assert re.search(r'"firmware_version":\s*None', src), (
        "configure() must pin firmware_version to None so the base class's "
        '"1.0.0" string never reaches systemcalc')


def test_smartsolar_never_publishes_a_firmware_string_to_the_path() -> None:
    src = open(os.path.join(os.path.normpath(SRC), "ble_device_smartsolar.py")).read()
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#.*", "", code)
    assert '"/FirmwareVersion"' not in code, (
        "this driver must not publish /FirmwareVersion at all: the pretty "
        "string belongs in settings, and systemcalc requires an int here")
