"""Accept-all listening windows for rotating-MAC (name-identified) devices.

The steady scan state is accept-list even when an EasyStart is
configured — its last-heard address rides in every adapter's list — and
one adapter periodically takes a brief accept-all window to catch MAC
rotations.  These tests pin the pure scheduler: windows fire on the
right ticks, rotate across every adapter, and stay closed in between.
"""
from __future__ import annotations

from dbus_ble_sensors import name_window_adapter, NAME_WINDOW_EVERY_TICKS


ADAPTERS = ['aaa', 'bbb']


def test_no_window_between_scheduled_ticks():
    for tick in (1, 2, 3, 4, 6, 7, 8, 9, 11):
        assert name_window_adapter(tick, 5, ADAPTERS) is None, tick


def test_window_fires_on_every_nth_tick():
    for tick in (0, 5, 10, 15):
        assert name_window_adapter(tick, 5, ADAPTERS) in ADAPTERS, tick


def test_windows_rotate_across_all_adapters():
    seen = {name_window_adapter(tick, 5, ADAPTERS)
            for tick in (0, 5, 10, 15)}
    assert seen == set(ADAPTERS), (
        "a device only one radio can hear (1-2 m range) needs every "
        "adapter to take a listening turn")


def test_single_adapter_gets_every_window():
    for tick in (0, 5, 10):
        assert name_window_adapter(tick, 5, ['solo']) == 'solo'


def test_no_adapters_yields_no_window():
    assert name_window_adapter(0, 5, []) is None


def test_disabled_schedule_yields_no_window():
    assert name_window_adapter(0, 0, ADAPTERS) is None
    assert name_window_adapter(0, -1, ADAPTERS) is None


def test_adapter_order_is_deterministic():
    # Keys arrive as an unordered dict view in production; the schedule
    # must not depend on arrival order.
    a = name_window_adapter(5, 5, ['bbb', 'aaa'])
    b = name_window_adapter(5, 5, ['aaa', 'bbb'])
    assert a == b


def test_duty_cycle_is_bounded():
    # At most one adapter listens wide, at most one tick in EVERY —
    # the whole point is bounding the neighbour-firehose cost.
    every = NAME_WINDOW_EVERY_TICKS
    open_ticks = sum(
        1 for tick in range(every * 4)
        if name_window_adapter(tick, every, ADAPTERS) is not None)
    assert open_ticks == 4
