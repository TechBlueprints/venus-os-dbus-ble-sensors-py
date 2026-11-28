#!/usr/bin/env python3
"""Say which threads make up each runnable-count burst within the second.

The load average counts runnable tasks, so a thread's contribution over any
stretch of time is exactly the time it spent running plus the time it spent
waiting on a run queue. The kernel keeps both per thread in
``/proc/<pid>/task/<tid>/schedstat`` (``run_ns wait_ns timeslices``), so there
is nothing to sample and no phase to aim at: read the counters at fixed
boundaries inside the second and the difference between two reads is that
thread's exact runnable time in that window.

Two steps, one process, no forks:

1. census: read every thread's counters, wait CENSUS seconds, read again,
   and keep the TOP threads by runnable time (the report prints how much of
   the box's runnable time they cover);
2. windows: for DURATION seconds read those threads at every WINDOW_MS
   boundary of CLOCK_MONOTONIC and fold the differences by phase.

Sample on the box with nothing written there, report locally:

    ssh cerbo python3 - 30 20 64 < scripts/loadavg_burst_attribution.py > attr.json
    python3 scripts/loadavg_burst_attribution.py --report attr.json [burst_min [rows]]

Cost while the windows run: about 64 small reads every 20 ms, 10-15 % of one
core on a Cerbo GX. The JSON carries the sampler's own start and end in UTC
and its CPU; a load-forensics dump inside that window is measurement-induced.

Caveats that matter when reading the report:
- ``wait_ns`` is credited when the thread finally gets a CPU, and ``run_ns``
  is brought up to date at the scheduler tick (10 ms at HZ=100), so a burst
  smears by up to one tick into the next window.
- The sampler's own thread is excluded from the census.
"""
import gc
import json
import os
import sys
import time


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def _name(pid, tid):
    cmd = _read("/proc/%s/cmdline" % pid).split(b"\0")
    cmd = [c.decode("utf-8", "replace") for c in cmd if c]
    comm = _read("/proc/%s/task/%s/comm" % (pid, tid)).decode("utf-8", "replace").strip()
    if not cmd:
        return "[%s]" % comm           # kernel thread
    words = [os.path.basename(cmd[0])]
    if words[0].startswith("python") and len(cmd) > 1:
        words = []
    for arg in cmd[1:4]:
        if arg.startswith("-") and words:
            continue
        words.append(os.path.basename(arg))
        if len(words) >= 2:
            break
    label = " ".join(words)[:44]
    if tid != pid:
        label += " {%s}" % comm
    return label


def sample(duration, window_ms, top, census):
    me = str(os.getpid())
    threads = []                       # (pid, tid, fd)
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            tids = os.listdir("/proc/%s/task" % pid)
        except OSError:
            continue
        for tid in tids:
            try:
                fd = os.open("/proc/%s/task/%s/schedstat" % (pid, tid), os.O_RDONLY)
            except OSError:
                continue
            threads.append((pid, tid, fd))

    def sweep(fds):
        out = []
        for fd in fds:
            try:
                out.append(os.pread(fd, 64, 0))
            except OSError:
                out.append(b"")
        return out

    mono = time.CLOCK_MONOTONIC
    all_fds = [t[2] for t in threads]
    t0w = time.time()
    c0t = time.clock_gettime(mono)
    c0 = sweep(all_fds)
    time.sleep(census)
    c1t = time.clock_gettime(mono)
    c1 = sweep(all_fds)

    rows = []
    for (pid, tid, fd), a, b in zip(threads, c0, c1):
        a, b = a.split(), b.split()
        if len(a) != 3 or len(b) != 3:
            continue
        run = int(b[0]) - int(a[0])
        wait = int(b[1]) - int(a[1])
        slices = int(b[2]) - int(a[2])
        rows.append((run + wait, run, wait, slices, pid, tid, fd))
    rows.sort(reverse=True)
    census_total = sum(r[0] for r in rows)
    keep = rows[:top]
    for r in rows[top:]:
        os.close(r[6])
    names = [_name(r[4], r[5]) for r in keep]
    fds = [r[6] for r in keep]

    win = window_ms / 1000.0
    cpu0 = time.process_time()
    sweeps = []
    gc.disable()
    start = time.clock_gettime(mono)
    nxt = (int(start / win) + 2) * win
    end = start + duration
    while nxt < end:
        d = nxt - time.clock_gettime(mono)
        if d > 0:
            time.sleep(d)
        t = time.clock_gettime(mono)
        sweeps.append((t, sweep(fds)))
        nxt += win
        now = time.clock_gettime(mono)
        while nxt <= now:               # never chase a missed boundary
            nxt += win
    stop = time.clock_gettime(mono)
    gc.enable()
    cpu = time.process_time() - cpu0
    t1w = time.time()

    nwin = int(round(1.0 / win))
    n = len(fds)
    run = [[0] * nwin for _ in range(n)]
    wait = [[0] * nwin for _ in range(n)]
    slices = [[0] * nwin for _ in range(n)]
    span = [0.0] * nwin                # seconds of wall covered per window
    visits = [0] * nwin
    dropped = 0
    for (ta, a), (tb, b) in zip(sweeps, sweeps[1:]):
        if tb - ta > 1.5 * win:
            dropped += 1
            continue
        w = int((((ta + tb) / 2.0) % 1.0) / win) % nwin
        span[w] += tb - ta
        visits[w] += 1
        for i in range(n):
            x, y = a[i].split(), b[i].split()
            if len(x) != 3 or len(y) != 3:
                continue
            run[i][w] += int(y[0]) - int(x[0])
            wait[i][w] += int(y[1]) - int(x[1])
            slices[i][w] += int(y[2]) - int(x[2])

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    json.dump({
        "start_utc": time.strftime(fmt, time.gmtime(t0w)),
        "end_utc": time.strftime(fmt, time.gmtime(t1w)),
        "wall_minus_mono_frac": (t1w - stop) % 1.0,
        "window_ms": window_ms, "duration": stop - start,
        "sampler_cpu_pct": 100.0 * cpu / (stop - start),
        "threads_total": len(rows), "threads_tracked": n,
        "census_seconds": c1t - c0t,
        "census_runnable_ns_all": census_total,
        "census_runnable_ns_tracked": sum(r[0] for r in keep),
        "census": [[names[i], keep[i][1], keep[i][2], keep[i][3]] for i in range(n)],
        "dropped_intervals": dropped, "visits": visits, "span": span,
        "names": names, "run": run, "wait": wait, "slices": slices,
        "loadavg": _read("/proc/loadavg").decode().strip(),
    }, sys.stdout)


def report(path, burst_min=3.0, show=14):
    d = json.load(open(path))
    nwin = len(d["visits"])
    wms = d["window_ms"]
    names = d["names"]
    n = len(names)
    span = d["span"]
    print("sampler %s .. %s  cpu %.1f%%  loadavg at end %s" % (
        d["start_utc"], d["end_utc"], d["sampler_cpu_pct"], d["loadavg"]))
    print("tracked %d of %d threads = %.1f%% of census runnable time; "
          "census mean runnable %.2f; %d dropped intervals; visits/window %d..%d" % (
              n, d["threads_total"],
              100.0 * d["census_runnable_ns_tracked"] / max(d["census_runnable_ns_all"], 1),
              d["census_runnable_ns_all"] / 1e9 / d["census_seconds"],
              d["dropped_intervals"], min(d["visits"]), max(d["visits"])))
    off = d["wall_minus_mono_frac"]
    # mean runnable count contributed by thread i in window w
    r = [[(d["run"][i][w] + d["wait"][i][w]) / 1e9 / span[w] if span[w] else 0.0
          for w in range(nwin)] for i in range(n)]
    tot = [sum(r[i][w] for i in range(n)) for w in range(nwin)]
    mean = sum(tot) / nwin
    print("\nphase-mean runnable (tracked threads) %.2f" % mean)
    print("\nmono_ms wall_ms runnable")
    for w in range(nwin):
        print("%6d %7d %6.2f %s" % (w * wms, int((w * wms / 1000.0 + off) % 1.0 * 1000),
                                     tot[w], "#" * int(round(tot[w] * 4))))
    # per-process totals (sum threads of one process label)
    def proc(label):
        return label.split(" {")[0]
    print("\nwhole-second mean runnable by thread (top %d)" % show)
    order = sorted(range(n), key=lambda i: -sum(r[i]))
    for i in order[:show]:
        print("  %5.3f  %s" % (sum(r[i]) / nwin, names[i]))
    # bursts: maximal runs of windows above burst_min
    w = 0
    bursts = []
    while w < nwin:
        if tot[w] >= burst_min:
            a = w
            while w < nwin and tot[w] >= burst_min:
                w += 1
            bursts.append((a, w))
        else:
            w += 1
    for a, b in bursts:
        ws = range(a, b)
        peak = max(tot[x] for x in ws)
        print("\nburst mono %d-%d ms (wall %d-%d ms), mean %.2f, peak window %.2f" % (
            a * wms, b * wms, int((a * wms / 1000.0 + off) % 1.0 * 1000),
            int((b * wms / 1000.0 + off) % 1.0 * 1000),
            sum(tot[x] for x in ws) / len(ws), peak))
        print("   runnable   run_ms/s wait_ms/s wakeups/s  thread")
        contrib = []
        for i in range(n):
            c = sum(r[i][x] for x in ws) / len(ws)
            visits = sum(d["visits"][x] for x in ws) / len(ws)
            contrib.append((c, i,
                            sum(d["run"][i][x] for x in ws) / 1e6 / visits,
                            sum(d["wait"][i][x] for x in ws) / 1e6 / visits,
                            sum(d["slices"][i][x] for x in ws) / visits))
        contrib.sort(reverse=True)
        for c, i, rm, wm, sl in contrib[:show]:
            print("   %7.3f   %7.2f  %7.2f   %7.2f   %s" % (c, rm, wm, sl, names[i]))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--report":
        report(sys.argv[2], *(float(x) for x in sys.argv[3:4]),
               *(int(x) for x in sys.argv[4:5]))
    else:
        a = sys.argv[1:]
        sample(float(a[0]) if a else 30.0,
               int(a[1]) if len(a) > 1 else 20,
               int(a[2]) if len(a) > 2 else 64,
               float(a[3]) if len(a) > 3 else 5.0)
