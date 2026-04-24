# Blue Smart IP22 Charger BLE Integration

Status: **first working pass landed on `feature/ip22-smart-charger`.**
The IP22 is published as a standard `com.victronenergy.charger.*` D-Bus
service, decoding live telemetry from encrypted Victron advertisements
and accepting on/off writes through a paired GATT session.

## Device model

| Parameter | Value |
|---|---|
| Manufacturer ID | `0x02E1` |
| Product IDs | `0xA330`–`0xA33F` (Blue Smart IP22 charger family) |
| Advertisement mode byte | `0x08` (AcCharger, per `victron_ble`) |
| GATT pairing | SMP passkey-entry bonding, default PIN `014916` |
| GATT service | `306b0001-b081-4037-83dc-e59fcc3cdfd0` (shared with Orion-TR) |

At power-off the IP22 drops its encrypted advertisement payload and only
broadcasts the 4-byte product-id prefix.  The driver treats that as a
synthetic "state = off" frame so the service tracks the on/off toggle
without gapping.

## Advertisement payload

The stock `victron_ble.devices.AcCharger` decoder parses IP22 payloads
without modification.  Fields surfaced today:

| GUI path | Source | Notes |
|---|---|---|
| `/State` | `charge_state` | Off / Bulk / Absorption / Float / Power Supply |
| `/ErrorCode` | `charger_error` | Victron charger-error enum |
| `/Dc/0/Voltage` | `output_voltage1` | Primary output volts |
| `/Dc/0/Current` | `output_current1` | Primary output amps |
| `/Dc/0/Power` | `v1 * i1` | Computed locally |
| `/Dc/1/{Voltage,Current}` | `output_{voltage,current}2` | Multi-output SKUs |
| `/Dc/2/{Voltage,Current}` | `output_{voltage,current}3` | Multi-output SKUs |
| `/Dc/0/Temperature` | `temperature` | Not populated by 12|30 SKU tested |
| `/Ac/In/L1/I` | `ac_current` | Not populated by 12|30 SKU tested |

## GATT control path

`BleDeviceIP22Charger._ip22_on_mode_write` funnels `/Mode` writes through
the shared `AsyncGATTWriter` → VREG `0x0200` (`DEVICE_MODE`).  Accepted
values match the Orion-TR driver: `1` = On, `4` = Off.  Other values are
rejected at the D-Bus layer.

Writes pause the passive scan loop, connect, write, disconnect, and
resume scanning — identical to the Orion-TR path.

## Key provisioning

The CLI at `orion_tr_key_cli.py` handles the first-time SMP bond + PUK/
PIN auth + VREG `0xEC65` read for any device that exposes the standard
Victron `306b`/`9758` services, so it is reused verbatim.  Keys land in a
dedicated settings namespace (`/Settings/Devices/ip22_<mac>/`) via
`ip22_key_settings.py` to keep the Orion-TR and IP22 trees separate.

Confirmed working: `ED:47:4D:2A:7C:2A` (HQ2133XMU6Y) bonded on the
second Pair() attempt with passkey `014916` (the default cerbo inbound
PIN) and returned a valid advertisement key.

## Known gaps / future work

- **Charger vs Power Supply mode toggle.** On VE.Direct IP43 chargers
  service, so no standard path exists.  A VREG enumeration pass may
  surface one — pending exploration.
- **Charge-setpoint writes.** `/Link/ChargeVoltage`, `/Link/ChargeCurrent`,
  `/Settings/ChargeCurrentLimit` are declared on the role service but
  not yet wired through to the GATT writer.  Needed for DVCC parity
  with the IP43 reference implementation.
- **Marginal-RSSI pairing.** The second IP22 on the bench (F2:86, RSSI
  -80 dBm) consistently fails Pair() with `AuthenticationCanceled`.
  Moving it closer to the cerbo or using a USB BLE adapter with a
  better antenna is the workaround; no driver change needed.
