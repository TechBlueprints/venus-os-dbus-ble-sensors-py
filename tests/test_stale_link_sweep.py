"""Links a previous life left behind are dropped before the tap opens.

svc -t ends this process with os._exit(0); a crash or kill ends it with
nothing.  bleak never sends Disconnect either way, and bluetoothd keeps
the LE link with no client behind it.  A connected peripheral does not
advertise, so the next life cannot hear the device on ANY card.

Prod, 2026-09-02 19:58Z: a restart orphaned easystart_89fe's link on
hci9 -- a card outside both the allowlist and ble-connect.conf.  The new
life logged "silent until its A/C runs" for ~17 minutes while the A/C
ran.  A manual Device1.Disconnect at 20:15:00Z had it re-advertising and
re-linked on a pooled card within ten seconds.

At our own startup we hold nothing, so any link to one of OUR addresses
is stale by construction.  Only our addresses are touched.
"""
from __future__ import annotations

import os
import re

import ble_gatt_dbus

OURS = "38:18:2B:FB:9B:76"
THEIRS = "AB:80:72:54:E0:B4"


class _Obj:
    """A bus object: the root answers GetManagedObjects, devices Disconnect."""
    disconnected: list = []
    raise_for: set = set()

    def __init__(self, path, objects):
        self._path = path
        self._objects = objects

    def GetManagedObjects(self):
        return self._objects

    def Disconnect(self):
        if self._path in _Obj.raise_for:
            raise RuntimeError("org.bluez.Error.NotConnected")
        _Obj.disconnected.append(self._path)


class _Bus:
    def __init__(self, devices):
        self.objects = {
            p: {ble_gatt_dbus.DEVICE_INTERFACE: props} for p, props in devices.items()
        }
        self.get_object_calls = 0

    def get_object(self, name, path, introspect=True):
        self.get_object_calls += 1
        return _Obj(path, self.objects)


def _install(monkeypatch):
    # dbus.Interface(obj, iface) -> obj, so the stub object answers directly.
    monkeypatch.setattr(ble_gatt_dbus.dbus, "Interface", lambda obj, *a, **kw: obj)
    _Obj.disconnected = []
    _Obj.raise_for = set()


def test_our_connected_device_is_dropped_on_any_adapter(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({
        "/org/bluez/hci9/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76"})
    assert _Obj.disconnected == ["/org/bluez/hci9/dev_38_18_2B_FB_9B_76"], (
        "hci9 is outside the pool and the allowlist; the sweep must not care")
    assert got == [("/org/bluez/hci9/dev_38_18_2B_FB_9B_76", OURS)]


def test_address_spelling_does_not_matter(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({"/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True}})
    ble_gatt_dbus.disconnect_stale_links(bus, {"38:18:2B:FB:9B:76"})
    assert len(_Obj.disconnected) == 1


def test_not_connected_and_not_ours_are_left_alone(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({
        "/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": False},
        "/org/bluez/hci4/dev_AB_80_72_54_E0_B4": {"Address": THEIRS, "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76"})
    assert _Obj.disconnected == [], (
        "a disconnected device needs nothing; another consumer's live "
        "link (serialbattery's pack) is never ours to drop")
    assert got == []


def test_one_failure_does_not_stop_the_sweep(monkeypatch) -> None:
    _install(monkeypatch)
    _Obj.raise_for = {"/org/bluez/hci1/dev_38_18_2B_FB_9B_76"}
    bus = _Bus({
        "/org/bluez/hci1/dev_38_18_2B_FB_9B_76": {"Address": OURS, "Connected": True},
        "/org/bluez/hci9/dev_38_18_2B_FA_24_FA": {"Address": "38:18:2B:FA:24:FA", "Connected": True},
    })
    got = ble_gatt_dbus.disconnect_stale_links(bus, {"38182bfb9b76", "38182bfa24fa"})
    assert [p for p, _ in got] == ["/org/bluez/hci9/dev_38_18_2B_FA_24_FA"]


def test_nothing_configured_means_no_bus_traffic(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({})
    assert ble_gatt_dbus.disconnect_stale_links(bus, set()) == []
    assert bus.get_object_calls == 0


def test_bluez_unreachable_does_not_raise(monkeypatch) -> None:
    _install(monkeypatch)

    class _Dead:
        def get_object(self, *a, **kw):
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

    assert ble_gatt_dbus.disconnect_stale_links(_Dead(), {"38182bfb9b76"}) == [], (
        "a startup sweep must never stop the service from starting")


def test_start_sweeps_before_the_tap_opens() -> None:
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
               "src", "opt", "victronenergy", "dbus-ble-sensors-py",
               "dbus_ble_sensors.py")).read()
    start = src.index("    def start(self):")
    end = src.index("\n    def ", start + 10)
    body = src[start:end]
    assert "disconnect_stale_links(" in body, "start() must run the sweep"
    assert body.index("disconnect_stale_links(") < body.index("self._start_tap()"), (
        "the sweep must run before the tap opens, so the freed device is "
        "heard on our own cards from the first advertisement")
    assert "owned_macs()" in body and "self._name_device_macs" in body, (
        "both address stores feed the sweep: HEX devices (owned prefixes "
        "only) and EasyStarts alike")


# --- Ownership and live claims: the two guards added after 20:19Z ------
#
# The first sweep keyed its address set on configured_macs(), which
# harvests a MAC from EVERY /Settings/Devices entry -- other services'
# included -- and disconnected power-watchdog's link (24:EC:4A:E4:69:A5,
# hci5) and easytouch's (88:13:BF:2E:10:BE, hci7).  Both had live .use.
# claims from live pids.  "Stale" means no live claimant; that is the
# definition, and the prefix filter is defence in depth behind it.

import glob
import importlib.util
import sys
import tempfile


def _load_real(name):
    """Load the real driver module from disk under a private name.

    Other test modules install stubs called ``dbus_ble_service`` into
    sys.modules that persist for the session, so a plain import here
    returns whichever stub ran first (it did: '_StubBleSvc' has no
    owned_macs).  Same trap, same fix as test_preferred_adapter_identity.
    """
    spec = importlib.util.spec_from_file_location(
        f"_real_{name}", os.path.join(SRC_DIR, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
dbus_ble_service = _load_real("dbus_ble_service")


class _Settings:
    def __init__(self, keys):
        self._keys = keys

    def list_device_settings(self):
        return self._keys


def test_owned_macs_excludes_other_services_entries() -> None:
    svc = dbus_ble_service.DbusBleService.__new__(dbus_ble_service.DbusBleService)
    svc._dbus_settings = _Settings([
        "orion_tr_c36eed421ff2/charger/Enabled",
        "microair_easystart_89fe/acload/Enabled",      # no MAC segment: skipped
        "seelevel_btp3_00a0508d9569_01/tank/Enabled",
        "power_watchdog_24ec4ae469a5/Enabled",          # NOT ours
        "easytouch_8813bf2e10be/Enabled",               # NOT ours
        "serialbattery_A4C138334124/Enabled",           # NOT ours
        "vebus_ttyS4/Enabled",
    ])
    assert svc.owned_macs() == {"c36eed421ff2", "00a0508d9569"}


def test_owned_prefixes_match_the_device_classes() -> None:
    """Every dev_prefix literal in a driver must be in OWNED_PREFIXES."""
    found = set()
    for path in glob.glob(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "src", "opt",
            "victronenergy", "dbus-ble-sensors-py", "ble_device_*.py")):
        for m in re.finditer(r"""['"]dev_prefix['"]\s*:\s*['"]([a-z0-9_]+)['"]""",
                             open(path).read()):
            found.add(m.group(1))
        for m in re.finditer(r"""DEV_PREFIX\s*=\s*['"]([a-z0-9_]+)['"]""",
                             open(path).read()):
            found.add(m.group(1))
    found.discard("dummy")   # the test-only device class
    missing = found - set(dbus_ble_service.DbusBleService.OWNED_PREFIXES)
    assert not missing, f"add to OWNED_PREFIXES: {sorted(missing)}"
    assert found, "the scan found no prefixes — the regex is dead"


def test_a_live_claimed_address_is_never_dropped(monkeypatch) -> None:
    _install(monkeypatch)
    bus = _Bus({"/org/bluez/hci5/dev_24_EC_4A_E4_69_A5":
                {"Address": "24:EC:4A:E4:69:A5", "Connected": True}})
    got = ble_gatt_dbus.disconnect_stale_links(
        bus, {"24ec4ae469a5"}, held={"24EC4AE469A5"})
    assert _Obj.disconnected == [] and got == [], (
        "even an address in our set must be left alone while a live "
        "process claims it — that is what 'stale' means")


def test_live_claims_are_parsed_from_the_real_filename_shapes() -> None:
    d = tempfile.mkdtemp()
    for n in ["000195C9B2EA.use.dbus-power-watchdog-15166.24EC4AE469A5",
              "000195C9B4C6.use.dbus-serialbattery.5320b7d7f9e7-7411.5320B7D7F9E7",
              "000195CC32F7.use.dbus-ble-sensors-py-4550.38182BFB9B76",
              "000195CC32F7.scan",
              "000195CC32F7.scan.holder.dbus-ble-sensors-py-8837-8837-5",
              "000195C9B4C6.link.0"]:
        open(os.path.join(d, n), "w").close()
    alive = lambda pid: pid != 4550          # the old sensors-py life is dead
    held = ble_gatt_dbus.live_claimed_addresses(d, alive=alive)
    assert held == {"24ec4ae469a5", "5320b7d7f9e7"}, (
        "dotted owners parse, dead pids do not count, scan/link files "
        "are not use-claims")


def test_start_uses_owned_macs_and_passes_live_claims() -> None:
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
               "src", "opt", "victronenergy", "dbus-ble-sensors-py",
               "dbus_ble_sensors.py")).read()
    body = src[src.index("    def start(self):"):]
    body = body[:body.index("\n    def ", 10)]
    assert "owned_macs()" in body
    assert "set(self._configured_macs)" not in body, (
        "configured_macs harvests other services' devices")
    assert "held=ble_gatt_dbus.live_claimed_addresses()" in body
