# load-forensics — why did the load go high?

A small, separate service that keeps a **continuous 30-minute ring** of
`/proc` samples and writes it out, with a short deep snapshot, the moment
load crosses a threshold. It exists because the 5- and 15-minute averages
that trip dbus-ble-sensors-py's throttle (and the stock watchdog's reboot)
lag their cause by minutes; by trip time the culprit is usually gone.

It runs as `/service/load-forensics`, installed and removed with the BLE
service but independent of it, so it survives that service's restarts and
can watch it. Source: `src/opt/victronenergy/load-forensics/load_forensics.py`.

## What a sample holds (every 30 s, one pass over /proc, no forks)
- `/proc/loadavg`; CPU split (user/system/iowait/softirq/idle) over the
  interval; `ctxt` and `processes` (fork counter) deltas; `procs_running`,
  `procs_blocked`; MemAvailable; open file handles; and eMMC writes as
  **both** time and volume — milliseconds, operations and kB. Time alone
  is ambiguous: a dev flood tripled the write-time column while the
  service logs wrote no more than in the quiet minutes before it, so the
  disk was not busier, its completions were queued behind a loaded CPU.
  "Wrote much more" and "same writes, slower" want opposite responses.
- Per process, for the top 8 by CPU plus a fixed watch list (bluetoothd,
  dbus-daemon, systemcalc, gui-v2, each pack, sensors-py, easytouch,
  watchdog, shyion, sshd): CPU %, state, threads, fd count, an **exact
  D-Bus connection count**, and the wait channel of every thread that is
  running or blocked. A watched process is flagged **RESTARTED** when it was
  *replaced* — this pid is new for that name and a pid the name had before
  is gone. A new pid alongside a living one is not a restart, because sshd
  has one process per connection and every login would otherwise read as one.

**What it costs.** Measured on dev over 264 tasks: **109 ms of CPU per
sample**, which at the 30-second interval is **0.37 % of one core**. The
walk reads one or two small files per task, so it uses a raw
open/read/close rather than buffered IO, and each process's resolved name
is cached for the life of that process. The tool warns in its own heartbeat
if it ever averages more than 1 % of a core.
- A global **bus** figure: live connections on the system bus, from the
  `/proc/net/unix` rows bound to the bus socket (listener excluded).
- An **mdns** figure: multicast-DNS packets and bytes this interval, with the
  top three source addresses. Venus's `dbus-modbus-client` binds port 5353,
  joins the mDNS group and parses *every* packet on the LAN with a
  pure-Python DNS parser. Its steady 0.3–0.4 % of a core is **not** that
  parsing — measured against an almost silent LAN it is the process's own
  100 ms update loop. Parsing is the **spike** term, and it is what took the
  same process to 21 % of a core during dev's two load events. So this
  column is the denominator for the spikes rather than the baseline, and it
  turns "modbus-client at 21 %" into "modbus-client at 21 % while mDNS ran
  at N packets/s from host X". We
  **count only, never parse** — parsing is the very cost being measured. A
  drain is capped, and a capped drain is flagged `SATURATED`, which is
  itself the signal that a flood is under way. It is counted even while
  tripped: those are the samples whose rate explains the event, and an
  undrained socket would overflow and lose exactly them.

**How the per-process bus count works, and why it is not the obvious rule.**
`/proc/net/unix` prints each socket's *own* bound path, so only the bus
listener and dbus-daemon's accepted sockets carry it; a client's connected
socket is unbound and prints nothing. Matching a process's fd inodes against
those rows credits every connection to dbus-daemon and none to any client,
the opposite of the fan-out signature. The exact rule needs each socket's
*peer* inode, which the kernel's socket-diagnostics netlink interface
provides (`unix_diag` with `UDIAG_SHOW_PEER`, no fork): a client socket
whose peer is one of the daemon's accepted sockets is one bus connection.
When that interface is unavailable the per-process figure renders as `?`,
never as a false zero, and the startup line says so.

**Reading the sensors-py figure.** `dbus-ble-sensors-py` holds one system-bus
connection per registered `VeDbusService` — one per published device role —
plus its own main connection and the advertisement router. It is normal for
it to own roughly a third of every connection on the bus: ~24 service names
map to ~37 connections, because a single BLE device commonly backs several
roles (a multi-sensor unit publishes tank + temperature, and so on). That is
the baseline, not a leak. A leak shows as connections climbing without
matching `registered com.victronenergy.*` lines in the service's log, or as
un-registrations that never free their connection.

## Triggers (one event = one dump)
- own 1-minute average ≥ 4.0 — the early catch;
- 5-minute ≥ 6.0 or 15-minute ≥ 5.5 — the exact thresholds the BLE service
  derives from `/etc/watchdog.conf`, imported from it, so a dump lands beside
  the throttle line;
- the service's own `load_throttle: tripped` line, tailed from `current`
  only (rotated logs are never read).
An event closes after two consecutive samples under the release levels.

**Two classes, kept apart.** A dump is *early* when only our own 1-minute
rule fired, and *trip* when a real threshold or the service's own trip line
did. On prod the early catch fires often — every pack restart, every GUI
session — while a real trip is rare and is the entire point, so the two are
kept in **separate pools of 10** and a trip dump is never evicted to make
room for an early one. An early-catch dump also waits out a 5-minute
cooldown, so a 1-minute average hovering at the threshold cannot open and
close an event every minute; a real trip never waits.

**Escalation.** If an event opened on the early catch and the load then
crosses a real threshold, a second dump is written and the event is
promoted. Without that, the moment the box actually tripped would be the one
moment never captured, because the event was already open.
While an event is open, each sample is a **lean** pass (no fd walk, no wait
channels, no bus inodes): nothing beyond one `/proc` pass while tripped.

## What a dump adds (on trigger only; two forks: `dmesg`, `hciconfig -a`)
The ring (oldest first), `dmesg` tail, adapter link/scan state, and the
last 40 lines of the sensors, systemcalc, sshd and pack logs, read directly
from each `current`. Written to
`/data/log/load-forensics/dumps/dump-<UTC>-<class>.txt`, about 96 kB each.

Dumps go in a **subdirectory** on purpose. On Venus `/var/log` is a symlink
to `/data/log`, so the multilog carrying this service's own log owns
`/data/log/load-forensics` and keeps its `current`, `state` and `lock`
there. Writing dumps beside them would mix two kinds of artifact in one
place, and a cleanup of "the log directory" would take the evidence with it.

Every dump and every hourly heartbeat carries the instrument's **own
cost** (CPU seconds since start and RSS). It runs at nice +10.

## Reading the signatures
| you see                                                  | it was                    |
|----------------------------------------------------------|---------------------------|
| systemcalc CPU spike + a pack marked RESTARTED            | a service restart/rescan  |
| bluetoothd and every bleak process climbing together     | an unfiltered discovery   |
| gui-v2 near 60 %                                         | a GUI session             |
| `forks` climbing sample after sample                     | a fork loop               |
| `blk` > 0 with usb/hci wait channels + dmesg USB lines   | a USB port drop           |

## Prohibitions (each has caused load on this box)
No Venus `dbus` CLI, no `busctl` / `bluetoothctl` / `dbus-send`, no scans of
rotated logs, no per-process fork loops, no work beyond one lean pass while
tripped. BusyBox: `head`/`tail` need `-n`.

## Soak check
`python3 load_forensics.py --dump-now --dump-dir /tmp/lf` takes three quick
samples and writes one dump without a trigger.
