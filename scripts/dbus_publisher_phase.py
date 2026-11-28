#!/usr/bin/env python3
"""Count who publishes com.victronenergy.BusItem signals, and when in the second.

Every velib ``DbusMonitor`` user, every Qt VeQItem consumer and the FlashMQ
D-Bus plugin subscribe to ``ItemsChanged`` / ``PropertiesChanged`` from ALL
senders, so one signal from one service wakes every one of them. The load
average therefore follows the bus-wide signal count, and the bursts in the
phase fold sit where the big periodic publishers emit. This script is one
more such subscriber for a few seconds: it records sender, member, item
count and the CLOCK_MONOTONIC phase of every BusItem signal, then resolves
each sender to a process once at the end.

Run on the box with nothing written there:

    ssh cerbo python3 - 15 < scripts/dbus_publisher_phase.py > pub.json
    python3 scripts/dbus_publisher_phase.py --report pub.json
    python3 scripts/dbus_publisher_phase.py --paths pub.json      # which paths churn

With a second argument it also reads the runnable count (field 4 of
/proc/loadavg) every that-many milliseconds, so the report can average the
runnable count around each signal:

    ssh cerbo python3 - 20 2 < scripts/dbus_publisher_phase.py > pub.json
    python3 scripts/dbus_publisher_phase.py --triggered pub.json

Cost: one process, no forks; it wakes once per signal like any other
subscriber, and at the end makes one GetConnectionUnixProcessID call per
distinct sender. The JSON carries its own start and end in UTC.
"""
import json
import os
import sys
import time


def sample(duration, runnable_ms=0):
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mono = time.CLOCK_MONOTONIC
    events = []                     # (t_mono, sender, member, n_items)

    def on_message(_bus, msg):
        if msg.get_interface() != "com.victronenergy.BusItem":
            return
        member = msg.get_member()
        n = 1
        paths = msg.get_path()
        if member == "ItemsChanged":
            try:
                items = msg.get_args_list()[0]
                n = len(items)
                paths = " ".join(str(k) for k in items)
            except Exception:
                n = -1
                paths = ""
        events.append((time.clock_gettime(mono), msg.get_sender(), member, n, paths))

    bus.add_match_string("type='signal',interface='com.victronenergy.BusItem'")
    bus.add_message_filter(on_message)
    loop = GLib.MainLoop()
    t0w = time.time()
    t0m = time.clock_gettime(mono)
    cpu0 = time.process_time()
    GLib.timeout_add(int(duration * 1000), loop.quit)
    runnable = []                   # (t_mono, running tasks other than us)
    if runnable_ms:
        fd = os.open("/proc/loadavg", os.O_RDONLY)

        def tick():
            raw = os.pread(fd, 64, 0)
            runnable.append((time.clock_gettime(mono),
                             int(raw.split()[3].split(b"/")[0]) - 1))
            return True
        GLib.timeout_add(int(runnable_ms), tick)
    loop.run()
    t1m = time.clock_gettime(mono)
    t1w = time.time()
    cpu = time.process_time() - cpu0
    bus.remove_message_filter(on_message)

    who = {}
    dbus_obj = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
    names_of = {}
    try:
        for name in dbus_obj.ListNames(dbus_interface="org.freedesktop.DBus"):
            name = str(name)
            if name.startswith("com.victronenergy."):
                try:
                    owner = str(dbus_obj.GetNameOwner(name, dbus_interface="org.freedesktop.DBus"))
                except Exception:
                    continue
                names_of.setdefault(owner, []).append(name)
    except Exception:
        pass
    for sender in sorted({e[1] for e in events}):
        try:
            pid = int(dbus_obj.GetConnectionUnixProcessID(
                sender, dbus_interface="org.freedesktop.DBus"))
            cmd = open("/proc/%d/cmdline" % pid, "rb").read().split(b"\0")
            cmd = " ".join(os.path.basename(c.decode("utf-8", "replace")) for c in cmd if c)[:70]
        except Exception:
            pid, cmd = -1, "?"
        who[sender] = [pid, cmd, names_of.get(sender, [])]

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    json.dump({
        "start_utc": time.strftime(fmt, time.gmtime(t0w)),
        "end_utc": time.strftime(fmt, time.gmtime(t1w)),
        "duration": t1m - t0m, "sampler_cpu_pct": 100.0 * cpu / (t1m - t0m),
        "wall_minus_mono_frac": (t1w - t1m) % 1.0,
        "who": who, "events": events, "runnable": runnable,
    }, sys.stdout)


def report(path, bin_ms=20):
    d = json.load(open(path))
    dur = d["duration"]
    ev = d["events"]
    who = d["who"]
    nb = 1000 // bin_ms
    print("sampler %s .. %s  cpu %.1f%%  %d signals in %.1f s = %.1f/s, %.1f items/s" % (
        d["start_utc"], d["end_utc"], d["sampler_cpu_pct"], len(ev), dur,
        len(ev) / dur, sum(max(e[3], 0) for e in ev) / dur))
    off = d["wall_minus_mono_frac"]

    def label(sender):
        pid, cmd, names = who.get(sender, [-1, "?", []])
        name = names[0].replace("com.victronenergy.", "") if names else sender
        return "%s [%s]" % (name, cmd.replace("python3 ", "").replace("-u ", "")[:40])

    per = {}
    for t, sender, member, n, _path in ev:
        p = per.setdefault(sender, {"sig": 0, "items": 0, "bins": [0] * nb})
        p["sig"] += 1
        p["items"] += max(n, 0)
        p["bins"][int((t % 1.0) * nb) % nb] += 1
    # group connections of one process
    proc = {}
    for sender, p in per.items():
        pid = who.get(sender, [-1])[0]
        g = proc.setdefault(pid, {"sig": 0, "items": 0, "bins": [0] * nb, "senders": []})
        g["sig"] += p["sig"]
        g["items"] += p["items"]
        g["senders"].append(sender)
        for i in range(nb):
            g["bins"][i] += p["bins"][i]
    total = [0] * nb
    for g in proc.values():
        for i in range(nb):
            total[i] += g["bins"][i]
    print("\nsignals per second by monotonic phase (%d ms bins; wall = mono + %.3f)" % (bin_ms, off))
    for i in range(nb):
        print("%5d %6.1f %s" % (i * bin_ms, total[i] / dur, "#" * int(round(total[i] / dur * 2))))
    print("\nby process: signals/s  items/s  conns  peak-phase(ms):share  who")
    for pid, g in sorted(proc.items(), key=lambda kv: -kv[1]["sig"]):
        peak = max(range(nb), key=lambda i: g["bins"][i])
        share = g["bins"][peak] / max(g["sig"], 1)
        cmd = who.get(g["senders"][0], [-1, "?", []])[1]
        print("  %6.1f  %7.1f  %3d   %4d:%.2f   pid %s %s" % (
            g["sig"] / dur, g["items"] / dur, len(g["senders"]), peak * bin_ms, share, pid,
            cmd.replace("python3 ", "").replace("-u ", "")[:60]))
    print("\nby connection (top 30): signals/s items/s  peak-phase:share  service")
    for sender, p in sorted(per.items(), key=lambda kv: -kv[1]["sig"])[:30]:
        peak = max(range(nb), key=lambda i: p["bins"][i])
        print("  %6.1f  %7.1f   %4d:%.2f   %s" % (
            p["sig"] / dur, p["items"] / dur, peak * bin_ms,
            p["bins"][peak] / max(p["sig"], 1), label(sender)))


def triggered(path, step_ms=5, before_ms=30, after_ms=100):
    """Runnable count averaged around each signal, per sender.

    The test for "a signal causes a burst": a sender whose signals fall at
    random phases (R near 0) still shows the jump at lag 0, so the burst
    follows the signal and not a place in the second.
    """
    import bisect
    import math
    d = json.load(open(path))
    run = d["runnable"]
    if not run:
        sys.exit("no runnable samples in this capture; sample with a period, e.g. `- 20 2`")
    ts = [r[0] for r in run]
    who = d["who"]
    lags = list(range(-before_ms, after_ms, step_ms))
    base = sum(r[1] for r in run) / len(run)
    print("sampler %s .. %s cpu %.1f%%; %d runnable samples, overall mean %.2f" % (
        d["start_utc"], d["end_utc"], d["sampler_cpu_pct"], len(run), base))

    def window_mean(t, a_ms, b_ms):
        i = bisect.bisect_left(ts, t + a_ms / 1000.0)
        j = bisect.bisect_left(ts, t + b_ms / 1000.0)
        if j <= i:
            return None
        return sum(r[1] for r in run[i:j]) / (j - i)

    per = {}
    for e in d["events"]:
        per.setdefault(e[1], []).append(e[0])
    print("\nlag_ms:" + " " * 40 + "".join("%5d" % x for x in lags))
    for sender, times in sorted(per.items(), key=lambda kv: -len(kv[1])):
        if len(times) < 8:
            continue
        c = sum(math.cos(2 * math.pi * (t % 1.0)) for t in times) / len(times)
        s = sum(math.sin(2 * math.pi * (t % 1.0)) for t in times) / len(times)
        names = who.get(sender, [0, "?", []])[2]
        name = names[0].replace("com.victronenergy.", "") if names else sender
        row = []
        for lag in lags:
            vals = [v for v in (window_mean(t, lag, lag + step_ms) for t in times) if v is not None]
            row.append(sum(vals) / len(vals) if vals else float("nan"))
        print("%-30s n=%3d R=%.2f  %s" % (name[:30], len(times), math.hypot(c, s),
                                          "".join("%5.1f" % v for v in row)))


def paths(path, top=12):
    """Which paths each service keeps changing: the list a publish gate works from."""
    d = json.load(open(path))
    dur = d["duration"]
    who = d["who"]
    per = {}
    for _t, sender, _member, _n, plist in d["events"]:
        p = per.setdefault(sender, {"sig": 0, "paths": {}})
        p["sig"] += 1
        for item in plist.split():
            p["paths"][item] = p["paths"].get(item, 0) + 1
    print("%s .. %s (%.0f s). For each service: signals/s, then how often each path was in a signal"
          % (d["start_utc"], d["end_utc"], dur))
    for sender, p in sorted(per.items(), key=lambda kv: -kv[1]["sig"]):
        pid, cmd, names = who.get(sender, [-1, "?", []])
        name = names[0].replace("com.victronenergy.", "") if names else sender
        print("\n%-40s %.2f signals/s   [%s]" % (name, p["sig"] / dur, cmd[:50]))
        for item, c in sorted(p["paths"].items(), key=lambda kv: -kv[1])[:top]:
            print("    %5.2f/s  %s" % (c / dur, item))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--paths":
        paths(sys.argv[2], *(int(x) for x in sys.argv[3:4]))
    elif len(sys.argv) > 2 and sys.argv[1] == "--report":
        report(sys.argv[2], *(int(x) for x in sys.argv[3:4]))
    elif len(sys.argv) > 2 and sys.argv[1] == "--triggered":
        triggered(sys.argv[2], *(int(x) for x in sys.argv[3:6]))
    else:
        sample(float(sys.argv[1]) if len(sys.argv) > 1 else 15.0,
               float(sys.argv[2]) if len(sys.argv) > 2 else 0)
