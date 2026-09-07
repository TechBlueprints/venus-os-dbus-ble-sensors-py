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
  `procs_blocked`; MemAvailable; open file handles; eMMC write and I/O ms.
- Per process, for the top 8 by CPU plus a fixed watch list (bluetoothd,
  dbus-daemon, systemcalc, gui-v2, each pack, sensors-py, easytouch,
  watchdog, shyion, sshd): CPU %, state, threads, fd count, **D-Bus
  connection count** (fd socket inodes matched against `/proc/net/unix`
  rows for the system bus — zero bus calls), and the wait channel of every
  thread that is running or blocked. A watch-list process whose pid changed
  is flagged **RESTARTED**.

## Triggers (one event = one dump)
- own 1-minute average ≥ 4.0 — the early catch;
- 5-minute ≥ 6.0 or 15-minute ≥ 5.5 — the exact thresholds the BLE service
  derives from `/etc/watchdog.conf`, imported from it, so a dump lands beside
  the throttle line;
- the service's own `load_throttle: tripped` line, tailed from `current`
  only (rotated logs are never read).
An event closes after two consecutive samples under the release levels.
While an event is open, each sample is a **lean** pass (no fd walk, no wait
channels, no bus inodes): nothing beyond one `/proc` pass while tripped.

## What a dump adds (on trigger only; two forks: `dmesg`, `hciconfig -a`)
The ring (oldest first), `dmesg` tail, adapter link/scan state, and the
last 40 lines of the sensors, systemcalc, sshd and pack logs, read directly
from each `current`. Written to `/data/log/load-forensics/dump-<UTC>.txt`;
the directory keeps the newest 20.

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
