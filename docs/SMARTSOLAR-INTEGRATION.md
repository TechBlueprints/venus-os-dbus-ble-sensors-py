# SmartSolar MPPT over BLE — what works, and what does not

Covers the SmartSolar Charger MPPT 75/15 (product `0xA053`) driven by
`ble_device_smartsolar.py`.  Written after taking one from "not on the
bus" to "publishing yield", including the parts that cost the most time.

## What the driver does

* Decodes encrypted Instant Readout advertisements (manufacturer
  `0x02E1`, mode byte `0x01`) into `/Dc/0/Voltage`, `/Dc/0/Current`,
  `/Pv/Power`, `/Yield/Power`, `/History/Daily/0/Yield`, `/Load/I`,
  `/State`, `/ErrorCode`.
* Recovers its own Instant Readout key over **one** paired HEX session
  (VREG `0xEC65`), then never opens a GATT link again.  Bounded to five
  attempts per process.

## The CBOR array dialect — read this before debugging silence

`victron_vreg.cbor_array` carries two encodings of the same request:

```
indefinite:  05009f19ec6619ec65ff      0x9F … 0xFF
definite:    05008219ec6619ec65        definite-length header
```

The IP22 and Orion answer the indefinite form.  **This MPPT answers only
the definite form**, and is completely silent to the other — no error, no
NAK, nothing.  A device asked in the wrong dialect will:

* pair successfully,
* return a good PUK CRC,
* accept the PIN,
* and then never push the register you asked for,

until the link times out.  The failure surfaces as `Service Discovery has
not been performed yet`, which is reads landing after teardown — a
symptom that hides the cause completely.  Both `_read_key` and
`_fetch_vreg` now try both dialects, indefinite first.

Writes carry the same split (`encode_write_command(..., definite=)`).  An
ignored write is worse than an ignored read: a dropped read returns
`None` and the caller knows, a dropped write looks like success.

## DVCC / BMS control is NOT implemented, deliberately

This driver publishes no `/Link/` paths, so systemcalc measures the
charger but never commands it.  That is honest rather than lazy —
declaring those paths without a working write would make DVCC report a
charge limit as enforced by something that is not enforcing it.

Measured on hardware, not assumed:

| register | MPPT 75/15 (not DVCC-controlled) | MPPT 100/50 (DVCC-controlled) |
|---|---|---|
| `0xED8D` output voltage | real, tracks live telemetry | real, tracks live telemetry |
| `0x2001` | stable `14.25 V` | `0xFFFF` (not available) |
| `0x2002` | tracks battery voltage | `0xFFFF` |
| `0x2008` | constant `0x0000` | register absent |

* `0x2001` on the 75/15 is that unit's **own** absorption setpoint.
  Writing a different value to it is acknowledged (`09 00 19 2001 01`,
  and `01` is not the `02` that means rejected) **and silently ignored** —
  the value does not change.
* It is not the register DVCC writes: a charger that *is* under DVCC
  control at 14.0 V reads `0xFFFF` there.

So there is no BLE-writable charge-voltage path known on this hardware.
Real BMS control needs a VE.Direct cable, which is how the units that do
obey DVCC are connected.

**If you revisit this:** a no-op write test (writing the value back over
itself) cannot tell an accepted write from a dropped one and will report
a false positive.  Write a *different* value and read it back — the bar
`ble_device_orion_tr.py` already set with `(write-probe: 28.50 V took)`.

## Other traps met along the way

* **`/FirmwareVersion` must be `int` or `None`, never `str`.**
  systemcalc's DVCC delegate evaluates it as an integer bitmask for
  `solarcharger` services; a string raises `TypeError` and takes
  systemcalc down in a restart loop, dropping DVCC for the whole system.
  `BleDevice` defaults it to `"1.0.0"`, so any new solarcharger-role
  driver must override it.
* **A device with settings is never silenced.**  These chargers
  interleave two manufacturer records and only one carries a product id;
  whichever arrives first used to decide whether the device existed.
* **Adapters:** a first-time GATT session needs a card that is
  GATT-eligible *and* not carrying the accept-list scan.  Add it to
  `ble-connect.conf` and leave it out of `adapter-allowlist.conf`.
