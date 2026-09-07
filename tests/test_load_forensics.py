"""load-forensics: the /proc ring, its triggers, and its dumps, against a fake /proc.

/proc does not exist on the machine that runs these tests, so every reader
takes a root directory.  The fake tree below carries exactly the files the
sampler reads, with values chosen so the arithmetic is checkable by hand.

The fake ``/proc/net/unix`` models the REAL kernel semantics, because the
first version of this suite modelled an assumption instead and passed while
the measurement would have read zero on the box: the path column is a
socket's OWN bound address, so only the listener and the daemon's accepted
sockets carry it; client sockets are unbound and print no path.  Per-process
attribution needs each socket's peer, which comes from ``unix_diag`` and is
injected here as a plain dict.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import sys

import pytest

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy", "load-forensics"))

BUS = "/var/run/dbus/system_bus_socket"


@pytest.fixture(scope="module")
def lf():
    spec = importlib.util.spec_from_file_location("load_forensics", os.path.join(SRC, "load_forensics.py"))
    m = importlib.util.module_from_spec(spec)
    # Register before executing: the module's dataclasses carry string
    # annotations (future annotations), and dataclasses resolves those by
    # looking the module up in sys.modules -- an unregistered module gets
    # None there and the import dies with AttributeError.
    sys.modules["load_forensics"] = m
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------ fake /proc ---
def _stat_line(pid, comm, state, utime, stime, threads, starttime):
    # fields after comm, 1-based in proc(5): state(3) ppid(4) ... utime(14) stime(15) ... threads(20) ... starttime(22)
    f = [state, "1", "1", "0", "-1", "0", "0", "0", "0", "0", "0", str(utime), str(stime),
         "0", "0", "20", "0", str(threads), "0", str(starttime), "0", "0"]
    return f"{pid} ({comm}) " + " ".join(f) + "\n"


# The bus, as the kernel prints it.  Listener 10 (flags 00010000 = accepting,
# St 01).  Accepted (server-side) sockets 11..13 carry the path, St 03.  The
# clients' own sockets 21..23 are unbound: NO path.  Their peers are the
# accepted sockets -- knowledge that lives in unix_diag, not /proc/net/unix.
UNIX_ROWS = [
    ("00010000", "01", 10, BUS),
    ("00000000", "03", 11, BUS), ("00000000", "03", 12, BUS), ("00000000", "03", 13, BUS),
    ("00000000", "03", 21, ""), ("00000000", "03", 22, ""), ("00000000", "03", 23, ""),
    ("00000000", "03", 99, "/tmp/other.sock"),
]
PEERS = {21: 11, 11: 21, 22: 12, 12: 22, 23: 13, 13: 23}


def make_proc(root, procs, *, load="0.50 0.60 0.70 2/300 4242", cpu=(100, 0, 50, 800, 10, 2, 8, 0),
              ctxt=1000, processes=500, running=2, blocked=0, memavail=400000, file_nr=4500,
              disk=(100, 200), unix_rows=UNIX_ROWS):
    """procs: list of dicts {pid, comm, state, utime, stime, threads, starttime, cmdline?, fds?, tasks?}.

    Rebuilds the whole tree from scratch, so calling it again between two
    samples models the next instant: changed counters, a pid that went
    away, a new one that appeared.
    """
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(f"{root}/sys/fs", exist_ok=True)
    os.makedirs(f"{root}/net", exist_ok=True)
    os.makedirs(f"{root}/self", exist_ok=True)
    open(f"{root}/loadavg", "w").write(load + "\n")
    open(f"{root}/stat", "w").write(
        "cpu " + " ".join(str(x) for x in cpu) + "\n"
        f"ctxt {ctxt}\nprocesses {processes}\nprocs_running {running}\nprocs_blocked {blocked}\n")
    open(f"{root}/meminfo", "w").write(f"MemTotal: 1000000 kB\nMemAvailable: {memavail} kB\n")
    open(f"{root}/sys/fs/file-nr", "w").write(f"{file_nr}\t0\t92824\n")
    open(f"{root}/diskstats", "w").write(
        f" 179 0 mmcblk1 10 0 100 5 20 0 200 {disk[0]} 0 300 {disk[1]}\n"
        " 179 1 mmcblk1p1 1 0 1 1 1 0 1 1 0 1 1\n")
    rows = ["Num RefCount Protocol Flags Type St Inode Path"]
    for flags, st, ino, path in unix_rows:
        rows.append(f"0000: 00000003 00000000 {flags} 0001 {st} {ino} {path}".rstrip())
    open(f"{root}/net/unix", "w").write("\n".join(rows) + "\n")
    open(f"{root}/self/stat", "w").write(_stat_line(999, "load_forensics", "S", 5, 3, 1, 10))
    open(f"{root}/self/status", "w").write("Name:\tpython3\nVmRSS:\t9000 kB\n")
    for p in procs:
        d = f"{root}/{p['pid']}"
        os.makedirs(f"{d}/fd", exist_ok=True)
        open(f"{d}/stat", "w").write(_stat_line(p["pid"], p["comm"], p.get("state", "S"), p["utime"],
                                                p["stime"], p.get("threads", 1), p.get("starttime", 100)))
        open(f"{d}/cmdline", "w").write(p.get("cmdline", p["comm"]).replace(" ", "\0") + "\0")
        for i, tgt in enumerate(p.get("fds", [])):
            os.symlink(tgt, f"{d}/fd/{i}")
        for tid, st, wchan in p.get("tasks", [(p["pid"], p.get("state", "S"), "do_epoll_wait")]):
            os.makedirs(f"{d}/task/{tid}", exist_ok=True)
            open(f"{d}/task/{tid}/stat", "w").write(_stat_line(tid, p["comm"], st, 0, 0, 1, 100))
            open(f"{d}/task/{tid}/wchan", "w").write(wchan)


def _procs_v1():
    return [
        # a client of the bus: two of its sockets (21, 22) are connected to the daemon; 99 is not
        dict(pid=100, comm="python3", cmdline="python3 /data/apps/x/dbus_ble_sensors.py", utime=100, stime=50,
             threads=6, fds=["socket:[21]", "socket:[22]", "/dev/null", "socket:[99]"],
             tasks=[(100, "S", "do_epoll_wait"), (101, "D", "usb_start_wait_urb")]),
        dict(pid=200, comm="bluetoothd", utime=10, stime=5, threads=1, fds=["socket:[23]"]),
        dict(pid=300, comm="python3", cmdline="python3 /opt/victronenergy/dbus-systemcalc-py/dbus_systemcalc.py",
             utime=1000, stime=100, threads=2),
        dict(pid=400, comm="idle-thing", utime=0, stime=0),
        # the daemon itself: the listener and its three accepted sockets
        dict(pid=837, comm="dbus-daemon", utime=50, stime=50,
             fds=["socket:[10]", "socket:[11]", "socket:[12]", "socket:[13]"]),
    ]


# ------------------------------------------------------------------ tests ---
def test_parsers_read_the_fake_tree(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    assert lf.read_loadavg(root) == (0.5, 0.6, 0.7, 2, 300, 4242)
    s = lf.read_stat(root)
    assert s.cpu == (100, 0, 50, 800, 10, 2, 8, 0) and s.ctxt == 1000 and s.processes == 500
    assert s.running == 2 and s.blocked == 0
    assert lf.read_memavailable_kb(root) == 400000
    assert lf.read_file_nr(root) == 4500
    assert lf.read_disk_ms(root) == (100, 200)


def test_bus_rows_separate_listener_from_accepted_and_ignore_clients(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    listeners, accepted = lf.read_bus_rows(root)
    assert listeners == {10}
    assert accepted == {11, 12, 13}, "the daemon's accepted sockets, and only those"
    assert not ({21, 22, 23} & accepted), "client sockets print no path and must not be counted here"


def test_python_processes_are_named_by_their_script(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    assert lf.read_proc(root, 100).name == "dbus_ble_sensors.py"
    assert lf.read_proc(root, 300).name == "dbus_systemcalc.py"
    assert lf.read_proc(root, 200).name == "bluetoothd"


def test_cpu_delta_between_two_samples_and_pid_reuse_guard(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=1000.0)
    procs = _procs_v1()
    procs[0]["utime"] = 100 + 150       # +150 ticks in 30 s at 100 Hz = 1.5 s = 5 % of one core
    procs[2]["starttime"] = 555         # same pid, NEW starttime: reused pid, delta must not apply
    procs[2]["utime"] = 0
    make_proc(root, procs)
    s = sm.sample(now=1030.0)
    by = {p.pid: p for p in s.procs}
    assert abs(by[100].cpu_pct - 5.0) < 1e-6
    assert by[300].cpu_pct == 0.0, "a reused pid must not inherit the old pid's ticks"


def test_top_n_plus_watchlist_and_restart_flag(lf, tmp_path):
    root = str(tmp_path / "proc")
    procs = _procs_v1()
    # 12 busy strangers so the top-N is full of them; the watched ones must still appear
    for i in range(12):
        procs.append(dict(pid=1000 + i, comm=f"busy{i}", utime=10, stime=0))
    make_proc(root, procs)
    sm = lf.Sampler(root, clk_tck=100, top_n=3, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    for p in procs:
        if p["comm"].startswith("busy"):
            p["utime"] += 500
    make_proc(root, procs)
    s = sm.sample(now=30.0)
    names = {p.name for p in s.procs}
    assert "bluetoothd" in names and "dbus_ble_sensors.py" in names and "dbus_systemcalc.py" in names
    assert sum(1 for p in s.procs if p.name.startswith("busy")) == 3, "top-N is exactly N strangers"
    # restart: the watched sensors process comes back as a new pid
    procs = [p for p in procs if p["pid"] != 100]
    procs.append(dict(pid=100_000, comm="python3", cmdline="python3 /x/dbus_ble_sensors.py", utime=1, stime=1))
    make_proc(root, procs)
    s = sm.sample(now=60.0)
    assert any(p.name == "dbus_ble_sensors.py" and p.restarted for p in s.procs)


def test_bus_connections_are_attributed_to_clients_by_peer(lf, tmp_path):
    """The bug the first suite hid: with the path rule every client read 0."""
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS).sample(now=0.0)
    by = {p.pid: p for p in s.procs}
    assert by[100].fds == 4 and by[100].dbus == 2, "two client sockets whose peers are accepted bus sockets"
    assert by[200].dbus == 1
    assert by[837].dbus == 3, "the daemon counts its accepted sockets (the listener is not a connection)"
    assert by[300].dbus == 0, "a process with no bus socket really is zero"
    assert s.bus_connections == 3, "global: accepted rows, listener excluded"


def test_without_peer_inodes_the_count_is_unknown_not_zero(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: {}).sample(now=0.0)
    by = {p.pid: p for p in s.procs}
    assert by[100].dbus == -1 and by[200].dbus == -1 and by[837].dbus == -1
    assert s.bus_connections == 3, "the global count needs no peers"
    text = lf.format_sample(s, 0.0)
    assert "dbus   ?" in text and "dbus   0" not in text, "rendered as '?', never as a false zero"


def test_unix_diag_reply_parser(lf):
    """A hand-built netlink datagram: one unix_diag_msg with a PEER attr, then NLMSG_DONE."""
    def msg(ino, peer):
        body = struct.pack("=BBBBIII", 1, 1, 3, 0, ino, 0, 0)       # unix_diag_msg
        rta = struct.pack("=HHI", 8, lf._UNIX_DIAG_PEER, peer)        # rtattr len=8 type=PEER
        payload = body + rta
        hdr = struct.pack("=IHHII", 16 + len(payload), lf._SOCK_DIAG_BY_FAMILY, 2, 1, 0)
        return hdr + payload
    done_hdr = struct.pack("=IHHII", 16, lf._NLMSG_DONE, 2, 1, 0)
    data = msg(21, 11) + msg(22, 12) + done_hdr
    done, peers = lf.parse_unix_diag(data)
    assert done and peers == {21: 11, 22: 12}
    # a truncated datagram must not raise
    assert lf.parse_unix_diag(data[:20])[1] == {}


def test_blocked_threads_name_their_wait_channel(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS).sample(now=0.0)
    by = {p.pid: p for p in s.procs}
    assert (101, "D", "usb_start_wait_urb") in by[100].active_threads
    assert all(st != "S" for _, st, _ in by[100].active_threads), "sleeping threads are not recorded"


def test_lean_pass_does_nothing_beyond_one_proc_pass(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    calls = []
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: calls.append(1) or PEERS).sample(lean=True, now=0.0)
    assert s.lean and s.bus_connections == -1 and calls == [], "no netlink, no fd walk, no wchan while tripped"
    for p in s.procs:
        assert p.fds == -1 and p.dbus == -1 and p.active_threads == []


def test_event_is_one_dump_with_hysteresis(lf):
    th = lf.Thresholds(trip_1m=4.0, trip_5m=6.0, trip_15m=5.5, release_1m=3.0, release_5m=5.0, release_15m=5.0)
    ev = lf.EventState(th, release_samples=2)
    assert ev.update(1.0, 1.0, 1.0, False, 0) is None
    assert ev.update(4.2, 2.0, 1.5, False, 30) == "1m>=4.0"          # opens
    assert ev.update(5.0, 3.0, 2.0, False, 60) is None                # still open: no second dump
    assert ev.update(2.0, 2.0, 2.0, False, 90) is None and ev.active  # one quiet sample: not yet
    assert ev.update(2.0, 2.0, 2.0, False, 120) is None and not ev.active  # two quiet: closed
    assert ev.update(1.0, 6.5, 1.0, False, 150) == "5m>=6.0"         # a NEW event opens
    assert ev.peak[1] == 6.5


def test_service_trip_line_is_a_trigger(lf):
    ev = lf.EventState(lf.Thresholds())
    assert ev.update(1.0, 1.0, 1.0, True, 0) == "sensors-py tripped"


def test_log_tail_starts_at_end_and_follows_rotation(lf, tmp_path):
    p = tmp_path / "current"
    p.write_text("old line: load_throttle: tripped\n")   # history must NOT replay
    t = lf.LogTail(str(p), "load_throttle: tripped")
    assert t.poll() is False
    with open(p, "a") as f:
        f.write("INFO: load_throttle: tripped now\n")
    assert t.poll() is True
    assert t.poll() is False, "consumed; not re-reported"
    # multilog rotation: a NEW file (new inode) at the same path, read from the start
    p.unlink()
    p.write_text("fresh life: load_throttle: tripped\n")
    assert t.poll() is True


def test_write_dump_caps_the_directory_and_records_self_cost(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    ring = [sm.sample(now=float(i)) for i in range(3)]
    d = str(tmp_path / "dumps")
    for i in range(lf.DUMP_KEEP + 3):
        path = lf.write_dump(ring, f"test {i}", d, deep=False, proc_root=root, started_at=1.0)
        # distinct names: the timestamp resolution is a second
        os.rename(path, os.path.join(d, f"dump-2026{i:04d}T000000Z.txt"))
    remaining = sorted(os.listdir(d))
    assert len(remaining) == lf.DUMP_KEEP, "the directory keeps only the newest N dumps"
    text = open(os.path.join(d, remaining[-1])).read()
    assert "self-cost:" in text and "RSS 9000 kB" in text
    assert "dbus_ble_sensors.py" in text and "usb_start_wait_urb" in text, "the ring and the wait channel are in the dump"
    assert "bus 3" in text


def test_forensics_step_dumps_once_per_event(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1(), load="4.50 2.00 1.50 2/300 1")
    d = str(tmp_path / "dumps")
    fx = lf.Forensics(root=root, dump_dir=d, th=lf.Thresholds(), sensors_log=str(tmp_path / "nolog"),
                      peers_reader=lambda: PEERS)
    path = fx.step(now=1000.0, deep=False)
    assert path and os.path.exists(path) and fx.dumps == 1
    assert fx.step(now=1030.0, deep=False) is None and fx.dumps == 1, "still open: one event, one dump"
    assert fx.ring[-1].lean, "while the event is open, samples are lean"


def test_ring_is_thirty_minutes_of_thirty_second_samples(lf):
    assert lf.RING_LEN == 60 and lf.INTERVAL_S == 30.0 and lf.RING_MINUTES == 30


def test_tail_file_reads_only_the_tail(lf, tmp_path):
    p = tmp_path / "log"
    p.write_text("".join(f"line {i}\n" for i in range(100)))
    out = lf._tail_file(str(p), n=40).splitlines()
    assert len(out) == 40 and out[0] == "line 60" and out[-1] == "line 99"


def _code_strings(src: str) -> list[str]:
    """Every string literal in CODE -- docstrings excluded, since the module's
    own docstring names the forbidden tools while stating the prohibition."""
    import ast
    tree = ast.parse(src)
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_nodes.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc_nodes]


def test_no_forbidden_tools_and_no_forks_in_the_sampling_path(lf):
    src = open(os.path.join(SRC, "load_forensics.py")).read()
    strings = "\n".join(_code_strings(src))
    for bad in ("dbus-send", "busctl", "bluetoothctl", "dbus -y", "pgrep", "pkill"):
        assert bad not in strings, f"{bad} is forbidden in code: it has caused load on this box"
    # subprocess is used only inside _run (dump-time), never in Sampler
    body = src[src.index("class Sampler"):src.index("# --------------------------------------------------------------- triggers")]
    assert "subprocess" not in body and "_run(" not in body
