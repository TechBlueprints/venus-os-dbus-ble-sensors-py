from ble_role import BleRole


class BleRoleBattery(BleRole):
    """
    Battery monitor role (Venus OS ``battery`` service type).

    Used for Victron SmartShunt / BMV-Smart units reached over BLE so
    they appear alongside VE.Direct BMVs as
    ``com.victronenergy.battery.*``.  A device claiming this role must
    at least publish ``/Dc/0/Voltage``; the SmartShunt driver fills the
    rest of the Instant Readout set (SOC, current, consumed Ah, TTG).
    """

    NAME = 'battery'

    def __init__(self, config: dict = None):
        super().__init__(config)

        self.info.update(
            {
                'name': 'battery',
                'dev_instance': 50,
                'settings': [],
                'alarms': [],
            },
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        with svc as s:
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            s.add_path("/Dc/0/Power", None)
            s.add_path("/Dc/0/Temperature", None)
            # Starter / midpoint aux (Instant Readout aux mode).
            s.add_path("/Dc/1/Voltage", None)
            s.add_path("/Soc", None)
            s.add_path("/ConsumedAmphours", None)
            s.add_path("/TimeToGo", None)
            s.add_path("/Serial", None)
            s.add_path("/Relay/0/State", None)
            # AlarmReason bits from Instant Readout.  0=ok, 2=alarm.
            s.add_path("/Alarms/LowVoltage", 0)
            s.add_path("/Alarms/HighVoltage", 0)
            s.add_path("/Alarms/LowSoc", 0)
            s.add_path("/Alarms/LowStarterVoltage", 0)
            s.add_path("/Alarms/HighStarterVoltage", 0)
            s.add_path("/Alarms/LowTemperature", 0)
            s.add_path("/Alarms/HighTemperature", 0)
            s.add_path("/Alarms/MidVoltage", 0)
            s.add_path("/Alarms/Overload", 0)
            s.add_path("/Alarms/Ripple", 0)
