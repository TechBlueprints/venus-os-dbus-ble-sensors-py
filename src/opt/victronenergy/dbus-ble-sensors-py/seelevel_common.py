from ve_types import *
from ble_device import BleDevice
import logging


class BleDeviceSeeLevel(BleDevice):
    """
    Shared base class for SeeLevel 709-BT protocols.

    Both BTP3 (manufacturer 305) and BTP7 (manufacturer 3264) share a common
    advertisement header, tank level processing, and error handling. Subclasses
    provide protocol-specific parsing in handle_manufacturer_data().

    Advertisement header (shared):
        Bytes 0-2: Coach ID (24-bit, little-endian)
    """

    CUSTOM_PARSING = True

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
            'product_name': self.PRODUCT_NAME,
            'device_name': 'SeeLevel',
            'dev_prefix': 'seelevel',
            'roles': dict(self.ROLES),
            'regs': [],
        })

    def _parse_coach_id(self, manufacturer_data: bytes) -> int:
        """Parse 24-bit little-endian coach ID from bytes 0-2."""
        return int.from_bytes(manufacturer_data[0:3], byteorder='little')

    def _build_tank_sensor_data(self, level: int, role_service) -> dict:
        """Build D-Bus sensor data dict from a tank level percentage (0-100)."""
        capacity = float(role_service['Capacity'] or 0)
        remaining = round(capacity * level / 100.0, 3) if capacity else 0.0

        return {
            'RawValue': float(level),
            'Level': level,
            'Remaining': remaining,
            'Status': 0,
        }

    def _set_error_status(self, role_service, error_code=None):
        """Set error status on a role service. Logs the error code if known."""
        if error_code is not None:
            status_msg = self.STATUS_CODES.get(error_code, f"Unknown ({error_code})")
            logging.debug(f"{self._plog} error: {status_msg}")
        role_service['Status'] = 5
        role_service.connect()
