#!/usr/bin/env python3
"""Each thread's share of the runnable count, by sampling its state.

``scripts/loadavg_burst_attribution.py`` reads exact per-thread counters, but
on Linux 6.12 the run-queue wait counter over-counts for some threads (tens
of seconds of "wait" for a thread that is in state R in 0.2 % of samples).
This is the slower, unbiased cross-check: sample the state of the busiest
threads at a fixed rate and report the fraction of samples each spent in
state R. Summed over threads that is the mean runnable count, the same
quantity the 15-minute load average converges to.

    ssh cerbo python3 - 40 48 0.05 < scripts/loadavg_state_share.py

Arguments: seconds, threads to follow, sampling period. One process, no
forks; about 150 us per thread per sample (48 threads at 20 Hz is ~15 % of
one core). The sampler's own thread is excluded.
"""
import os
import sys
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0
TOP = int(sys.argv[2]) if len(sys.argv) > 2 else 48
PERIOD = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05
CENSUS = 3.0


def name(pid, tid):
    try:
        cmd = open("/proc/%s/cmdline" % pid, "rb").read().split(b"\0")
        comm = open("/proc/%s/task/%s/comm" % (pid, tid)).read().strip()
    except OSError:
        return "?"
    cmd = [os.path.basename(c.decode("utf-8", "replace")) for c in cmd if c]
    if not cmd:
        return "[%s]" % comm
    words = [w for w in cmd if not w.startswith("-") and not w.startswith("python")][:2]
    label = " ".join(words)[:46] or cmd[0]
    return label + (" {%s}" % comm if tid != pid else "")


def main():
    me = str(os.getpid())
    threads = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            tids = os.listdir("/proc/%s/task" % pid)
        except OSError:
            continue
        for tid in tids:
            try:
                threads.append((pid, tid, os.open("/proc/%s/task/%s/schedstat" % (pid, tid), os.O_RDONLY)))
            except OSError:
                pass

    def snap():
        out = []
        for _, _, fd in threads:
            try:
                f = os.pread(fd, 64, 0).split()
                out.append((int(f[0]), int(f[2])))
            except (OSError, IndexError, ValueError):
                out.append((0, 0))
        return out

    a = snap()
    time.sleep(CENSUS)
    b = snap()
    rank = sorted(range(len(threads)), key=lambda i: -(b[i][0] - a[i][0]))
    total_run = sum(y[0] - x[0] for x, y in zip(a, b))
    keep = [i for i in rank[:TOP] if b[i][1] != a[i][1]]
    covered = sum(b[i][0] - a[i][0] for i in keep)
    fds, names = [], []
    for i in keep:
        pid, tid, _ = threads[i]
        try:
            fds.append(os.open("/proc/%s/task/%s/stat" % (pid, tid), os.O_RDONLY))
            names.append(name(pid, tid))
        except OSError:
            pass
    for _, _, fd in threads:
        os.close(fd)
    la = os.open("/proc/loadavg", os.O_RDONLY)
    hits = [0] * len(fds)
    kernel = 0
    counted = 0
    n = 0
    mono = time.CLOCK_MONOTONIC
    t0w = time.time()
    t0 = time.clock_gettime(mono)
    cpu0 = time.process_time()
    nxt = t0
    while nxt - t0 < DURATION:
        d = nxt - time.clock_gettime(mono)
        if d > 0:
            time.sleep(d)
        kernel += int(os.pread(la, 64, 0).split()[3].split(b"/")[0]) - 1
        for i, fd in enumerate(fds):
            try:
                st = os.pread(fd, 400, 0)
            except OSError:
                continue
            if st[st.rindex(b")") + 2:st.rindex(b")") + 3] == b"R":
                hits[i] += 1
                counted += 1
        n += 1
        nxt += PERIOD
        now = time.clock_gettime(mono)
        while nxt <= now:
            nxt += PERIOD
    dur = time.clock_gettime(mono) - t0
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    print("start %s end %s; %d samples of %d threads (%.0f%% of census CPU time); sampler cpu %.1f%%" % (
        time.strftime(fmt, time.gmtime(t0w)), time.strftime(fmt, time.gmtime()), n, len(fds),
        100.0 * covered / max(total_run, 1), 100.0 * (time.process_time() - cpu0) / dur))
    print("mean threads in state R %.2f; mean kernel count at the same instants %.2f; loadavg %s" % (
        counted / n, kernel / n, os.pread(la, 64, 0).decode().strip()))
    print("share_of_R  R_fraction  thread")
    for h, nm in sorted(zip(hits, names), reverse=True):
        if h:
            print("  %5.1f%%     %.3f     %s" % (100.0 * h / max(counted, 1), h / n, nm))


main()
