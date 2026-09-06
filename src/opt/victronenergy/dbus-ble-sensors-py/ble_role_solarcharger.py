"""
Solar-charger role (Venus OS ``solarcharger`` service type).

Used for Victron SmartSolar MPPTs reached over BLE advertisements so they
appear beside the VE.Direct-wired units on the *Solar* pages and feed
dbus-systemcalc through the standard ``com.victronenergy.solarcharger``
paths it reads (``/Pv/Power``, ``/Yield/Power``, ``/Load/I``).

Paths whose source is not in the Instant Readout advertisement start as
``None``, never a fabricated 0.  ``/Pv/V`` and ``/MppOperationMode`` come
from the VE.Direct HEX registers (0xEDBB, 0xEDB3), which the SmartSolar
driver polls over a short GATT session every couple of minutes; gui-v2's
Solar list shows ``/Pv/V`` as *Voltage* and derives *Current* as
``/Yield/Power`` divided by it, so both columns depend on that poll and
read "--" again when it goes stale.  ``/Pv/I`` stays ``None``: the
75/15 does not expose PV current (0xEDBD), and the VE.Direct-wired
units do not publish it either.
"""
from ble_role import BleRole


class BleRoleSolarCharger(BleRole):
    NAME = "solarcharger"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update({
            "name": "solarcharger",
            "dev_instance": 288,
            "settings": [],
            "alarms": [],
        })

    def init(self, role_service):
        svc = role_service._dbus_service
        with svc as s:
            # Battery side
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            # PV side.  /Pv/V is HEX-only (0xEDBB), filled by the driver's
            # PV poll; the advertisement carries PV power, from which
            # systemcalc derives what it needs.
            s.add_path("/Pv/V", None)
            s.add_path("/Pv/I", None)
            s.add_path("/Pv/Power", None)
            s.add_path("/NrOfTrackers", 1)
            # Yield.  /Yield/Power is instantaneous PV power in W (what
            # systemcalc sums); /History/Daily/0/Yield is today's kWh,
            # straight from the advertisement.  Lifetime yield is HEX-only.
            s.add_path("/Yield/Power", None)
            s.add_path("/Yield/User", None)
            s.add_path("/Yield/System", None)
            s.add_path("/History/Daily/0/Yield", None)
            # gui-v2's Solar list (TrackerSolarInput::daysAvailableChanged)
            # does not even subscribe to /History/Daily/0/Yield until
            # /History/Overall/DaysAvailable is > 0, so without this the
            # "Yield Today" column reads "--" while the value sits on the
            # bus.  We have exactly one day of history: today's, from the
            # advertisement.  Declaring 1 is the truth, not a fabrication.
            s.add_path("/History/Overall/DaysAvailable", 1)
            # Load output (75/15 has one)
            s.add_path("/Load/State", None)
            s.add_path("/Load/I", None)
            # Status
            s.add_path("/State", 0)
            s.add_path("/ErrorCode", 0)
            s.add_path("/MppOperationMode", None)
            s.add_path("/Relay/0/State", 0)
            s.add_path("/Serial", None)
            s.add_path("/Settings/BatteryVoltage", None)
            # Charger-side alarms only, as on the charger role.
            s.add_path("/Alarms/HighTemperature", 0)
            s.add_path("/Alarms/HighVoltage", 0)
            s.add_path("/Alarms/HighRipple", 0)
            s.add_path("/Alarms/Fan", 0)
