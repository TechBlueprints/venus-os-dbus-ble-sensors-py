"""The router must not pay for dbus-python's automatic introspection.

Both call sites issue Introspect themselves, so letting the proxy do its
own buys a second round-trip per service -- and per node in the
recursive child walk -- and emits an ERROR from dbus.proxies when a
short-lived service vanishes before answering.  That error names the
resolved unique owner (":1.5524"), so it is unreadable after the fact
and describes nothing wrong.  Prod logged 22 of them on 2026-09-03.
"""
from __future__ import annotations

import os
import re

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                   "opt", "victronenergy", "dbus-ble-sensors-py",
                   "ble_advertisement_router.py")


def test_every_get_object_in_the_router_skips_auto_introspection() -> None:
    src = open(os.path.normpath(SRC)).read()
    calls = re.findall(r"self\._bus\.get_object\((.*?)\)", src, re.S)
    assert calls, "expected get_object call sites in the router"
    offenders = [c for c in calls if "introspect=False" not in c
                 and "org.freedesktop.DBus" not in c]
    assert not offenders, (
        "these get_object calls still trigger dbus-python's automatic "
        f"Introspect: {offenders}")
