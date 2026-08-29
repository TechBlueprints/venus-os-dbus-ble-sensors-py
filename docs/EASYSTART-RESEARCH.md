# Micro-Air EasyStart — public community implementations

A survey of the publicly available EasyStart BLE integrations: what they do,
what they agree on, and where they stop. Micro-Air state publicly that they do
not offer or support an integration API, so these are the only third-party
references available.

**This is a survey, not the specification.** For the protocol this project
implements against, see [EASYSTART-PROTOCOL.md](EASYSTART-PROTOCOL.md), which is
broader than anything below and is the authority where the two differ.

## Sources

Three independent community implementations, all ESPHome/Home Assistant, all
converging on the same UUIDs and the same command bytes:

- <https://github.com/Keen-coffee/home_assistant> (`easyStart`) — earliest; the
  other two credit it for the payload decode.
- <https://github.com/DerekSeaman/ESPHome-Micro-Air-EasyStart> — most complete;
  adds a status-code table and range validation. Write-up:
  <https://www.derekseaman.com/2026/03/esphome-micro-air-easystart-integration.html>
- <https://github.com/Alternating/HA-micro-air-softstart>
- <https://github.com/ptr727/ESPHome-Config> — same approach as a BT-proxy template.

Vendor Bluetooth manual (end-user only, no protocol):
<https://www.micro-air.com/support-documents/installation_resources/EasyStart_Bluetooth_Manual.pdf>

## GATT

    Service          d973f2e0-b19e-11e2-9e96-0800200c9a66
      Notify char    d973f2e1-b19e-11e2-9e96-0800200c9a66   telemetry in
      Write char     d973f2e2-b19e-11e2-9e96-0800200c9a66   commands out

Connection-oriented — not an advertisement broadcaster. No pairing, bonding, or
authentication reported by any of the three implementations.

## Command

The only command anyone has published is a 17-byte ASCII JSON-ish string written
to the write characteristic:

    {"Cmd": ReadLive}

    7B 22 43 6D 64 22 3A 20 52 65 61 64 4C 69 76 65 7D

Note the unquoted value — it is not valid JSON. All three clients poll this on a
10 s interval and read the answer from the notify characteristic. A write that
the unit accepts but has no data for answers `{"Sts": Success}` in ASCII;
telemetry answers are binary.

The `Cmd` framing implies other verbs exist — the unit's settings are adjustable
in normal use, so something must carry those changes. No public implementation
uses any command but this one. The full command set is in
[EASYSTART-PROTOCOL.md](EASYSTART-PROTOCOL.md).

## Telemetry frame (notify, >= 18 bytes, little-endian)

| Offset | Type | Field | Scaling |
|---|---|---|---|
| 2 | u8  | Status code | index into table below |
| 4..5 | u16 | Live current | / 10 → A |
| 6..7 | u16 | Line period | 500000 / raw → Hz |
| 8..9 | u16 | Last start peak current | / 10 → A |
| 10..11 | u16 | Short-cycle protection delay | seconds |
| 12..13 | u16 | Total faults | count |
| 14..17 | u32 | Total starts | count |

Bytes 0 and 1 are unidentified; byte 3 carries a field no public
implementation reads. Frames shorter than 18 bytes are discarded.
Derek Seaman's version additionally rejects a live current outside 0–50 A as a
bad decode, which suggests the frame is not always well-formed.

Power is not reported — the ESPHome configs synthesise it as `current * 240 V`.

Status codes (index of byte 2):

    0 Normal              5 Stuck SR fault
    1 Unexpected current  6 Open overload fault
    2 Short cycle delay   7 Overcurrent fault
    3 Power interruption  8 Bad wiring fault
    4 Stall fault         9 Wrong voltage fault

## Operational constraints

These matter more than the protocol for anything running on a Cerbo:

- **The unit only accepts a BLE connection while the A/C is running.** No
  compressor, no link. There is no idle telemetry to collect.
- **Single connection only.** While we are connected no other client can, and
  vice versa.
- **The advertised MAC rotates.** Derek Seaman observed it change after a few
  hours. Discovery is by advertised name, not by address — a configured MAC
  goes stale. As with adapters elsewhere in this project, the address is not the
  identity.
- **Very short range**, ~3–6 ft / 1–2 m reported. The Cerbo would need to be
  close to the A/C unit, or need a proxy.

## Fit against this codebase

This is a connect-and-poll device, so it belongs on the BLE connection layer
(`docs/ble-connection-layer.md`), not the advertisement router. Two things do not
fit the existing drivers cleanly and need decisions before any implementation:

1. Rotating MAC + name-based discovery. Existing drivers key off a stable
   address; this one cannot.
2. Connection only available while the compressor runs, and exclusive. A
   persistent `auto_connect` holds the link against every other client for as
   long as the A/C is on.

## Unanswered

- Meaning of frame bytes 0 and 1. Byte 3 is identified in the protocol spec;
  no public implementation reads it.
- Whether the Breeze / Flex / 364 variants share this protocol. The community
  work is on Flex and 364; nothing confirms Breeze.
