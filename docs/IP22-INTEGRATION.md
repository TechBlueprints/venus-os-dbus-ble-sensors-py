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

- **`/Mode` writes are a no-op on this firmware.**  A direct probe of
  `ED:47:4D:2A:7C:2A` (firmware `fc00c140`, advertised as `0.162`)
  confirmed that writing VREG `0x0200` with either opcode `0x06` (Set)
  or `0x26` (privileged Set), payload `= 1` or `= 4`, returns the
  application-layer error `09 00 19 02 00 01` on `DATA_LAST` — i.e.
  "register not writable" for every value.  The earlier bench
  observation that `/Mode = 4` caused the unit to drop to the short
  product-id-only advertisement was a coincidence: subsequent writes
  do not move the state machine either direction.

  DEVICE_MODE is effectively read-only on IP22 charger firmware
  on a different VREG (or using a mechanism outside the plain CBOR
  Set op); finding it is deferred until there's a reason to pursue
  on/off control beyond telemetry.

  Progress notes from a live GATT probe against ED:47:

  | VREG | SetValue=01 response | Meaning |
  |---|---|---|
  | `0x0200` (DEVICE_MODE) | `09 00 19 02 00 01` | code `01` = **unknown** (not in VREG table) |
  | `0x0101` (COMMAND) | `09 00 19 01 01 03` | code `03` = **readonly** (exists, but rejected) |
  | `0xEDE0`-`0xEDE1` | no ACK / session drop | device doesn't respond |

  So on this firmware `0x0200` simply isn't implemented — the Orion-TR
  path is a dead end here.  `0x0101` exists but isn't writable either.
  on"/"Charger off" switch to must route through yet another VREG;
  candidates worth probing next are the BPC range (`0xDA00`+) and
  `/Settings/Function` (0xEDFA-ish), plus the command-shaped writes
  into `0xEC77`/`0xEC78` the VE.Smart keep-alive path uses.  Easiest
  unit on and off — both writes will be obvious in a `btmon` trace.

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
