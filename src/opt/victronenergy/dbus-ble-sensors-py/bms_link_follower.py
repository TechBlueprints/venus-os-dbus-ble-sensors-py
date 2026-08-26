"""Follow the system's active BMS and drive local charger roles' /Link paths.

dbus-systemcalc-py's DVCC never writes to ``com.victronenergy.charger``
services — its charge subsystems are solarcharger, alternator, dcgenset,
inverter, multi, vecan and acsystem (checked against the delegate list in
Venus OS 3.72) — so a charger role that is fully ready for external control
never hears from the BMS that systemcalc itself selected.  This follower
closes that gap from the charger side: each tick reads systemcalc's
``/ActiveBmsService``, pulls that battery service's charge limits
(``/Info/MaxChargeVoltage``, ``/Info/MaxChargeCurrent``), and writes them
onto every local charger role's ``/Link/ChargeVoltage`` and
``/Link/ChargeCurrent`` — the same writes systemcalc performs against a
VE.Bus or solar charger, produced locally.

The writes go over D-Bus rather than through the role objects on purpose:
external ``SetValue`` is what triggers the role's registered write
callbacks (engagement bookkeeping plus the queued GATT setpoint writes), so
the follower exercises exactly the code path a future systemcalc would.
Engagement and disengagement reuse the role's own contract:
``/Settings/BmsPresent = 1`` plus the setpoints flips the role into
external control (``/State`` 252), and ``BmsPresent = 0`` hands the charger
back to its internal charge algorithm when the BMS disappears.

A BMS that stops publishing either limit is treated as absent: holding a
charger at stale setpoints is worse than letting it fall back to its own
algorithm.

The follower runs on its own daemon thread, never on the GLib mainloop.
That is a hard requirement, learned on the bench: systemcalc polls charger
services synchronously (delegates/batterydata.py aggregates
com.victronenergy.charger), so a synchronous client call made FROM our
mainloop TOWARD systemcalc can collide with systemcalc's call into us -
each mainloop waiting on the other - and with a 10 s tick re-entering the
25 s D-Bus timeout window faster than it drains, the whole service wedges
permanently. From a separate thread the collision cannot bite: our
mainloop stays free to answer systemcalc while this thread waits, and
every call carries an explicit timeout so a stalled peer costs one bounded
tick, not the service.
"""

import logging
import threading

logger = logging.getLogger(__name__)

DBUS_CALL_TIMEOUT_S = 5.0

# Only charger services this process publishes are driven.  Scoped by dev-id
# prefix rather than by asking the main service for its role registry so the
# follower also re-engages a role service that was torn down and republished.
CHARGER_SERVICE_PREFIXES = ("com.victronenergy.charger.ip22_",)

SYSTEMCALC_SERVICE = "com.victronenergy.system"
ACTIVE_BMS_PATH = "/ActiveBmsService"
CHARGE_VOLTAGE_PATH = "/Info/MaxChargeVoltage"
CHARGE_CURRENT_PATH = "/Info/MaxChargeCurrent"
# The DVCC master switch. systemcalc keeps /ActiveBmsService populated even
# with DVCC off (verified on Venus 3.72: Bol=0 leaves it set), so following
# it alone would keep chargers externally controlled after the user turns
# DVCC off in the GUI. The toggle is the user's word on whether the BMS
# runs the chargers; obey it.
SETTINGS_SERVICE = "com.victronenergy.settings"
DVCC_PATH = "/Settings/Services/Bol"


class DbusBusOps(object):
    """Thin D-Bus accessor, separated so tests can inject a fake."""

    BUS_ITEM = "com.victronenergy.BusItem"

    # A dead connection is retried at most this often, so a daemon that
    # is genuinely gone costs one warning per interval, not one per tick.
    REBUILD_RETRY_S = 30.0

    def __init__(self):
        # NOT get_bus(): that returns a connection wired into the default
        # GLib context, i.e. the main thread's.  We run on our own thread
        # (see the module docstring for why that is required), and calling
        # synchronously on a connection the main thread dispatches is the
        # unsupported case that corrupted the heap.  This one has no main
        # loop at all and belongs to this thread alone.
        self._bus = self._new_bus()
        self._closed = False
        self._next_rebuild = 0.0

    @staticmethod
    def _new_bus():
        from dbus_bus import get_private_unattached_bus

        return get_private_unattached_bus()

    def close(self):
        """Release the connection.  Called when the follower stops."""
        self._closed = True
        bus, self._bus = self._bus, None
        if bus is None:
            return
        try:
            bus.close()
        except Exception:
            logger.exception("bms-link-follower: closing bus failed")

    def _live_bus(self):
        """The connection, rebuilt if it died under us.

        Without this, a dbus-daemon restart leaves get/set swallowing
        exceptions against a dead socket and the follower goes silently
        inert — the charger keeps its last setpoints and nothing says
        why.  Silent absence is this fleet's worst failure shape, so a
        dead bus is rebuilt (and said out loud) rather than tolerated.

        There is no exit-on-disconnect escape hatch here, unlike the
        mainloop connections in dbus_bus: with NULL_MAIN_LOOP nothing
        ever dispatches the Disconnected message, so the process would
        not notice, let alone exit.  Detection has to be explicit.

        Only the owning thread calls this (the same rule as every other
        use of the private bus), so the swap cannot race.  A follower
        that was close()d stays closed: rebuild is for a connection that
        died, not one we released.
        """
        if self._closed:
            return None
        bus = self._bus
        try:
            alive = bus is not None and bus.get_is_connected()
        except Exception:
            alive = False
        if alive:
            return bus
        import time

        now = time.monotonic()
        if now < self._next_rebuild:
            return bus
        self._next_rebuild = now + self.REBUILD_RETRY_S
        logger.warning(
            "bms-link-follower: D-Bus connection died; rebuilding "
            "(a daemon restart is the usual cause)")
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
        try:
            self._bus = self._new_bus()
        except Exception:
            logger.warning(
                "bms-link-follower: rebuild failed; retrying in %ss",
                self.REBUILD_RETRY_S)
            self._bus = None
            return None
        return self._bus

    def _item(self, service, path):
        import dbus

        bus = self._live_bus()
        if bus is None:
            raise RuntimeError("no D-Bus connection")
        obj = bus.get_object(service, path, introspect=False)
        return dbus.Interface(obj, self.BUS_ITEM)

    def get(self, service, path):
        """Value at service/path, or None when absent/invalid/unreachable."""
        try:
            value = self._item(service, path).GetValue(timeout=DBUS_CALL_TIMEOUT_S)
        except Exception:
            return None
        # Venus encodes an invalid value as an empty array
        if isinstance(value, (list, tuple)) or value is None:
            return None
        return value

    def set(self, service, path, value):
        """Write value; True when the service acknowledged it."""
        try:
            return int(self._item(service, path).SetValue(value, timeout=DBUS_CALL_TIMEOUT_S)) == 0
        except Exception:
            return False

    def charger_services(self):
        try:
            names = self._live_bus().list_names()
        except Exception:
            return []
        return sorted(str(name) for name in names if str(name).startswith(CHARGER_SERVICE_PREFIXES))


class BmsLinkFollower(object):
    """Periodic bridge from the active BMS's limits to charger /Link paths."""

    def __init__(self, bus_ops=None, interval_s=10.0):
        self._ops = bus_ops
        self._interval_s = interval_s
        # service name -> (cvl, ccl) last written, to keep quiet ticks off
        # the bus; the role's own deadbands are the real dedup for GATT
        self._written = {}
        self._engaged = set()
        self._last_bms = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Start the follower's own thread - see the module docstring for
        why these calls must never run on the GLib mainloop."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="bms-link-follower")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        # the bus connection is created inside the thread that will use it
        try:
            while not self._stop.wait(self._interval_s):
                self.tick()
        finally:
            # Close from the owning thread, and only once the loop is done
            # — a connection left open leaks an fd per follower restart.
            ops, self._ops = self._ops, None
            if ops is not None:
                ops.close()

    def tick(self):
        """One bridge pass; safe against every failure but a coding error."""
        try:
            if self._ops is None:
                self._ops = DbusBusOps()
            self._tick()
        except Exception:
            logger.exception("bms-link-follower tick failed")
        return True

    def _tick(self):
        dvcc = self._ops.get(SETTINGS_SERVICE, DVCC_PATH)
        try:
            dvcc_on = int(dvcc) == 1
        except (TypeError, ValueError):
            dvcc_on = False
        bms = self._ops.get(SYSTEMCALC_SERVICE, ACTIVE_BMS_PATH) if dvcc_on else None
        bms = str(bms) if bms else None
        if bms != self._last_bms:
            logger.info("active BMS is now %s", bms or "absent")
            self._last_bms = bms

        cvl = self._ops.get(bms, CHARGE_VOLTAGE_PATH) if bms else None
        ccl = self._ops.get(bms, CHARGE_CURRENT_PATH) if bms else None
        chargers = self._ops.charger_services()

        # forget chargers that left the bus, so a republished role service
        # is engaged from scratch
        for gone in [s for s in self._engaged if s not in chargers]:
            self._engaged.discard(gone)
            self._written.pop(gone, None)

        if cvl is None or ccl is None:
            self._disengage_all(chargers)
            return

        for service in chargers:
            self._drive(service, float(cvl), float(ccl))

    def _drive(self, service, cvl, ccl):
        if service not in self._engaged:
            if not self._ops.set(service, "/Settings/BmsPresent", 1):
                return
            self._engaged.add(service)
            logger.info("driving %s from the active BMS (cvl=%.2fV ccl=%.1fA)", service, cvl, ccl)
        if self._written.get(service) == (cvl, ccl):
            return
        ok_v = self._ops.set(service, "/Link/ChargeVoltage", cvl)
        ok_c = self._ops.set(service, "/Link/ChargeCurrent", ccl)
        if ok_v and ok_c:
            self._written[service] = (cvl, ccl)
        else:
            # partial write: retry both next tick rather than caching a lie
            self._written.pop(service, None)

    def _disengage_all(self, chargers):
        for service in chargers:
            if service in self._engaged:
                if self._ops.set(service, "/Settings/BmsPresent", 0):
                    logger.info("released %s to its internal charge algorithm", service)
                self._engaged.discard(service)
                self._written.pop(service, None)
