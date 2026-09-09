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
import time

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
    # (ms writing, weighted io ms, writes completed, sectors written)
    assert lf.read_disk_ms(root) == (100, 200, 20, 200)


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
    # restart: the watched sensors process is REPLACED -- old pid gone, new one in its place
    procs = [p for p in procs if p["pid"] != 100]
    procs.append(dict(pid=100_000, comm="python3", cmdline="python3 /x/dbus_ble_sensors.py", utime=1, stime=1))
    make_proc(root, procs)
    s = sm.sample(now=60.0)
    assert any(p.name == "dbus_ble_sensors.py" and p.restarted for p in s.procs)


def test_two_processes_of_one_script_are_told_apart_by_their_mac_argument(lf, tmp_path):
    """Prod runs both packs as `dbus-serialbattery.py HumsiENK_Ble <MAC>`.

    Sharing a name would collapse them into one watched entry, so a restart
    could not say which pack went -- and the restart rule would see one name
    holding two pids.
    """
    root = str(tmp_path / "proc")
    packs = [
        dict(pid=9827, comm="python", utime=1, stime=1,
             cmdline="python /data/apps/dbus-serialbattery/dbus-serialbattery.py HumsiENK_Ble 53:20:B7:D7:F9:E7"),
        dict(pid=10737, comm="python", utime=1, stime=1,
             cmdline="python /data/apps/dbus-serialbattery/dbus-serialbattery.py HumsiENK_Ble AB:80:72:54:E0:B4"),
    ]
    make_proc(root, _procs_v1() + packs)
    names = {p.pid: p.name for p in lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS).sample(now=0.0).procs}
    assert names[9827] == "dbus-serialbattery.py:f9e7"
    assert names[10737] == "dbus-serialbattery.py:e0b4"
    assert names[9827] != names[10737], "two packs must not collapse into one watched name"
    assert lf.is_watched(names[9827]), "the tag must not break watch-list matching"
    # a script with no MAC argument keeps its plain name
    assert names[100] == "dbus_ble_sensors.py"


def test_one_pack_restarting_is_flagged_and_the_other_is_not(lf, tmp_path):
    root = str(tmp_path / "proc")
    pack_a = dict(pid=9827, comm="python", utime=1, stime=1,
                  cmdline="python /x/dbus-serialbattery.py HumsiENK_Ble 53:20:B7:D7:F9:E7")
    pack_b = dict(pid=10737, comm="python", utime=1, stime=1,
                  cmdline="python /x/dbus-serialbattery.py HumsiENK_Ble AB:80:72:54:E0:B4")
    make_proc(root, _procs_v1() + [pack_a, pack_b])
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    pack_b["pid"] = 10800                      # pack B is replaced; pack A untouched
    make_proc(root, _procs_v1() + [pack_a, pack_b])
    s = sm.sample(now=30.0)
    by = {p.name: p for p in s.procs}
    assert by["dbus-serialbattery.py:e0b4"].restarted, "the pack that was replaced"
    assert not by["dbus-serialbattery.py:f9e7"].restarted, "the pack that was not"


def test_a_second_process_of_the_same_name_is_not_a_restart(lf, tmp_path):
    """sshd has one process per connection: a new login must not read as a restart."""
    root = str(tmp_path / "proc")
    procs = _procs_v1() + [dict(pid=2091, comm="sshd", utime=1, stime=1)]
    make_proc(root, procs)
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    # a second connection: the first sshd is still alive
    procs.append(dict(pid=2092, comm="sshd", utime=1, stime=1))
    make_proc(root, procs)
    s = sm.sample(now=30.0)
    assert not any(p.name == "sshd" and p.restarted for p in s.procs), \
        "a new pid alongside a living one is a new connection, not a restart"
    # now the original goes away and only the new one remains: still not a restart
    # for 2092 (it was already known), and 2091 is simply gone
    procs = [p for p in procs if p["pid"] != 2091]
    make_proc(root, procs)
    s = sm.sample(now=60.0)
    assert not any(p.name == "sshd" and p.restarted for p in s.procs)


def test_the_name_cache_is_pruned_to_live_processes(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    assert (100, 100) in sm._name_cache, "an interpreter's script name is cached by (pid, starttime)"
    make_proc(root, [p for p in _procs_v1() if p["pid"] != 100])
    sm.sample(now=30.0)
    assert (100, 100) not in sm._name_cache, "a dead process's entry is dropped, so the cache cannot grow"


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


def test_unavailable_peer_lookup_is_unknown_not_zero(lf, tmp_path):
    """None = the interface refused or is absent.  Never a false zero."""
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: None).sample(now=0.0)
    by = {p.pid: p for p in s.procs}
    assert by[100].dbus == -1 and by[200].dbus == -1 and by[837].dbus == -1
    assert s.bus_connections == 3, "the global count needs no peers"
    text = lf.format_sample(s, 0.0)
    assert "dbus   ?" in text and "dbus   0" not in text, "rendered as '?', never as a false zero"


def test_an_empty_but_successful_dump_is_zero_not_unknown(lf, tmp_path):
    """{} = the kernel answered and there are no peers.  A real zero.

    The two must not be conflated: a zero states mask returns an immediate
    DONE with no messages, which is an answer, while NLMSG_ERROR is not.
    """
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: {}).sample(now=0.0)
    by = {p.pid: p for p in s.procs}
    assert by[100].dbus == 0, "no peer matched, but the lookup worked"
    assert by[837].dbus == 3, "the daemon's own accepted sockets need no peer lookup"


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
    done, error, peers = lf.parse_unix_diag(data)
    assert done and not error and peers == {21: 11, 22: 12}
    # a truncated datagram must not raise
    assert lf.parse_unix_diag(data[:20])[2] == {}
    # NLMSG_ERROR is distinct from an empty dump: the kernel refused, so the
    # answer is "unknown", not "none".  A zero states mask returns an
    # immediate DONE with no messages and must NOT read as an error.
    err_hdr = struct.pack("=IHHII", 16, lf._NLMSG_ERROR, 2, 1, 0)
    assert lf.parse_unix_diag(err_hdr) == (True, True, {})
    assert lf.parse_unix_diag(done_hdr) == (True, False, {})


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
    th = lf.Thresholds()
    ev = lf.EventState(th, release_samples=2)
    assert ev.update(1.0, 1.0, 1.0, False, 0) is None
    r = ev.update(4.2, 2.0, 1.5, False, 30)                           # opens on the floor
    assert r == ("1m>=4.00 (floor)", lf.CLASS_EARLY)
    assert ev.update(5.0, 3.0, 2.0, False, 60) is None                # still open: no second dump
    assert ev.update(1.0, 2.0, 2.0, False, 90) is None and ev.active  # one quiet sample: not yet
    assert ev.update(1.0, 2.0, 2.0, False, 120) is None and not ev.active  # two quiet: closed
    assert ev.update(1.0, 6.5, 1.0, False, 150) == ("5m>=6.0", lf.CLASS_TRIP)   # a NEW event opens
    assert ev.peak[1] == 6.5


def test_the_early_catch_is_relative_to_the_box_it_runs_on(lf):
    """An absolute-only bar measures the machine, not an event.

    Prod idles near a 1-minute load of 3 while charging with the GUI up, so a
    fixed 4.0 fired 48 times in 17 hours on excursions of a few tenths.  Dev
    idles near 0.3, where 4.0 is a real event.  One number cannot serve both.
    """
    ev = lf.EventState(lf.Thresholds())
    # PROD: baseline 3.0, a few tenths over the old fixed bar -> NOT an event
    assert ev.update(4.04, 3.0, 2.8, False, 0) is None, \
        "a tenth above a 3-baseline is the baseline, not an excursion"
    assert ev.update(4.40, 3.0, 2.8, False, 30) is None, "still under 5m+1.5"
    # PROD: a genuine excursion above that same baseline -> an event
    r = ev.update(5.10, 3.0, 2.8, False, 60)
    assert r == ("1m>=4.50 (5m+1.5)", lf.CLASS_EARLY)

    # DEV: a near-idle box, where the absolute floor is what matters
    ev2 = lf.EventState(lf.Thresholds())
    assert ev2.update(2.00, 0.4, 0.4, False, 0) is None
    r2 = ev2.update(4.20, 0.4, 0.4, False, 30)
    assert r2 == ("1m>=4.00 (floor)", lf.CLASS_EARLY), \
        "0.4+1.5 is below the floor, so the floor applies"


def test_release_is_relative_too(lf):
    """An absolute release floor is almost never reached on a box whose
    baseline already sits near it -- the event would never close."""
    ev = lf.EventState(lf.Thresholds(), release_samples=2)
    assert ev.update(5.10, 3.0, 2.8, False, 0) is not None      # opens, bar 4.50
    # back to the box's own baseline: 3.0 < 4.50 - 1.0, so this is quiet
    assert ev.update(3.00, 3.0, 2.8, False, 30) is None and ev.active
    assert ev.update(3.00, 3.0, 2.8, False, 60) is None
    assert not ev.active, "returning to baseline closes the event"


def test_service_trip_line_is_a_trigger(lf):
    ev = lf.EventState(lf.Thresholds())
    assert ev.update(1.0, 1.0, 1.0, True, 0) == ("sensors-py tripped", lf.CLASS_TRIP)


def test_an_early_event_that_becomes_a_real_trip_dumps_again(lf):
    """Otherwise the moment the box actually tripped is the one never captured,
    because the early catch had already opened the event."""
    ev = lf.EventState(lf.Thresholds())
    assert ev.update(4.2, 2.0, 1.0, False, 0) == ("1m>=4.00 (floor)", lf.CLASS_EARLY)
    res = ev.update(4.5, 6.2, 2.0, False, 30)
    assert res is not None and res[1] == lf.CLASS_TRIP, "the escalation must be captured"
    assert ev.update(4.5, 6.3, 2.1, False, 60) is None, "but only once"


def test_classification_puts_a_real_threshold_above_the_early_catch(lf):
    c = lf.EventState.classify
    assert c(["1m>=4.00 (floor)"]) == lf.CLASS_EARLY
    assert c(["1m>=4.50 (5m+1.5)", "5m>=6.0"]) == lf.CLASS_TRIP
    assert c(["1m>=4.50 (5m+1.5)"]) == lf.CLASS_EARLY, "the tag names 5m but the rule is the early one"
    assert c(["15m>=5.5"]) == lf.CLASS_TRIP
    assert c(["sensors-py tripped"]) == lf.CLASS_TRIP


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


def test_write_dump_records_the_ring_and_its_own_cost(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    ring = [sm.sample(now=float(i)) for i in range(3)]
    d = str(tmp_path / "dumps")
    path = lf.write_dump(ring, "5m>=6.0", d, deep=False, proc_root=root, started_at=1.0,
                         cls=lf.CLASS_TRIP)
    assert path.endswith("-trip.txt"), "the class is in the filename, so pools can be capped apart"
    text = open(path).read()
    assert "class: trip" in text and "self-cost:" in text and "RSS 9000 kB" in text
    assert "dbus_ble_sensors.py" in text and "usb_start_wait_urb" in text, "the ring and the wait channel"
    assert "bus 3" in text


def test_an_early_catch_burst_cannot_evict_a_trip_dump(lf, tmp_path):
    """On prod the early catch fires constantly and a real trip is rare.
    One shared pool would rotate away the only dump that matters."""
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    ring = [lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS).sample(now=0.0)]
    d = str(tmp_path / "dumps")
    keep = lf.DUMP_KEEP[lf.CLASS_EARLY]
    trip = lf.write_dump(ring, "5m>=6.0", d, deep=False, proc_root=root, cls=lf.CLASS_TRIP)
    trip_name = "dump-20260101T000000Z-trip.txt"
    os.rename(trip, os.path.join(d, trip_name))
    for i in range(keep + 5):
        p = lf.write_dump(ring, "1m>=4.0", d, deep=False, proc_root=root, cls=lf.CLASS_EARLY)
        os.rename(p, os.path.join(d, f"dump-2026{i:04d}T000000Z-early.txt"))
    names = os.listdir(d)
    assert trip_name in names, "the trip dump survived a burst well past the early cap"
    assert sum(1 for n in names if n.endswith("-early.txt")) <= keep + 1, "the early pool is capped"


def test_early_catch_cooldown_holds_but_a_real_trip_never_waits(lf, tmp_path):
    root = str(tmp_path / "proc")
    d = str(tmp_path / "dumps")
    hot = "4.50 1.00 1.00 2/300 1"
    calm = "0.50 0.50 0.50 2/300 1"
    make_proc(root, _procs_v1(), load=hot)
    fx = lf.Forensics(root=root, dump_dir=d, sensors_log=str(tmp_path / "nolog"),
                      peers_reader=lambda: PEERS)
    assert fx.step(now=1000.0, deep=False), "first early catch dumps"
    make_proc(root, _procs_v1(), load=calm)
    fx.step(now=1030.0, deep=False)
    fx.step(now=1060.0, deep=False)
    assert not fx.event.active, "two quiet samples close it"
    make_proc(root, _procs_v1(), load=hot)
    assert fx.step(now=1090.0, deep=False) is None, "inside the cooldown: event opens, no dump"
    assert fx.dumps == 1
    # a REAL trip during that same cooldown is never withheld
    make_proc(root, _procs_v1(), load="4.50 6.50 2.00 2/300 1")
    assert fx.step(now=1120.0, deep=False), "a threshold trip ignores the early cooldown"
    assert fx.dumps == 2


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


def test_mdns_counter_counts_without_parsing(lf):
    """It must never parse: parsing is the cost being measured."""
    import socket as sk
    rx = sk.socket(sk.AF_INET, sk.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.setblocking(False)
    port = rx.getsockname()[1]
    tx = sk.socket(sk.AF_INET, sk.SOCK_DGRAM)
    c = lf.MdnsCounter(sock=rx)
    assert c.available
    assert c.drain() == (0, 0, False, []), "nothing sent yet"
    for _ in range(3):
        tx.sendto(b"x" * 100, ("127.0.0.1", port))
    tx.sendto(b"y" * 50, ("127.0.0.1", port))
    time.sleep(0.2)
    pkts, nbytes, sat, top = c.drain()
    assert pkts == 4 and nbytes == 350 and not sat
    assert top and top[0][0] == "127.0.0.1" and top[0][1] == 4
    assert c.drain()[0] == 0, "counts are per-interval, not cumulative"
    c.close(); tx.close()
    assert not c.available


def test_mdns_drain_is_capped_and_says_so(lf):
    import socket as sk
    rx = sk.socket(sk.AF_INET, sk.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0)); rx.setblocking(False)
    rx.setsockopt(sk.SOL_SOCKET, sk.SO_RCVBUF, 1 << 20)
    tx = sk.socket(sk.AF_INET, sk.SOCK_DGRAM)
    port = rx.getsockname()[1]
    for _ in range(20):
        tx.sendto(b"z" * 20, ("127.0.0.1", port))
    time.sleep(0.2)
    pkts, _b, sat, _t = lf.MdnsCounter(sock=rx, cap=5).drain()
    assert pkts == 5 and sat, "a flood is bounded, and the lower bound is flagged"
    rx.close(); tx.close()


def test_an_unavailable_mdns_socket_does_not_break_a_sample(lf, tmp_path):
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    # an unjoinable group: deterministic on every platform, unlike a
    # privileged port, which this machine turned out to allow
    c = lf.MdnsCounter(sock=None, group="not-a-multicast-address", port=0)
    assert not c.available and c.error
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS, mdns=c).sample(now=0.0)
    assert s.mdns_pkts == -1 and "mdns" not in lf.format_sample(s, 0.0)


def test_the_mdns_column_reaches_the_sample_and_the_dump(lf, tmp_path):
    class FakeMdns:
        available = True
        def drain(self):
            return 1200, 480000, True, [("10.0.0.5", 900), ("10.0.0.9", 300)]
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS, mdns=FakeMdns()).sample(now=0.0)
    assert (s.mdns_pkts, s.mdns_bytes, s.mdns_saturated) == (1200, 480000, True)
    text = lf.format_sample(s, 0.0)
    assert "mdns +1200 pkt/468 kB SATURATED from 10.0.0.5=900,10.0.0.9=300" in text


def test_mdns_is_counted_even_while_tripped(lf, tmp_path):
    """The samples DURING an event are the ones whose mDNS rate explains it,
    and an undrained socket would overflow and lose exactly those."""
    class FakeMdns:
        available = True
        def drain(self):
            return 7, 700, False, [("10.0.0.5", 7)]
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    s = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS,
                   mdns=FakeMdns()).sample(lean=True, now=0.0)
    assert s.lean and s.mdns_pkts == 7, "counted in lean mode too"
    assert s.bus_connections == -1, "but the expensive work is still skipped"


def test_disk_column_separates_volume_from_latency(lf, tmp_path):
    """A dev flood showed write TIME tripling while the logs wrote no more
    than in the quiet minutes before it.  Time alone cannot tell 'wrote much
    more' from 'same writes, queued longer', and those want opposite fixes."""
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1(), disk=(100, 200))
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    # same number of writes and sectors, but three times the milliseconds:
    # the disk is not busier, its completions are slower
    make_proc(root, _procs_v1(), disk=(400, 900))
    s = sm.sample(now=30.0)
    assert s.disk_write_ms == 300, "time tripled"
    assert s.disk_writes == 0 and s.disk_kb == 0, "no extra writes: this is latency, not volume"
    text = lf.format_sample(s, 0.0)
    assert "mmc wr +300 ms/+0 w/+0 kB" in text, "a reader can see both at once"


def test_disk_column_shows_real_volume_when_there_is_some(lf, tmp_path):
    root = str(tmp_path / "proc")
    procs = _procs_v1()
    make_proc(root, procs, disk=(100, 200))
    sm = lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS)
    sm.sample(now=0.0)
    # 40 more writes, 4096 more sectors = 2048 kB
    open(f"{root}/diskstats", "w").write(
        " 179 0 mmcblk1 10 0 100 5 60 0 4296 150 0 300 250\n")
    s = sm.sample(now=30.0)
    assert s.disk_writes == 40 and s.disk_kb == 2048, "sectors are 512 B"


def test_self_cost_does_not_cry_wolf_on_a_fresh_process(lf, tmp_path):
    """Self-cost is CPU-since-start over uptime, so a young process reads high:
    a prod dump 3.6 min in showed 1.26 %, which is startup amortised, not a
    rate.  Warning on that fires at every restart -- exactly when an operator
    is already looking -- and teaches them to ignore the line that matters."""
    root = str(tmp_path / "proc")
    make_proc(root, _procs_v1())
    d = str(tmp_path / "dumps")
    ring = [lf.Sampler(root, clk_tck=100, peers_reader=lambda: PEERS).sample(now=0.0)]
    young = lf.write_dump(ring, "t", d, deep=False, proc_root=root,
                          started_at=time.time() - 60, cls="manual")
    assert "startup-dominated" in open(young).read(), "a 1-minute-old process says so"
    old = lf.write_dump(ring, "t2", d, deep=False, proc_root=root,
                        started_at=time.time() - 7200, cls="trip")
    assert "startup-dominated" not in open(old).read(), "a settled process does not"
    assert lf.SELF_COST_SETTLE_S == 600.0


def test_ring_is_thirty_minutes_of_thirty_second_samples(lf):
    assert lf.RING_LEN == 60 and lf.INTERVAL_S == 30.0 and lf.RING_MINUTES == 30


def test_dumps_do_not_share_a_directory_with_the_service_log(lf):
    """On Venus /var/log is a symlink to /data/log, so the service's multilog
    owns /data/log/load-forensics.  Dumps must not land among its files."""
    assert lf.DUMP_DIR.rstrip("/") != "/data/log/load-forensics", \
        "that is multilog's directory: its current/state/lock live there"
    assert lf.DUMP_DIR.startswith("/data/log/load-forensics/"), \
        "keep dumps under the tool's own name, in a subdirectory"


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
