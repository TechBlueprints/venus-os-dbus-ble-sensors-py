# Why prod's load average is high with half the CPU idle

Measured on the Airstream Cerbo GX ("prod", hostname einstein, Allwinner A20, two cores, kernel
6.12.90-venus-4, `HZ=100`) on 2026-11-26 between 15:17Z and 16:15Z. Every instrument used is in
[`scripts/`](../scripts) and runs over ssh with nothing written to the box. The companion
document for the resident recorder is [LOAD-FORENSICS.md](LOAD-FORENSICS.md).

## The short version

The load average counts runnable tasks, not CPU. On this box the runnable count is near 1 most
of the time and jumps to 9-11 for about 20 ms whenever any service emits a D-Bus
`com.victronenergy.BusItem` signal, because every such signal, whoever sends it, is delivered to
the same nine processes. The bus carries 11 to 12 of those signals a second. The 15-minute
average is roughly the time-mean of that sawtooth; the 1- and 5-minute averages additionally
depend on where in the second the kernel happens to take its sample, and that instant drifts.

| claim | status |
|---|---|
| Every `BusItem` signal is delivered to the same 9 processes (13 threads) | proven: read from the bus daemon's match rules |
| Each signal is followed by a burst of 9-11 runnable tasks | proven: signal-triggered average, including senders at random phases; natural on/off at a fixed phase |
| The kernel's own samples are high exactly when a signal precedes them | proven: [see below](#what-the-kernels-own-samples-show) |
| The sharp bursts in the phase fold are the five VE.Direct services | proven for "who"; what fixes their phase is inferred (process start), not tested by a restart |
| GLib `timeout_add_seconds` puts every seconds-timer on the whole monotonic second | proven as a mechanism on dev; a minor contributor on prod (two services) |
| systemcalc's 1 Hz publish drifts through the second at the kernel sampler's rate | measured: period 1.0015 s over 5 min and 1.0020 s over 12 min, against 5.0100 s / 5 |
| That co-movement is what produces the minutes-long excursions that trip the throttle | hypothesis: fits the trip dumps, never caught in a capture |
| The 2026-10-17 step is the rig coming back into use | timing and mechanism fit; no before/after signal census exists, so not proven |
| A kernel worker (`kworker/0:0-events`, ~300 wakeups/s) is the largest single item outside the fan-out | measured: in state R in 24 % of samples, somewhere between 0.1 and 0.25 of a load point; what feeds it is not identified (this kernel has no ftrace) |

## How the load average is built here

The kernel adds up `nr_running` over both run queues every `LOAD_FREQ = 5*HZ+1` ticks, 5.01 s,
and feeds that one number into three exponential averages. Measured period: 5.0100 s over 60
consecutive updates. Because the period is 5 s plus one tick, the sample instant moves 10 ms
later within the second on every sample, 2.0 ms per second, and sweeps the whole second every
501 s.

Field 4 of `/proc/loadavg` is the same `nr_running` sum. Read at burst peaks and compared with
thread states (`scripts/loadavg_runnable_vs_state.py`, 15:40:05-15:40:25Z, 236 triggers): where
the kernel count stayed at 6 or more across the read, it was 10.7 before and 9.3 after, with 8.0
threads in state R in between. So the count is real runnable threads; 6.12's delayed dequeue
(sleeping tasks left on the run queue) is not a large part of it.

## One signal, nine processes

Read once from the daemon (`org.freedesktop.DBus.Debug.Stats.GetAllMatchRules`, 116 connections,
2273 rules) and matched against each publisher's unique name:

| recipient | owner | how it subscribes |
|---|---|---|
| `dbus_systemcalc.py` | Victron | velib_python `DbusMonitor`: `ItemsChanged` and `PropertiesChanged`, no `sender=` |
| `vrmlogger.py` | Victron | same |
| `flashmq` | Victron | `type='signal',interface='com.victronenergy.BusItem'` |
| `venus-gui-v2` | Victron | one `sender=` rule per service, for every service (124 rules) |
| `venus-platform` | Victron | same (124) |
| `hub4control` | Victron | same (122) |
| `dbus-fronius` | Victron | same (122) |
| `dbus-aggregate-batteries.py` | Clint | vendored velib_python `DbusMonitor`, no `sender=` |
| `dbus-virtual-dcsystem.py` | Clint | same |

The recipient set is the same for a SmartShunt on VE.Direct, the MultiPlus, systemcalc itself, a
Ruuvi tag published by this service and the Power Watchdog: 9 processes (10 or 11 for a few
senders with one extra specific subscriber). Each Qt process wakes two threads, so one signal
makes 13 threads plus `dbus-daemon` and the sender runnable on two cores. `dbus-ble-sensors-py`
is not a recipient: its 271 match rules all name a sender.

velib_python's `dbusmonitor.py` says it in a comment, "Subscribe to ItemsChanged for all
services" (lines 141-150 of the copy on the box): the receiver is added once with no `bus_name`,
and the handler discards signals from services it does not track after Python has already been
woken to unmarshal them.

## What a signal does to the runnable count

`scripts/dbus_publisher_phase.py` is one more subscriber for a few seconds; with a second
argument it also reads the runnable count every 2 ms. Run 15:28:13-15:28:38Z, 8737 runnable
samples, 11.7 signals/s. Mean runnable count by time relative to each sender's signals:

| sender | n | phase lock R | −10 ms | 0 | +5 | +10 | +20 | +40 |
|---|---|---|---|---|---|---|---|---|
| `vebus.ttyS4` (mk2-dbus) | 60 | 0.06 | 4.5 | 9.5 | 7.0 | 6.5 | 2.9 | 2.8 |
| `system` (systemcalc) | 33 | 0.74 | 4.0 | 9.9 | 6.9 | 5.1 | 3.9 | 2.6 |
| `battery.ttyS6` | 25 | 1.00 | 3.9 | 10.7 | 7.0 | 6.6 | 3.5 | 2.0 |
| `battery.ttyS5` | 25 | 1.00 | 4.4 | 7.5 | 8.2 | 5.3 | 3.0 | 1.7 |
| `battery.ble_5320b7d7f9e7` | 25 | 0.41 | 3.6 | 8.8 | 7.4 | 5.1 | 4.1 | 3.3 |
| `battery.ble_ab807254e0b4` | 25 | 0.33 | 2.5 | 8.8 | 5.5 | 3.9 | 3.0 | 2.9 |
| `grid.power_watchdog_…` | 15 | 0.15 | 5.3 | 10.1 | 6.6 | 5.2 | 2.4 | 1.1 |
| `temperature.ruuvi_fed678ea752c` | 11 | 0.35 | 6.8 | 11.3 | 7.5 | 4.8 | 2.0 | 0.5 |

R is the resultant length of the signal times folded on the second: 1 means always the same
phase, 0 means any phase. The MultiPlus, the Power Watchdog, the BLE packs and the Ruuvi tags
publish at no particular phase and are followed by the same jump, so the burst belongs to the
signal and not to a place in the second.

Over that run, 53 % of all runnable-time fell in the 25 ms after a signal, which is 22 % of the
time (mean count 4.73 there, 0.91 in the stretches more than 60 ms after any signal).

A natural on/off test at a fixed phase: `solarcharger.ttyS7` publishes in some seconds and not
in others, always at 465-505 ms. In the 9 seconds where it did, the mean count in that window
was 3.49; in the 13 seconds where neither it nor anything else did, 1.16. For
`alternator.ttyUSB0` (330-375 ms): 2.13 over 13 seconds with, 1.32 over 6 without.

## What the kernel's own samples show

`scripts/loadavg_sampler_vs_publisher.py` with sender `all`, 16:01:10-16:13:12Z: 141 consecutive
kernel samples and all 7701 signals emitted meanwhile (10.7/s). The count the kernel sampled is
recovered from consecutive 1-minute figures (`new = old·e + n·(1−e)`, `e = 1884/2048`), and each
sample is placed by how long before it the last signal was emitted.

| last signal before the kernel's sample | samples | mean count the kernel sampled |
|---|---|---|
| 0-5 ms | 13 | 12.31 |
| 5-10 ms | 7 | 9.84 |
| 10-20 ms | 13 | 3.78 |
| 20-30 ms | 16 | 2.44 |
| 30-50 ms | 14 | 0.57 |
| 50-100 ms | 35 | 0.16 |
| more than 100 ms | 43 | 0.93 |

With a signal in the 30 ms before the sample: 49 samples, mean 6.47. Without: 92 samples, mean
0.58. The mean over all 141 is 2.63, which is the load average for that stretch, and the 49
samples that followed a signal carry 86 % of it. Integrating the table, one signal costs about
0.16 runnable-seconds as the kernel sees it; the scheduled samplers above read lower because
they run after part of each burst has drained.

So the load average on this box is, to within about a fifth, the bus-wide `BusItem` signal rate
times the cost of waking nine processes. What is left when no signal precedes the sample, 0.58,
is everything else on the box.

**systemcalc rides with the sampler.** In those 12 minutes systemcalc published 717 times, once
a second, and not once in the 30 ms before a kernel sample (0.6 % likely if the two were
unrelated). Its mean period over the 717 ticks was 1.0020 s, the same as the sampler's
5.010 s / 5. The gap from the kernel's sample to systemcalc's next publish stayed between 117
and 272 ms while both went round the second 1.4 times.

What that implies is a hypothesis, not an observation: the gap is not held by anything, it
wanders, and when it reaches zero the kernel would read systemcalc's burst on every sample for
as long as it stays there. The trip dumps look like that (1-minute average 6.3 to 9.0 for four
minutes at an unchanged 55 % idle, `dump-20261126T144932Z-trip.txt`), and the 501 s sweep across
a fixed-phase burst cannot produce it, because it crosses a 30 ms burst in 15 s. No capture
caught the gap at zero.

## The bursts in the phase fold, and what sets each phase

`scripts/loadavg_phase_fold.py`, 15:17:51-15:18:11Z, 7487 samples. Wall phase was monotonic
phase plus 0.322 that hour; the monotonic phase is the one that stays put.

| monotonic phase | wall phase (that hour) | mean runnable at peak | P(≥4) | who publishes there |
|---|---|---|---|---|
| 10-20 ms | 330-340 ms | 10.0 | 0.81 | `battery.ttyS6` (SmartShunt, VE.Direct) at 6-16 ms; `dcsystem.virtual` and the aggregator's timers at 0 |
| 170-180 ms | 490-500 ms | 11.2 | 0.95 | `battery.ttyS5` and `solarcharger.ttyUSB1`, both from 166 ms |
| 340-350 ms | 660-670 ms | 6.3 | 0.60 | `alternator.ttyUSB0` (Orion XS) from 336 ms |
| 400-520 ms | 720-840 ms | 4-7.7 | 0.4-0.8 | `solarcharger.ttyS7` from 466 ms, and whatever of the drifting publishers is passing |

The parent session's fold on 11-20 had the first two at wall 40 and 200 ms: the same 160 ms
apart, 290 ms earlier. The pair is fixed to the monotonic clock and the wall clock has moved
against it since; the hump is not a fixed-phase object and did not move with them.

Publishers, from four captures (15:25:49Z 15 s, 15:28:13Z 25 s, 15:33:57Z 12 s, 15:47:32Z 12 s;
11.3 to 11.8 signals/s and about 48 items/s bus-wide each time):

| service | process | signals/s | phase |
|---|---|---|---|
| `vebus.ttyS4` | `mk2-dbus` | 2.5 | none (every ~0.40 s) |
| `system` | `dbus_systemcalc.py` | 1.2-1.3 | drifts, see below |
| `battery.ttyS6` | `vedirect-dbus` | 1.0 | fixed, earliest 6 ms in all four captures |
| `battery.ttyS5` | `vedirect-dbus` | 1.0 | fixed, earliest 165-166 ms |
| `solarcharger.ttyUSB1` | `vedirect-dbus` | 0.9-1.0 | fixed, earliest 165-168 ms |
| `alternator.ttyUSB0` | `vedirect-dbus` | 0.5-0.9 | fixed, earliest 336-337 ms |
| `solarcharger.ttyS7` | `vedirect-dbus` | 0.3-0.5 | fixed, earliest 465-468 ms |
| `battery.ble_ab807254e0b4`, `battery.ble_5320b7d7f9e7` | `dbus-serialbattery.py` ×2 | 0.7-1.0 each | none (every ~1.18 s) |
| `grid.power_watchdog_…` | `dbus-power-watchdog.py` | 0.7 | none (every ~1.4 s) |
| 5-7 temperature and tank services | `dbus_ble_sensors.py` | 0.8-1.1 together | none (advertisement driven) |
| `dcsystem.virtual` | `dbus-virtual-dcsystem.py` | 0.2 | whole second |
| `battery.aggregate` | `dbus-aggregate-batteries.py` | 0.1-0.6 | follows the packs |

What sets each phase:

- **VE.Direct services.** Five different devices hold their publish phase to within 1 ms over
  22 minutes. Five independent crystals cannot do that (1 ms in 1300 s is under 1 ppm), so the
  once-a-second cadence is `vedirect-dbus`'s own, on the Cerbo's clock. The phases differ per
  process and have not changed since the 10-25 boot, which points at process start. Not tested:
  restarting one `vedirect-dbus` should move its burst, and that is a write on prod.
- **GLib seconds timers.** `g_timeout_add_seconds` rounds every seconds-granularity timer to one
  phase per machine, hashed from `DBUS_SESSION_BUS_ADDRESS`, else `HOSTNAME`, else zero. No
  daemon on prod has either variable in its environment (read from each daemon's own
  `/proc/<pid>/environ`), so all of them land on the whole monotonic second. Shown on dev with a
  four-tick script: no `HOSTNAME` fires at 0.0004-0.0051; `HOSTNAME=einstein` at 0.6306-0.6315
  (the hash predicts 0.6299); `HOSTNAME=localhost` at 0.9321-0.9337 (predicted 0.9316). On prod
  this aligns `dbus-virtual-dcsystem` (`timeout_add_seconds(1)`) and `dbus-aggregate-batteries`
  with each other and, by coincidence of start time, with `battery.ttyS6`. `/proc/timer_list`
  showed only two timers due within a millisecond of the whole second: the rest of that burst is
  woken by signals, not by its own timers. `dbus-ble-sensors-py` has four seconds-timers, all 30 s
  or longer.
- **systemcalc.** `GLib.timeout_add(1000, …)` for `_handletimertick`. A plain GLib timeout is
  re-armed from the time of dispatch and the poll timeout is rounded up to a millisecond, so the
  period is a little over a second: 1.0015 s mean (1.0007 median) over 300 ticks
  (`scripts/loadavg_sampler_vs_publisher.py`, 15:49:40-15:54:44Z). Its publish moves 1.5 ms
  later every second, 2.0 ms/s in the first 150 s of that run. The kernel's sample instant moves
  2.00 ms/s the same way. Over those five minutes each moved about half a second through the
  second while the gap between them only went from +293 ms to +145 ms.
- **BLE packs, MultiPlus, Power Watchdog, BLE sensors.** No phase: their cadence is not a second.

## Who is runnable

`scripts/loadavg_state_share.py`, 15:41:12-15:41:52Z: the state of the 48 busiest threads (99 %
of CPU time in a 3 s census) read every 50 ms, 801 samples. Mean threads in state R 2.09; the
kernel count at the same instants 2.23. A sampler that has to wait its turn under-weights the
bursts, so these are low: the load average over the same hour was 2.6-3.1. Shares are the
useful part.

| R fraction | thread | role |
|---|---|---|
| 0.245 | `[kworker/0:0-events]` | kernel, unidentified |
| 0.245 | `vedirect-dbus` ×5 | publishers, 50 ms tick |
| 0.165 | `dbus_systemcalc.py` | recipient and publisher |
| 0.145 | `dbus-daemon` | routes every signal to 9-11 connections |
| 0.127 | `dbus-serialbattery.py` ×2 (4 threads) | publishers |
| 0.125 | `dbus_ble_sensors.py` (3 threads; 0.084 is the HCI tap thread) | publisher |
| 0.104 | `venus-gui-v2` (3 threads) | recipient |
| 0.100 | `mk2-dbus` | publisher, 20 ms tick |
| 0.097 | `vrmlogger.py` | recipient |
| 0.090 | `dbus-aggregate-batteries.py` | recipient |
| 0.072 | `[kworker/u9:3-hci2]` | kernel, Bluetooth receive |
| 0.066 | `venus-platform` (2 threads) | recipient |
| 0.064 | `dbus-virtual-dcsystem.py` | recipient |
| 0.059 | `hub4control` (2 threads) | recipient |
| 0.056 | `flashmq` | recipient |
| 0.055 | `dbus-fronius` (2 threads) | recipient |
| 0.047, 0.043, 0.032 | `dbus-easytouchrv`, `dbus-power-watchdog`, `dbus-shyion-switch` | BLE pollers |

By owner: the nine recipients and the daemon 0.90 (43 %), of which Clint's two are 0.15;
Victron's publishers 0.41; kernel workers 0.36; Clint's other services 0.37, of which this
repository 0.125. These shares under-state the recipients: by the kernel's own samples the
bursts are about four fifths of the load average, not two fifths.

Per-burst membership from exact counters (`scripts/loadavg_burst_attribution.py`, 72 threads,
20 ms windows, 30 cycles at 15:20:40-15:21:15Z and 40 cycles at 15:35:17-15:36:02Z; CPU time and
wakeups per cycle inside the burst):

| thread | monotonic 0-40 ms: CPU ms / wakeups | 160-200 ms: CPU ms / wakeups |
|---|---|---|
| `dbus-aggregate-batteries.py` | 7.2 / 4.8 | 5.3 / 3.6 |
| `dbus_systemcalc.py` | 5.8 / 3.9 | 7.5 / 5.7 |
| `dbus-daemon` | 4.6 / 4.1 | 6.6 / 5.4 |
| `vrmlogger.py` | 3.7 / 2.9 | 5.2 / 3.4 |
| `dbus-virtual-dcsystem.py` | 3.4 / 2.8 | 3.2 / 2.7 |
| `flashmq` | 2.1 / 2.1 | 3.0 / 3.4 |
| `dbus-serialbattery.py` (one pack) | 2.0 / 1.8 | 1.7 / 1.3 |
| Qt threads (`gui-v2`, `venus-platform`, `hub4control`, `dbus-fronius`) | 0.9-1.2 / 1.3-1.7 each | 1.2-2.0 / 2.0-3.1 each |

The Python recipients spend 3 to 7 ms of CPU on a signal carrying three values; the Qt ones
about 1 ms. With a dozen tasks and two cores, each waits its turn, which is where the runnable
time goes.

## The 2026-10-17 step

Daily medians of the 15-minute average, from the resident recorder's hourly `alive:` lines
(TAI64N label = unix + 2^62 + 10):

| period | 15-minute median | bus connections |
|---|---|---|
| 09-24 … 10-07 | 1.57-1.75 | 113 |
| 10-08 … 10-13 | 1.73-1.94 | 113 |
| 10-14 … 10-16 | 2.04-2.11 | 113, 115 from 10-16 15:07Z |
| 10-17 … 10-25 | 2.40-3.14 | 115-120 |
| 10-26 … 11-26 | 2.48-2.98 | 115-117 |

What the service logs show for those days:

- The EasyTouch thermostats were unreachable from 09-15 to 10-12 ("Reachable again after 38732
  min", 10-12 20:30Z), back for a few hours on 10-12, 10-13 and 10-14, and gone again on 10-15
  and 10-16.
- The Power Watchdog, which sits at the shore-power pedestal, answered "not found (offline?)"
  every five minutes through 10-15 and 10-16 and first connected on 10-17 at 02:13:19Z.
- The EasyStart soft starters began sending live telemetry at 10-17 02:24Z and cycled all night:
  the air conditioners were running.
- On 10-16 at 14:52:22Z all four Orion-TR advertisements changed state at once, 15 minutes
  before the bus count first read 115.
- `dbus-ble-sensors-py` registered no new service between 10-15 and 10-18. The two SmartShunts on
  ttyS5 and ttyS6 were already there in August.
- `dbus-serialbattery` was redeployed on 10-16 22:14Z and 10-17 16:00Z (the fallback-sensor and
  shunt SoC sync work). Its per-process bus connections are 5 now and were 5 on 09-08.

So the rig went from storage to travelling on 10-16 and to shore power with the air conditioners
on from 10-17 02:13Z. Under the mechanism above that is more values changing every second (the
grid meter alone is 0.7 signals/s, the MultiPlus 2.5) and therefore more bursts. The same reading
fits the other direction: the calm of 09-15 to 10-12 is exactly the span the thermostats were
unpowered.

That is a timing match with a mechanism, not a proof. Nobody recorded the bus-wide signal rate
before 10-17, so the serialbattery update cannot be excluded as a contributor: its two packs
emit 1.6-1.9 of today's 11.7 signals/s, and whether that was lower before the update is unknown.

The bus count itself says little. Between the one surviving older dump (09-08, 107 connections)
and today (116) the growth is `dbus-easytouchrv` 8 → 16 and `dbus-power-watchdog` 3 → 4.

## Fixes, ranked

The arithmetic: the 15-minute average is the time-mean of the runnable count. Spreading bursts
across the second does not change that mean; only fewer signals, fewer recipients per signal or
cheaper recipients do. The 1- and 5-minute swings are a separate matter: they depend on bursts
that keep a fixed offset from the kernel's sample.

Predicted effects are estimates from the per-signal cost above (0.15-0.2 on the 15-minute
average for each signal per second removed, by the kernel's own samples) and from the state
shares, made on 11-26 before any fix was deployed. After any deploy, re-run
`loadavg_sampler_vs_publisher.py 720 all` and `--sampled`: it prints the signal rate and the
mean the kernel sampled, the same instrument before and after, and compare the daily medians
of the `alive:` lines.

| # | fix | owner | removes | 15-minute average (median now 2.8) | 1- and 5-minute swings |
|---|---|---|---|---|---|
| 1 | Stop publishing `/History/TimeSinceLastFullCharge` every poll; publish it once a minute | Clint's dbus-serialbattery fork | 1.3 of 1.8 signals/s from the two packs. That one path is in 0.70-0.77 signals/s per pack; every other path together 0.2 | −0.2 to −0.26 | proportionally fewer bursts to land on |
| 2 | Remove the 1 s `_update` timer left beside the reactive path (three call sites register both `timeout_add_seconds(max(REACTIVE_FLOOR_S, …))` and `timeout_add_seconds(UPDATE_INTERVAL_DATA)`), and subscribe per tracked battery service instead of to all senders | Clint's dbus-aggregate-batteries | one recipient, 5-7 ms of CPU, from about 7 of 10.7 signals/s, and a once-a-second wakeup on the whole second | −0.1 to −0.2 | smaller burst at the whole second |
| 3 | Do not run `hub4control` and `dbus-fronius` if the rig has no ESS assistant and no AC PV inverter | Victron's services, Clint's decision | 4 of 13 recipient threads from every signal | −0.15 to −0.4 | every burst is shorter |
| 4 | Gate slow environmental paths by deadband instead of rounded equality (`/BatteryVoltage` on one Mopeka alone is 0.11-0.23 signals/s; Ruuvi `/Humidity`, `/Pressure`, `/Temperature`) | this repository | 0.4-0.9 of the 0.6-1.1 signals/s this service emits | −0.06 to −0.18 | none of its own |
| 5 | Coarser deadband on grid power and current; `/UpdateIndex` rides in every signal | Clint's dbus-power-watchdog | about 0.3 of 0.6-0.7 signals/s | about −0.05 | none of its own |
| 6 | Subscribe per monitored service | Clint's dbus-virtual-dcsystem | one recipient from about 1.5 of 10.7 signals/s (it watches most classes) | about −0.02 | none |
| 7 | `timeout_add_seconds` for systemcalc's tick | Victron | nothing | none | if the hypothesis above holds, the long excursions become 15 s crossings |
| 8 | SmartShunt `/History/TimeSinceLastFullCharge` once a minute in `vedirect-dbus`; per-service receivers in velib_python `DbusMonitor` | Victron | up to 2 signals/s when the shunts' current is steady; a recipient for every third-party velib user | small here | none |

Fixes 1, 2, 4, 5 and 6 are code Clint controls and add up to roughly −0.4 to −0.7; with 3,
−0.55 to −1.1. About two thirds of the signals are Victron publishers (MultiPlus 2.4/s, five
VE.Direct services 3.9/s, systemcalc 1.0-1.3/s) waking seven Victron subscribers, and that part
is by design.

Where the fixes stood on 2026-11-27 (deploy times as reported by the sessions that did them):

| # | state |
|---|---|
| 1 | TechBlueprints/venus-os_dbus-serialbattery#550, on prod from 11-27 16:22Z |
| 2 | dbus-aggregate-batteries `integration/all` 864150f, on prod from 11-27 17:08Z; the subscription half is also TechBlueprints/dbus-aggregate-batteries#181 |
| 3 | both services stopped on prod 11-27 16:36Z |
| 4 | #36, on prod from 11-27 17:07Z; one `ItemsChanged` per publish block instead of per path is #39 |
| 5 | TechBlueprints/dbus-power-watchdog#13, open |
| 6 | not started |

The after-measurement uses the same instruments as above: `loadavg_sampler_vs_publisher.py
720 all` with `--sampled`, the phase fold, and whole days of `alive:` medians after the last
deploy.

What does not help the 15-minute figure: moving bursts apart. Giving services different
`HOSTNAME` values would spread the GLib seconds-timers, and restarting a `vedirect-dbus` would
move its burst, but the same tasks would be runnable for the same time.

Which paths churn, from `dbus_publisher_phase.py --paths`, 16:13:52-16:14:22Z:

| service | signals/s | paths in most of them |
|---|---|---|
| `vebus.ttyS4` | 2.47 | `/Ac/ActiveIn/L2/S`, `/Ac/Out/L2/S`, `/Ac/ActiveIn/L2/I`, `/Ac/Out/L2/I` (2.0/s each), `/Dc/0/Current` 1.8/s |
| `system` | 1.30 | `/Dc/Vebus/Current`, `/Dc/InverterCharger/Current` (1.0/s), AC consumption currents and powers |
| `battery.ttyS5`, `battery.ttyS6` | 1.00 each | `/History/TimeSinceLastFullCharge` 1.00/s, `/Dc/0/Power` and `/Dc/0/Current` 0.7-0.8/s |
| `battery.ble_…` ×2 | 0.90, 0.93 | `/History/TimeSinceLastFullCharge` 0.70, 0.77/s; cell voltages 0.07-0.2/s |
| `solarcharger.ttyUSB1`, `alternator.ttyUSB0`, `solarcharger.ttyS7` | 0.80, 0.77, 0.50 | `/Dc/0/Voltage` (and `/Pv/V`) |
| `grid.power_watchdog_…` | 0.67 | `/UpdateIndex` 0.67/s, `/Ac/Power` 0.40/s, `/Ac/L1/Current` 0.33/s |
| `tank.mopeka_cb157d296144` | 0.23 | `/BatteryVoltage` |
| five Ruuvi services | 0.03-0.07 each | `/Humidity`, `/Pressure`, `/Temperature`, `/BatteryVoltage` |

## Instruments, and what they cannot say

| script | what it measures | cost on prod |
|---|---|---|
| `loadavg_phase_fold.py` | runnable count by phase in the second, wall and monotonic | ~8 % of a core |
| `dbus_publisher_phase.py` | who publishes when; `--triggered` runnable count around each signal; `--paths` which paths churn | 2 % (11 % with 2 ms runnable sampling) |
| `loadavg_burst_attribution.py` | per-thread CPU, wait and wakeups by 20 ms window, exact counters | ~5.5 % |
| `loadavg_state_share.py` | per-thread share of state R, by sampling | ~6 % |
| `loadavg_runnable_vs_state.py` | kernel count against threads in state R at peaks | brief bursts |
| `loadavg_sampler_vs_publisher.py` | publishers against the kernel's sample instant; `--sampled` what the kernel saw | 5 % for 6 s at each end, then idle |
| `dbus_signal_dose_response.py` | experiment: emits signals at set rates. Dev only without the owner's say-so | ~10 % |

- **`schedstat` wait time is not trustworthy on this kernel.** For some threads the run-queue
  wait counter grows while the thread is asleep: one `dbus-easytouchrv` thread showed 10.0 s of
  wait in 30 s and was in state R in 2 of 1015 samples. CPU time and timeslice counts are fine.
  `loadavg_burst_attribution.py` therefore over-states totals (phase-mean 3.95-4.75 against a
  load average near 3); use its CPU and wakeup columns, and `loadavg_state_share.py` for shares.
  Why the counter does this was not established.
- **A sampler that is itself scheduled under-counts bursts.** The fold and the state sampler read
  when they get the CPU, which is after part of a burst has drained. The kernel samples from the
  tick and has no such bias.
- **The dose-response on dev was inconclusive**: 0 → 10 → 0 → 30 → 0 → 10 signals/s gave 1.18,
  1.66, 1.35, 1.66, 0.98, 1.13. Dev has three subscribers without a sender filter where prod has
  nine recipients, and the injected signals came from an unnamed connection that the Qt
  processes do not subscribe to. Dev cannot show this at prod's scale.
- **`kworker/0:0-events`** is woken about 300 times a second for about 60 µs of work and is in
  state R in a quarter of samples, waiting behind whatever holds CPU 0 (all hardware interrupts
  land there; `ehci_hcd:usb1`, the Bluetooth dongles' hub, alone is 927/s). What queues that work
  is not identified. The CPU governor is `performance`, so it is not frequency sampling.
  Naming it needs tracing, and this kernel is built without ftrace.

## Measurement log (UTC, 2026-11-26)

Samplers on prod: 15:17:51-15:18:11 (fold), 15:20:40-15:21:15 (attribution), 15:25:49-15:26:04
(publishers), 15:28:13-15:28:38 (publishers + runnable), 15:33:57-15:34:09 (publishers),
15:35:17-15:36:02 (attribution), 15:37:46-15:38:16 (state probe, 19 % of a core),
15:40:05-15:40:25 (count vs state), 15:41:12-15:41:52 (state share), 15:47:32-15:47:44
(publishers), 15:49:40-15:54:44 (systemcalc listener, passive), 16:01:10-16:13:12 (all-signal listener, passive), 16:13:52-16:14:22 (publishers with paths). Between them: single
reads of logs, `/proc/timer_list`, `/proc/interrupts`, the match rules and two dumps. Two of
those reads were shell loops over `/proc` that forked once per process (about 340 forks each, at
about 15:42:40 and 16:06:14, the second inside the passive capture): a mistake against this
box's own rule, a second or two each.

`dump-20261126T152502Z-early.txt` fell among these and is measurement-induced. A sibling
session deployed the corrected throttle thresholds at 15:31:22Z, which restarted
`dbus-ble-sensors-py` (pid 6247 → 17506) and `load-forensics`; captures before and after that
moment agree on every figure quoted here.
