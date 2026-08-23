"""Accept-list slicing across adapters.

A controller's accept list is fixed-size hardware — 25 and 32 entries on
prod's two scan cards — and adds past the end fail silently.  Handing
every adapter the same MAC-sorted list caps coverage at the largest
single table and always drops the same devices: on prod, four sensors
whose addresses begin with "f" went unheard while lower ones kept
working.  Slicing makes the tables add up instead of overlap.
"""
from __future__ import annotations

import hci_scan_control


def _module():
    """The slicing helper lives with the accept-list code it serves."""
    return hci_scan_control


def test_slices_are_contiguous_and_add_up() -> None:
    m = _module()
    slices = m.accept_list_slices(["a", "b"], {"a": 25, "b": 32}, 46)
    assert slices["a"] == (0, 25)
    assert slices["b"] == (25, 21)          # only 21 left to take
    assert sum(count for _o, count in slices.values()) == 46


def test_every_device_lands_on_exactly_one_adapter() -> None:
    m = _module()
    devices = [f"dev{i}" for i in range(46)]
    slices = m.accept_list_slices(["a", "b"], {"a": 25, "b": 32}, len(devices))
    covered = []
    for key in ("a", "b"):
        off, count = slices[key]
        covered.extend(devices[off:off + count])
    assert covered == devices                # union is complete, no overlap


def test_shortfall_is_visible_when_hardware_is_too_small() -> None:
    # 46 devices, one 25-entry card: 21 cannot be watched at all.  The
    # caller warns on this; here we just prove the arithmetic surfaces it.
    m = _module()
    slices = m.accept_list_slices(["a"], {"a": 25}, 46)
    assert sum(count for _o, count in slices.values()) == 25


def test_single_adapter_matches_old_behaviour_when_it_fits() -> None:
    m = _module()
    slices = m.accept_list_slices(["a"], {"a": 32}, 20)
    assert slices["a"] == (0, 20)


def test_unknown_capacity_yields_none_so_caller_can_fall_back() -> None:
    # A capacity we could not read must not silently strand devices.
    m = _module()
    slices = m.accept_list_slices(["a", "b"], {"a": None, "b": 32}, 46)
    assert slices["a"] is None


def test_order_is_stable_so_devices_do_not_migrate() -> None:
    # Re-applying must keep a device on the same card; changing which
    # adapter watches it loses that device for a scan cycle.
    m = _module()
    first = m.accept_list_slices(["a", "b"], {"a": 25, "b": 32}, 46)
    second = m.accept_list_slices(["a", "b"], {"a": 25, "b": 32}, 46)
    assert first == second


def test_zero_capacity_adapter_takes_nothing() -> None:
    m = _module()
    slices = m.accept_list_slices(["a", "b"], {"a": 0, "b": 32}, 10)
    assert slices["a"] == (0, 0)
    assert slices["b"] == (0, 10)
