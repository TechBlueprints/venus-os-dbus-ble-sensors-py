"""Slow environmental values are gated by deadband, not rounded equality.

Prod, 2026-11-27 16:13:43-16:28:43Z: this service emitted 654
``ItemsChanged`` signals in 15 minutes, nearly all of them sensor noise
straddling a rounding boundary: a Mopeka battery walking four adjacent
1/32 V codes, a Ruuvi pressure at 873.47..873.56 hPa across 873.5, Ruuvi
humidity and temperature a few LSBs either side of x.x5.  Every flicker
rounded to a new value, so every flicker was a signal, and every signal
wakes nine processes on the box (docs/LOAD-AVERAGE-BURSTS.md).

These tests drive the real drivers (parse -> update_data -> publish)
through a role service that records each changed write per path
(``writes``) and counts signals the way the box emits them since the
batching fix: one per ``with role_service:`` block that changed anything
(see SignalCountingService).  The readings are the values captured on
prod in that window; in it the box still sent one signal per changed
path, which is why the per-path ``writes`` are what the gating is judged
by.
"""
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

import pytest  # noqa: E402

import sensor_publisher  # noqa: E402
from sensor_publisher import SensorPublisher  # noqa: E402
from sensor_rounding import SensorRoundingPolicy  # noqa: E402
from ble_device_mopeka import BleDeviceMopeka  # noqa: E402
from ble_device_ruuvi import BleDeviceRuuvi  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────

class _Item:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value


class FakeSettings:
    """Hands every setting its default, so the policy is the shipped one."""

    def set_item(self, path, default, min_, max_, callback=None):
        return _Item(default)


class SignalCountingService:
    """Role service that counts D-Bus signals the way this service emits them.

    ``DbusRoleService._set_value`` drops a write equal to the current
    value.  Inside ``with role_service:`` the changed writes are batched
    and go out as one ``ItemsChanged`` when the outermost block exits; a
    write outside any block is its own signal.  That is the real
    service's behaviour, pinned against real vedbus in
    test_role_service_batching.py.  (Before that fix every changed path
    was its own signal: ruuvi_f0ad5dba8b9d's 195 signals in the 15-minute
    capture were exactly its 113 + 54 + 16 + 12 path changes.)
    """

    class _Role:
        NAME = 'tank'

    ble_role = _Role()

    def __init__(self, **settings):
        self.values = dict(settings)
        self.signals = 0
        self.writes = {}          # path -> number of changed writes
        self._depth = 0
        self._pending = False     # a changed write waits in the open batch

    def __enter__(self):
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._pending:
            self._pending = False
            self.signals += 1
        return False

    def __setitem__(self, path, value):
        if self.values.get(path, object()) == value:
            return
        self.values[path] = value
        self.writes[path] = self.writes.get(path, 0) + 1
        if self._depth:
            self._pending = True
        else:
            self.signals += 1

    def __getitem__(self, path):
        return self.values.get(path)


@pytest.fixture(autouse=True)
def publisher():
    SensorPublisher._INSTANCE = None
    SensorRoundingPolicy._INSTANCE = None
    pub = SensorPublisher(SensorRoundingPolicy(FakeSettings()))
    yield pub
    SensorPublisher._INSTANCE = None
    SensorRoundingPolicy._INSTANCE = None


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test advances; one advertisement a second."""
    now = [1000.0]
    monkeypatch.setattr(sensor_publisher.time, 'monotonic', lambda: now[0])
    return now


# ── frame builders (layouts as the drivers parse them) ───────────────────

def ruuvi_frame(temperature, humidity, pressure, battery, seq=0):
    """RuuviTag RAWv2 (format 5), stationary (MovementCounter fixed)."""
    t = int(round(temperature * 200)) & 0xFFFF
    h = int(round(humidity * 400))
    p = int(round((pressure - 500) * 100))
    power = ((int(round(battery * 1000)) - 1600) << 5) | 24   # TxPower +8 dBm
    return (bytes([0x05]) + t.to_bytes(2, 'big') + h.to_bytes(2, 'big')
            + p.to_bytes(2, 'big') + bytes(6) + power.to_bytes(2, 'big')
            + bytes([0x10]) + (seq & 0xFFFF).to_bytes(2, 'big')
            + bytes.fromhex('cb157d296144'))


def mopeka_frame(battery_code):
    """Mopeka Pro Check cb157d296144 (prod capture), battery byte replaced.

    BatteryVoltage is the low 7 bits of byte 1 in 1/32 V steps.
    """
    raw = bytearray(bytes.fromhex('03583c4ec5296144e6fd'))
    raw[1] = battery_code & 0x7F
    return bytes(raw)


def feed(device, svc, frame, role):
    """One advertisement through the driver, as handle_manufacturer_data does."""
    data = device._parse_manufacturer_data(frame)[role]
    device.update_data(svc, data)
    device._update_dbus_data(svc, data)


def ruuvi(mac='f0ad5dba8b9d'):
    dev = BleDeviceRuuvi(mac)
    dev.configure(ruuvi_frame(14.4, 30.3, 873.5, 2.67))
    return dev


def mopeka():
    dev = BleDeviceMopeka('cb157d296144')
    dev.configure(mopeka_frame(64))
    return dev


# ── replays of prod readings ─────────────────────────────────────────────

# temperature.ruuvi_f0ad5dba8b9d, 16:13:43-16:28:43Z: the values each path
# published, in order (Pressure 54 signals, Humidity 113 on its own).
F0AD_PRESSURE = [873.48, 873.54, 873.47, 873.51, 873.48, 873.5, 873.48, 873.51,
                 873.48, 873.51, 873.49, 873.51, 873.48, 873.51, 873.48, 873.5,
                 873.48, 873.5, 873.48, 873.52, 873.47, 873.51, 873.48, 873.53]
F0AD_HUMIDITY = [30.355, 30.335, 30.35, 30.3425, 30.36, 30.3375, 30.355, 30.325,
                 30.3525, 30.345, 30.35, 30.34, 30.365, 30.3375, 30.355, 30.3425,
                 30.3575, 30.325, 30.355, 30.34, 30.35, 30.3475, 30.355, 30.33]
F0AD_TEMPERATURE = [14.46, 14.41, 14.46, 14.41, 14.455, 14.39, 14.47, 14.39,
                    14.46, 14.445, 14.475, 14.43, 14.46, 14.445, 14.46, 14.43]
F0AD_BATTERY = [2.672, 2.616, 2.679, 2.661, 2.665, 2.658, 2.668, 2.661,
                2.672, 2.679]

# tank.mopeka_cb157d296144: 94 signals, all /BatteryVoltage, walking
# 1.9375 / 1.96875 / 2.0 / 2.03125 V (codes 62..65).
CB15_BATTERY_CODES = [63, 64, 63, 64, 63, 64, 65, 64, 62, 63, 64, 63, 65, 64,
                      63, 64, 63, 65, 64, 65, 64, 65, 64, 62, 63, 64, 65, 64,
                      63, 64, 63, 64, 63, 64, 63, 62, 64, 65, 64, 63]


def _cycle(seq, i):
    return seq[i % len(seq)]


def test_ruuvi_replay_of_prod_noise_is_silent_after_the_first_advert(clock):
    """Forty adverts of the f0ad tag's real noise: after the first advert
    (which publishes every path once), not one signal.

    Under rounded equality nearly every one of them crossed a boundary
    on some path (873.5 hPa, 30.35 %, 14.45 °C, 2.665 V) and signalled.
    """
    dev, svc = ruuvi(), SignalCountingService()
    clock[0] += 1
    feed(dev, svc, ruuvi_frame(F0AD_TEMPERATURE[0], F0AD_HUMIDITY[0],
                               F0AD_PRESSURE[0], F0AD_BATTERY[0]), 'temperature')
    first = svc.signals
    for i in range(1, 40):
        clock[0] += 1
        feed(dev, svc, ruuvi_frame(_cycle(F0AD_TEMPERATURE, i),
                                   _cycle(F0AD_HUMIDITY, i),
                                   _cycle(F0AD_PRESSURE, i),
                                   _cycle(F0AD_BATTERY, i), seq=i),
             'temperature')
    assert svc.signals == first


def test_mopeka_battery_replay_of_prod_noise_is_one_signal(clock):
    """The cb157 Pro Check's four-code battery walk: one signal, not 40."""
    dev, svc = mopeka(), SignalCountingService(ButaneRatio=0, FluidType=8)
    for code in CB15_BATTERY_CODES:
        clock[0] += 1
        feed(dev, svc, mopeka_frame(code), 'tank')
    assert svc.writes['BatteryVoltage'] == 1


# ── alarms read the published (gated) value and must not chatter ────────

def test_mopeka_low_battery_alarm_does_not_chatter_at_threshold(clock):
    """A battery one LSB either side of the 15 % threshold.

    ``/Alarms/LowBattery`` is computed from the published BatteryVoltage
    (CR2032 scale: < 15 % is below 2.2975 V).  Codes 73 and 74 are
    2.28125 V (alarm) and 2.3125 V (no alarm).  Under rounded equality
    both published and the alarm flipped with every advert; gated, the
    published value holds and so does the alarm.  The alarm reads the
    same value the GUI shows, and the deadband is its hysteresis.
    """
    dev, svc = mopeka(), SignalCountingService(ButaneRatio=0, FluidType=8)
    states = []
    for i in range(30):
        clock[0] += 1
        feed(dev, svc, mopeka_frame(73 if i % 2 == 0 else 74), 'tank')
        states.append(dev._get_low_battery_state(svc))
    assert states[0] == 1
    assert set(states) == {1}


def test_mopeka_low_battery_alarm_follows_a_real_drop(clock):
    """A battery that really falls below the threshold raises the alarm.

    From 2.4375 V (code 78, 36 %) to 2.28125 V (code 73): 0.156 V, past
    the 0.1 V band, so it is published on the first advert at the new
    level and the alarm follows on that advert.
    """
    dev, svc = mopeka(), SignalCountingService(ButaneRatio=0, FluidType=8)
    clock[0] += 1
    feed(dev, svc, mopeka_frame(78), 'tank')
    assert dev._get_low_battery_state(svc) == 0
    clock[0] += 1
    feed(dev, svc, mopeka_frame(73), 'tank')
    assert svc['BatteryVoltage'] == pytest.approx(73 / 32)
    assert dev._get_low_battery_state(svc) == 1


def test_ruuvi_low_battery_alarm_does_not_chatter_at_threshold(clock):
    """Ruuvi battery noise straddling the 2.5 V threshold.

    2.495 .. 2.503 V alternating: under rounded equality each published
    (2.50 / 2.49 / 2.5 ...) and the alarm toggled.  Gated, one publish.
    """
    dev, svc = ruuvi(), SignalCountingService()
    states = []
    for i, v in enumerate([2.503, 2.495, 2.501, 2.497, 2.503, 2.494] * 5):
        clock[0] += 1
        feed(dev, svc, ruuvi_frame(14.4, 30.3, 873.0, v, seq=i), 'temperature')
        states.append(BleDeviceRuuvi._get_low_battery_state(svc))
    assert len(set(states)) == 1


# ── real changes are prompt, slow drift is published, heartbeat holds ───

def test_ruuvi_real_temperature_step_publishes_on_the_next_advert(clock):
    dev, svc = ruuvi(), SignalCountingService()
    clock[0] += 1
    feed(dev, svc, ruuvi_frame(14.43, 30.3, 873.0, 2.67), 'temperature')
    clock[0] += 1
    feed(dev, svc, ruuvi_frame(15.43, 30.3, 873.0, 2.67), 'temperature')
    assert svc['Temperature'] == pytest.approx(15.43)
    assert svc.writes['Temperature'] == 2


def test_ruuvi_slow_noisy_drift_is_tracked_within_one_band(clock):
    """A 0.5 °C warm-up over 100 adverts, with the tag's ±0.03 °C noise.

    The published value never trails the reading by a band or more, and
    the drift costs a handful of signals, not one per boundary flicker.
    """
    dev, svc = ruuvi(), SignalCountingService()
    noise = [0.03, -0.03, 0.02, -0.025, 0.0, 0.015, -0.03, 0.025]
    for i in range(100):
        clock[0] += 1
        reading = round(14.0 + 0.005 * i + noise[i % len(noise)], 3)
        feed(dev, svc, ruuvi_frame(reading, 30.3, 873.0, 2.67, seq=i), 'temperature')
        assert abs(svc['Temperature'] - reading) < 0.1 + 1e-9
    assert svc['Temperature'] > 14.3      # the drift was published
    assert svc.writes['Temperature'] <= 8


def test_heartbeat_republishes_a_value_held_inside_the_band(clock):
    """Noise inside the band stays silent until the heartbeat (900 s),
    which then publishes the current precise value: no published value
    is older than the heartbeat, whatever the deadband."""
    dev, svc = ruuvi(), SignalCountingService()
    for i in range(899):
        clock[0] += 1
        feed(dev, svc, ruuvi_frame(_cycle(F0AD_TEMPERATURE, i), 30.3, 873.0, 2.67, seq=i),
             'temperature')
    assert svc.writes['Temperature'] == 1
    clock[0] += 2
    feed(dev, svc, ruuvi_frame(14.39, 30.3, 873.0, 2.67), 'temperature')
    assert svc.writes['Temperature'] == 2
    assert svc['Temperature'] == pytest.approx(14.39)


def test_returning_device_publishes_at_once(clock):
    """A device that went silent past its TTL is deleted with its role
    service; on return it gets a new one and publishes on its first
    advert, even a value inside the band of what it last published."""
    dev = ruuvi()
    first = SignalCountingService()
    clock[0] += 1
    feed(dev, first, ruuvi_frame(14.43, 30.3, 873.0, 2.67), 'temperature')
    returned = SignalCountingService()
    clock[0] += 60
    feed(dev, returned, ruuvi_frame(14.45, 30.3, 873.0, 2.67), 'temperature')
    assert returned['Temperature'] == pytest.approx(14.45)
    assert returned.writes['Temperature'] == 1


def test_movement_service_battery_is_gated_too(clock):
    """BatteryVoltage has no roles key, so it is on the movement service
    as well (prod: 8 signals in 15 min there, all BatteryVoltage)."""
    dev, svc = ruuvi(), SignalCountingService()
    for i, v in enumerate([2.704, 2.689, 2.7, 2.686, 2.668, 2.696, 2.637, 2.696]):
        clock[0] += 1
        feed(dev, svc, ruuvi_frame(14.4, 30.3, 873.0, v, seq=i), 'movement')
    assert svc.writes['BatteryVoltage'] == 1


def test_tank_level_and_raw_value_stay_on_rounded_equality():
    """The tank's own measurement is not deadbanded: a real fill or draw
    must show on the next advert, and it does not flap (prod: RawValue
    once, Remaining four times in 15 min)."""
    dev = mopeka()
    regs = dev._regs_by_name()
    assert 'deadband' not in regs['RawValue']
    assert 'deadband' not in regs['Temperature']
    assert 'Level' not in dev.info.get('deadbands', {})
    assert 'Remaining' not in dev.info.get('deadbands', {})
