#!/usr/bin/env python3
"""Compare the kernel's runnable count with the threads actually in state R.

Field 4 of /proc/loadavg is the sum of every run queue's ``nr_running``, the
same number the load average is built from. This reads it, immediately reads
the state of every recently active thread, then reads it again, and reports
how the two compare when the count is high.

If the kernel counts N tasks while only a few threads are in state R both
before and after, the run queues are holding tasks that have already gone
back to sleep. Linux 6.12's EEVDF scheduler does exactly that ("delayed
dequeue": a task that blocks with negative lag stays on the run queue until
the lag is repaid or the CPU goes idle), and ``nr_running`` still counts it.

    ssh cerbo python3 - 20 < scripts/loadavg_runnable_vs_state.py

One process, no forks. The census costs one pass over /proc; after that each
trigger reads only the threads that ran during the census.
"""
import os
import sys
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
TRIGGER = int(sys.argv[2]) if len(sys.argv) > 2 else 6
CENSUS = 3.0


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
                threads.append((os.open("/proc/%s/task/%s/schedstat" % (pid, tid), os.O_RDONLY),
                                "/proc/%s/task/%s/stat" % (pid, tid)))
            except OSError:
                pass

    def counts():
        out = []
        for fd, _ in threads:
            try:
                out.append(int(os.pread(fd, 64, 0).split()[2]))
            except (OSError, IndexError, ValueError):
                out.append(-1)
        return out

    a = counts()
    time.sleep(CENSUS)
    b = counts()
    active = []
    for (fd, stat), x, y in zip(threads, a, b):
        os.close(fd)
        if y != x:
            try:
                active.append(os.open(stat, os.O_RDONLY))
            except OSError:
                pass
    la = os.open("/proc/loadavg", os.O_RDONLY)

    def running():
        return int(os.pread(la, 64, 0).split()[3].split(b"/")[0]) - 1

    mono = time.CLOCK_MONOTONIC
    t0w = time.time()
    t0 = time.clock_gettime(mono)
    rows = []          # (kernel before, threads in R, kernel after, ms taken)
    polls = 0
    while time.clock_gettime(mono) - t0 < DURATION:
        polls += 1
        n1 = running()
        if n1 < TRIGGER:
            time.sleep(0.002)
            continue
        ta = time.clock_gettime(mono)
        r = 0
        for fd in active:
            try:
                st = os.pread(fd, 400, 0)
            except OSError:
                continue
            if st[st.rindex(b")") + 2:st.rindex(b")") + 3] == b"R":
                r += 1
        n2 = running()
        rows.append((n1, r, n2, (time.clock_gettime(mono) - ta) * 1000.0))
        time.sleep(0.02)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    print("start %s end %s; %d of %d threads active in a %.0f s census; %d polls, %d triggers at kernel count >= %d" % (
        time.strftime(fmt, time.gmtime(t0w)), time.strftime(fmt, time.gmtime()),
        len(active), len(threads), CENSUS, polls, len(rows), TRIGGER))
    if not rows:
        return
    n = len(rows)
    print("mean kernel count before %.2f, threads in state R %.2f, kernel count after %.2f, read took %.1f ms" % (
        sum(r[0] for r in rows) / n, sum(r[1] for r in rows) / n,
        sum(r[2] for r in rows) / n, sum(r[3] for r in rows) / n))
    held = [r for r in rows if r[2] >= TRIGGER]
    if held:
        print("triggers where the kernel count was still >= %d afterwards: %d; there, kernel before %.2f, in R %.2f, after %.2f" % (
            TRIGGER, len(held), sum(r[0] for r in held) / len(held),
            sum(r[1] for r in held) / len(held), sum(r[2] for r in held) / len(held)))
    print("first 25 triggers (before, R, after, ms): " + " ".join(
        "%d/%d/%d/%.0f" % r for r in rows[:25]))


main()
