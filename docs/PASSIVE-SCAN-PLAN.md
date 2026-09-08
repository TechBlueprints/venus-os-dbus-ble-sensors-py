# Passive scan: scan everything, filter early — a plan to remove the device limit

**Status:** proposal, 2026-09-06. Nothing here is implemented. Measurements
in this document were taken on prod and dev-cerbo the same day.

## 1. Where we are, and how we got here (from the commit history)

**Act 1 — April/May: the leak was BlueZ, not scanning.** `3997030` and
`d13891a` replaced BlueZ-driven scanning with a raw HCI scan plus an
`HCI_CHANNEL_MONITOR` tap. The problem they fixed was memory: BlueZ created a
`Device1` object per advertiser and emitted `PropertiesChanged` per advert,
and dbus-daemon's allocator never gave it back (~95 MB/hr, OOM every few
hours). With BlueZ out of the loop, accept-all at ~126 adverts/s was fine.

**Act 2 — May 17: the hardware list arrived as a convenience.** `ae1c051`
added the controller's LE Filter Accept List for `ContinuousScan OFF`, to
"narrow what the controller delivers" in production mode. The card reported
32 slots; we used ~12. Load was not the stated reason; headroom was noted.

**Act 3 — August 23: the limit bit.** `3e4667f`: the fleet passed 32, and
because every card was handed the same MAC-sorted list, the four highest
addresses fell off the end and went silent. Fix: a contiguous slice per card
(25 + 32 = 57). The limit became a real constraint from this commit on.

**Act 4 — August 28: the load you remember.** `ea4f6ba` put both radios on
accept-all for the (believed) rotating-MAC EasyStart. `f7a502f` measured the
cost: **~23 % of a core parsing "the neighbour firehose" on both adapters,
15-minute load ~4.0 against the 5.5 throttle trip**, and traded it back to
accept-list plus learned addresses (23 % → 12 %, load 4.4 → 2.8). `694f182`
then found the units hold fixed public addresses and removed the windows.

**Today.** Prod has 46+ known devices against slices of 25 and 32. hci1's 25
is full: the EasyStart's extra address fails to add every 60 s (a warning a
minute). `ContinuousScan OFF` couples two things — the adoption gate and the
radio filter — and `ea4f6ba` already had to half-decouple them.

## 2. What the load actually was

The 23 % was not the radio, the USB path, the kernel, or BlueZ. It was **our
own full parse of every neighbour frame** — measured today at **~67 µs per
report on the Cerbo** — applied to frames we then discarded. Two more facts
from today:

- A header + adapter + address check, before any AD parsing, costs
  **1/6.5 of the full parse** in a micro-benchmark, i.e. **~10 µs on the
  Cerbo**. Manufacturer-ID matching adds a bounded TLV scan, ~5 µs.
- Foreign discovery bursts already hit us the same way: a card we do not
  scan (hci5) delivered 204 adverts/s during another consumer's discovery
  window and the tap parsed all of them before its own filter dropped them
  (~10 % of a core for the burst).
- Reports batch: 34 datagrams/s carried 203 reports/s on dev. The syscall
  cost is per datagram; the parse cost is per report. An early filter that
  iterates reports inside a datagram pays the syscall once.
- Dev today (a sparse site, 12 devices in range): accept-all delivers 203
  adverts/s and the process sits at 2–5 %. **That is not the dense case**;
  the plan is sized for the August firehose, not for dev.

## 3. The key result: the kernel will filter for us

`SO_ATTACH_FILTER` (classic BPF) **works on `HCI_CHANNEL_MONITOR`**. Proven
on dev with controls, on our own socket, no service impact:

| filter                        | frames / 4 s |
|-------------------------------|-------------:|
| none                          | 923          |
| pass-all (`ret 0xFFFF`)       | 897          |
| drop-all (`ret 0`)            | 3            |
| detached                      | 915          |
| `adapter == 2` (our card)     | 814          |
| `adapter == 0` (idle card)    | 32 (its own housekeeping) |

A BPF program is a **software accept list inside the kernel**: match the
adapter index (byte at offset 2 of the monitor header) and the advertiser
address (6 bytes at offset 13 of a legacy report, two compares). Frames that
do not match **never reach userspace**. Classic BPF allows 4,096
instructions, ~4 per address → **on the order of 1,000 addresses**, no
hardware table involved. (Gotcha found while proving it: `ldh` is big-endian
and the header is little-endian; use byte loads or swap the constant.)

## 4. The plan

### Phase 1 — early software filter in the tap (no radio change)
Per report, before any AD parsing: drop if the adapter is not one we scan;
**fast-accept** if the address is configured, a learned name-device address,
or a router-registered address; otherwise a bounded TLV scan and accept only
if the manufacturer ID is ours or router-registered, or the local name
carries one of our prefixes; **else drop**. Full parse only for accepts.
- Expected: firehose cost 67 → ~10–15 µs per report (~5×). The foreign-burst
  cost measured today disappears.
- Inputs already exist: `mfg_filter`, `name_prefixes`, the configured and
  name-device address sets, and `ble_advertisement_router.manufacturer_ids()`
  (external services register by manufacturer, product, range, or address —
  prod has none registered this life).
- Extended advertising reports (subevent 0x0D) have a different layout: pass
  them to the full parser initially.
- Gate: dev with `ContinuousScan ON`, tap CPU before/after; prod unchanged
  on the radio, lower tap CPU during the next foreign burst.
- **Pure win regardless of what follows.**

### Phase 2 — retire the hardware accept list (radio accept-all, always)
`ContinuousScan` becomes **only** the adoption gate (finishing `ea4f6ba`'s
decoupling). The radio runs accept-all on scanning cards always; Phase 1's
filter (or Phase 3's) does the whitelisting.
- Delete: slicing, name-device injection, `read_accept_list_size`, the 60 s
  list re-program, the shortfall log, the 0x07 handling. Keep the 60 s
  **enable** backstop (other consumers still reset scan parameters).
- Net: **no device limit**; no "fell off the end" class; no 0x07 warning;
  discovery always possible; rotating-MAC devices would simply work.
- Gate: a bounded prod measurement at **this** site — `ContinuousScan ON`
  for 5 minutes with Phase 1 in place, the load throttle as the safety net.
  Go if process CPU stays under ~10 % and the 15-minute load does not climb.
- Risk: the kernel/USB floor of accept-all (every advert is a USB interrupt
  and an HCI event before any filter). Dev softirq 2–3 % at 203/s. Unmeasured
  at the dense site; that is what the gate measures.

### Phase 3 — kernel BPF, revised: a small static add-on, not the big win
**Correction (2026-09-07, on reflection).** The first draft proposed
generating a BPF program that whitelists advertiser addresses in the kernel.
That does not survive batching: one datagram can carry many advertising
reports (dev: 34 datagrams/s carried 203 reports/s, ~6 per datagram), and a
loop-free BPF program can only inspect the FIRST report's address at a fixed
offset — later reports sit at offsets that depend on the lengths before
them. Dropping a datagram on its first report would discard our own device's
report when it is the second or sixth in the batch. Unrolling does not fit
the instruction budget for 46+ addresses, and batching is highest exactly at
the dense sites the filter was meant for. **The address whitelist stays in
userspace (Phase 1), where it is applied per report inside each datagram.**

What does survive is everything that is a property of the whole datagram:
- the **adapter index** (byte at offset 2 of the monitor header) — drop
  frames from cards we do not scan; this fully removes the foreign
  discovery-burst cost measured on prod (hci5 at 204/s);
- the **frame type** (HCI event 0x3E at offset 6, sub-event at offset 8) —
  drop commands, command-completes and ACL data, which removes the wakeups
  our tap currently takes for GATT-session chatter and discards.

That is a **static ~6-instruction program**: attached once at tap start,
regenerated only when the set of scanning cards changes, never on adoption.
It needs the same tiny assembler (`ldb`, `jeq`, `ret`) and is already
demonstrated by the selective-adapter test above. Benefit: modest and
certain. It does not touch the kernel's own cost of receiving every advert
from the controller; that floor is Phase 2's gate, not Phase 3's.

## 5. What does not change
GATT and claims (bcmv2), the PV poll, the adoption-gate semantics (`OFF` =
adopt nothing new), the 60 s scan-enable backstop, the tap's dedup and the
main-loop hop (PR #8).

## 6. Effort and recommendation
Phase 1: half a day plus measurement. Phase 2: half a day plus a prod soak.
Phase 3: a few hours for the static program and tests.

**Recommendation:** do Phase 1 now (it pays for itself even if we keep the
list). Gate Phase 2 on the dense-site measurement. Add Phase 3's static adapter/type filter alongside Phase 1; it is cheap and
certain, but it is not a substitute for the userspace whitelist.

## Appendix — the "20 device limit"
The limits are hardware-reported per card: **25 and 32** on prod's two
scanning cards (`LE_Read_Filter_Accept_List_Size`), sliced to 57 in total;
prod is at 46+ with the 25-slot card full. Dev runs `ContinuousScan ON`
(accept-all, active scan) today and is fine at its sparse site.
