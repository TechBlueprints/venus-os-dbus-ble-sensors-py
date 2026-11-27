"""The RuuviTag low-battery alarm: temperature-dependent level, 0.4 V hysteresis.

Upstream ``dbus-ble-sensors`` (src/ruuvi.c ``ruuvi_lowbat`` and
src/ble-dbus.c ``alarm_level``/``update_alarm``) raises /Alarms/LowBattery
when /BatteryVoltage is below a level that depends on /Temperature:

    T >= 0 °C        2.5 V
    -20 <= T < 0 °C  2.3 V
    T < -20 °C       2.0 V

and, while the alarm is active, adds 0.4 V to that level, so it clears only
once the voltage has recovered by the hysteresis.  If the voltage item is
not valid the alarm is left alone.

The Python port had both inputs dead: it read the temperature under
``'temperature'`` (path ``/temperature``; the published path is
``/Temperature``), so every tag used the warm level, and it tested the
alarm with ``is True`` against the int 0/1 the alarm is published as, so
the hysteresis never applied.  These tests drive the driver's own alarm
callback through a role service that resolves paths the way
DbusRoleService does.
"""
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

import pytest  # noqa: E402

from ble_device_ruuvi import BleDeviceRuuvi  # noqa: E402
from ble_role import BleRole  # noqa: E402

BleRole.load_classes(os.path.dirname(os.path.abspath(__file__)))

# A format-5 (RAWv2) advertisement, only used to configure the driver.
RAWV2 = (b'\x05\x11\x94\x55\xA8\xC8\x7D\x00\x64\xFF\x9C\x00\x00\x05\x78'
         b'\x10\x12\x34\x56\x78\x9A\xBC\xDE\xF0')

ALARM = '/Alarms/LowBattery'


class FakeRoleService:
    """Resolves paths like DbusRoleService: ``'x'`` and ``'/x'`` are one path,
    case matters, a missing path reads as None."""

    def __init__(self, **values):
        self.values = {}
        for name, value in values.items():
            self[name] = value
        self[ALARM] = 0                     # DbusRoleService.add_alarm

    @staticmethod
    def _clear_path(path):
        return f"/{path.lstrip('/').rstrip('/')}"

    def __getitem__(self, path):
        return self.values.get(self._clear_path(path))

    def __setitem__(self, path, value):
        self.values[self._clear_path(path)] = value


_tags = []


@pytest.fixture(autouse=True)
def _drop_fake_services():
    """BleDevice.__del__ disconnects its role services; these are fakes."""
    yield
    for device in _tags:
        device._role_services.clear()
    _tags.clear()


def make_tag(temperature=None, **extra):
    """A configured RuuviTag with a temperature role service (and, if asked,
    a movement role service), as BleDevice.init would build them."""
    device = BleDeviceRuuvi('012345332211')
    _tags.append(device)
    device.configure(RAWV2)
    device._load_configuration()
    temp_values = {}
    if temperature is not None:
        temp_values['Temperature'] = temperature
    temp_svc = FakeRoleService(**temp_values)
    device._role_services = {'temperature': temp_svc}
    if extra.get('movement'):
        device._role_services['movement'] = FakeRoleService()
    return device, temp_svc


def evaluate(device, service, voltage):
    """Publish a voltage, then run the alarm the way
    BleDevice.handle_manufacturer_data does (DbusRoleService.update_alarm)."""
    service['BatteryVoltage'] = voltage
    for alarm in device.info['alarms']:
        service[alarm['name']] = alarm['update'](service)
    return service[ALARM]


# ── levels by temperature ────────────────────────────────────────────────

@pytest.mark.parametrize('temperature, below, above', [
    (20.0, 2.49, 2.51),     # warm: 2.5 V
    (0.0, 2.49, 2.51),      # 0 °C is warm (upstream: T < 0 is cold)
    (-5.0, 2.29, 2.31),     # cold: 2.3 V
    (-20.0, 2.29, 2.31),    # -20 °C is cold, not very cold (T < -20)
    (-25.0, 1.99, 2.01),    # very cold: 2.0 V
])
def test_level_depends_on_temperature(temperature, below, above):
    device, svc = make_tag(temperature)
    assert evaluate(device, svc, above) == 0
    device, svc = make_tag(temperature)
    assert evaluate(device, svc, below) == 1


def test_cold_tag_at_2v4_does_not_alarm():
    """The dead lookup made every tag use the warm 2.5 V level."""
    device, svc = make_tag(-5.0)
    assert evaluate(device, svc, 2.40) == 0


def test_very_cold_tag_at_2v1_does_not_alarm():
    device, svc = make_tag(-25.0)
    assert evaluate(device, svc, 2.10) == 0


def test_no_temperature_uses_warm_level():
    device, svc = make_tag(None)
    assert evaluate(device, svc, 2.49) == 1


def test_movement_service_uses_the_tags_temperature():
    """The movement role service publishes BatteryVoltage and the alarm but
    not /Temperature; it must use the same level as the temperature service
    of the same tag, or the two services disagree in the cold."""
    device, temp_svc = make_tag(-5.0, movement=True)
    mov_svc = device._role_services['movement']
    assert evaluate(device, temp_svc, 2.40) == 0
    assert evaluate(device, mov_svc, 2.40) == 0
    assert evaluate(device, mov_svc, 2.25) == 1


# ── hysteresis ───────────────────────────────────────────────────────────

def test_warm_alarm_sets_holds_inside_band_and_clears_above_it():
    device, svc = make_tag(20.0)
    assert evaluate(device, svc, 2.45) == 1     # set below 2.5
    assert evaluate(device, svc, 2.60) == 1     # inside 2.5 .. 2.9: hold
    assert evaluate(device, svc, 2.85) == 1     # still inside
    assert evaluate(device, svc, 2.95) == 0     # above 2.9: clear
    assert evaluate(device, svc, 2.60) == 0     # cleared stays cleared above 2.5


def test_cold_alarm_clears_above_cold_level_plus_hysteresis():
    device, svc = make_tag(-5.0)
    assert evaluate(device, svc, 2.25) == 1     # set below 2.3
    assert evaluate(device, svc, 2.65) == 1     # inside 2.3 .. 2.7: hold
    assert evaluate(device, svc, 2.75) == 0     # above 2.7: clear


def test_noisy_voltage_at_the_level_does_not_chatter():
    """Prod Ruuvi battery noise spans up to 0.074 V.  A tag sitting on the
    level sets the alarm once and holds it."""
    device, svc = make_tag(20.0)
    readings = [2.52, 2.48, 2.55, 2.47, 2.53, 2.49, 2.54, 2.50, 2.51, 2.48] * 5
    states = [evaluate(device, svc, v) for v in readings]
    changes = sum(1 for a, b in zip(states, states[1:]) if a != b)
    assert states[0] == 0           # 2.52: above the level
    assert changes == 1             # set once, at the first 2.48
    assert states[-1] == 1


def test_temperature_crossing_zero_does_not_chatter():
    """A voltage between the cold and warm levels, with the temperature
    flickering across 0 °C: the alarm may set, then must hold, because the
    hysteresis (0.4 V) is wider than the step between levels (0.2 V)."""
    device, svc = make_tag(-0.05)
    states = []
    for i in range(20):
        svc['Temperature'] = 0.05 if i % 2 else -0.05
        states.append(evaluate(device, svc, 2.40))
    changes = sum(1 for a, b in zip(states, states[1:]) if a != b)
    assert changes <= 1
    assert states[-1] == 1


@pytest.mark.parametrize('temperature', [25.0, 5.0, 0.0, -5.0, -20.0, -25.0])
def test_healthy_tag_never_changes_state(temperature):
    device, svc = make_tag(temperature)
    states = [evaluate(device, svc, v)
              for v in [2.95, 2.90, 2.93, 2.88, 2.97, 2.91] * 5]
    assert set(states) == {0}


def test_missing_voltage_leaves_the_alarm_alone():
    """Upstream skips the alarm when the voltage item is not valid."""
    device, svc = make_tag(20.0)
    assert evaluate(device, svc, 2.40) == 1
    svc.values.pop('/BatteryVoltage')
    for alarm in device.info['alarms']:
        svc[alarm['name']] = alarm['update'](svc)
    assert svc[ALARM] == 1
