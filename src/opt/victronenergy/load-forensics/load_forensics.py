#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Load forensics: explain WHY the Cerbo's load went high, from a continuous ring.

The load throttle in dbus-ble-sensors-py trips on the 5- and 15-minute load
averages.  Those lag their cause by minutes: by trip time the offender -- a
restart rescan, a VEConfigure upload, a fork loop, a GUI session, a USB port
drop -- is often gone, and a diagnostic taken AT the trip explains nothing.

So this is a ring, not a trip-time snapshot.  Every ``INTERVAL_S`` it takes
one pass over ``/proc`` and keeps the last ``RING_MINUTES`` of samples in
memory.  When a trigger fires it writes the whole ring, plus a small deep
snapshot, to a size-capped directory under ``/data/log``.  One event yields
one dump (hysteresis), and every dump carries the instrument's own cost.

It is its own process, deliberately outside dbus-ble-sensors-py: it must
survive that service's restarts and be able to watch it.

Per sample (``/proc`` only, one pass, tens of milliseconds):
  - ``/proc/loadavg``; ``/proc/stat`` cpu split, ``ctxt``, ``processes``
    (the fork counter), ``procs_running``, ``procs_blocked``;
    ``/proc/meminfo`` MemAvailable; ``/proc/sys/fs/file-nr``;
    ``/proc/diskstats`` for the eMMC (write and I/O milliseconds).
  - Per process: CPU delta (utime+stime), state, thread count.  For the
    top-N by CPU plus a fixed watch list: fd count, D-Bus connection count
    (socket inodes matched against ``/proc/net/unix`` rows for the system
    bus -- zero bus calls), and the wait channel of every thread that is
    running or blocked (a USB/HCI stall names itself there).
  - A watch-list process whose pid changed is flagged: a restart.

Triggers: own 1-minute crossing (early catch) and the same 5m/15m
thresholds dbus-ble-sensors-py derives from ``/etc/watchdog.conf``, so a
dump lands beside the throttle line; optionally the service's own
``load_throttle: tripped`` log line, tailed from ``current`` only.

Prohibitions, each of which has itself caused load on this box: no Venus
``dbus`` CLI, no ``busctl``/``bluetoothctl``/``dbus-send``, no scans of
rotated logs, no per-process fork loops, and while the box is tripped no
work beyond one lean ``/proc`` pass.  A dump forks exactly twice (``dmesg``
and ``hciconfig -a``); a sample forks never.

Signatures this makes legible: a restart (systemcalc CPU spike + a new pack
pid); an unfiltered discovery (bluetoothd and every bleak process climbing
together); a GUI session (gui-v2 ~60 %); a fork loop (the ``processes``
counter's rate); a USB port drop (``procs_blocked`` with usb/hci wait
channels and dmesg lines in the same sample).
"""
from __future__ import annotations

import argparse
import collections
import glob
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("load_forensics")

# ---------------------------------------------------------------- settings ---
INTERVAL_S = 30.0
RING_MINUTES = 30
RING_LEN = int(RING_MINUTES * 60 / INTERVAL_S)     # 60 samples
TOP_N = 8
DUMP_DIR = "/data/log/load-forensics"
DUMP_KEEP = 20                                      # ring of dump files
TAIL_LINES = 40
TRIGGER_1M = 4.0                                    # early catch, own rule
RELEASE_1M = 3.0
RELEASE_SAMPLES = 2                                 # consecutive quiet samples close an event
HEARTBEAT_S = 3600.0
SELF_COST_WARN_PCT = 1.0                            # average % of one core; the instrument must not be the load

# Processes always reported, matched as substrings of comm or the script name.
WATCH_LIST = (
    "bluetoothd", "dbus-daemon", "systemcalc", "gui-v2", "blebattery",
    "serialbattery", "dbus_ble_sensors", "easytouch", "watchdog", "shyion",
    "sshd", "load_forensics",
)
DBUS_SOCKET_PATHS = ("/var/run/dbus/system_bus_socket", "/run/dbus/system_bus_socket")
EMMC_DEVICE = "mmcblk1"

# Logs tailed into a dump (direct file reads of ``current`` only, no forks).
TAIL_LOGS = (
    "/var/log/dbus-ble-sensors-py/current",
    "/var/log/dbus-systemcalc-py/current",
    "/var/log/sshd/current",
)
TAIL_LOG_GLOBS = ("/var/log/dbus-blebattery*/current",)
SENSORS_LOG = "/var/log/dbus-ble-sensors-py/current"
TRIP_LINE = "load_throttle: tripped"

_SENSORS_PY_DIR = "/data/apps/dbus-ble-sensors-py/src/opt/victronenergy/dbus-ble-sensors-py"


def sensors_py_thresholds() -> tuple[float, float, float, float]:
    """(trip_15m, trip_5m, release_15m, release_5m) exactly as the service derives them.

    Imports the service's own derivation from ``/etc/watchdog.conf`` so a
    dump lands beside the throttle line.  If the service has moved on, fall
    back to its historical defaults rather than dying: the forensics tool
    must outlive the service's refactors.
    """
    try:
        if _SENSORS_PY_DIR not in sys.path:
            sys.path.append(_SENSORS_PY_DIR)
        import load_throttle  # type: ignore
        return (load_throttle.TRIP_15M, load_throttle.TRIP_5M,
                load_throttle.RELEASE_15M, load_throttle.RELEASE_5M)
    except Exception as e:  # noqa: BLE001
        log.warning("could not import the service's thresholds (%r); using 5.5/6.0/5.0/5.0", e)
        return (5.5, 6.0, 5.0, 5.0)


# ------------------------------------------------------------- /proc reads ---
def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def read_loadavg(root: str = "/proc") -> tuple[float, float, float, int, int, int]:
    """(1m, 5m, 15m, running, total, last_pid)."""
    p = _read(f"{root}/loadavg").split()
    r, t = p[3].split("/")
    return float(p[0]), float(p[1]), float(p[2]), int(r), int(t), int(p[4])


@dataclass
class SysCounters:
    cpu: tuple[int, ...]        # user nice system idle iowait irq softirq steal
    ctxt: int
    processes: int              # forks since boot
    running: int
    blocked: int


def read_stat(root: str = "/proc") -> SysCounters:
    cpu: tuple[int, ...] = ()
    ctxt = processes = running = blocked = 0
    for line in _read(f"{root}/stat").splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "cpu":
            cpu = tuple(int(x) for x in f[1:9])
        elif f[0] == "ctxt":
            ctxt = int(f[1])
        elif f[0] == "processes":
            processes = int(f[1])
        elif f[0] == "procs_running":
            running = int(f[1])
        elif f[0] == "procs_blocked":
            blocked = int(f[1])
    return SysCounters(cpu, ctxt, processes, running, blocked)


def read_memavailable_kb(root: str = "/proc") -> int:
    for line in _read(f"{root}/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return -1


def read_file_nr(root: str = "/proc") -> int:
    return int(_read(f"{root}/sys/fs/file-nr").split()[0])


def read_disk_ms(root: str = "/proc", device: str = EMMC_DEVICE) -> tuple[int, int]:
    """(ms writing, weighted ms doing I/O) for *device*, or (0, 0)."""
    for line in _read(f"{root}/diskstats").splitlines():
        f = line.split()
        if len(f) >= 14 and f[2] == device:
            return int(f[10]), int(f[13])
    return 0, 0


def read_bus_inodes(root: str = "/proc", paths=DBUS_SOCKET_PATHS) -> set[int]:
    """Inodes of every unix socket bound or connected to the system bus."""
    inodes: set[int] = set()
    try:
        lines = _read(f"{root}/net/unix").splitlines()[1:]
    except OSError:
        return inodes
    for line in lines:
        f = line.split()
        if len(f) >= 8 and f[7] in paths:
            try:
                inodes.add(int(f[6]))
            except ValueError:
                pass
    return inodes


@dataclass
class Proc:
    pid: int
    name: str
    state: str
    ticks: int                  # utime + stime
    starttime: int
    threads: int
    cpu_pct: float = 0.0        # over the last interval, % of one core
    fds: int = -1
    dbus: int = -1
    active_threads: list = field(default_factory=list)   # (tid, state, wchan) for R/D threads
    restarted: bool = False


def _proc_name(root: str, pid: int, comm: str) -> str:
    """comm, or for an interpreter the script's basename (packs, sensors, easytouch are all python3)."""
    if not (comm.startswith("python") or comm in ("sh", "bash")):
        return comm
    try:
        argv = _read(f"{root}/{pid}/cmdline").split("\0")
    except OSError:
        return comm
    for a in argv[1:]:
        if a.endswith(".py") or a.endswith(".sh"):
            return os.path.basename(a)
    return comm


def read_proc(root: str, pid: int) -> Optional[Proc]:
    try:
        stat = _read(f"{root}/{pid}/stat")
    except OSError:
        return None
    # comm may contain spaces/parens: split on the LAST ')'
    lp = stat.rfind(")")
    comm = stat[stat.find("(") + 1:lp]
    f = stat[lp + 2:].split()
    # fields after comm: state(0) ppid(1) ... utime(11) stime(12) ... num_threads(17) ... starttime(19)
    try:
        return Proc(pid=pid, name=_proc_name(root, pid, comm), state=f[0],
                    ticks=int(f[11]) + int(f[12]), starttime=int(f[19]), threads=int(f[17]))
    except (IndexError, ValueError):
        return None


def enrich_proc(root: str, p: Proc, bus_inodes: set[int]) -> None:
    """fd count, D-Bus connections, and wait channels of running/blocked threads."""
    try:
        fds = os.listdir(f"{root}/{p.pid}/fd")
        p.fds = len(fds)
        n = 0
        for fd in fds:
            try:
                tgt = os.readlink(f"{root}/{p.pid}/fd/{fd}")
            except OSError:
                continue
            if tgt.startswith("socket:[") and int(tgt[8:-1]) in bus_inodes:
                n += 1
        p.dbus = n
    except OSError:
        pass
    try:
        for tid in os.listdir(f"{root}/{p.pid}/task"):
            try:
                ts = _read(f"{root}/{p.pid}/task/{tid}/stat")
                st = ts[ts.rfind(")") + 2:].split()[0]
            except (OSError, IndexError):
                continue
            if st in ("R", "D"):
                try:
                    wchan = _read(f"{root}/{p.pid}/task/{tid}/wchan").strip() or "-"
                except OSError:
                    wchan = "?"
                p.active_threads.append((int(tid), st, wchan))
    except OSError:
        pass


def is_watched(name: str, watch=WATCH_LIST) -> bool:
    return any(w in name for w in watch)


# ---------------------------------------------------------------- sampler ---
@dataclass
class Sample:
    t: float                    # time.time()
    load: tuple                 # (1m, 5m, 15m, running, total, last_pid)
    cpu_pct: dict               # user/system/iowait/softirq/idle over the interval
    forks: int                  # since last sample
    ctxt: int
    running: int
    blocked: int
    memavail_kb: int
    file_nr: int
    disk_write_ms: int          # since last sample
    disk_io_ms: int
    procs: list                 # of Proc, top-N + watched
    lean: bool = False          # taken while tripped: no fd/wchan/bus work


class Sampler:
    def __init__(self, root: str = "/proc", clk_tck: Optional[int] = None,
                 top_n: int = TOP_N, watch=WATCH_LIST):
        self.root = root
        self.clk_tck = clk_tck or os.sysconf("SC_CLK_TCK")
        self.top_n = top_n
        self.watch = watch
        self._prev_ticks: dict[int, tuple[int, int]] = {}    # pid -> (ticks, starttime)
        self._prev_sys: Optional[SysCounters] = None
        self._prev_disk: Optional[tuple[int, int]] = None
        self._prev_t: Optional[float] = None
        self._watched_pids: dict[str, int] = {}                # name -> pid (restart detection)

    def sample(self, lean: bool = False, now: Optional[float] = None) -> Sample:
        now = time.time() if now is None else now
        load = read_loadavg(self.root)
        sysc = read_stat(self.root)
        disk = read_disk_ms(self.root)
        dt = (now - self._prev_t) if self._prev_t else INTERVAL_S

        # system cpu split over the interval
        cpu_pct: dict = {}
        if self._prev_sys and sysc.cpu and self._prev_sys.cpu:
            d = [a - b for a, b in zip(sysc.cpu, self._prev_sys.cpu)]
            tot = sum(d) or 1
            cpu_pct = {"user": 100.0 * (d[0] + d[1]) / tot, "system": 100.0 * d[2] / tot,
                       "idle": 100.0 * d[3] / tot, "iowait": 100.0 * d[4] / tot,
                       "softirq": 100.0 * (d[5] + d[6]) / tot}
        forks = (sysc.processes - self._prev_sys.processes) if self._prev_sys else 0
        ctxt = (sysc.ctxt - self._prev_sys.ctxt) if self._prev_sys else 0
        dwr = (disk[0] - self._prev_disk[0]) if self._prev_disk else 0
        dio = (disk[1] - self._prev_disk[1]) if self._prev_disk else 0

        # one pass over processes
        procs: list[Proc] = []
        cur_ticks: dict[int, tuple[int, int]] = {}
        for entry in os.listdir(self.root):
            if not entry.isdigit():
                continue
            p = read_proc(self.root, int(entry))
            if p is None:
                continue
            cur_ticks[p.pid] = (p.ticks, p.starttime)
            prev = self._prev_ticks.get(p.pid)
            if prev and prev[1] == p.starttime and dt > 0:
                p.cpu_pct = 100.0 * (p.ticks - prev[0]) / self.clk_tck / dt
            procs.append(p)
        self._prev_ticks = cur_ticks

        # top-N by cpu, plus everything on the watch list
        procs.sort(key=lambda x: x.cpu_pct, reverse=True)
        chosen = {p.pid: p for p in procs[:self.top_n]}
        for p in procs:
            if is_watched(p.name, self.watch):
                chosen[p.pid] = p
        selected = sorted(chosen.values(), key=lambda x: x.cpu_pct, reverse=True)

        # restart detection on watched names (a name's pid changed)
        for p in selected:
            if is_watched(p.name, self.watch):
                old = self._watched_pids.get(p.name)
                if old is not None and old != p.pid:
                    p.restarted = True
                self._watched_pids[p.name] = p.pid

        if not lean:
            bus = read_bus_inodes(self.root)
            for p in selected:
                enrich_proc(self.root, p, bus)

        self._prev_sys, self._prev_disk, self._prev_t = sysc, disk, now
        return Sample(t=now, load=load, cpu_pct=cpu_pct, forks=forks, ctxt=ctxt,
                      running=sysc.running, blocked=sysc.blocked,
                      memavail_kb=read_memavailable_kb(self.root), file_nr=read_file_nr(self.root),
                      disk_write_ms=dwr, disk_io_ms=dio, procs=selected, lean=lean)


# --------------------------------------------------------------- triggers ---
@dataclass
class Thresholds:
    trip_1m: float = TRIGGER_1M
    trip_5m: float = 6.0
    trip_15m: float = 5.5
    release_1m: float = RELEASE_1M
    release_5m: float = 5.0
    release_15m: float = 5.0


class EventState:
    """One event = one dump.  Opens on any trigger; closes after RELEASE_SAMPLES quiet samples."""

    def __init__(self, th: Thresholds, release_samples: int = RELEASE_SAMPLES):
        self.th = th
        self.release_samples = release_samples
        self.active = False
        self.quiet = 0
        self.opened_at: Optional[float] = None
        self.peak: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.reason = ""

    def reasons(self, l1: float, l5: float, l15: float, log_tripped: bool) -> list[str]:
        r = []
        if l1 >= self.th.trip_1m:
            r.append(f"1m>={self.th.trip_1m}")
        if l5 >= self.th.trip_5m:
            r.append(f"5m>={self.th.trip_5m}")
        if l15 >= self.th.trip_15m:
            r.append(f"15m>={self.th.trip_15m}")
        if log_tripped:
            r.append("sensors-py tripped")
        return r

    def update(self, l1: float, l5: float, l15: float, log_tripped: bool, now: float) -> Optional[str]:
        """Returns a reason string when a NEW event opens (dump now); None otherwise."""
        rs = self.reasons(l1, l5, l15, log_tripped)
        if self.active:
            self.peak = tuple(max(a, b) for a, b in zip(self.peak, (l1, l5, l15)))
            quiet = (l1 < self.th.release_1m and l5 < self.th.release_5m and l15 < self.th.release_15m)
            self.quiet = self.quiet + 1 if quiet else 0
            if self.quiet >= self.release_samples:
                self.active = False
            return None
        if rs:
            self.active, self.quiet, self.opened_at = True, 0, now
            self.peak, self.reason = (l1, l5, l15), " | ".join(rs)
            return self.reason
        return None


class LogTail:
    """Tail ``current`` for a marker.  Follows multilog rotation (inode change).  Never reads rotated files."""

    def __init__(self, path: str, marker: str):
        self.path, self.marker = path, marker
        self._ino: Optional[int] = None
        self._pos = 0

    def poll(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            return False
        if self._ino != st.st_ino:
            # first open: start at the end (do not replay history); rotation: start at 0
            self._pos = st.st_size if self._ino is None else 0
            self._ino = st.st_ino
        if st.st_size < self._pos:
            self._pos = 0
        try:
            with open(self.path, "rb") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
        except OSError:
            return False
        return self.marker.encode() in data


# ------------------------------------------------------------------ dumps ---
def _tail_file(path: str, n: int = TAIL_LINES, max_bytes: int = 64 * 1024) -> str:
    """Last *n* lines by reading only the file's tail.  No forks, no rotated files."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError as e:
        return f"(unavailable: {e})"


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    """One bounded subprocess.  Used exactly twice per dump, never per sample."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return f"({cmd[0]} unavailable: {e})"


def self_cost(root: str = "/proc", clk_tck: Optional[int] = None) -> tuple[float, int]:
    """(own CPU seconds since start, own RSS kB)."""
    clk = clk_tck or os.sysconf("SC_CLK_TCK")
    try:
        st = _read(f"{root}/self/stat")
        f = st[st.rfind(")") + 2:].split()
        cpu = (int(f[11]) + int(f[12])) / clk
    except (OSError, IndexError, ValueError):
        cpu = -1.0
    rss = -1
    try:
        for line in _read(f"{root}/self/status").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
    except OSError:
        pass
    return cpu, rss


def format_sample(s: Sample, t0: float) -> str:
    l1, l5, l15, run, tot, _ = s.load
    c = s.cpu_pct
    head = (f"t{s.t - t0:+8.0f}s {time.strftime('%H:%M:%S', time.gmtime(s.t))}Z "
            f"load {l1:.2f}/{l5:.2f}/{l15:.2f} run {s.running} blk {s.blocked} "
            f"forks +{s.forks} ctxt +{s.ctxt} memavail {s.memavail_kb // 1024} MB fds {s.file_nr} "
            f"mmc wr +{s.disk_write_ms} ms io +{s.disk_io_ms} ms"
            + (f" | cpu user {c.get('user', 0):.0f}% sys {c.get('system', 0):.0f}% iow {c.get('iowait', 0):.0f}% "
               f"sirq {c.get('softirq', 0):.0f}% idle {c.get('idle', 0):.0f}%" if c else "")
            + (" [LEAN]" if s.lean else ""))
    rows = []
    for p in s.procs:
        flags = " RESTARTED" if p.restarted else ""
        act = " ".join(f"{tid}:{st}:{w}" for tid, st, w in p.active_threads) if p.active_threads else ""
        rows.append(f"    {p.pid:>6} {p.name[:28]:<28} {p.cpu_pct:5.1f}% {p.state} thr {p.threads:>3} "
                    f"fds {p.fds:>4} dbus {p.dbus:>3}{flags}{('  ' + act) if act else ''}")
    return head + "\n" + "\n".join(rows)


def write_dump(ring, reason: str, dump_dir: str = DUMP_DIR, keep: int = DUMP_KEEP,
               tails=TAIL_LOGS, tail_globs=TAIL_LOG_GLOBS, deep: bool = True,
               proc_root: str = "/proc", started_at: float = 0.0) -> str:
    os.makedirs(dump_dir, exist_ok=True)
    now = time.time()
    path = os.path.join(dump_dir, time.strftime("dump-%Y%m%dT%H%M%SZ.txt", time.gmtime(now)))
    cpu, rss = self_cost(proc_root)
    up = now - started_at if started_at else 0.0
    out = [f"=== load-forensics dump {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} "
           f"trigger: {reason} ===",
           f"self-cost: {cpu:.2f} s CPU since start (up {up / 3600:.2f} h, "
           f"{(100.0 * cpu / up) if up > 0 else 0:.2f}% avg of one core), RSS {rss} kB",
           f"--- ring: {len(ring)} samples, oldest first (t relative to now) ---"]
    for s in ring:
        out.append(format_sample(s, now))
    if deep:
        out += ["--- dmesg (tail) ---", "\n".join(_run(["dmesg"]).splitlines()[-60:]),
                "--- hciconfig -a ---", _run(["hciconfig", "-a"])]
        for t in list(tails) + [g for pat in tail_globs for g in sorted(glob.glob(pat))]:
            out += [f"--- {t} (tail) ---", _tail_file(t)]
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    # cap the directory
    dumps = sorted(glob.glob(os.path.join(dump_dir, "dump-*.txt")))
    for old in (dumps[:-keep] if len(dumps) > keep else []):
        try:
            os.remove(old)
        except OSError:
            pass
    return path


# ------------------------------------------------------------------- main ---
class Forensics:
    def __init__(self, root: str = "/proc", dump_dir: str = DUMP_DIR, interval: float = INTERVAL_S,
                 th: Optional[Thresholds] = None, sensors_log: str = SENSORS_LOG):
        self.root, self.dump_dir, self.interval = root, dump_dir, interval
        self.th = th or Thresholds()
        self.sampler = Sampler(root)
        self.ring = collections.deque(maxlen=RING_LEN)
        self.event = EventState(self.th)
        self.tail = LogTail(sensors_log, TRIP_LINE)
        self.started_at = time.time()
        self.samples = 0
        self.dumps = 0
        self._stop = False
        self._last_beat = self.started_at

    def step(self, now: Optional[float] = None, deep: bool = True) -> Optional[str]:
        """One sample; returns the dump path if an event opened."""
        now = time.time() if now is None else now
        lean = self.event.active            # tripped: one lean pass, nothing more
        s = self.sampler.sample(lean=lean, now=now)
        self.ring.append(s)
        self.samples += 1
        l1, l5, l15 = s.load[0], s.load[1], s.load[2]
        reason = self.event.update(l1, l5, l15, self.tail.poll(), now)
        path = None
        if reason:
            path = write_dump(self.ring, reason, self.dump_dir, proc_root=self.root,
                              started_at=self.started_at, deep=deep)
            self.dumps += 1
            log.warning("event opened: %s (load %.2f/%.2f/%.2f) -> %s", reason, l1, l5, l15, path)
        if now - self._last_beat >= HEARTBEAT_S:
            cpu, rss = self_cost(self.root)
            up = now - self.started_at
            pct = 100.0 * cpu / up if up > 0 else 0.0
            lvl = logging.WARNING if pct > SELF_COST_WARN_PCT else logging.INFO
            log.log(lvl, "alive: %d samples, %d dumps, self-cost %.2f s CPU (%.2f%% of one core), "
                    "RSS %d kB, load %.2f/%.2f/%.2f",
                    self.samples, self.dumps, cpu, pct, rss, l1, l5, l15)
            self._last_beat = now
        return path

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        log.info("load-forensics started: interval %.0f s, ring %d min, triggers 1m>=%.1f | 5m>=%.1f | "
                 "15m>=%.1f (release 1m<%.1f 5m<%.1f 15m<%.1f), dumps -> %s (keep %d), watch: %s",
                 self.interval, RING_MINUTES, self.th.trip_1m, self.th.trip_5m, self.th.trip_15m,
                 self.th.release_1m, self.th.release_5m, self.th.release_15m, self.dump_dir, DUMP_KEEP,
                 ", ".join(WATCH_LIST))
        next_t = time.monotonic()
        while not self._stop:
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("sample failed; continuing")
            next_t += self.interval
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()   # fell behind (a slow dump); realign, do not burst
        log.info("load-forensics stopping")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=INTERVAL_S)
    ap.add_argument("--dump-dir", default=DUMP_DIR)
    ap.add_argument("--dump-now", action="store_true",
                    help="take a few quick samples and write one dump without a trigger (soak check)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(levelname)s:%(name)s:%(message)s", stream=sys.stdout)
    t15, t5, r15, r5 = sensors_py_thresholds()
    th = Thresholds(trip_5m=t5, trip_15m=t15, release_5m=r5, release_15m=r15)
    fx = Forensics(dump_dir=a.dump_dir, interval=a.interval, th=th)
    if a.dump_now:
        for _ in range(3):
            fx.step()
            time.sleep(2)
        path = write_dump(fx.ring, "manual --dump-now", a.dump_dir, started_at=fx.started_at)
        print(path)
        return 0
    fx.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
