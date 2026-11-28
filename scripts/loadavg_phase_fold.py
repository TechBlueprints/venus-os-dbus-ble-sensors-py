#!/usr/bin/env python3
"""Fold the kernel's runnable-task count by phase within the second.

Reads field 4 of /proc/loadavg (``running/total``) every ~2 ms for a short
window and bins the running count by where the read fell inside the second,
on two clocks: wall (CLOCK_REALTIME) and CLOCK_MONOTONIC. The sampler itself
is always running at the instant it reads, so one is subtracted.

Why two clocks: GLib's ``g_timeout_add_seconds`` and Qt's coarse timers align
to the MONOTONIC clock, and the kernel's load sampler runs on the tick, which
is also monotonic. The wall phase is the monotonic phase plus a constant that
only moves when the clock is stepped, so a burst that is sharp on one clock
is sharp on both; the monotonic column is the one that stays put.

Run it on the box with nothing written there:

    ssh cerbo python3 - 20 < scripts/loadavg_phase_fold.py

Cost: one process, no forks, about 5 % of one core while it runs. It prints
its own start and end in UTC; any load-forensics dump inside that window is
measurement-induced (docs/LOAD-AVERAGE-BURSTS.md).
"""
import os
import sys
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
PERIOD = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
BINS = 100  # 10 ms


def main():
    fd = os.open("/proc/loadavg", os.O_RDONLY)
    wall = [[0, 0, 0, 0] for _ in range(BINS)]   # n, sum, n>=4, max
    mono = [[0, 0, 0, 0] for _ in range(BINS)]
    mono_clock = time.CLOCK_MONOTONIC
    t0w = time.time()
    t0m = time.clock_gettime(mono_clock)
    cpu0 = time.process_time()
    end = t0m + DURATION
    n = 0
    total = 0
    while True:
        m = time.clock_gettime(mono_clock)
        if m >= end:
            break
        w = time.time()
        raw = os.pread(fd, 64, 0)
        run = int(raw.split()[3].split(b"/")[0]) - 1
        if run < 0:
            run = 0
        for table, t in ((wall, w), (mono, m)):
            b = table[int((t % 1.0) * BINS)]
            b[0] += 1
            b[1] += run
            if run >= 4:
                b[2] += 1
            if run > b[3]:
                b[3] = run
        n += 1
        total += run
        time.sleep(PERIOD)
    t1w = time.time()
    t1m = time.clock_gettime(mono_clock)
    cpu = time.process_time() - cpu0
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    print("start %s end %s samples %d mean_runnable %.2f sampler_cpu %.1f%%" % (
        time.strftime(fmt, time.gmtime(t0w)), time.strftime(fmt, time.gmtime(t1w)),
        n, total / max(n, 1), 100.0 * cpu / (t1m - t0m)))
    print("wall_minus_mono_frac start %.4f end %.4f" % (
        (t0w - t0m) % 1.0, (t1w - t1m) % 1.0))
    print("loadavg %s" % os.pread(fd, 64, 0).decode().strip())
    print("bin_ms  wall: n mean P(>=4) max   |  mono: n mean P(>=4) max")
    for i in range(BINS):
        a, b = wall[i], mono[i]
        print("%4d    %4d %5.2f %4.2f %3d      |  %4d %5.2f %4.2f %3d" % (
            i * 10,
            a[0], a[1] / max(a[0], 1), a[2] / max(a[0], 1), a[3],
            b[0], b[1] / max(b[0], 1), b[2] / max(b[0], 1), b[3]))


main()
