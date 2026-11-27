"""One advertisement, one ``ItemsChanged``: role-service writes are batched.

Prod, 2026-11-27 16:13:43-16:28:43Z: ``temperature.ruuvi_f0ad5dba8b9d``
sent 195 signals, exactly the sum of its four paths' change counts
(113 + 54 + 16 + 12).  ``DbusRoleService.__enter__`` discarded the vedbus
``ServiceContext`` and ``_set_value`` opened one per write, so the
``with role_service:`` around each publish batched nothing.

These tests run the real ``DbusRoleService`` over the real ``vedbus``
(``vedbus_harness``) and count the signals vedbus emits, so they say
what the box does, not what a fake was written to believe.
"""
import os
import sys
import threading
import types

sys.path.insert(1, os.path.dirname(__file__))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))

import pytest  # noqa: E402

import ble_device  # noqa: E402
import sensor_publisher  # noqa: E402
from sensor_publisher import SensorPublisher  # noqa: E402
from sensor_rounding import SensorRoundingPolicy  # noqa: E402
from ble_device_ruuvi import BleDeviceRuuvi  # noqa: E402
from ble_role_temperature import BleRoleTemperature  # noqa: E402

import vedbus_harness as vh  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────

class _Item:
    def __init__(self, value):
        self._value = value

    def get_value(self):
        return self._value


class _DefaultSettings:
    """Every setting at its default, so the policy is the shipped one."""

    def set_item(self, path, default, min_, max_, callback=None):
        return _Item(default)


class _EnabledBleService:
    def is_device_enabled(self, info):
        return True

    def is_device_role_enabled(self, info, role):
        return True


@pytest.fixture(autouse=True)
def harness(monkeypatch):
    SensorPublisher._INSTANCE = None
    SensorRoundingPolicy._INSTANCE = None
    SensorPublisher(SensorRoundingPolicy(_DefaultSettings()))
    now = [1000.0]
    monkeypatch.setattr(sensor_publisher.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(ble_device.DbusBleService, 'get',
                        staticmethod(lambda: _EnabledBleService()))
    vh.reset()
    yield now
    SensorPublisher._INSTANCE = None
    SensorRoundingPolicy._INSTANCE = None


def ruuvi_frame(temperature, humidity, pressure, battery, seq=0):
    """RuuviTag RAWv2 (format 5), stationary (MovementCounter fixed)."""
    t = int(round(temperature * 200)) & 0xFFFF
    h = int(round(humidity * 400))
    p = int(round((pressure - 500) * 100))
    power = ((int(round(battery * 1000)) - 1600) << 5) | 24
    return (bytes([0x05]) + t.to_bytes(2, 'big') + h.to_bytes(2, 'big')
            + p.to_bytes(2, 'big') + bytes(6) + power.to_bytes(2, 'big')
            + bytes([0x10]) + (seq & 0xFFFF).to_bytes(2, 'big')
            + bytes.fromhex('f0ad5dba8b9d'))


def ruuvi_with_service():
    """A configured Ruuvi whose temperature role is a real role service."""
    dev = BleDeviceRuuvi('f0ad5dba8b9d')
    dev._plog = '[ruuvi test]'
    dev.configure(ruuvi_frame(14.4, 30.3, 873.5, 2.67))
    svc = vh.make_role_service(dev, BleRoleTemperature(), dev_id='ruuvi_f0ad5dba8b9d')
    dev._role_services = {'temperature': svc}
    vh.reset()       # the mandatory paths added at init are not under test
    return dev, svc


def advert(dev, clock, *values, seq=0):
    clock[0] += 1
    dev.handle_manufacturer_data(ruuvi_frame(*values, seq=seq))


# ── the advertisement path ───────────────────────────────────────────────

def test_an_advert_that_changes_four_paths_is_one_signal(harness):
    """The f0ad tag moving on all four values at once: one ItemsChanged
    carrying all four, where the unbatched service sent four."""
    dev, svc = ruuvi_with_service()
    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)
    vh.reset()

    advert(dev, harness, 16.0, 35.0, 880.0, 2.90, seq=1)

    assert vh.signal_count() == 1
    (changes,) = vh.items_changed()
    assert {'/Temperature', '/Humidity', '/Pressure', '/BatteryVoltage'} <= set(changes)
    assert changes['/Temperature']['Value'] == svc['/Temperature']


def test_first_advert_announces_every_new_path_in_one_signal(harness):
    dev, svc = ruuvi_with_service()

    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)

    assert vh.signal_count() == 1
    (changes,) = vh.items_changed()
    for path in ('/Temperature', '/Humidity', '/Pressure', '/BatteryVoltage',
                 '/Alarms/LowBattery'):
        assert path in changes, path


def test_a_single_changed_path_goes_out_alone_before_the_call_returns(harness):
    dev, svc = ruuvi_with_service()
    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)
    vh.reset()

    advert(dev, harness, 18.0, 30.3, 873.5, 2.67, seq=1)

    assert vh.items_changed() == [{'/Temperature': {'Value': 18.0, 'Text': '18.0'}}]
    assert vh.signal_count() == 1


def test_an_advert_that_changes_nothing_emits_nothing(harness):
    dev, svc = ruuvi_with_service()
    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)
    vh.reset()

    advert(dev, harness, 14.4, 30.3, 873.5, 2.67, seq=1)

    assert vh.signal_count() == 0


def test_an_alarm_raised_by_the_advert_rides_in_the_same_signal(harness):
    """Battery 2.67 -> 2.30 V crosses the 2.5 V low-battery line: the
    voltage and the alarm go out together, at once."""
    dev, svc = ruuvi_with_service()
    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)
    vh.reset()

    advert(dev, harness, 14.4, 30.3, 873.5, 2.30, seq=1)

    assert vh.signal_count() == 1
    (changes,) = vh.items_changed()
    assert changes['/BatteryVoltage']['Value'] == pytest.approx(2.30)
    assert changes['/Alarms/LowBattery']['Value'] == 1


def test_an_invalid_reading_does_not_split_the_advert(harness):
    """0x8000 is the RAWv2 'not available' temperature.  The parser drops
    it (the path keeps its last value), and the humidity change beside it
    still goes out as the advert's one signal."""
    dev, svc = ruuvi_with_service()
    advert(dev, harness, 14.4, 30.3, 873.5, 2.67)
    vh.reset()

    frame = bytearray(ruuvi_frame(14.4, 36.0, 873.5, 2.67, seq=1))
    frame[1:3] = (0x8000).to_bytes(2, 'big')
    harness[0] += 1
    dev.handle_manufacturer_data(bytes(frame))

    assert vh.signal_count() == 1
    assert '/Humidity' in vh.items_changed()[0]


def test_prod_noise_replay_is_one_signal_then_silence(harness):
    """The f0ad tag's captured noise (test_environmental_deadband) through
    the real stack: the first advert is one signal for every path, and
    the deadband gating (PR #36) keeps the next 39 adverts silent."""
    from test_environmental_deadband import (
        F0AD_TEMPERATURE, F0AD_HUMIDITY, F0AD_PRESSURE, F0AD_BATTERY)
    dev, svc = ruuvi_with_service()

    def cyc(seq, i):
        return seq[i % len(seq)]

    advert(dev, harness, F0AD_TEMPERATURE[0], F0AD_HUMIDITY[0],
           F0AD_PRESSURE[0], F0AD_BATTERY[0])
    assert vh.signal_count() == 1
    for i in range(1, 40):
        advert(dev, harness, cyc(F0AD_TEMPERATURE, i), cyc(F0AD_HUMIDITY, i),
               cyc(F0AD_PRESSURE, i), cyc(F0AD_BATTERY, i), seq=i)
    assert vh.signal_count() == 1


# ── a GATT driver: EasyStart live telemetry ──────────────────────────────

def _easystart_live(current, state=3):
    return {'current': current, 'frequency': 60.0, 'state': state,
            'state_name': 'Running', 'learned_starts': 5, 'peak_current': 40.0,
            'scpt_remaining': 0, 'total_starts': 100, 'total_faults': 1,
            'fault': False}


def easystart_with_service(monkeypatch):
    import ble_device_easystart
    from ble_role_acload import BleRoleAcLoad
    monkeypatch.setattr(ble_device_easystart.DbusBleService, 'get',
                        staticmethod(lambda: _EnabledBleService()))
    dev = ble_device_easystart.BleDeviceEasyStart('aabbccddeeff')
    dev._plog = '[easystart test]'
    dev.info.update({'product_name': 'EasyStart', 'device_name': 'EasyStart'})
    svc = vh.make_role_service(dev, BleRoleAcLoad(), dev_id='easystart_aabbccddeeff')
    dev._role_services = {'acload': svc}
    vh.reset()
    return dev, svc


def test_easystart_poll_is_one_signal(harness, monkeypatch):
    """A GATT driver's publish block batches the same way: the first poll
    announces every path in one signal; a load change moves the four
    current/power paths in one signal."""
    dev, svc = easystart_with_service(monkeypatch)

    dev._publish_live_glib(_easystart_live(10.0))
    assert vh.signal_count() == 1
    first = vh.items_changed()[0]
    assert {'/Ac/L1/Current', '/Ac/Power', '/EasyStart/State',
            '/Alarms/Fault'} <= set(first)

    vh.reset()
    harness[0] += 1
    dev._publish_live_glib(_easystart_live(12.0))
    assert vh.signal_count() == 1
    assert set(vh.items_changed()[0]) == {
        '/Ac/L1/Current', '/Ac/Current', '/Ac/L1/Power', '/Ac/Power'}


# ── DbusRoleService itself ───────────────────────────────────────────────

def bare_service():
    dev = types.SimpleNamespace(_plog='[bare]', info={
        'product_id': 0xC029, 'product_name': 'Test', 'firmware_version': '1',
        'hardware_version': '1', 'device_name': 'Test'})
    svc = vh.make_role_service(dev, BleRoleTemperature())
    vh.reset()
    return svc


def test_a_write_outside_any_block_is_its_own_signal_at_once():
    svc = bare_service()
    svc['/Temperature'] = 20.0
    assert vh.items_changed() == [{'/Temperature': {'Value': 20.0, 'Text': '20.0'}}]
    svc['/Humidity'] = 40.0
    assert vh.signal_count() == 2


def test_writes_in_a_block_go_out_together_when_it_exits():
    svc = bare_service()
    svc['/Temperature'] = 20.0
    svc['/Humidity'] = 40.0
    vh.reset()

    with svc:
        svc['/Temperature'] = 21.0
        svc['/Humidity'] = 41.0
        svc['/Temperature'] = 22.0      # last write wins inside the batch
        assert vh.signal_count() == 0
        assert svc['/Temperature'] == 22.0   # local reads see the write at once

    assert vh.items_changed() == [{
        '/Temperature': {'Value': 22.0, 'Text': '22.0'},
        '/Humidity': {'Value': 41.0, 'Text': '41.0'},
    }]
    assert vh.signal_count() == 1


def test_nested_blocks_flush_once_at_the_outermost_exit():
    svc = bare_service()
    with svc:
        svc['/Temperature'] = 20.0
        with svc:
            svc['/Humidity'] = 40.0
        assert vh.signal_count() == 0
        svc['/Pressure'] = 900.0
    assert vh.signal_count() == 1
    assert set(vh.items_changed()[0]) == {'/Temperature', '/Humidity', '/Pressure'}


def test_a_value_going_to_none_is_in_the_batch_and_goes_out_at_once():
    """The IP22/SmartShunt off-state blocks clear measurement paths to None
    alongside other writes: the None is announced in that block's signal."""
    svc = bare_service()
    svc['/Dc/0/Voltage'] = 13.2
    svc['/State'] = 3
    vh.reset()
    with svc:
        svc['/Dc/0/Voltage'] = None
        svc['/State'] = 0
    assert vh.items_changed() == [{
        '/Dc/0/Voltage': {'Value': None, 'Text': '---'},
        '/State': {'Value': 0, 'Text': '0'},
    }]


def test_a_block_that_raises_still_announces_what_it_wrote():
    svc = bare_service()
    with pytest.raises(RuntimeError):
        with svc:
            svc['/Temperature'] = 20.0
            raise RuntimeError('driver bug')
    assert vh.items_changed() == [{'/Temperature': {'Value': 20.0, 'Text': '20.0'}}]
    # And the next write is not swallowed by a batch left open.
    vh.reset()
    svc['/Temperature'] = 21.0
    assert vh.signal_count() == 1


def test_a_path_deleted_in_a_block_is_not_announced():
    svc = bare_service()
    svc['/Gone'] = 1
    vh.reset()
    with svc:
        svc['/Temperature'] = 20.0
        svc['/Gone'] = 2
        del svc['/Gone']
    assert vh.items_changed() == [{'/Temperature': {'Value': 20.0, 'Text': '20.0'}}]


def test_a_write_from_another_thread_is_not_held_in_the_open_batch():
    svc = bare_service()
    with svc:
        svc['/Temperature'] = 20.0
        t = threading.Thread(target=lambda: svc.__setitem__('/Humidity', 40.0))
        t.start()
        t.join()
        assert vh.items_changed() == [{'/Humidity': {'Value': 40.0, 'Text': '40.0'}}]
    assert vh.items_changed()[1] == {'/Temperature': {'Value': 20.0, 'Text': '20.0'}}


def test_role_init_paths_added_through_vedbus_directly_are_one_signal():
    """The ble_role_*.init blocks use vedbus's own context
    (``with role_service._dbus_service as s``); they batched before this
    change and still do."""
    svc = bare_service()
    with svc._dbus_service as s:
        s.add_path('/Dc/0/Voltage', None)
        s.add_path('/Dc/0/Current', None)
    assert vh.signal_count() == 1
    assert set(vh.items_changed()[0]) == {'/Dc/0/Voltage', '/Dc/0/Current'}
