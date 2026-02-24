from ve_types import *
from ble_device import BleDevice
import logging


class BleDeviceSeeLevelBTP7(BleDevice):
    """
    SeeLevel 709-BTP7 tank monitor.

    Broadcasts 8 tank levels (0-100%) and battery voltage in a single
    14-byte manufacturer data advertisement.

    Byte layout:
        0-2: Coach ID (24-bit, little-endian)
        3-10: Tank levels (1 byte each, 0-100 = %, >100 = error code)
              Slots: Fresh, Wash, Toilet, Fresh2, Wash2, Toilet2, Wash3, LPG
        11:  Battery voltage * 100

    Cf.
    - https://github.com/TechBlueprints/victron-seelevel-python
    """

    MANUFACTURER_ID = 0x0CC0  # 3264
    CUSTOM_PARSING = True

    TANK_SLOTS = [
        ("Fresh Water", 1),      # slot 0: FluidType = Fresh water
        ("Wash Water", 2),       # slot 1: FluidType = Waste water
        ("Toilet Water", 5),     # slot 2: FluidType = Black water
        ("Fresh Water 2", 1),    # slot 3
        ("Wash Water 2", 2),     # slot 4
        ("Toilet Water 2", 5),   # slot 5
        ("Wash Water 3", 2),     # slot 6
        ("LPG", 8),             # slot 7: FluidType = LPG
    ]

    STATUS_CODES = {
        101: "Short Circuit",
        102: "Open",
        103: "Bitcount error",
        104: "Non-stacked config with stacked data",
        105: "Stacked, missing bottom sender",
        106: "Stacked, missing top sender",
        108: "Bad Checksum",
        110: "Tank disabled",
        111: "Tank init",
    }

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_id': 0xA142,
            'product_name': 'SeeLevel 709-BTP7',
            'device_name': 'SeeLevel',
            'dev_prefix': 'seelevel',
            'roles': {'tank': {}},
            'regs': [],
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        return len(manufacturer_data) >= 12

    def init(self):
        self._load_configuration()

        for slot, (tank_name, fluid_type) in enumerate(self.TANK_SLOTS):
            role_service = self._create_indexed_role_service(
                'tank', slot, device_name=f"SeeLevel {tank_name}")
            if role_service and role_service['FluidType'] == 0:
                role_service['FluidType'] = fluid_type

        logging.debug(f"{self._plog} initialized {len(self._role_services)} tank slots")

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        for slot in range(8):
            if not self._is_indexed_role_enabled('tank', slot):
                continue

            role_service = self._role_services.get(f'tank_{slot:02d}')
            if role_service is None:
                continue

            level = manufacturer_data[slot + 3]

            if level > 100:
                status_msg = self.STATUS_CODES.get(level, f"Unknown ({level})")
                logging.debug(f"{self._plog} slot {slot}: error {status_msg}")
                role_service['Status'] = 5
                role_service.connect()
                continue

            capacity = float(role_service['Capacity'] or 0)
            remaining = round(capacity * level / 100.0, 3) if capacity else 0.0

            sensor_data = {
                'RawValue': float(level),
                'Level': level,
                'Remaining': remaining,
                'Status': 0,
            }

            self._update_dbus_data(role_service, sensor_data)
            role_service.connect()
