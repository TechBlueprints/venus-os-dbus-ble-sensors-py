"""
Charger role (Venus OS ``charger`` service type).

Used for Victron Blue Smart IP22 chargers reached over BLE so they appear
alongside VE.Direct Phoenix Smart IP43 units on the gui-v2 *DC Sources*
page and interact with the rest of the system through the standard
``com.victronenergy.charger`` D-Bus API.
"""
from ble_role import BleRole

class BleRoleCharger(BleRole):
    NAME = "charger"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "charger",
                "dev_instance": 290,
                "settings": [],
                "alarms": [],
            }
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        with svc as s:
            # Output/battery side
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            s.add_path("/Dc/0/Power", None)
            s.add_path("/Dc/0/Temperature", None)
            # Multi-output chargers (IP22 30A is single-output; 2/3 stay None)
            s.add_path("/Dc/1/Voltage", None)
            s.add_path("/Dc/1/Current", None)
            s.add_path("/Dc/2/Voltage", None)
            s.add_path("/Dc/2/Current", None)
            s.add_path("/NrOfOutputs", 1)

            # AC input
            s.add_path("/Ac/In/L1/I", None)
            s.add_path("/Ac/In/CurrentLimit", None)

            # Status
            s.add_path("/State", 0)
            s.add_path("/ErrorCode", 0)
            s.add_path("/DeviceOffReason", 0)
            s.add_path("/Relay/0/State", 0)

            def on_mode(path, value):
                return role_service._ble_device._ip22_on_mode_write(
                    role_service, int(value))

            s.add_path("/Mode", 1, writeable=True, onchangecallback=on_mode)
