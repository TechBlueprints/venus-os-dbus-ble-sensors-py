# Micro-Air EasyStart — BLE protocol

Protocol description for the EasyStart soft starter's Bluetooth Low Energy
interface. Micro-Air publish no integration API, so this is a behavioural
description: what the device advertises, accepts, and emits.

Confidence is marked per section. **Confirmed** items are consistent across
every source examined. **Inferred** items are single-source or derived, and
should be checked against hardware before being relied on.

## Scope: this integration is read-only

**The driver never writes device state.** It sends the two read commands and
nothing else. It does not change settings, does not clear counters, does not
touch either configuration mask, and never enters firmware update mode.

This is a deliberate constraint, not an unimplemented feature. The EasyStart is
a protective device in series with a compressor: its fault mask arms the cutouts
that stop the compressor being damaged, and its startup mask governs how the
unit ramps. A monitoring integration has no business changing either, and a bug
that did so silently would be expensive in a way that a bad reading is not.

The mutating commands are documented below so that an implementer recognises
them and can be sure of not sending them. They are not a roadmap.

## Transport (confirmed)

Connection-oriented GATT. The device is not an advertisement broadcaster — no
useful data appears in advertisement payloads.

    Service        d973f2e0-b19e-11e2-9e96-0800200c9a66
      Notify char  d973f2e1-b19e-11e2-9e96-0800200c9a66
      Write char   d973f2e2-b19e-11e2-9e96-0800200c9a66

No pairing, bonding, or authentication. Subscribe to the notify characteristic
(standard CCCD, `ENABLE_NOTIFICATION_VALUE`) before issuing any command.

These UUIDs are not Micro-Air's. The `d973f2e0/e1/e2` family is the stock
example service shipped in STMicroelectronics' BLE firmware templates — the
same three UUIDs appear verbatim in ST's STM32WB middleware (`template_stm.c`)
and BlueNRG sample services, and therefore in any unrelated product built from
those templates without changing them. Two consequences: the service UUID must
never be used as a discovery filter or identity (an unrelated ST-template
device would match), and the absence of pairing is inherited template
behaviour, not a vendor decision that might be revisited per model.

## Discovery (confirmed)

Match on the advertised device name, which begins `EasyStart_`. Two lengths
occur: bare `EasyStart_` (10 characters) and `EasyStart_` plus a four-character
unit suffix (14 characters). The suffix is the per-unit identifier a user sees.

Match on **name, not address** — the advertised address rotates (observed
changing within hours). Any driver caching a MAC will lose the unit.

## Commands (confirmed)

ASCII strings written to the write characteristic. The framing looks like JSON
but is not — values are unquoted, and setting commands use `=` rather than `:`.
Send the bytes literally; do not round-trip through a JSON serialiser.

Writing to a characteristic is how you *ask* this device for data — both read
commands are characteristic writes. "Read-only" here means read-only with
respect to device state, not an absence of GATT writes.

### Used by this driver

| Command | Effect |
|---|---|
| `{"Cmd": ReadLive}` | Stream the live telemetry block |
| `{"Cmd": ReadEEP}` | Stream the configuration / history block |

Both return data and are not observed to change device state. That is an
observation about their behaviour, not a guarantee from the vendor — but it
is the same pair of commands the unit answers continuously in normal use.

### Mutating — never sent

| Command | Effect if sent |
|---|---|
| `{"Cmd": SMask=XX}` | Overwrites the startup mask |
| `{"Cmd": FMask=XX}` | Overwrites the fault-enable mask — **disarms compressor protections** |
| `{"Cmd": SCPT=XX}` | Overwrites the short-cycle protection delay |

`XX` is a value as two uppercase hex digits. These are recorded so they are
recognisable, and so that a malformed command string cannot be mistaken for a
harmless one. Nothing in this codebase should construct them.

## Response framing (confirmed)

This is the part a single-notification reader gets wrong.

A reply is **not** a single notification. The device emits a sequence of binary
notification chunks, which the client must concatenate in arrival order into a
reassembly buffer, followed by a terminating **ASCII text** notification that
marks the end of the transfer.

Distinguish the two by **shape, not printability**: a terminator is an ASCII
status line beginning `{"` (the full form observed is `{"Sts": Success}`); a
successful one contains the substring `Success`. Any other terminator, or a
timeout, means the transfer failed and the partial buffer must be discarded.

Printability alone is not a safe discriminator — **confirmed on hardware**:
the configuration block embeds fully-printable data chunks, and a reader that
classified "decodes as text" as a terminator died 88 bytes into every 1100-byte
configuration transfer.

Reset the reassembly buffer and its length to zero **before** writing the
command, not after receiving the reply.

A mutating command produces only a terminator, with no binary chunks. A read
that returns a terminator and no data has therefore failed — it has not
"succeeded with nothing to say".

The live block fits in one chunk at a default MTU, so single-notification
implementations appear to work; the configuration block does not, and will
silently truncate if chunks are not accumulated.

## Live block (confirmed)

20 bytes nominal, little-endian; offsets are into the reassembled buffer.
**Accept 18 bytes as the floor** — every identified field ends at offset 17,
and units in the field answer with frames a single-notification reader sees
as 18 bytes (the community implementations all validate `>= 18`; a reader
requiring 20 rejected every live frame on hardware).

| Offset | Type | Field | Scaling |
|---|---|---|---|
| 0–1 | — | unidentified | |
| 2 | u8 | System state | index into state table |
| 3 | u8 | Learned starts | count |
| 4–5 | u16 | Live current | ÷ 10 → A |
| 6–7 | u16 | Line period | 500000 ÷ raw → Hz |
| 8–9 | u16 | Last start peak current | ÷ 10 → A |
| 10–11 | u16 | SCPT delay remaining | seconds |
| 12–13 | u16 | Total faults | count |
| 14–17 | u32 | Total starts | count |
| 18–19 | — | unidentified | |

A state byte greater than 9 is undefined and should be surfaced as unknown
rather than clamped.

    0 Normal                  5 Stuck start relay fault
    1 Unexpected current      6 Open overload fault
    2 Short cycle delay       7 Overcurrent fault
    3 Power interruption      8 Bad wiring fault
    4 Stall fault             9 Wrong voltage fault

Real power is not reported. Deriving it as current × nominal voltage is an
approximation with no power factor and should be labelled as such if exposed.

Poll interval: 5 s is the interval the device is designed around. Slower is
safe; faster is untested.

## Configuration block (partly confirmed)

Reassembly buffer must be at least 1100 bytes. The block is largely opaque —
most of it is fault history and factory data with no established meaning. Four
offsets are identified:

| Offset | Type | Field |
|---|---|---|
| 10 | u8 | Firmware version |
| 906 | u8 | Startup mask (`SMask`) |
| 907 | u8 | Fault-enable mask (`FMask`) |
| 908 | u8 | SCPT delay setting |

Firmware version is a single byte, not a dotted string; values in the 26–29
range were reported current by the community sources, but a live unit in the
field reports 10, so treat the range as descriptive rather than a validity
check. Feature availability varies by version and by model — at
least one model identifier (`399BT`) behaves differently at the same firmware
version, so version alone is not a sufficient capability test.

Offsets 906–908 sit near the end of the block. A truncated read will appear to
succeed and yield garbage settings, so validate the reassembled length before
indexing.

### Startup mask bits (offset 906, `SMask`)

| Bit | Value | Meaning when set |
|---|---|---|
| 0 | 0x01 | ReLearn on next start |
| 1 | 0x02 | Use default ramp |
| 2 | 0x04 | No power-up delay |
| 3 | 0x08 | Treat the delay setting as a start delay rather than SCPT |
| 4 | 0x10 | SuperLearn (in place of ordinary ReLearn) |

Bits 5–7 unidentified. Decode only — this byte is reported, never written.

### Fault-enable mask bits (offset 907, `FMask`)

A set bit enables detection of that fault.

| Bit | Value | Fault |
|---|---|---|
| 0 | 0x01 | Unexpected current |
| 1 | 0x02 | Power interruption |
| 2 | 0x04 | Compressor stall |
| 3 | 0x08 | Start hardware failed |
| 4 | 0x10 | Open overload |
| 5 | 0x20 | Overcurrent |
| 6 | 0x40 | Wiring issue |

Bit 7 unidentified. Decode only.

**These are protective cutouts.** A clear bit means that protection is disarmed
on the unit right now, which is worth surfacing to the user as a diagnostic —
they may not know a previous owner or installer turned one off. Reporting it is
useful; changing it is not ours to do.

### SCPT delay (offset 908)

Valid range 1–250; the unit rejects values outside it. Read and report only.

**Units are minutes here, seconds in the live block (inferred).** This setting
is presented to users in minutes; the live block's remaining-delay countdown at
offsets 10-11 is in seconds. The two are consistent — a setting of 5 counts down
from 300 — but they are different units on differently-named fields, so convert
deliberately rather than assuming a shared unit. Worth confirming against a
running unit before the value is surfaced with a unit label.

## Firmware update mode (confirmed — do not implement)

The write characteristic also accepts a firmware-update mode in which raw binary
blocks, not ASCII commands, are written to the same characteristic.

This is the strongest argument for the read-only constraint. There is one write
characteristic, and it carries telemetry requests, settings changes, and
firmware blocks alike — the device distinguishes them by content. A driver that
only ever emits two fixed, constant ASCII strings cannot stumble into that mode.
One that builds command strings from variables can.

Emit the two read commands as literal constants. Any code path that writes
bytes to this characteristic from a computed value is a bug regardless of what
it was trying to do.

## Operational constraints (confirmed)

These dominate the design of any always-on integration:

- **The unit accepts a connection only while the A/C is running.** There is no
  idle telemetry. Absence of a link is the normal off-state, not an error, and
  must not be logged or retried as a fault. **It does advertise while idle**
  (confirmed on hardware) — the connect is refused instantly — so presence of
  the advertisement is not evidence the unit is reachable, and connect
  attempts against an idle unit need a backoff.
- **One connection at a time.** Holding a persistent link locks every other
  client out for as long as the compressor runs. This is a product decision, not
  a technical one — an integration should be able to release the link on
  request.
- **The advertised address rotates.** Identify by name; never cache an address
  as identity.
- **Range is roughly 1–2 m.** A rooftop A/C unit is likely out of reach of a
  cabinet-mounted controller without a proxy.
- A connection can succeed while the configuration read fails. Live telemetry
  remains available in that state, so treat the two planes as independent rather
  than failing the whole device.

## Open questions

- Meaning of live-block bytes 0–1 and 18–19.
- Whether the Breeze variant shares this protocol. The Flex and 364 do.
- Whether the configuration block's fault history can be decoded into structured
  per-fault records.
