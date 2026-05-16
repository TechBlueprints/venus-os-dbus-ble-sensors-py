from __future__ import annotations
from ve_types import *
from ble_device import BleDevice
import logging
import math
from dbus_role_service import DbusRoleService

class BleDeviceRuuvi(BleDevice):
    """
    Ruuvi devices class managing :
    - Ruuvi Tag
    - Ruuvi Air

    Cf.
    - https://ruuvi.com/ruuvitag/
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/ruuvi.c
    """

    MANUFACTURER_ID = 0x0499 # 'Ruuvi Innovations Ltd.'

    @staticmethod
    def _get_low_battery_state(role_service: DbusRoleService) -> int:
        level = 2.5
        if (temperature := role_service['temperature']) is not None:
            if temperature < -20:
                level = 2.0
            elif temperature < 0:
                level = 2.2

        if role_service['/Alarms/LowBattery'] is True:
            level += 0.4

        return int(role_service['BatteryVoltage'] < level)

    @staticmethod
    def _xlate_lum(value: int) -> int:
        scale = 16 * math.log(2) / 254
        value_int8 = value & 0xff
        return math.exp(value_int8 * scale) - 1.0

    MODELS = {
        5: {  # Format 5, aka RAWv2
            'device_name': 'Ruuvi',
            'roles': {'temperature': {}, 'movement': {}},
            'regs': [
                {
                    'name':  'Temperature',
                    'type': VE_SN16,
                    'offset': 1,
                    'scale': 200,
                    'inval': 0x8000,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'temperature',  # 0.1 °C display
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'Humidity',
                    'type': VE_UN16,
                    'offset': 3,
                    'scale': 400,
                    'inval': 0xffff,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'humidity',     # 0.1 %
                    # .format	= &veUnitPercentage,
                },
                {
                    'name':  'Pressure',
                    'type': VE_UN16,
                    'offset': 5,
                    'scale': 100,
                    'bias': 500,
                    'inval': 0xffff,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'pressure',     # 1 hPa
                    # .format	= &veUnitHectoPascal,
                },
                {
                    'name': 'AccelX',
                    'type': VE_SN16,
                    'offset': 7,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'acceleration',  # 0.01 g
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'AccelY',
                    'type': VE_SN16,
                    'offset': 9,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'acceleration',
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'AccelZ',
                    'type': VE_SN16,
                    'offset': 11,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'acceleration',
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'BatteryVoltage',
                    'type': VE_UN16,
                    'offset': 13,
                    'shift': 5,
                    'bits': 11,
                    'scale': 1000,
                    'bias': 1.6,
                    'inval': 0x3ff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'voltage',       # 0.01 V
                    # .format	= &veUnitVolt2Dec,
                },
                {
                    'name': 'TxPower',
                    'type': VE_UN8,
                    'offset': 14,
                    'bits': 5,
                    'scale': 0.5,
                    'bias': -40,
                    'inval': 0x1f,
                    'flags': ['REG_FLAG_INVALID'],
                    # TxPower is already discrete in 0.5 dBm steps —
                    # no rounding needed; dedup alone handles it.
                    # .format	= &veUnitdBm,
                },
                {
                    # MovementCounter increments only when the Ruuvi's
                    # onboard accelerometer interrupt fires — i.e., the
                    # firmware decided the tag actually moved.  We use
                    # this as a per-device gate in ``update_data``
                    # below: when the counter is unchanged between two
                    # ads, AccelX/Y/Z are dropped from the role_data
                    # dict so vedbus is not poked with another redundant
                    # gravity-vector reading.  Stationary tag => silent.
                    'name': 'MovementCounter',
                    'type': VE_UN8,
                    'offset': 15,
                    'inval': 0xff,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_INVALID'],
                    # Integer counter — vedbus's exact-equality dedup
                    # handles it, no rounding policy needed.
                },
                {
                    'name': 'SeqNo',
                    'type': VE_UN16,
                    'offset': 16,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # SeqNo (measurement sequence number) increments on
                    # every advertisement.  If published it would drive
                    # an ItemsChanged emit per ad on every Ruuvi role
                    # service (was the single biggest IC-rate driver
                    # before this change).  Mark roles=[None] so the
                    # parser skips it entirely — no D-Bus path created,
                    # no consumer can rely on it.
                    'roles': [None],
                    # .format	= &veUnitNone,
                },
            ],
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': _get_low_battery_state
                }
            ]
        },
        6: {  # Format 6
            'device_name': 'Ruuvi Air',
            'roles': {'temperature': {}},
            'regs': [
                {
                    'name':  'Temperature',
                    'type': VE_SN16,
                    'offset': 1,
                    'scale': 200,
                    'inval': 0x8000,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'temperature',
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'Humidity',
                    'type': VE_UN16,
                    'offset': 3,
                    'scale': 400,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'humidity',
                    # .format	= &veUnitPercentage,
                },
                {
                    'name':  'Pressure',
                    'type': VE_UN16,
                    'offset': 5,
                    'scale': 100,
                    'bias': 500,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'pressure',
                    # .format	= &veUnitHectoPascal,
                },
                {
                    'name':  'PM25',
                    'type': VE_UN16,
                    'offset': 7,
                    'scale': 10,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'concentration',
                    # .format	= &veUnitUgM3,
                },
                {
                    'name':  'CO2',
                    'type': VE_UN16,
                    'offset': 9,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    'sensor_type': 'concentration',
                    # .format	= &veUnitPPM,
                },
                {
                    'name':  'VOC',
                    'type': VE_UN8,
                    'offset': 11,
                    # Index value (no unit) — no rounding.
                    # .format	= &veUnitIndex,
                },
                {
                    'name':  'NOX',
                    'type': VE_UN8,
                    'offset': 12,
                    # .format	= &veUnitIndex,
                },
                {
                    'name':  'Luminosity',
                    'type': VE_UN8,
                    'offset': 13,
                    'xlate': _xlate_lum,
                    'inval': 0xff,
                    'flags': ['REG_FLAG_INVALID'],
                    'sensor_type': 'luminosity',
                    # .format	= &veUnitLux,
                },
                {
                    'name':  'SeqNo',
                    'type': VE_UN8,
                    'offset': 15,
                    'roles': [None]
                    # .format	= &veUnitLux,
                },
                {
                    'name':  'Flags',
                    'type': VE_UN8,
                    'offset': 16,
                    'roles': [None]
                    # .format	= &veUnitNone,
                },
            ],
        }
    }

    def __init__(self, address: str):
        super().__init__(address)
        self.manufacturer_data_length = None

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_name': 'RuuviTag',
            'product_id': 0xC029,
            'dev_prefix': 'ruuvi',
        })

        model_id = self._load_number(
            {'name':  'model', 'type': VE_UN8, 'offset': 0},
            manufacturer_data
        )
        model_info = BleDeviceRuuvi.MODELS.get(model_id, None)
        if model_info is None:
            raise ValueError(f"Unknown Ruuvi model ID: {model_id}")

        self.manufacturer_data_length = 24 if model_id == 5 else 20
        self.info.update(model_info)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if (length := self.manufacturer_data_length) is None:
            return len(manufacturer_data) >= 1
        else:
            return len(manufacturer_data) == self.manufacturer_data_length

    def update_data(self, role_service: DbusRoleService, sensor_data: dict):
        # ── Movement-role: gate AccelX/Y/Z on MovementCounter changes ──
        #
        # The Ruuvi format 5 firmware increments MovementCounter only
        # when its onboard accelerometer interrupt fires — i.e., the
        # tag actually moved.  Between motion events the counter is
        # constant, even though the gravity-vector axes flicker by a
        # few mg from sensor noise on every advertisement.  Rounding
        # alone can't suppress those flickers because one axis is
        # always near a rounding boundary (the horizontal one, ≈0 g).
        #
        # Per-device cache: when this ad's MovementCounter matches the
        # last one we saw, we delete AccelX/Y/Z from the role_data
        # dict.  ``_update_dbus_data`` only iterates what's in the
        # dict, so accel paths are not poked that cycle.  MovementCounter
        # itself stays in the dict; vedbus's exact-equality dedup will
        # then skip emitting it too (same value as last time), and
        # vedbus emits no ItemsChanged at all — exactly the "stationary
        # tag, silent service" outcome we want.
        if 'MovementCounter' in sensor_data:
            counter = sensor_data['MovementCounter']
            last = getattr(self, '_last_movement_counter', None)
            if counter is not None and counter == last:
                for k in ('AccelX', 'AccelY', 'AccelZ'):
                    sensor_data.pop(k, None)
            else:
                # Counter advanced (or first ad after boot) — let the
                # accel values through and remember the new counter.
                self._last_movement_counter = counter

        # ── Format 6 / Ruuvi Air: pack VOC/NOX flag bits ───────────────
        #
        # Flags (format 6 / Ruuvi Air only) carry extra VOC/NOX bits.
        # Format 5 (RuuviTag) has no Flags reg — do not require it for
        # those frames.
        voc = sensor_data.get('VOC', None)
        nox = sensor_data.get('NOX', None)
        if voc is None and nox is None:
            return

        flags = sensor_data.get('Flags', None)
        if flags is None or flags > 255:
            logging.warning(f"{self._plog} can not update sensor data, missing Flags value")
            return

        if voc is not None:
            sensor_data['VOC'] = (voc << 1) | ((flags >> 6) & 1)
            if sensor_data['VOC'] == 0x1ff:
                sensor_data['VOC'] = None

        if nox is not None:
            sensor_data['NOX'] = (nox << 1) | ((flags >> 7) & 1)
            if sensor_data['NOX'] == 0x1ff:
                sensor_data['NOX'] = None
