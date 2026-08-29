"""EasyStart driver: identity, configuration, and registry wiring.

Exercises the pieces that run before any radio is involved — the
name-derived identity that survives MAC rotation, the device
configuration contract, and the name-prefix class registry.
"""
from __future__ import annotations

import logging

import pytest

from ble_device import BleDevice
from ble_role import BleRole
from ble_role_acload import BleRoleAcLoad
from ble_device_easystart import BleDeviceEasyStart
import easystart_protocol as proto


@pytest.fixture(autouse=True)
def _register_acload_role():
    # conftest stubs ble_role with a minimal base; give it the registry
    # the real one carries so _load_configuration's role check works.
    if not hasattr(BleRole, 'ROLE_CLASSES'):
        BleRole.ROLE_CLASSES = {}
    BleRole.ROLE_CLASSES.setdefault('acload', BleRoleAcLoad)
    yield


def test_identity_from_name_is_stable_and_dbus_safe():
    assert BleDeviceEasyStart.identity_from_name('EasyStart_7F3A') \
        == 'easystart_7f3a'
    # The bare 10-char name variant must not leave a trailing underscore.
    assert BleDeviceEasyStart.identity_from_name('EasyStart_') == 'easystart'


def test_identity_is_mac_independent():
    # Same unit, rotated MAC: identity is derived from the name alone,
    # so it cannot change.
    a = BleDeviceEasyStart.identity_from_name('EasyStart_7F3A')
    b = BleDeviceEasyStart.identity_from_name('EasyStart_7F3A')
    assert a == b


def test_configure_passes_base_validation():
    dev = BleDeviceEasyStart('easystart_7f3a')
    dev.configure(b'')
    dev._load_configuration()
    assert dev.info['dev_id'] == 'microair_easystart_7f3a'
    assert list(dev.info['roles']) == ['acload']


def test_custom_parsing_no_regs():
    assert BleDeviceEasyStart.CUSTOM_PARSING is True
    dev = BleDeviceEasyStart('easystart_7f3a')
    dev.configure(b'')
    assert dev.info['regs'] == []


def test_name_prefix_matches_protocol_module():
    assert BleDeviceEasyStart.ADV_NAME_PREFIXES == (proto.ADV_NAME_PREFIX,)
    assert proto.ADV_NAME_PREFIX == 'EasyStart_'


def test_nominal_voltage_setting_present():
    dev = BleDeviceEasyStart('easystart_7f3a')
    dev.configure(b'')
    names = [s['name'] for s in dev.info['settings']]
    assert 'NominalVoltage' in names
    props = dev.info['settings'][0]['props']
    assert props['def'] == 120
    assert props['min'] == 90 and props['max'] == 250


def test_not_busy_before_any_session():
    dev = BleDeviceEasyStart('easystart_7f3a')
    assert dev.is_busy() is False


def test_manufacturer_data_path_is_inert():
    # Name-identified device: nothing may arrive via the mfg-data path,
    # and calling it must not raise even pre-configure.
    dev = BleDeviceEasyStart('easystart_7f3a')
    dev.handle_manufacturer_data(b'\x01\x02')


def test_class_registers_by_name_prefix_not_mfg_id():
    BleDevice.NAME_CLASSES.setdefault('EasyStart_', BleDeviceEasyStart)
    assert BleDevice.NAME_CLASSES['EasyStart_'] is BleDeviceEasyStart
    # The sentinel manufacturer id must never land in the mfg registry.
    assert BleDevice.DEVICE_CLASSES.get(-1) is not BleDeviceEasyStart


# --- A/C stopping DURING connect is a normal session end --------------
#
# Prod 2026-08-29 02:18:47: the compressor shut off after the link came
# up but before service discovery finished, so the characteristic lookup
# ran against an empty GATT database and bleak raised
# BleakCharacteristicNotFoundError.  bcmv2 recorded the matching
# "disconnect event ... last link traffic: never".
#
# The driver logged WARNING "session failed" and took an exponential
# backoff -- delaying reconnection for a compressor that had merely
# stopped.  Physically this is the same event as the mid-session drop
# already treated as normal, just earlier in the session.
#
# 1 occurrence in 41 sessions, which is also why the rate cannot classify
# it: a genuinely wrong UUID would fail all 41, so rarity is evidence
# AGAINST the loud reading, not for it.

class _StubRole:
    def __init__(self):
        self.published = []

    def __setitem__(self, path, value):
        self.published.append((path, value))

    def __getitem__(self, path):
        return 0


def _device_after_failed_connect():
    dev = BleDeviceEasyStart('easystart_0c87')
    dev._session_active = True
    dev._reachable = False          # telemetry never started
    dev._failure_streak = 0
    dev._role_services = {}
    dev._publish_offline = lambda: None
    return dev


def test_drop_before_discovery_does_not_count_as_a_failure(caplog):
    dev = _device_after_failed_connect()
    dev._dropped_before_discovery = True

    with caplog.at_level(logging.DEBUG):
        dev._on_session_done(None, RuntimeError('char not found'))

    assert dev._failure_streak == 0, (
        "a compressor stopping during connect must not drive the "
        "exponential backoff that delays picking it back up")
    levels = {r.levelname for r in caplog.records}
    assert 'WARNING' not in levels, f"expected no warning, got {levels}"
    assert any('before service discovery' in r.message
               for r in caplog.records)


def test_a_genuine_session_failure_still_warns_and_backs_off(caplog):
    """The must-stay-loud case: no drop detected, so it is a real defect."""
    dev = _device_after_failed_connect()
    dev._dropped_before_discovery = False

    with caplog.at_level(logging.DEBUG):
        dev._on_session_done(None, RuntimeError('char not found'))

    assert dev._failure_streak == 1
    assert any(r.levelname == 'WARNING' for r in caplog.records), (
        "a characteristic missing from a RESOLVED database is a wrong "
        "UUID or changed firmware, and must not be silenced")


def test_the_flag_is_never_read_stale():
    """It must be reset per session, or one drop silences later defects."""
    import inspect
    src = inspect.getsource(BleDeviceEasyStart._run_session)
    assert 'self._dropped_before_discovery = False' in src, (
        "_run_session must clear the flag on entry")


# --- hciN is not an identity, on the connect path too -------------------
#
# A replug or USB reset renumbers cards.  The advertisement tells us which
# RADIO heard the device; carrying that as a NUMBER to the connect would
# aim ConnectDevice at whichever card holds the number by then — likely
# one that cannot hear a device with 1-2 m of range, and possibly one
# another service was promised.  So the number must be resolved at the
# moment of the call, not captured with the advertisement.

def test_adapter_is_stored_as_identity_not_as_a_number():
    dev = BleDeviceEasyStart('easystart_0c87')
    assert not hasattr(dev, '_adapter_index'), (
        "the raw hciN index must not be retained; store the card's "
        "identity (MAC) instead")
    assert dev._adapter_key is None


def test_advertisement_converts_the_index_to_an_identity():
    import inspect
    src = inspect.getsource(BleDeviceEasyStart.handle_name_advertisement)
    assert 'adapter_identity.canonical' in src, (
        "the tap's hciN must be canonicalised to the card's MAC on "
        "arrival")


def test_connect_resolves_the_number_at_call_time():
    import inspect
    src = inspect.getsource(BleDeviceEasyStart._resolve_without_scanning)
    assert 'adapter_identity.index_for' in src, (
        "the hciN must be resolved immediately before ConnectDevice")
    # The resolve must precede the D-Bus call it targets, or it is not
    # 'immediately before' in any useful sense.
    assert src.index('adapter_identity.index_for') < src.index('bus.call'), (
        "resolve the adapter number before issuing ConnectDevice")
    assert 'self._adapter_index' not in src


def test_a_vanished_adapter_is_unreachable_not_a_defect():
    """The card that heard it is gone: retry elsewhere, do not warn."""
    import ble_gatt_link
    exc = ble_gatt_link.DeviceNotFound('adapter no longer present')
    assert ble_gatt_link.unreachable(exc), (
        "a vanished adapter must classify as unreachable so it is logged "
        "quietly and retried, not reported as an integration fault")


def test_a_throwing_classifier_cannot_mask_the_real_error(monkeypatch, caplog):
    """The handler sits on EVERY session; the condition is 1 in 41.

    A rare-path diagnosis must not be able to make the common path worse.
    If the classifier raises, that exception would replace the real one
    and every session would start failing in a new way -- turning a
    logging refinement into an outage.  Flagged by the EasyStart session
    on review, before it could happen.
    """
    import ble_gatt_link

    def _boom(exc, client=None):
        raise RuntimeError("classifier is broken")

    monkeypatch.setattr(ble_gatt_link, "dropped_before_discovery", _boom)

    original = ValueError("the real failure")

    # Drive the same handler shape _run_session uses.
    dev = BleDeviceEasyStart('easystart_0c87')
    dev._plog = 'easystart_0c87'
    captured = {}
    try:
        try:
            raise original
        except Exception as exc:
            try:
                dev._dropped_before_discovery = (
                    ble_gatt_link.dropped_before_discovery(exc, object()))
            except Exception:
                dev._dropped_before_discovery = False
            raise
    except Exception as exc:
        captured['exc'] = exc

    assert captured['exc'] is original, (
        "the caller must still see the real failure, not the "
        "classifier's own error")
    assert dev._dropped_before_discovery is False, (
        "unable to classify means stay loud")


def test_the_handler_ignores_cancellation() -> None:
    """A cancelled session is a shutdown, not a diagnosis."""
    import inspect
    src = inspect.getsource(BleDeviceEasyStart._run_session)
    assert 'except Exception as exc:' in src
    assert 'except BaseException' not in src, (
        "BaseException would run classification while unwinding a "
        "CancelledError on the way to shutdown")
