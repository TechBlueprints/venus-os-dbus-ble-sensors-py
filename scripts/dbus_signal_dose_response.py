#!/usr/bin/env python3
"""Dose-response: does the runnable count follow the BusItem signal rate?

EXPERIMENT, NOT A MONITOR. It emits ``com.victronenergy.BusItem.ItemsChanged``
signals on the system bus, so it changes the box it runs on for as long as it
runs. Use it on a development GX; on a production one only with the owner's
say-so. Nothing persists: the signals come from an unnamed connection (no
``com.victronenergy.*`` name is claimed, so no service, device instance or
setting is created), and they stop when the script exits.

Every subscriber whose match rule has no ``sender=`` (velib ``DbusMonitor``
users such as systemcalc and vrmlogger, the FlashMQ plugin) is woken by each
signal and discards it. Subscribers with per-sender rules (the Qt apps) are
not, so this understates the cost of a signal from a real service.

It steps through the given rates, DWELL seconds each, and for every step
prints the mean runnable count read from /proc/loadavg field 4 every 2 ms
(minus this process).

    ssh dev-cerbo python3 - 15 0 10 0 30 0 < scripts/dbus_signal_dose_response.py
"""
import os
import sys
import time

import dbus
import dbus.lowlevel
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DWELL = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
RATES = [float(x) for x in sys.argv[2:]] or [0, 10, 0, 30, 0]


def main():
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    loop = GLib.MainLoop()
    fd = os.open("/proc/loadavg", os.O_RDONLY)
    mono = time.CLOCK_MONOTONIC
    state = {"n": 0, "sum": 0, "sent": 0, "seq": 0, "src": None, "step": -1,
             "t0": 0.0, "cpu0": 0.0}
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    print("start %s; dwell %.0f s per step; rates %s" % (
        time.strftime(fmt, time.gmtime()), DWELL, RATES))

    def sample():
        raw = os.pread(fd, 64, 0)
        state["sum"] += max(int(raw.split()[3].split(b"/")[0]) - 1, 0)
        state["n"] += 1
        return True

    def emit():
        state["seq"] += 1
        msg = dbus.lowlevel.SignalMessage("/", "com.victronenergy.BusItem", "ItemsChanged")
        msg.append(dbus.Dictionary({
            "/LoadTest/A": dbus.Dictionary({"Value": dbus.Int32(state["seq"]),
                                            "Text": str(state["seq"])}, signature="sv"),
            "/LoadTest/B": dbus.Dictionary({"Value": dbus.Double(state["seq"] / 10.0),
                                            "Text": "%.1f" % (state["seq"] / 10.0)}, signature="sv"),
            "/LoadTest/C": dbus.Dictionary({"Value": dbus.Int32(-state["seq"]),
                                            "Text": str(-state["seq"])}, signature="sv"),
        }, signature="sa{sv}"), signature="a{sa{sv}}")
        bus.send_message(msg)
        state["sent"] += 1
        return True

    def next_step():
        now = time.clock_gettime(mono)
        if state["step"] >= 0:
            dur = now - state["t0"]
            print("rate %5.1f/s asked, %5.1f/s sent: mean runnable %.2f over %d samples; "
                  "this process %.1f%% cpu" % (
                      RATES[state["step"]], state["sent"] / dur,
                      state["sum"] / max(state["n"], 1), state["n"],
                      100.0 * (time.process_time() - state["cpu0"]) / dur))
            sys.stdout.flush()
        if state["src"]:
            GLib.source_remove(state["src"])
            state["src"] = None
        state["step"] += 1
        if state["step"] >= len(RATES):
            print("end %s" % time.strftime(fmt, time.gmtime()))
            loop.quit()
            return False
        state.update(n=0, sum=0, sent=0, t0=now, cpu0=time.process_time())
        rate = RATES[state["step"]]
        if rate > 0:
            state["src"] = GLib.timeout_add(max(int(1000.0 / rate), 1), emit)
        return True

    GLib.timeout_add(2, sample)
    next_step()
    GLib.timeout_add(int(DWELL * 1000), next_step)
    loop.run()


main()
