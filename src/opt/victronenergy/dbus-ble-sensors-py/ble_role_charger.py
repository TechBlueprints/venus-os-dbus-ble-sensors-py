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
        ble = role_service._ble_device

        def _bind(handler_name: str):
            """Build an onchangecallback that forwards to a method on the
            BLE device class if it exists, otherwise treats the write as a
            passive store-only path (D-Bus updates, no GATT)."""
            handler = getattr(ble, handler_name, None)
            if handler is None:
                return lambda _path, _value: True
            return lambda _path, value: bool(handler(role_service, value))

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

            # IP22 firmware does not implement VREG 0x0200; /Mode stays
            # read-only.  Tell gui-v2 not to expose a "Charger off" toggle
            # — the rotary switch on the front panel is the only off
            # control on this hardware.
            s.add_path("/Mode", 1)
            s.add_path("/Capabilities/HasNoDeviceOffMode", 1)

            # ----------------------------------------------------------
            # DVCC contract — paths dbus-systemcalc-py writes onto a
            # charger to integrate it into the system.  Mirrors the set
            # an integrated VE.Bus / VE.Direct charger publishes.
            # ----------------------------------------------------------
            # /Link/NetworkStatus: 4 = "stand-alone" until DVCC takes over.
            # systemcalc flips this when a BMS / GX takes control.
            s.add_path("/Link/NetworkStatus", 4)

            # /Link/NetworkMode: bitmask DVCC writes to indicate which
            # links are active (1=ext control, 2=ext voltage, 4=BMS, ...).
            # We just store it; IP22 firmware has no consumer VREG.
            s.add_path("/Link/NetworkMode", 0,
                       writeable=True,
                       onchangecallback=_bind("_ip22_on_link_passive_write"))

            # /Link/ChargeCurrent: target current pushed by DVCC (amps).
            # Wired to VREG 0xEDF0 with a 0.1 A deadband so steady-state
            # DVCC re-publishes don't flap the GATT link.
            s.add_path(
                "/Link/ChargeCurrent", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_link_charge_current_write"))

            # /Link/ChargeVoltage: target absorption voltage pushed by
            # DVCC (volts).  Wired to VREG 0xEDF7 with 0.05 V deadband.
            # The IP22 requires battery-type = USER (VREG 0xEDF1 = 0xFF)
            # before 0xEDF7 accepts writes; the handler sets that
            # transparently on first use.
            s.add_path(
                "/Link/ChargeVoltage", None,
                writeable=True,
                onchangecallback=_bind("_ip22_on_link_charge_voltage_write"))

            # /Link/{TemperatureSense,VoltageSense,BatteryCurrent}: BMS
            # sense values DVCC pushes for temperature- and
            # voltage-compensated charging.  IP22 has no VREG consumer
            # for these; we surface them on D-Bus for systemcalc's own
            # bookkeeping but don't push to the wire.
            s.add_path("/Link/TemperatureSense", None,
                       writeable=True,
                       onchangecallback=_bind("_ip22_on_link_passive_write"))
            s.add_path("/Link/VoltageSense", None,
                       writeable=True,
                       onchangecallback=_bind("_ip22_on_link_passive_write"))
            s.add_path("/Link/BatteryCurrent", None,
                       writeable=True,
                       onchangecallback=_bind("_ip22_on_link_passive_write"))
            s.add_path("/Link/TemperatureSenseActive", 0)
            s.add_path("/Link/VoltageSenseActive", 0)

            # /Settings/BmsPresent: DVCC writes 1 when a BMS is in the
            # system.  Stored only — IP22 has no equivalent VREG.
            s.add_path("/Settings/BmsPresent", 0,
                       writeable=True,
                       onchangecallback=_bind("_ip22_on_link_passive_write"))

            # /Settings/ChargeCurrentLimit — writable via GATT 0xEDF0.
            # The device clamps below ~7.5A to its hardware minimum.
            # Same VREG as /Link/ChargeCurrent above; both paths land at
            # 0xEDF0.  /Link/ChargeCurrent is the DVCC-side override,
            # /Settings/ChargeCurrentLimit is the user-set cap.
            if hasattr(ble, "_ip22_on_charge_current_limit_write"):
                def on_ccl(_path, value):
                    return ble._ip22_on_charge_current_limit_write(
                        role_service, value)
                s.add_path(
                    "/Settings/ChargeCurrentLimit", None,
                    writeable=True, onchangecallback=on_ccl)
            else:
                s.add_path("/Settings/ChargeCurrentLimit", None)
