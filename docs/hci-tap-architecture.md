# The advertisement pipeline: radio, kernel, userspace, distribution

**Status:** describes what runs on prod as of main (2026-09-13).
Numbers are measurements from prod on 2026-09-11/12 unless stated.

The service hears Bluetooth LE advertisements without BlueZ. It programs
the scanning cards itself over raw HCI sockets, reads every HCI packet
back through the kernel's monitor channel, filters in the kernel with a
classic-BPF program, parses on one thread, and hands survivors to the
GLib main loop for device dispatch and D-Bus distribution. This document
walks that path stage by stage. For the history that led here see
[PASSIVE-SCAN-PLAN.md](PASSIVE-SCAN-PLAN.md); for the outbound (GATT)
half see [ble-connection-layer.md](ble-connection-layer.md); for the
consumer-facing router see [advertisement-router.md](advertisement-router.md).

## 1. Radio

### Programming a card

`hci_scan_control.enable_scan` opens a raw HCI socket on the card and
sends three commands: `LE Set Scan Enable(0)`, `LE Set Scan Parameters`,
`LE Set Scan Enable(1)`. A `Command Disallowed` (0x0C) on the disable
step means the card was already off and is treated as success.

The parameters are:

| parameter | value | meaning |
|---|---|---|
| scan type | passive (0x00) | listen only; never send scan requests, so a device is never asked for its scan response |
| filter policy | accept-all (0x00) | the controller reports every advertiser it hears |
| interval | 0x0060 = 60 ms | how often the scanner starts a listening window and moves to the next advertising channel |
| window | 0x0060 = 60 ms | how long each window lasts |

Interval equal to window means the card listens 100% of the time while
it is on; the interval then only sets how fast the scanner cycles through
the three advertising channels (37, 38, 39). Every hop costs the
controller a retune during which it is deaf: Nordic documents 760 us per
window on its SoftDevice, and a measurement paper on real chipsets found
a fixed ~1.1 ms gap per interval. At the 10 ms / 10 ms this service ran
from May 2026 to September 2026 (the default hcitool, ESP-IDF and
Silicon Labs all ship) that is about 10% of the time; at 60 ms it is
about 2%, and the curve is flat past ~100 ms. 60 / 60 is the kernel's
own profile for a card that scans while it may also carry connections;
Nordic's guidance is the same: equal values, kept short, on a card that
holds links, because its scheduler skips a whole scan window that
collides with a connection event. hci1 carries our GATT links, so the
one value used for every card is the one that is right for hci1. Longer
windows (Android's low-latency mode uses 5 s, Nordic suggests ~10 s for
a scan-only device) are deliberately not used: one value for every card.

An advertiser sends each advertising event on all three channels within
a few milliseconds, so a continuously listening scanner catches it on
whichever channel it is sitting on. The interval does not change the
number of reports, which is set by what is on the air; the lever that
would is the duty cycle (window shorter than interval), which is not
used. The kernel's own background profile is 60 ms / 30 ms, a 50% duty
cycle chosen to share the radio and save power.

There is **no hardware accept list**. Phase 2 (PR #28) retired it; the
radio delivers every advertiser and filtering happens downstream.

### Passive, unless a device needs active

Passive scanning never transmits the scan request that an active scanner
sends to every advertiser in range; on a gateway sharing a few radios
with BMS links, that is the difference between coexisting and
interfering. `/Settings/BleSensors/ActiveScan` switches the cards to
active scanning. Turn it on only when a device needs it: some Victron
firmwares moved the encrypted instant-readout record out of the primary
advertisement and into the scan response, which a passive scanner never
solicits, so the unit shows only its short product-id beacon and reads
as off. Active delivers everything passive does plus the scan responses,
parsed through the same advertising-report path, so nothing downstream
changes; the box pays for the responses as extra reports. The toggle is
re-applied on the next scan enable rather than waiting for the 60 s
re-enable tick.

### An adapter is its MAC; `hciN` is only what it is called right now

The numbering is not stable: a USB reset renumbers a card, so does
replugging, and so does a reboot. On dev-cerbo the onboard Broadcom once
failed its firmware reset at boot and the USB dongle that had been
`hci1` came up as `hci0`; everything keyed on "hci1" then referred to a
different radio. So state is keyed by the adapter's MAC (colons
stripped, uppercase), and the `hciN` is resolved from it immediately
before each HCI socket call and never cached across one.
`adapter_identity.py` does the resolving, borrowing bcmv2's resolver:
Venus OS populates no sysfs `address` attribute for any adapter, so the
whole table comes from a single `hciconfig` call behind a short-lived
cache. `adapter-allowlist.conf` therefore takes adapter MACs in any
spelling; an `hciN` entry still works but names a number, not a card,
and protects the wrong radio the moment the numbering changes.

### Rotation: one card listens at a time

Two cards are in the scan set on prod: hci0 (68:4E:05:44:77:B0, a
Realtek WLAN+BT combo on the internal USB) and hci1 (00:01:95:CC:32:F7,
a CSR dongle on the external hub). hci9 is deliberately kept out of the
scan set for GATT work and the SmartSolar poll.

`_apply_rotation` in `dbus_ble_sensors.py` makes exactly one card scan
and disables the rest; a 60 s timer (`_SCAN_ROTATION_INTERVAL_S`) moves
the role to the next card in sorted order. Idle cards keep their
`hciN.scan` claim in `/run/bt-claims` so the connection manager does not
hand them to another consumer. Adapter add/remove, the throttle's
release path, the 60 s re-enable tick and the prune tick's eager path
all defer to the rotation; none of them enables every card any more.
The first full cycle logs at INFO, later swaps at DEBUG.

Why: accept-all costs the kernel work per advertisement the radio
delivers (section 2), and with both cards open that was ~38k context
switches and ~12 idle points per 30 s, enough to trip the load throttle
three times in the first eight hours of Phase 2. One card at a time
halves it. Every configured device advertising in a 60 s census was
heard on both cards, so rotation loses no coverage today; hci1 alone
delivers about 260 reports/s, hci0 alone about 130.

## 2. Kernel

### What every advertisement costs before any filter of ours

The controller sends each report up USB as an HCI event. The USB
interrupt fires (ehci for hci1, musb for hci0), the kernel's Bluetooth
receive worker (`kworker/u9:*-hciN`) wakes and processes the event, and
the event is copied to the monitor channel. Measured across a throttle
suspension with both radios off, that is roughly four context switches
per delivered advertisement. No filter in this service can avoid it;
only delivering fewer advertisements (the radio, or fewer cards) can.

### The monitor channel

`hci_advertisement_tap.create_tap_socket` opens a raw Bluetooth socket
bound to `HCI_CHANNEL_MONITOR` with `HCI_DEV_NONE`. The kernel copies
every HCI packet on **every** adapter to such a socket: commands,
events, ACL data, from all ten cards on prod. Each copy is a 6-byte
header (`opcode`, `adapter index`, `length`, little-endian) followed by
the raw HCI packet. For an LE advertising report the frame is:

```
hdr(6) | evt 0x3E | plen | subevent | num_reports | report ...
legacy   (subevent 0x02): event_type(1) addr_type(1) addr(6) data_len(1) data rssi(1)
extended (subevent 0x0D): event_type(2) addr_type(1) addr(6) phys(2) sid(1) tx(1)
                          rssi(1) periodic(2) direct(7) data_len(1) data
```

Prod cards deliver one legacy report per datagram (census: 5596 of 5596).

### The BPF program, run at enqueue

Before a copy is queued to our socket the kernel runs the classic-BPF
program attached with `SO_ATTACH_FILTER`. `build_adapter_filter` in
`hci_advertisement_tap.py` assembles it with a small label assembler:

1. opcode must be event-rx; event must be LE Meta; adapter index must be
   one of ours (hci0, hci1). Everything else, including the other eight
   cards' traffic, is dropped here.
2. A datagram with more than one report, or an LE Meta subevent other
   than the two advertising-report kinds, is **accepted** unexamined;
   userspace judges it.
3. For a single-report frame, one chain per subevent (the offsets differ):
   * address blocks: accept if report[0]'s address equals a
     router-registered address (word + halfword compare, bytes as they
     lie);
   * the advertising-data walk: with indexed loads and the X register,
     up to eight unrolled steps read each structure's length and type;
     type 0xFF compares the company id (as the two bytes lie) against
     the allowed set; type 0x09/0x08 compares the name against each
     allowed prefix in word/halfword/byte pieces, and only when the
     structure is long enough to hold the prefix; otherwise advance by
     length + 1. The walk stops at end of data or a zero length.
   * anything unmatched after the last step is dropped.

A load past the end of the datagram returns drop (the kernel's rule).
Every conditional jump is local (per-step accept/drop targets), so the
program grows without hitting the 8-bit jump fields; the real program is
about 760 instructions of the 4096 the kernel allows. `attach_adapter_filter`
degrades in stages if the kernel refuses a program: address blocks
first, then the whole gate, then adapter-only.

The allowed company ids come from the loaded device classes
(`BleDevice.DEVICE_CLASSES`: Victron 0x02E1, Ruuvi 0x0499, SeeLevel
0x0131 and 0x0CC0, Gobius 0x0F53, Safiery 0x0067, Teltonika 0x089A,
and Nordic 0x0059 / Texas Instruments 0x000D for the Mopeka classes)
plus the router's manufacturer registrations. The name prefixes are the
name-routed classes (`EasyStart_`) plus router name-prefix registrations
(the power watchdog's `PM` and `WD_`). `_attach_kernel_filter` rebuilds
and re-attaches whenever ids, prefixes or registrations change, and at
tap start before the thread runs so nothing unfiltered is ever queued.

Configured and learned device addresses are deliberately **not** in the
kernel program: the program is static across adoption, and the ~50/s of
strangers that share our makers' ids (four neighbouring Victron devices
in the census) cost userspace about 1% of a core to drop.

Measured with both cards on: 112 of 389 frames/s reach userspace, with
zero false drops in 2,250 wanted frames and zero strangers passed.

A note for anyone validating a freshly attached program: `sk_filter`
runs at enqueue, so frames already queued under the previous program
still arrive and look like leaks. Drain for ~300 ms first.

## 3. Userspace

### The tap thread

`run_tap_loop` blocks in `select`, receives one datagram, and calls
`parse_monitor_frame`. Each wakeup costs about 200 us on the Cerbo's
CPU before Python sees a byte, which is why filtering in the kernel
matters more than any early return in the parser. The parser:

1. re-checks opcode, event and adapter index (the userspace fallback if
   the kernel program is absent);
2. formats the address and applies two address gates: `ignored_macs`
   (addresses proven useless for the life of the process, no TTL) and
   `known_macs`, the pre-walk gate. When adoption is closed the known
   set holds configured, learned name-device and router-registered
   addresses and any other address is dropped before the walk; when
   ContinuousScan is on, or an external consumer registered by
   manufacturer id, or the setting cannot be read, the set is empty and
   everything is walked;
3. walks the advertising data into a manufacturer-data dict (only the
   allowed ids) and a name (only the allowed prefixes);
4. yields a `TappedAdvertisement(adapter_index, mac, address_type,
   rssi, manufacturer_data, local_name)`.

With the rotation and the kernel gate the thread sits near 2% of a core.

Two threads cooperate. The tap thread (`hci-monitor-tap`) runs the loop
and the callback below; the GLib main thread runs the main loop and all
D-Bus work. The three sets the parser gates on (`_tap_ignored_macs`,
`_tap_known_macs`, `_name_prefixes`) and the manufacturer-id set are
shared objects mutated in place by the main thread and read by the tap
thread; CPython's GIL makes the `in`, `add` and `discard` calls safe.
The 30 s `_prune_tick` keeps the tap's ignore set in step with the
main-loop state: an address whose ignore entry expired, or that was
adopted, is discarded from `_tap_ignored_macs` so the device can be
re-evaluated.

### Deduplication, still on the tap thread

`_on_advertisement` in `_start_tap`:

* a frame with neither manufacturer data nor a name is dropped;
* a name advertisement is a presence signal with no payload to compare,
  so it is rate-limited per address (`NAME_ADV_MIN_INTERVAL`) and then
  crossed to the main loop;
* a fully disabled device has its presence recorded and goes no further
  (the main loop would discard it anyway, so the hop is not paid);
* a manufacturer payload byte-identical to the device's previous one is
  dropped until the rounding policy's heartbeat elapses, so a beacon
  repeating the same reading many times a second costs the main loop
  once;
* survivors are handed to the GLib main loop with `GLib.idle_add`.

### The main thread

`_glib_process_tap` calls `_process_advertisement`; `_glib_process_name_tap`
calls `_process_name_advertisement`.

For manufacturer-data advertisements: an address in the TTL'd ignore
dict is dropped; the router is offered the advertisement first (section
4); then the company id selects a device class. A known device gets its
instance's handler. An unknown address is adopted only when
ContinuousScan is on; otherwise it is refused, and if nothing could
parse it and it is not a device we hold settings for, its address is
silenced into `_tap_ignored_macs`. ContinuousScan therefore means
exactly "adopt something new"; it no longer steers the radio.

For name advertisements: EasyStart units rotate their advertised
address, so the device store is keyed by an identity derived from the
name; the address heard right now is handed to the driver purely as the
address for its next connection, and the learned address is what puts
the device into the known set.

## 4. Distribution

**Internal device classes** publish through their role services
(`DbusRoleService`: temperature, tank, battery, acload, dcdc, ...), one
`com.victronenergy.<role>.<id>` service each, read by the GUI and by
systemcalc. Presence tracking, alarms and DVCC bridging live in the
device and role layers, not in this pipeline.

**External consumers** register interest by writing paths under
`/ble_advertisements/<service>/` (by manufacturer id, product, product
range, address or name prefix; see advertisement-router.md). The router
matches each advertisement against the registrations and emits an
`Advertisement` D-Bus signal (signature `sqaynss`: address, manufacturer
id, data bytes, RSSI, adapter, name) on the registration path. A
manufacturer-id registration opens the userspace known-address gate,
because "every device of that maker" includes strangers; a name-prefix
or address registration is folded into the kernel program instead.

**GATT sessions** are a separate path. When a driver needs a connection
(EasyStart, Orion-TR, the SmartSolar poll) it goes through the
connection manager, which ranks cards by their claims, and the link runs
on BlueZ. Scanning and an established LE connection are independent
controller states: a rotation swap disables a card's scan and drops no
link. On hci1 the scan shares radio time with the links it carries, as
it has since May.

## 5. Where the cost sits today

| stage | who pays | measured |
|---|---|---|
| radio delivering reports | the kernel, per report, before any filter | ~4 context switches per report; both cards open ~38k/30 s, rotation about half |
| monitor-channel copy + BPF | the kernel, per report, cheap | 389/s in, 112/s out with both cards on |
| tap wakeup + parse | our tap thread | ~200 us per datagram; ~2% of a core under rotation |
| dedup + main-loop hop | tap thread, then main loop | main thread ~2% |
| load throttle | samples `/proc/loadavg` every 30 s | trips at 5-min >= 6.0 or 15-min >= 5.5, derived from the watchdog's limit; release below 5.0 on both; a sub-30 s crossing can be missed |

The rotation's acceptance run (2026-09-12 14:38Z to 2026-09-13 05:16Z):
zero trip-class load dumps and zero throttle trips, box context switches
~100k per 30 s against 131-136k with both cards open, idle 52-65%.

## 6. Key constants

| constant | value | purpose |
|---|---|---|
| `_DEFAULT_SCAN_INTERVAL` / `_DEFAULT_SCAN_WINDOW` (`hci_scan_control.py`) | 0x0060 / 0x0060 (60 ms) | continuous listening while a card is on; 60 ms hops cost ~2% retune time vs ~10% at 10 ms |
| `_SCAN_ROTATION_INTERVAL_S` | 60 | the listening role moves to the next card |
| `_SCAN_REENABLE_INTERVAL_S` | 60 | re-issue the enable on the listening card (recovery from a foreign scan reset) |
| `NAME_ADV_MIN_INTERVAL` | 5 s | per-address rate limit for name-only advertisements crossing to the main loop |
| `/Settings/SensorRounding/HeartbeatSeconds` | setting | how long a byte-identical manufacturer payload is suppressed before it is re-forwarded as a keepalive |
| `IGNORED_DEVICES_TIMEOUT` (`conf.py`) | 600 s | TTL of the main loop's ignore dict |
| `DEVICE_SERVICES_TIMEOUT` (`conf.py`) | 3600 s | TTL of the known-device dict |
| `SILENCE_WARNING_SECONDS` | 300 s | warn, and force a scan re-enable, when no matching advertisement arrives |
| `ADV_LOG_QUIET_PERIOD` | 1800 s | per-device log throttle |
| `_AD_WALK_STEPS` (`hci_advertisement_tap.py`) | 8 | advertising-data structures the kernel program examines per report |
| `_MAX_KERNEL_IDS` / `_MAX_KERNEL_PREFIXES` / `_MAX_KERNEL_MACS` | 32 / 8 / 200 | size caps that keep the program under the kernel's 4096-instruction limit |
