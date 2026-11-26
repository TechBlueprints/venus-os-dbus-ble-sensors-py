#!/usr/bin/env python3
"""Track one publisher's signals against the instant the kernel samples load.

The kernel samples the runnable count every LOAD_FREQ = 5*HZ+1 ticks (5.01 s
at HZ=100), so its sample instant moves 10 ms later within the second on
every sample: 2.0 ms per second. A publisher on a plain 1000 ms GLib timer
also moves later, because GLib re-arms from the moment of dispatch and rounds
the poll timeout up to a millisecond. If the two rates are close, the kernel
keeps sampling at a nearly fixed offset from that publisher's burst for many
minutes: inside the burst the load average climbs with no change in CPU,
outside it the load average sinks.

This script:
1. finds the kernel's sample instant, by polling /proc/loadavg every 2 ms
   until the averages change (the published figures change 10 ticks after
   the sample is taken; that offset is subtracted), at the start and at the
   end;
2. in between only listens to the signals of ONE sender (match rule with
   ``sender=``), so it wakes about once a second.

    ssh cerbo python3 - 300 com.victronenergy.system < scripts/loadavg_sampler_vs_publisher.py > svp.json
    python3 scripts/loadavg_sampler_vs_publisher.py --report svp.json

With the sender given as ``all`` it listens to every sender instead (it is
then one more subscriber woken by each signal, ~12 times a second on a busy
box) and ``--sampled`` shows what the kernel saw at each of its samples
against the signals emitted just before:

    ssh cerbo python3 - 720 all < scripts/loadavg_sampler_vs_publisher.py > svp_all.json
    python3 scripts/loadavg_sampler_vs_publisher.py --sampled svp_all.json

Cost: about 5 % of a core for at most 6 s at each end, next to nothing in
between (one read of /proc/loadavg every 250 ms).
"""
import json
import os
import sys
import time


def sample(duration, sender):
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    mono = time.CLOCK_MONOTONIC
    fd = os.open("/proc/loadavg", os.O_RDONLY)

    def kernel_update():
        """Monotonic time at which the published averages changed."""
        first = os.pread(fd, 64, 0).split()[:3]
        prev = time.clock_gettime(mono)
        deadline = prev + 6.0
        while True:
            time.sleep(0.002)
            now = time.clock_gettime(mono)
            if os.pread(fd, 64, 0).split()[:3] != first:
                return (prev + now) / 2.0
            if now > deadline:
                return None
            prev = now

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    events = []

    everyone = sender == "all"
    senders = []                    # parallel to events when listening to everyone
    loads = []                      # (t, 1-minute, 5-minute, 15-minute) at each change

    def on_message(_bus, msg):
        if msg.get_interface() == "com.victronenergy.BusItem" and msg.get_member() == "ItemsChanged":
            events.append(time.clock_gettime(mono))
            if everyone:
                senders.append(msg.get_sender())

    def poll_load():
        f = os.pread(fd, 64, 0).split()[:3]
        if not loads or f != loads[-1][1]:
            loads.append((time.clock_gettime(mono), f))
        return True

    t0w = time.time()
    k0 = kernel_update()
    rule = "type='signal',interface='com.victronenergy.BusItem',member='ItemsChanged'"
    if not everyone:
        rule += ",sender='%s'" % sender
    bus.add_match_string(rule)
    bus.add_message_filter(on_message)
    loop = GLib.MainLoop()
    GLib.timeout_add(int(duration * 1000), loop.quit)
    GLib.timeout_add(250, poll_load)
    loop.run()
    bus.remove_message_filter(on_message)
    k1 = kernel_update()
    names = {}
    if everyone:
        o = bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
        for n in o.ListNames(dbus_interface="org.freedesktop.DBus"):
            n = str(n)
            if n.startswith("com.victronenergy."):
                try:
                    names[str(o.GetNameOwner(n, dbus_interface="org.freedesktop.DBus"))] = n
                except Exception:
                    pass
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    json.dump({"start_utc": time.strftime(fmt, time.gmtime(t0w)),
               "end_utc": time.strftime(fmt, time.gmtime()),
               "sender": sender, "kernel_update_start": k0, "kernel_update_end": k1,
               "hz": os.sysconf("SC_CLK_TCK"), "events": events,
               "senders": [names.get(x, x) for x in senders],
               "loads": [(t, [float(x) for x in f]) for t, f in loads]}, sys.stdout)


def report(path):
    d = json.load(open(path))
    hz = d["hz"]
    period = (5 * hz + 1) / float(hz)
    k0, k1 = d["kernel_update_start"], d["kernel_update_end"]
    print("%s .. %s, sender %s, %d signals" % (d["start_utc"], d["end_utc"], d["sender"], len(d["events"])))
    if k0 is None:
        sys.exit("no kernel update seen at the start")
    if k1 is not None:
        n = round((k1 - k0) / period)
        print("kernel updates: %d periods between the two sightings, measured period %.4f s "
              "(LOAD_FREQ predicts %.4f s)" % (n, (k1 - k0) / max(n, 1), period))
    sample0 = k0 - 10.0 / hz          # the fold happens 10 ticks before the update
    ev = d["events"]
    # the sender's main once-a-second train: keep the first signal of each second-long gap
    train = [ev[0]]
    for t in ev[1:]:
        if t - train[-1] > 0.6:
            train.append(t)
    gaps = sorted(b - a for a, b in zip(train, train[1:]) if b - a < 1.5)
    if gaps:
        mean_gap = sum(gaps) / len(gaps)
        print("publisher: %d ticks, mean period %.4f s (median %.4f) = %.2f ms later per second" % (
            len(train), mean_gap, gaps[len(gaps) // 2], (mean_gap - 1.0) * 1000.0 / mean_gap))
    print("kernel sampler: %.2f ms later per second" % ((period / 5 - 1.0) * 1000.0 / (period / 5)))
    print("\n   t_s  kernel_sample_phase  publisher_phase  publisher_minus_kernel_ms")
    step = max(len(train) // 30, 1)
    for i in range(0, len(train), step):
        t = train[i]
        # kernel sample instant nearest before t
        n = int((t - sample0) / period)
        ks = sample0 + n * period
        kphase = ks % 1.0
        delta = ((t % 1.0) - kphase + 0.5) % 1.0 - 0.5
        print("%6.0f        %.3f             %.3f            %+7.0f" % (
            t - train[0], kphase, t % 1.0, delta * 1000.0))


def sampled(path, window_ms=30.0):
    """What the kernel saw at each of its samples, against the signals just before.

    The count the kernel sampled is recovered from consecutive 1-minute
    figures: new = old * e + n * (1 - e), e = 1884/2048. Each sample is then
    labelled by whether a BusItem signal was emitted in the WINDOW before it.
    Needs a capture taken with sender ``all``.
    """
    import bisect
    d = json.load(open(path))
    hz = d["hz"]
    period = (5 * hz + 1) / float(hz)
    e = 1884.0 / 2048.0
    ev, who, loads = d["events"], d.get("senders") or [], d["loads"]
    sample0 = d["kernel_update_start"] - 10.0 / hz
    rows = []
    for (ta, a), (tb, b) in zip(loads, loads[1:]):
        if tb - ta > period * 1.5:
            continue                               # missed an update
        n = (b[0] - a[0] * e) / (1.0 - e)
        k = round((tb - 10.0 / hz - sample0) / period)
        ks = sample0 + k * period
        i = bisect.bisect_right(ev, ks)
        j = bisect.bisect_left(ev, ks - window_ms / 1000.0)
        last = ks - ev[i - 1] if i else None
        rows.append((ks, n, i - j, last, set(who[j:i]) if who else set()))
    print("%s .. %s: %d kernel samples, %d signals (%.1f/s)" % (
        d["start_utc"], d["end_utc"], len(rows), len(ev), len(ev) / max(ev[-1] - ev[0], 1)))
    with_sig = [r[1] for r in rows if r[2] > 0]
    without = [r[1] for r in rows if r[2] == 0]

    def mean(x):
        return sum(x) / len(x) if x else float("nan")
    print("kernel-sampled count, signal in the %.0f ms before the sample: n=%d mean %.2f" % (
        window_ms, len(with_sig), mean(with_sig)))
    print("kernel-sampled count, no signal in that window:               n=%d mean %.2f" % (
        len(without), mean(without)))
    print("all samples: mean %.2f; samples with a signal just before carry %.0f%% of the total" % (
        mean([r[1] for r in rows]), 100.0 * sum(with_sig) / max(sum(r[1] for r in rows), 1e-9)))
    print("\nby time since the last signal before the sample:")
    for lo, hi in ((0, 5), (5, 10), (10, 20), (20, 30), (30, 50), (50, 100), (100, 1e9)):
        v = [r[1] for r in rows if r[3] is not None and lo <= r[3] * 1000.0 < hi]
        print("  %4.0f-%-6s ms  n=%3d  mean sampled count %.2f" % (
            lo, "%.0f" % hi if hi < 1e8 else "inf", len(v), mean(v)))
    if who:
        print("\nby sender present in the window (a sample can count for several):")
        tally = {}
        for r in rows:
            for s in r[4]:
                tally.setdefault(s, []).append(r[1])
        for s, v in sorted(tally.items(), key=lambda kv: -len(kv[1])):
            print("  n=%3d mean %.2f  %s" % (len(v), mean(v), s.replace("com.victronenergy.", "")))
    print("\nsampled count over time (one char per kernel sample, 0-9, + for >=10):")
    print("".join("+" if r[1] >= 9.5 else str(int(max(r[1], 0) + 0.5)) for r in rows))
    print("signal in window (x) or not (.):")
    print("".join("x" if r[2] else "." for r in rows))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--sampled":
        sampled(sys.argv[2], *(float(x) for x in sys.argv[3:4]))
    elif len(sys.argv) > 2 and sys.argv[1] == "--report":
        report(sys.argv[2])
    else:
        sample(float(sys.argv[1]) if len(sys.argv) > 1 else 300.0,
               sys.argv[2] if len(sys.argv) > 2 else "com.victronenergy.system")
