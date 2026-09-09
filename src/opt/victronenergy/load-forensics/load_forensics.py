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
    ``/proc/diskstats`` for the eMMC (write and I/O milliseconds); the
    number of connections on the system bus (rows in ``/proc/net/unix``
    bound to the bus socket, minus the listener).
  - Per process: CPU delta (utime+stime), state, thread count.  For the
    top-N by CPU plus a fixed watch list: fd count, an EXACT D-Bus
    connection count (see below), and the wait channel of every thread
    that is running or blocked (a USB/HCI stall names itself there).
  - A watch-list process whose pid changed is flagged: a restart.

The per-process bus count.  ``/proc/net/unix`` prints each socket's OWN
bound path: the bus listener has it, dbus-daemon's accepted sockets
inherit it, and a client's connected socket is unbound and shows nothing.
Matching a process's fd inodes against the path rows therefore credits
every connection to dbus-daemon and none to any client -- the opposite of
the fan-out signature the count exists to show.  The exact rule needs
each socket's PEER inode, which ``/proc`` does not print but the kernel's
socket-diagnostics netlink interface does (``unix_diag`` with
``UDIAG_SHOW_PEER``, no fork): a client socket whose peer is one of the
daemon's accepted sockets is one bus connection.  When that interface is
unavailable the per-process count is reported as unknown, never as zero.

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
import socket
import struct
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
# A SUBDIRECTORY of the service's log directory, not the directory itself.
# On Venus ``/var/log`` is a symlink to ``/data/log``, so the multilog that
# writes this service's own log owns ``/data/log/load-forensics`` -- its
# ``current``, ``state`` and ``lock`` live there.  Writing dumps beside them
# mixes two kinds of artifact in one place and invites a cleanup of "the log
# directory" to take the evidence with it.  multilog ignores subdirectories.
DUMP_DIR = "/data/log/load-forensics/dumps"
# Dumps are kept in two SEPARATE pools, by what opened the event.  On prod
# the early catch fires often -- every pack restart, every GUI session --
# while a real 5m/15m throttle trip is rare and is the whole point.  A single
# pool would let a burst of routine early-catch dumps rotate out the one dump
# that matters, so a "trip" dump is never evicted to make room for an "early"
# one.  The two pools also make the cooldowns independent.
CLASS_EARLY = "early"                               # our own 1-minute crossing
CLASS_TRIP = "trip"                                 # the service's own 5m/15m thresholds
DUMP_KEEP = {CLASS_EARLY: 10, CLASS_TRIP: 10}
# After an early-catch dump, wait before writing another: a 1-minute average
# hovering around the threshold would otherwise open and close an event every
# minute.  A real trip is never cooled down.
COOLDOWN_S = {CLASS_EARLY: 300.0, CLASS_TRIP: 0.0}
TAIL_LINES = 40
# The early catch is RELATIVE to the box's own recent baseline, with an
# absolute floor.  An absolute-only rule measures the machine, not an event:
# prod idles near a 1-minute load of 3 under a charging regime with the GUI
# up, so a fixed 4.0 fired 48 times in 17 hours on excursions of a few
# tenths -- every one of them early-class, none of them anomalies, and the
# ten-deep pool rotated a genuinely interesting one away within hours.  Dev
# idles near 0.3, where the same 4.0 is a real event.  One number cannot be
# right for both, so the bar is "4.0, or 1.5 above the 5-minute average,
# whichever is higher".
TRIGGER_1M = 4.0                                    # absolute floor
TRIGGER_1M_OVER_5M = 1.5                            # ...and this far above the baseline
RELEASE_1M_BELOW_BAR = 1.0                          # hysteresis band under whichever bar applied
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

# Multicast DNS.  Venus's dbus-modbus-client binds this port, joins this
# group, and parses EVERY packet on the LAN with a pure-Python DNS parser.
# Its steady ~0.3-0.4 % of a core is NOT that parsing -- measured on prod
# against an almost silent LAN (0.3 packets/s), it is the process's own
# 100 ms update loop.  Parsing is the SPIKE term: it is what took the same
# process to 21 % of a core during dev's two load events.  So this column
# is the denominator for the spikes, not for the baseline, and it turns
# "modbus-client at 21 %" into "modbus-client at 21 % while mDNS ran at N
# packets/s from host X".
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
MDNS_RCVBUF = 4 * 1024 * 1024      # hold a burst between two 30 s samples
MDNS_DRAIN_CAP = 20000             # bound the work one sample can be handed

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


def _read_fast(path: str, size: int = 4096) -> str:
    """One open/read/close, no buffered-IO layer.

    The per-sample walk reads one or two small files for every task on the
    box (264 on dev), so this is the hot path and the layer is worth
    skipping.  Everything it is used for -- ``stat``, ``cmdline``, ``wchan``
    -- is far under one page.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.read(fd, size).decode("utf-8", "replace")
    finally:
        os.close(fd)


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


def read_disk_ms(root: str = "/proc", device: str = EMMC_DEVICE) -> tuple:
    """(ms writing, weighted ms doing I/O, writes completed, sectors written).

    Time AND volume, because on their own they cannot be told apart.  A
    dev flood showed the write-time column jumping to 272-304 ms per sample
    while the service logs wrote no more than in the quiet minutes before
    it -- so the eMMC was not busier, its completions were simply slower
    behind a loaded CPU.  With only milliseconds recorded, a reader cannot
    distinguish "wrote much more" from "same writes, queued longer", and
    those call for opposite responses.
    """
    for line in _read(f"{root}/diskstats").splitlines():
        f = line.split()
        if len(f) >= 14 and f[2] == device:
            # after major/minor/name: reads(3) merged(4) sectors(5) ms(6)
            # writes(7) merged(8) sectors(9) ms(10) inflight(11) io_ms(12) weighted(13)
            return int(f[10]), int(f[13]), int(f[7]), int(f[9])
    return 0, 0, 0, 0


# /proc/net/unix columns: Num RefCount Protocol Flags Type St Inode Path.
# Flags 0x10000 marks a listening socket (__SO_ACCEPTCON); St 03 is connected.
_UNIX_FLAG_ACCEPTCON = 0x10000
_UNIX_ST_CONNECTED = 3


def read_bus_rows(root: str = "/proc", paths=DBUS_SOCKET_PATHS) -> tuple[set[int], set[int]]:
    """(listener inodes, accepted inodes) of the system bus, from the path rows.

    Only the bus side of each connection carries the path (the listener and
    the daemon's accepted sockets); client sockets are unbound and print no
    path at all, so they are NOT in either set.  ``len(accepted)`` is the
    number of live connections to the bus.
    """
    listeners: set[int] = set()
    accepted: set[int] = set()
    try:
        lines = _read(f"{root}/net/unix").splitlines()[1:]
    except OSError:
        return listeners, accepted
    for line in lines:
        f = line.split()
        if len(f) < 8 or f[7] not in paths:
            continue
        try:
            flags, state, ino = int(f[3], 16), int(f[5], 16), int(f[6])
        except ValueError:
            continue
        if flags & _UNIX_FLAG_ACCEPTCON:
            listeners.add(ino)
        elif state == _UNIX_ST_CONNECTED:
            accepted.add(ino)
    return listeners, accepted


# unix_diag over NETLINK_SOCK_DIAG: each socket's peer inode, without a fork.
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST, _NLM_F_DUMP = 0x01, 0x300
_NLMSG_ERROR, _NLMSG_DONE = 2, 3
_AF_UNIX = 1
_UDIAG_SHOW_PEER = 1 << 2
_UNIX_DIAG_PEER = 2


def parse_unix_diag(data: bytes) -> tuple[bool, bool, dict[int, int]]:
    """Parse one netlink datagram of ``unix_diag`` replies -> (done, error, {inode: peer inode}).

    Messages are ``nlmsghdr`` (16 bytes) + ``unix_diag_msg`` (16 bytes) +
    rtattrs, all 4-byte aligned.  Only ``UNIX_DIAG_PEER`` is read.  An
    ``NLMSG_ERROR`` (EOPNOTSUPP, EINVAL: the interface is not there) is
    reported distinctly from a dump that is simply empty, because "unknown"
    and "none" are different answers.
    """
    peers: dict[int, int] = {}
    off = 0
    done = error = False
    while off + 16 <= len(data):
        ln, typ, _flags, _seq, _pid = struct.unpack_from("=IHHII", data, off)
        if ln < 16 or off + ln > len(data):
            break
        if typ == _NLMSG_DONE:
            done = True
            break
        if typ == _NLMSG_ERROR:
            done = error = True
            break
        body = data[off + 16:off + ln]
        if len(body) >= 16:
            _fam, _typ, _state, _pad, ino, _c0, _c1 = struct.unpack_from("=BBBBIII", body, 0)
            a = 16
            while a + 4 <= len(body):
                rta_len, rta_type = struct.unpack_from("=HH", body, a)
                if rta_len < 4:
                    break
                if rta_type == _UNIX_DIAG_PEER and rta_len >= 8:
                    peers[ino] = struct.unpack_from("=I", body, a + 4)[0]
                a += (rta_len + 3) & ~3
        off += (ln + 3) & ~3
    return done, error, peers


def read_unix_peers() -> Optional[dict[int, int]]:
    """{socket inode: peer inode} for every unix socket via ``unix_diag``.

    ``None`` when the interface is unavailable (no netlink, an OS error, or
    an ``NLMSG_ERROR`` reply); a dict -- possibly empty -- when it answered.
    The request asks for every state (``udiag_states`` all ones): a zero
    mask returns an immediate DONE with no messages, which is not an error.
    """
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    try:
        s.settimeout(1.0)
        # struct unix_diag_req: family u8, protocol u8, pad u16, states u32, ino u32, show u32, cookie u32[2]
        req = struct.pack("=BBHIIIII", _AF_UNIX, 0, 0, 0xFFFFFFFF, 0, _UDIAG_SHOW_PEER, 0, 0)
        hdr = struct.pack("=IHHII", 16 + len(req), _SOCK_DIAG_BY_FAMILY, _NLM_F_REQUEST | _NLM_F_DUMP, 1, 0)
        s.send(hdr + req)
        peers: dict[int, int] = {}
        for _ in range(64):                     # bounded; a full table is a handful of datagrams
            done, error, part = parse_unix_diag(s.recv(65536))
            if error:
                return None
            peers.update(part)
            if done:
                break
        return peers
    except OSError:
        return None
    finally:
        s.close()


class MdnsCounter:
    """Count multicast-DNS packets and bytes.  Never parse them.

    Parsing is the very cost being measured -- it is what takes
    dbus-modbus-client from its 0.3 % idle loop to 21 % of a core during a
    burst -- so doing it here would turn the instrument into the thing it is
    watching.  A parsing census of this same traffic undercounted it about
    fivefold on this hardware, dropping what it could not keep up with,
    which is the same failure the Modbus client pays for in CPU.  We take
    the length and the source address, both of which ``recvfrom`` hands
    over for free.

    The socket is a passive listener: joining a group the host has already
    joined adds no traffic to the network, and nothing is ever sent.  If the
    port cannot be opened the counter reports "unavailable" and the rest of
    the sample is unaffected.
    """

    def __init__(self, sock=None, group: str = MDNS_GROUP, port: int = MDNS_PORT,
                 cap: int = MDNS_DRAIN_CAP):
        self.cap = cap
        self.error: Optional[str] = None
        self.sock = sock
        if sock is None:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, MDNS_RCVBUF)
                except OSError:
                    pass
                s.bind(("", port))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             struct.pack("4sL", socket.inet_aton(group), socket.INADDR_ANY))
                s.setblocking(False)
                self.sock = s
            except OSError as e:
                self.error = repr(e)
                self.sock = None
                if s is not None:
                    try:
                        s.close()          # never leak the half-set-up socket
                    except OSError:
                        pass

    @property
    def available(self) -> bool:
        return self.sock is not None

    def drain(self) -> tuple:
        """(packets, bytes, saturated, top sources) since the last call.

        *saturated* means the drain hit its cap, so the counts are a lower
        bound -- which is itself the signal that a flood is under way.
        """
        if self.sock is None:
            return -1, -1, False, []
        n = nbytes = 0
        src: collections.Counter = collections.Counter()
        saturated = False
        while n < self.cap:
            try:
                pkt, addr = self.sock.recvfrom(9000)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            n += 1
            nbytes += len(pkt)
            src[addr[0]] += 1
        else:
            saturated = True
        return n, nbytes, saturated, src.most_common(3)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


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
    dbus: int = -1              # bus connections; -1 = unknown (peer inodes unavailable)
    active_threads: list = field(default_factory=list)   # (tid, state, wchan) for R/D threads
    restarted: bool = False


_HEX = set("0123456789abcdefABCDEF")


def _disambiguator(rest: list) -> str:
    """A short tag telling two processes of the same script apart.

    On prod both battery packs run ``dbus-serialbattery.py`` and differ only
    in a MAC argument.  Without this they share one name, and the RESTARTED
    signature -- whose whole job is to say a pack was replaced -- cannot say
    WHICH pack, while the restart rule sees one name carrying two pids.
    The last four hex digits of a MAC-shaped argument are enough, and are
    what the logs already call these devices by.
    """
    for a in reversed(rest):
        if not a or a.startswith("-"):
            continue
        compact = a.replace(":", "")
        if len(compact) == 12 and all(c in _HEX for c in compact):
            return compact[-4:].lower()
    return ""


def _proc_name(root: str, pid: int, comm: str) -> str:
    """comm, or for an interpreter the script's basename plus, when two
    processes run the same script, a short tag from its arguments."""
    if not (comm.startswith("python") or comm in ("sh", "bash")):
        return comm
    try:
        argv = _read_fast(f"{root}/{pid}/cmdline").split("\0")
    except OSError:
        return comm
    for i, a in enumerate(argv[1:], start=1):
        if a.endswith(".py") or a.endswith(".sh"):
            base = os.path.basename(a)
            tag = _disambiguator(argv[i + 1:])
            return f"{base}:{tag}" if tag else base
    return comm


def read_proc(root: str, pid: int, name_cache: Optional[dict] = None) -> Optional[Proc]:
    """One task's line.  *name_cache* maps (pid, starttime) -> name.

    A live process never renames itself here, and resolving an interpreter's
    script means a second file read, so the name is resolved once per
    process life.  ``starttime`` is in the key because pids are reused.
    """
    try:
        stat = _read_fast(f"{root}/{pid}/stat")
    except OSError:
        return None
    # comm may contain spaces/parens: split on the LAST ')'
    lp = stat.rfind(")")
    comm = stat[stat.find("(") + 1:lp]
    f = stat[lp + 2:].split()
    # fields after comm: state(0) ppid(1) ... utime(11) stime(12) ... num_threads(17) ... starttime(19)
    try:
        starttime = int(f[19])
        key = (pid, starttime)
        if name_cache is not None and key in name_cache:
            name = name_cache[key]
        else:
            name = _proc_name(root, pid, comm)
            if name_cache is not None:
                name_cache[key] = name
        return Proc(pid=pid, name=name, state=f[0],
                    ticks=int(f[11]) + int(f[12]), starttime=starttime, threads=int(f[17]))
    except (IndexError, ValueError):
        return None


def enrich_proc(root: str, p: Proc, accepted: set[int], peers: Optional[dict[int, int]]) -> None:
    """fd count, exact bus-connection count, and wait channels of running/blocked threads.

    A socket counts as a bus connection when it IS one of the daemon's
    accepted sockets (dbus-daemon itself) or when its PEER is one (every
    client).  Without *peers* the count is unknown (-1), never zero.
    """
    try:
        fds = os.listdir(f"{root}/{p.pid}/fd")
        p.fds = len(fds)
        n = 0
        for fd in fds:
            try:
                tgt = os.readlink(f"{root}/{p.pid}/fd/{fd}")
            except OSError:
                continue
            if not tgt.startswith("socket:["):
                continue
            ino = int(tgt[8:-1])
            if ino in accepted or (peers is not None and peers.get(ino) in accepted):
                n += 1
        p.dbus = n if peers is not None else -1
    except OSError:
        pass
    try:
        for tid in os.listdir(f"{root}/{p.pid}/task"):
            try:
                ts = _read_fast(f"{root}/{p.pid}/task/{tid}/stat")
                st = ts[ts.rfind(")") + 2:].split()[0]
            except (OSError, IndexError):
                continue
            if st in ("R", "D"):
                try:
                    wchan = _read_fast(f"{root}/{p.pid}/task/{tid}/wchan").strip() or "-"
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
    disk_writes: int            # write operations completed, since last sample
    disk_kb: int                # kB written, since last sample
    bus_connections: int        # live connections on the system bus (accepted sockets); -1 unknown
    mdns_pkts: int              # multicast-DNS packets since the last sample; -1 unavailable
    mdns_bytes: int
    mdns_saturated: bool        # the drain hit its cap: the counts are a lower bound
    mdns_top: list              # [(source ip, packets)], at most three
    procs: list                 # of Proc, top-N + watched
    lean: bool = False          # taken while tripped: no fd/wchan/bus work


class Sampler:
    def __init__(self, root: str = "/proc", clk_tck: Optional[int] = None,
                 top_n: int = TOP_N, watch=WATCH_LIST, peers_reader=None, mdns=None):
        # *mdns* is supplied by the caller rather than opened here, so a unit
        # test of the sampler never touches the network.
        self.mdns = mdns
        self.root = root
        self.clk_tck = clk_tck or os.sysconf("SC_CLK_TCK")
        self.top_n = top_n
        self.watch = watch
        self.peers_reader = peers_reader or read_unix_peers
        self._prev_ticks: dict[int, tuple[int, int]] = {}    # pid -> (ticks, starttime)
        self._prev_sys: Optional[SysCounters] = None
        self._prev_disk: Optional[tuple[int, int]] = None
        self._prev_t: Optional[float] = None
        self._watched_pids: dict[str, set] = {}                # name -> live pids (restart detection)
        self._name_cache: dict[tuple, str] = {}                # (pid, starttime) -> name

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
        dwn = (disk[2] - self._prev_disk[2]) if self._prev_disk else 0
        dkb = ((disk[3] - self._prev_disk[3]) // 2) if self._prev_disk else 0   # 512 B sectors

        # one pass over processes
        procs: list[Proc] = []
        cur_ticks: dict[int, tuple[int, int]] = {}
        for entry in os.listdir(self.root):
            if not entry.isdigit():
                continue
            p = read_proc(self.root, int(entry), self._name_cache)
            if p is None:
                continue
            cur_ticks[p.pid] = (p.ticks, p.starttime)
            prev = self._prev_ticks.get(p.pid)
            if prev and prev[1] == p.starttime and dt > 0:
                p.cpu_pct = 100.0 * (p.ticks - prev[0]) / self.clk_tck / dt
            procs.append(p)
        self._prev_ticks = cur_ticks
        # keep the name cache to the live set, so it cannot grow without bound
        live_keys = {(p.pid, p.starttime) for p in procs}
        self._name_cache = {k: v for k, v in self._name_cache.items() if k in live_keys}

        # top-N by cpu, plus everything on the watch list
        procs.sort(key=lambda x: x.cpu_pct, reverse=True)
        chosen = {p.pid: p for p in procs[:self.top_n]}
        for p in procs:
            if is_watched(p.name, self.watch):
                chosen[p.pid] = p
        selected = sorted(chosen.values(), key=lambda x: x.cpu_pct, reverse=True)

        # Restart detection.  A watched name can have several live processes
        # -- sshd has one per connection -- so a NEW pid is not by itself a
        # restart; treating it as one flags every ssh login.  A restart is a
        # REPLACEMENT: this pid is new for the name AND some pid the name had
        # before is now gone.
        live: dict[str, set] = {}
        for p in procs:
            if is_watched(p.name, self.watch):
                live.setdefault(p.name, set()).add(p.pid)
        for p in selected:
            if not is_watched(p.name, self.watch):
                continue
            prev_pids = self._watched_pids.get(p.name)
            if prev_pids and p.pid not in prev_pids and (prev_pids - live.get(p.name, set())):
                p.restarted = True
        self._watched_pids = live

        # Drained even in lean mode: it is a counter, not a parse, and the
        # samples taken DURING an event are exactly the ones whose mDNS rate
        # explains it.  Skipping it would also let the socket buffer overflow
        # and lose the count for those samples.
        mdns_pkts, mdns_bytes, mdns_sat, mdns_top = (
            self.mdns.drain() if self.mdns is not None else (-1, -1, False, []))

        bus_connections = -1
        if not lean:
            _listeners, accepted = read_bus_rows(self.root)
            bus_connections = len(accepted)
            peers = self.peers_reader()              # None = unknown; {} = the kernel answered, none
            for p in selected:
                enrich_proc(self.root, p, accepted, peers)

        self._prev_sys, self._prev_disk, self._prev_t = sysc, disk, now
        return Sample(t=now, load=load, cpu_pct=cpu_pct, forks=forks, ctxt=ctxt,
                      running=sysc.running, blocked=sysc.blocked,
                      memavail_kb=read_memavailable_kb(self.root), file_nr=read_file_nr(self.root),
                      disk_write_ms=dwr, disk_io_ms=dio, disk_writes=dwn, disk_kb=dkb,
                      bus_connections=bus_connections,
                      mdns_pkts=mdns_pkts, mdns_bytes=mdns_bytes,
                      mdns_saturated=mdns_sat, mdns_top=mdns_top,
                      procs=selected, lean=lean)


# --------------------------------------------------------------- triggers ---
@dataclass
class Thresholds:
    trip_1m: float = TRIGGER_1M                     # absolute floor for the early catch
    trip_1m_over_5m: float = TRIGGER_1M_OVER_5M     # ...or this far above the 5-min baseline
    trip_5m: float = 6.0
    trip_15m: float = 5.5
    release_below_bar: float = RELEASE_1M_BELOW_BAR
    release_5m: float = 5.0
    release_15m: float = 5.0

    def early_bar(self, l5: float) -> float:
        """The 1-minute level that counts as an excursion on THIS box."""
        return max(self.trip_1m, l5 + self.trip_1m_over_5m)


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
        self.cls = CLASS_EARLY

    def reasons(self, l1: float, l5: float, l15: float, log_tripped: bool) -> list[str]:
        r = []
        bar = self.th.early_bar(l5)
        if l1 >= bar:
            # say which bound applied, so a dump explains its own trigger
            how = "floor" if bar <= self.th.trip_1m else f"5m+{self.th.trip_1m_over_5m:g}"
            r.append(f"1m>={bar:.2f} ({how})")
        if l5 >= self.th.trip_5m:
            r.append(f"5m>={self.th.trip_5m}")
        if l15 >= self.th.trip_15m:
            r.append(f"15m>={self.th.trip_15m}")
        if log_tripped:
            r.append("sensors-py tripped")
        return r

    @staticmethod
    def classify(reasons: list) -> str:
        """A real threshold trip outranks our own early catch."""
        for r in reasons:
            if r.startswith("5m") or r.startswith("15m") or "tripped" in r:
                return CLASS_TRIP
        return CLASS_EARLY

    def update(self, l1: float, l5: float, l15: float, log_tripped: bool,
               now: float) -> 'Optional[tuple[str, str]]':
        """Returns (reason, class) when a dump should be written; None otherwise.

        A dump is written when an event opens, and once more if an event that
        opened on the early catch ESCALATES into a real threshold trip --
        otherwise the moment the box actually tripped would be the one moment
        never captured, because the event was already open.
        """
        rs = self.reasons(l1, l5, l15, log_tripped)
        if self.active:
            self.peak = tuple(max(a, b) for a, b in zip(self.peak, (l1, l5, l15)))
            if rs and self.cls == CLASS_EARLY and self.classify(rs) == CLASS_TRIP:
                self.cls = CLASS_TRIP
                self.reason = " | ".join(rs)
                self.quiet = 0
                return self.reason, CLASS_TRIP
            # Release under whichever bar applied, minus a hysteresis band --
            # an absolute release floor would almost never be reached on a
            # box whose baseline already sits near it.
            quiet = (l1 < self.th.early_bar(l5) - self.th.release_below_bar
                     and l5 < self.th.release_5m and l15 < self.th.release_15m)
            self.quiet = self.quiet + 1 if quiet else 0
            if self.quiet >= self.release_samples:
                self.active = False
            return None
        if rs:
            self.active, self.quiet, self.opened_at = True, 0, now
            self.cls = self.classify(rs)
            self.peak, self.reason = (l1, l5, l15), " | ".join(rs)
            return self.reason, self.cls
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


def _mdns_text(s: Sample) -> str:
    """The mDNS column: what dbus-modbus-client had to parse this interval."""
    if s.mdns_pkts < 0:
        return ""
    out = f" mdns +{s.mdns_pkts} pkt/{s.mdns_bytes // 1024} kB"
    if s.mdns_saturated:
        out += " SATURATED"
    if s.mdns_top:
        out += " from " + ",".join(f"{ip}={n}" for ip, n in s.mdns_top)
    return out


def format_sample(s: Sample, t0: float) -> str:
    l1, l5, l15, run, tot, _ = s.load
    c = s.cpu_pct
    head = (f"t{s.t - t0:+8.0f}s {time.strftime('%H:%M:%S', time.gmtime(s.t))}Z "
            f"load {l1:.2f}/{l5:.2f}/{l15:.2f} run {s.running} blk {s.blocked} "
            f"forks +{s.forks} ctxt +{s.ctxt} memavail {s.memavail_kb // 1024} MB fds {s.file_nr} "
            f"bus {'?' if s.bus_connections < 0 else s.bus_connections}{_mdns_text(s)} "
            f"mmc wr +{s.disk_write_ms} ms/+{s.disk_writes} w/+{s.disk_kb} kB io +{s.disk_io_ms} ms"
            + (f" | cpu user {c.get('user', 0):.0f}% sys {c.get('system', 0):.0f}% iow {c.get('iowait', 0):.0f}% "
               f"sirq {c.get('softirq', 0):.0f}% idle {c.get('idle', 0):.0f}%" if c else "")
            + (" [LEAN]" if s.lean else ""))
    rows = []
    for p in s.procs:
        flags = " RESTARTED" if p.restarted else ""
        act = " ".join(f"{tid}:{st}:{w}" for tid, st, w in p.active_threads) if p.active_threads else ""
        dbus = "?" if p.dbus < 0 else str(p.dbus)
        rows.append(f"    {p.pid:>6} {p.name[:28]:<28} {p.cpu_pct:5.1f}% {p.state} thr {p.threads:>3} "
                    f"fds {p.fds:>4} dbus {dbus:>3}{flags}{('  ' + act) if act else ''}")
    return head + "\n" + "\n".join(rows)


def write_dump(ring, reason: str, dump_dir: str = DUMP_DIR, keep: Optional[dict] = None,
               tails=TAIL_LOGS, tail_globs=TAIL_LOG_GLOBS, deep: bool = True,
               proc_root: str = "/proc", started_at: float = 0.0,
               cls: str = CLASS_EARLY) -> str:
    """Write the ring plus a deep snapshot, and cap only *cls*'s own pool."""
    os.makedirs(dump_dir, exist_ok=True)
    now = time.time()
    path = os.path.join(dump_dir, time.strftime(f"dump-%Y%m%dT%H%M%SZ-{cls}.txt", time.gmtime(now)))
    cpu, rss = self_cost(proc_root)
    up = now - started_at if started_at else 0.0
    out = [f"=== load-forensics dump {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} "
           f"class: {cls}  trigger: {reason} ===",
           f"self-cost: {cpu:.2f} s CPU since start (up {up / 3600:.2f} h, "
           f"{(100.0 * cpu / up) if up > 0 else 0:.2f}% avg of one core), RSS {rss} kB",
           f"--- ring: {len(ring)} samples, oldest first (t relative to now); "
           f"dbus '?' = peer inodes unavailable, not zero ---"]
    for s in ring:
        out.append(format_sample(s, now))
    if deep:
        out += ["--- dmesg (tail) ---", "\n".join(_run(["dmesg"]).splitlines()[-60:]),
                "--- hciconfig -a ---", _run(["hciconfig", "-a"])]
        for t in list(tails) + [g for pat in tail_globs for g in sorted(glob.glob(pat))]:
            out += [f"--- {t} (tail) ---", _tail_file(t)]
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")
    # Cap THIS class's pool only, so an early-catch burst can never evict a
    # threshold-trip dump.
    n = (keep or DUMP_KEEP).get(cls, 10)
    mine = sorted(glob.glob(os.path.join(dump_dir, f"dump-*-{cls}.txt")))
    for old in (mine[:-n] if len(mine) > n else []):
        try:
            os.remove(old)
        except OSError:
            pass
    return path


# ------------------------------------------------------------------- main ---
class Forensics:
    def __init__(self, root: str = "/proc", dump_dir: str = DUMP_DIR, interval: float = INTERVAL_S,
                 th: Optional[Thresholds] = None, sensors_log: str = SENSORS_LOG, peers_reader=None,
                 mdns=None):
        self.root, self.dump_dir, self.interval = root, dump_dir, interval
        self.th = th or Thresholds()
        self.mdns = mdns
        self.sampler = Sampler(root, peers_reader=peers_reader, mdns=mdns)
        self.ring = collections.deque(maxlen=RING_LEN)
        self.event = EventState(self.th)
        self.tail = LogTail(sensors_log, TRIP_LINE)
        self.started_at = time.time()
        self.samples = 0
        self.dumps = 0
        self._stop = False
        self._last_beat = self.started_at
        self._last_dump_at: dict = {}          # class -> when we last wrote one

    def step(self, now: Optional[float] = None, deep: bool = True) -> Optional[str]:
        """One sample; returns the dump path if an event opened."""
        now = time.time() if now is None else now
        lean = self.event.active            # tripped: one lean pass, nothing more
        s = self.sampler.sample(lean=lean, now=now)
        self.ring.append(s)
        self.samples += 1
        l1, l5, l15 = s.load[0], s.load[1], s.load[2]
        res = self.event.update(l1, l5, l15, self.tail.poll(), now)
        path = None
        if res:
            reason, cls = res
            since = now - self._last_dump_at.get(cls, 0.0)
            if self._last_dump_at.get(cls) and since < COOLDOWN_S.get(cls, 0.0):
                log.info("event (%s): %s (load %.2f/%.2f/%.2f) -- no dump, %.0f s "
                         "into the %.0f s cooldown for this class",
                         cls, reason, l1, l5, l15, since, COOLDOWN_S[cls])
            else:
                path = write_dump(self.ring, reason, self.dump_dir, proc_root=self.root,
                                  started_at=self.started_at, deep=deep, cls=cls)
                self._last_dump_at[cls] = now
                self.dumps += 1
                log.warning("event (%s): %s (load %.2f/%.2f/%.2f) -> %s",
                            cls, reason, l1, l5, l15, path)
        if now - self._last_beat >= HEARTBEAT_S:
            cpu, rss = self_cost(self.root)
            up = now - self.started_at
            pct = 100.0 * cpu / up if up > 0 else 0.0
            lvl = logging.WARNING if pct > SELF_COST_WARN_PCT else logging.INFO
            log.log(lvl, "alive: %d samples, %d dumps, self-cost %.2f s CPU (%.2f%% of one core), "
                    "RSS %d kB, load %.2f/%.2f/%.2f, bus %s",
                    self.samples, self.dumps, cpu, pct, rss, l1, l5, l15,
                    # '?' not -1: a lean sample during an open event does not
                    # count bus connections, and -1 reads as a failure
                    "?" if s.bus_connections < 0 else s.bus_connections)
            self._last_beat = now
        return path

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        peers_ok = read_unix_peers() is not None
        log.info("load-forensics started: interval %.0f s, ring %d min, triggers "
                 "1m>=max(%.1f, 5m+%.1f) | 5m>=%.1f | "
                 "15m>=%.1f (release 1m below that bar by %.1f, 5m<%.1f 15m<%.1f), dumps -> %s "
                 "(keep %d early / %d trip, separate pools; early cooldown %.0f s), "
                 "per-process bus count via unix_diag: %s, mDNS counter: %s, watch: %s",
                 self.interval, RING_MINUTES, self.th.trip_1m, self.th.trip_1m_over_5m,
                 self.th.trip_5m, self.th.trip_15m,
                 self.th.release_below_bar, self.th.release_5m, self.th.release_15m, self.dump_dir,
                 DUMP_KEEP[CLASS_EARLY], DUMP_KEEP[CLASS_TRIP], COOLDOWN_S[CLASS_EARLY],
                 "available" if peers_ok else "UNAVAILABLE (reported as '?')",
                 "listening on %s:%d" % (MDNS_GROUP, MDNS_PORT) if (self.mdns and self.mdns.available)
                 else "unavailable (%s)" % (getattr(self.mdns, "error", "not enabled")),
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
    # Opened here, not in a constructor, so every unit test stays off the network.
    mdns = MdnsCounter()
    if not mdns.available:
        log.warning("mDNS counter unavailable (%s); the modbus-client load "
                    "signature will have no packet-rate column", mdns.error)
    fx = Forensics(dump_dir=a.dump_dir, interval=a.interval, th=th, mdns=mdns)
    if a.dump_now:
        for _ in range(3):
            fx.step()
            time.sleep(2)
        # its own class, so a soak check never evicts a real event's dump
        path = write_dump(fx.ring, "manual --dump-now", a.dump_dir,
                          started_at=fx.started_at, cls="manual")
        print(path)
        return 0
    fx.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
