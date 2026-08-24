"""Cached D-Bus connection factory.

BusConnection objects created with DBusGMainLoop as the default main loop
are pinned in memory by C-level GLib watch/timeout references that Python's
GC cannot reach.  Without caching, every call site that creates a new bus
connection leaks a connection to the D-Bus daemon, eventually exhausting
the per-UID connection limit (typically 256 for root).

Usage::

    from dbus_bus import get_bus

    # For a VeDbusService that registers object paths — one connection per
    # service name so that '/' registrations don't collide:
    bus = get_bus("com.victronenergy.tank.mopeka_abc123")
    svc = VeDbusService("com.victronenergy.tank.mopeka_abc123", bus)

    # For settings access — all callers share one connection:
    bus = get_bus("com.victronenergy.settings")
"""

import os
import dbus
import dbus.bus

class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

_bus_instances: dict[str, dbus.bus.BusConnection] = {}

def get_bus(cache_key: str) -> dbus.bus.BusConnection:
    """Return a cached bus connection for *cache_key*, creating one if needed.

    Each unique *cache_key* gets its own ``BusConnection``.  This is
    necessary because ``VeDbusService`` registers D-Bus object paths
    (like ``'/'``) and two services on the same connection would collide.

    Use a stable, well-known name as the key:

    * The service name for ``VeDbusService`` instances
      (e.g. ``"com.victronenergy.tank.mopeka_abc123"``).
    * ``"com.victronenergy.settings"`` for all settings access — all
      callers can share one connection since they only make outgoing
      method calls and don't register object paths.
    """
    bus = _bus_instances.get(cache_key)
    if bus is None or not bus.get_is_connected():
        _bus_instances[cache_key] = (
            SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else SystemBus()
        )
    return _bus_instances[cache_key]


def get_private_unattached_bus() -> dbus.bus.BusConnection:
    """A private connection with NO GLib main loop integration.

    For code that must run OFF the mainloop thread.  dbus-python's
    ``DBusGMainLoop`` supports only the DEFAULT main context
    (``dbus_glib_native_mainloop(NULL)`` — "Non-default main contexts are
    not currently supported"), so a normally-constructed connection
    attaches its watches, timeouts and dispatch source to the MAIN
    thread's context no matter which thread creates it.  A worker thread
    then makes synchronous calls on a connection the main thread is
    simultaneously dispatching, which libdbus does not support
    (freedesktop dbus#15, open since 2009).  On Venus that showed up as
    six SIGABRTs on dev-cerbo with ``malloc(): unaligned fastbin chunk
    detected`` — free-list corruption, diagnosed from the cores by the
    BCM crash-analysis session.

    ``NULL_MAIN_LOOP`` opts out entirely: no GSources are registered, so
    nothing on the main thread ever dispatches this connection and the
    owning thread has it to itself.

    Rules for callers, each learned the hard way:

    * Create it INSIDE the thread that will use it, and share it with no
      other thread.
    * Reuse it.  Do NOT create and close one per tick — that is the
      pattern that crashed dbus-serialbattery.
    * Close it when the owning thread stops; otherwise it leaks an fd and
      counts against ``max_connections_per_user``.
    * Synchronous calls only.  Signals cannot be delivered without a main
      loop, which is fine for callers that only Get and Set with explicit
      timeouts.

    Deliberately NOT part of :func:`get_bus`: that cache exists to share
    mainloop-integrated connections, and its callers want signal delivery.
    """
    from dbus.mainloop import NULL_MAIN_LOOP

    kind = (dbus.bus.BusConnection.TYPE_SESSION
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.bus.BusConnection.TYPE_SYSTEM)
    return dbus.bus.BusConnection(kind, mainloop=NULL_MAIN_LOOP)
