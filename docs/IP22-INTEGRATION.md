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

  - The CTRL char returns `00 01 00 01 50 14 00` on a `ReadValue` —
    byte 0 is the "Flags" field, and `0x00` means **path protocol
    not supported** on this firmware.  The opcode-10 / opcode-11 /
    opcode-12 path tree (see `vesmart-server/gattserver.py`) is the
    interface modern devices expose; IP22 fw `0.162` predates it.
    `0x00208c10`, size ~24 KB) is where the app falls back to a
    hard-coded **VBusItem-path ↔ VREG** map per device family.  The
    decoder tables from its caller — those callers are what register
    the SmartCharger-specific mappings.  Those data tables aren't
    extractable with `strings` / static scanning alone; the path-string
    through the GOT, not directly encoded in the call-site bytes.
  - Probed `0xEDE0`-`EDFC`, `EDFA-FD`, `EDF8` on IP22 with PUK+PIN
    auth + every opcode variant I could think of (`0x06`, `0x26`,
    `0x46`, `0x66`): all return code `02` ("encryption not supported")
    on 1-byte writes, and get no ACK at all on 2-byte writes.  So
    the IP22 has a real write-privilege class above what bond + PUK
    + PIN provides, or the correct register is in a different range
    entirely — most likely the latter given the unambiguous "unknown
    register" response on `0x0200`.

  Next time this is pursued, the pragmatic move is to capture a BLE
  The write frame on `306b0003` will reveal both the VREG and the
  opcode in one shot, sidestepping the stripped-binary archaeology.

  path (`/Link/Command`, `/Mode`, `/Settings/Function`, `/Bpc/...`,
  `/Settings/PowerSupplyModeVoltage`, etc.) in `.rodata`.

  The path↔VREG map is **not** stored as a static C++ array — no
  `.data.rel.ro` or `.rodata` location holds 32-bit pointers to the
  QSL headers paired with VREG immediates.  The map is built at
  the caller passes in, and `init()` itself has no direct callers in
  the symbol graph (it's reached only through Qt's virtual dispatch).
  None of the candidate VREGs yielded a writable register class via
  any opcode tried after PUK + PIN authentication.

  Conclusion: the static analysis available without runtime
  not sufficient to recover the IP22 on/off VREG.  A `btsnoop_hci.log`
  the unit will show the write directly and is the clearly cheaper
  next step.

  that the global tables are:

    (3568 entries)
  - `pathsByVreg`  = `QMultiHash<ushort, std::pair<Path*, int>>`
    (1199 entries — keyed by VREG directly)
    (1199 entries — name-keyed, not vreg-keyed despite the symbol)

  (144-byte Spans: 128 ctrl bytes + Entry* + alloc/nextFree).  Walking
  pathsByPath with 32-byte stride correctly yields path strings,
  including all 14 SmartCharger paths we care about.  pathsByVreg's
  per-entry layout for `QMultiHash<ushort, pair<Path*, int>>` did NOT
  yield readable (vreg, Path*) pairs with strides 16/24/32 — Path*
  values stored there are byte-distinct from the Path* values in
  pathsByPath, so the cross-reference scan came up empty in some runs
  and inconsistent in others.

  A few more hours of either (a) Qt-6-internal layout reconstruction
  HCI snoop log path is still strictly cheaper.

  **April 2026 — confirmed register set and writability via direct GATT
  probe of ED:47** (firmware 3.65, app version VREG `0x0102`):

  | VREG | Type | Read | Write | Notes |
  |---|---|---|---|---|
  | `0x0100` | u32 | ✓ | n/a | product id `0x00FFA330` (BSC IP22 12/30) |
  | `0x0102` | u16 | ✓ | n/a | application version `0x365FF` ≈ fw 3.65 |
  | `0x010A` | str | ✓ | n/a | serial `HQ2133XMU6Y` |
  | `0x010B` | str | ✓ | n/a | "BSC IP22 12/30 (1)" |
  | `0x010C` | str | ✓ | n/a | "BSC IP22 12/30…HQ2133XMU6Y" (long name) |
  | `0x010F` | — | ✗ code 1 | — | not implemented |
  | `0x0140` | u32 | ✓ | n/a | capabilities `0x40C100FC` |
  | `0x0200` | — | ✗ code 1 | ✗ code 1 | **not implemented on IP22** (Orion-TR uses this) |
  | `0x0201` | u8 | ✓ | ✗ code 3 | Device State (read-only) — `0x03=Bulk`, `0x04=Absorption`, etc. |
  | `0x0202` | — | ✗ code 1 | ✗ code 1 | **not implemented on IP22** (BlueSolar remote-control mask) |
  | `0x0207` | u32 | ✓ | ✗ code 3 | Device off reason — read-only (re-probed Apr 2026) |
  | `0xEDF0` | u16 | ✓ | ✓ (clamped) | **Battery max current** in 0.1A; writes accepted but device clamps to ≥7.5A |
  | `0xEDF1` | u8 | ✓ | ✓ | Battery type; `0xFF`=USER unlocks voltage writes |
  | `0xEDF7` | u16 | ✓ | ✓ when `EDF1=USER` | Absorption voltage in 0.01V |
  | `0xEDF6` | u16 | ✓ | ✓ | Float voltage |
  | `0xEDFC` | u16 | ✓ | ✓ | Bulk time limit |
  | `0xEDFE` | u8 | ✓ | ✗ code 3 | Adaptive mode (read-only on this fw) |
  | `0xEDFF` | u8 | ✓ | ? | Batterysafe mode |
  | (~22 more `0xEDxx` settings) | | ✓ | ✓ when unlocked | per BlueSolar doc |

  Error code interpretation observed on this BLE CBOR layer:
  `1` = unknown register, `2` = bad value/size, `3` = read-only.

  **No on/off VREG was found.**  The IP22 firmware does not expose
  `0x0200`/`0x0202`, and `0x0207` (off-reason) appears read-only on
  this firmware (writes accepted silently but state doesn't change).
  Both [pvtex/Victron_BlueSmart_IP22](https://github.com/pvtex/Victron_BlueSmart_IP22)
  and [wasn-eu/Victron_BlueSmart_IP22](https://github.com/wasn-eu/Victron_BlueSmart_IP22)
  achieve "remote control" by manipulating `0xEDF0` (charge-current
  limit) only.  That's the practical control surface this driver should
  expose.

- **Charger vs Power Supply mode toggle.** On VE.Direct IP43 chargers
  service, so no standard path exists.  A VREG enumeration pass may
  surface one — pending exploration.
- **Charge-setpoint writes.** `/Settings/ChargeCurrentLimit` is now
  wired through `BleDeviceIP22Charger._ip22_on_charge_current_limit_write`
  → VREG `0xEDF0` (commit `aa7c137`).  Setting it to a value at or below
  the firmware's hardware minimum (~7.5 A) clamps to that minimum rather
  than turning the unit off — see "On/off mechanism" below.
  `/Link/ChargeVoltage` / `/Link/ChargeCurrent` are still declared on the
  role service but not yet wired; deferred until DVCC pulls actually
  arrive against this driver.
- **Short-frame "off" override.** Some IP22 firmwares interleave the
  4-byte product-id beacon with the encrypted telemetry advertisement as
  a power-saving rotation even while the charger is running.  An older
  version of `handle_manufacturer_data` interpreted any short frame as a
  hard "off" snapshot, which constantly clobbered live telemetry.  The
  driver now keeps a `_last_full_telemetry_at` timestamp and only honours
  the short frame as off-state once the IP22 has gone quiet for
  `_OFF_FRAME_GRACE_S` (30 s).
- **On/off mechanism (final answer).**  The IP22 firmware on the bench
  unit (3.65, advertised as `0.162`) does not implement `0x0200`
  (`DEVICE_MODE`) or `0x0202` (BlueSolar remote-control mask).  `0x0207`
  (`DeviceOffReason`) is read-only (returns `09 00 19 02 07 03` for any
  write).  No alternative writable on/off VREG was found over multiple
  range probes (0x0000–0x02FF, 0x0E00–0x0FFF, 0xEC00–0xECFF,
  0xEDA0–0xEDFF, 0x0140–0x017F).  Both the
  [pvtex](https://github.com/pvtex/Victron_BlueSmart_IP22) and
  [wasn-eu](https://github.com/wasn-eu/Victron_BlueSmart_IP22) reference
  drivers come to the same conclusion: the only practical control over
  IP22 BLE is the charge-current limit (`0xEDF0`), which is what this
  driver exposes via `/Settings/ChargeCurrentLimit`.  `/Mode` remains
  read-only; gui-v2 should rely on `/Capabilities/HasNoDeviceOffMode = 1`
  if it gets exposed in a future revision.
- **Marginal-RSSI pairing.** The second IP22 on the bench (F2:86, RSSI
  -80 dBm) consistently fails Pair() with `AuthenticationCanceled`.
  Moving it closer to the cerbo or using a USB BLE adapter with a
  better antenna is the workaround; no driver change needed.
