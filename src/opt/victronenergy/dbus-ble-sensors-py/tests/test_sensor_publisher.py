"""Tests for sensor_publisher.SensorPublisher."""
import gc
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

from sensor_publisher import SensorPublisher  # noqa: E402


class FakePolicy:
    """Stand-in for SensorRoundingPolicy: small lookup table + heartbeat."""

    def __init__(self, heartbeat: int = 900, table: 'dict | None' = None):
        self.heartbeat_seconds = heartbeat
        self._table = table if table is not None else {
            'temperature': 1, 'voltage': 2, 'current': 2,
        }

    def round_value(self, value, sensor_type=None, override=None):
        if value is None:
            return None
        ndigits = override if override is not None else (
            self._table.get(sensor_type) if sensor_type else None
        )
        if ndigits is None:
            return value
        try:
            return round(value, ndigits)
        except (TypeError, ValueError):
            return value


class FakeRoleService:
    """Minimal stand-in for DbusRoleService — needs only ``__setitem__``
    and weak-ref support."""

    def __init__(self, name: str = 'svc'):
        self._name = name
        self._values: dict = {}

    def __setitem__(self, path, value):
        self._values[path] = value

    def __getitem__(self, path):
        return self._values.get(path)

    def get(self, path, default=None):
        return self._values.get(path, default)


@pytest.fixture
def policy():
    return FakePolicy()


@pytest.fixture
def publisher(policy):
    SensorPublisher._INSTANCE = None  # reset between tests
    return SensorPublisher(policy)


@pytest.fixture
def svc():
    return FakeRoleService()


# ─── Basic publish behavior ─────────────────────────────────────────


def test_first_publish_writes(publisher, svc):
    # Precise value goes through; rounding is only used to decide
    # whether to emit, not what to publish.
    assert publisher.publish(svc, '/Temp', 23.456, 'temperature') is True
    assert svc['/Temp'] == 23.456   # PRECISE, not rounded to 23.5


def test_unchanged_publish_skips(publisher, svc):
    publisher.publish(svc, '/Temp', 23.49, 'temperature')           # round=23.5, emit 23.49
    # Same rounded value (23.51 -> 23.5) → skip the emit
    assert publisher.publish(svc, '/Temp', 23.51, 'temperature') is False


def test_changed_publish_writes(publisher, svc):
    publisher.publish(svc, '/Temp', 23.49, 'temperature')           # round=23.5
    # 23.65 rounds to a different bucket → emit
    assert publisher.publish(svc, '/Temp', 23.65, 'temperature') is True
    # Stored value is the precise input, not the rounded comparison value.
    assert svc['/Temp'] == 23.65


def test_none_clears_stale_value(publisher, svc):
    """Writing None after a real value clears the path (stale-data hygiene)."""
    publisher.publish(svc, '/Temp', 23.5, 'temperature')
    assert svc['/Temp'] == 23.5
    assert publisher.publish(svc, '/Temp', None, 'temperature') is True
    assert svc['/Temp'] is None


def test_repeated_none_skips(publisher, svc):
    """After None is written, repeating None inside heartbeat is a no-op."""
    publisher.publish(svc, '/Temp', None, 'temperature')   # first None — writes
    assert publisher.publish(svc, '/Temp', None, 'temperature') is False


def test_zero_is_valid(publisher, svc):
    """``0`` and ``0.0`` are real readings, not 'skip me'."""
    assert publisher.publish(svc, '/Current', 0.0, 'current') is True
    assert svc['/Current'] == 0.0
    # Same zero again → dedup
    assert publisher.publish(svc, '/Current', 0.0, 'current') is False
    # New zero → still dedups
    assert publisher.publish(svc, '/Current', 0.001, 'current') is False
    # Real change beyond rounding → write
    assert publisher.publish(svc, '/Current', 0.05, 'current') is True


def test_no_sensor_type_still_dedups(publisher, svc):
    """Even without rounding, exact-equal values should dedup."""
    publisher.publish(svc, '/X', 0.005)
    assert publisher.publish(svc, '/X', 0.005) is False
    assert publisher.publish(svc, '/X', 0.006) is True


def test_override_takes_precedence(publisher, svc):
    # ``override`` controls the *comparison* precision, not what is
    # emitted: the precise input still lands on the bus.  Verified
    # by a follow-on call where 23.456 → round(0)=23 caches "23", and
    # 23.49 also rounds to 23 → dedups.
    assert publisher.publish(svc, '/Temp', 23.456, 'temperature',
                              override=0) is True
    assert svc['/Temp'] == 23.456                              # precise emit
    assert publisher.publish(svc, '/Temp', 23.49, 'temperature',
                              override=0) is False             # same rounded bucket → skip


def test_force_writes_unchanged(publisher, svc):
    publisher.publish(svc, '/Temp', 23.456, 'temperature')          # 23.5
    assert publisher.publish(svc, '/Temp', 23.45, 'temperature',
                              force=True) is True


# ─── Deadband mode ──────────────────────────────────────────────────


def test_deadband_first_publish_writes(publisher, svc):
    # No prior value cached — always emit.
    assert publisher.publish(svc, '/V', 13.5, deadband=0.5) is True
    assert svc['/V'] == 13.5


def test_deadband_small_change_skips(publisher, svc):
    """The exact failure case that motivated this mode: value
    flickering around a rounding boundary stays within deadband."""
    publisher.publish(svc, '/V', 13.46, deadband=0.5)
    # All within 0.5 V of 13.46 → silent
    assert publisher.publish(svc, '/V', 13.53, deadband=0.5) is False
    assert publisher.publish(svc, '/V', 13.44, deadband=0.5) is False
    assert publisher.publish(svc, '/V', 13.50, deadband=0.5) is False


def test_deadband_at_threshold_emits(publisher, svc):
    publisher.publish(svc, '/V', 13.0, deadband=0.5)
    # |13.5 - 13.0| == 0.5 → ``>=`` → emit
    assert publisher.publish(svc, '/V', 13.5, deadband=0.5) is True
    assert svc['/V'] == 13.5


def test_deadband_large_change_emits_precise(publisher, svc):
    publisher.publish(svc, '/V', 13.5, deadband=0.5)
    assert publisher.publish(svc, '/V', 15.234, deadband=0.5) is True
    # Precise value lands on the bus, NOT a rounded one.
    assert svc['/V'] == 15.234


def test_deadband_negative_delta_uses_abs(publisher, svc):
    publisher.publish(svc, '/V', 13.5, deadband=0.5)
    # 12.9 is 0.6 V below 13.5 → ``abs >= 0.5`` → emit
    assert publisher.publish(svc, '/V', 12.9, deadband=0.5) is True


def test_deadband_compares_against_last_emitted_not_last_seen(publisher, svc):
    """Cache the last EMITTED value, not the last skipped one.
    Otherwise small steps could ratchet through the deadband one
    sub-deadband at a time and never trigger an emit even though
    the value has drifted far from the last published reading."""
    publisher.publish(svc, '/V', 13.0, deadband=0.5)
    publisher.publish(svc, '/V', 13.4, deadband=0.5)   # skip (0.4 < 0.5)
    publisher.publish(svc, '/V', 13.49, deadband=0.5)  # skip (0.49 < 0.5)
    # Now 13.5 vs last emitted (13.0) is exactly 0.5 → emit.
    assert publisher.publish(svc, '/V', 13.5, deadband=0.5) is True
    assert svc['/V'] == 13.5


def test_deadband_none_handling(publisher, svc):
    """None ↔ value transitions emit; None ↔ None inside heartbeat
    skips (same as rounded mode)."""
    publisher.publish(svc, '/V', None, deadband=0.5)
    # repeated None → skip
    assert publisher.publish(svc, '/V', None, deadband=0.5) is False
    # None → value → emit
    assert publisher.publish(svc, '/V', 13.0, deadband=0.5) is True
    # value → None → emit (stale-data hygiene)
    assert publisher.publish(svc, '/V', None, deadband=0.5) is True


def test_deadband_zero_disables(publisher, svc):
    """``deadband=0`` falls back to rounded-equality mode (the
    sentinel "positive deadband" rule).  Sensible since "emit on any
    change at all" is what rounded mode already does at ndigits=N."""
    publisher.publish(svc, '/V', 13.456, deadband=0)
    # deadband=0 ignored → falls back to no-rounding equality;
    # 13.457 != 13.456 → emit
    assert publisher.publish(svc, '/V', 13.457, deadband=0) is True


# ─── Heartbeat ──────────────────────────────────────────────────────


def test_heartbeat_re_publish(publisher, svc, monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr('sensor_publisher.time.monotonic',
                        lambda: fake_now[0])

    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is True
    fake_now[0] = 100.0
    # Inside heartbeat (default 900) → skip
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is False
    fake_now[0] = 950.0  # past heartbeat
    # Outside heartbeat → write
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is True


def test_per_path_heartbeat_independent(publisher, svc, monkeypatch):
    """Each (service, path) tracks its own clock — no thundering herd."""
    fake_now = [0.0]
    monkeypatch.setattr('sensor_publisher.time.monotonic',
                        lambda: fake_now[0])

    publisher.publish(svc, '/Temp', 23.5, 'temperature')             # at t=0
    fake_now[0] = 500.0
    publisher.publish(svc, '/Volt', 12.34, 'voltage')                # at t=500

    fake_now[0] = 950.0
    # Temp first published at 0, now 950 — past heartbeat → write
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is True
    # Volt first published at 500, now 950 — only 450 elapsed → skip
    assert publisher.publish(svc, '/Volt', 12.34, 'voltage') is False


def test_heartbeat_zero_disables_keepalive(monkeypatch, svc):
    """``heartbeat=0`` means 'never re-emit unchanged values'."""
    SensorPublisher._INSTANCE = None
    publisher = SensorPublisher(FakePolicy(heartbeat=0))

    fake_now = [0.0]
    monkeypatch.setattr('sensor_publisher.time.monotonic',
                        lambda: fake_now[0])

    publisher.publish(svc, '/Temp', 23.5, 'temperature')
    fake_now[0] = 1_000_000.0  # arbitrarily far future
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is False


def test_force_after_unchanged_resets_clock(publisher, svc, monkeypatch):
    """``force=True`` writes AND resets the heartbeat clock."""
    fake_now = [0.0]
    monkeypatch.setattr('sensor_publisher.time.monotonic',
                        lambda: fake_now[0])

    publisher.publish(svc, '/Temp', 23.5, 'temperature')             # t=0
    fake_now[0] = 500.0
    publisher.publish(svc, '/Temp', 23.5, 'temperature', force=True)  # forces write at t=500
    fake_now[0] = 1100.0  # 600s after the forced write — still inside HB
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is False
    fake_now[0] = 1500.0  # 1000s after forced write — past HB
    assert publisher.publish(svc, '/Temp', 23.5, 'temperature') is True


# ─── Cache lifetime ─────────────────────────────────────────────────


def test_weakref_cleans_up_on_gc(publisher):
    """When a role_service is GC'd, its entries vanish from the cache."""
    svc = FakeRoleService()
    publisher.publish(svc, '/X', 1.0)
    assert len(publisher._last) == 1
    del svc
    gc.collect()
    assert len(publisher._last) == 0


def test_two_services_isolated(publisher):
    """Two services don't share dedup state."""
    a = FakeRoleService('a')
    b = FakeRoleService('b')
    publisher.publish(a, '/Temp', 23.5, 'temperature')
    # b should still write — it has no last_value yet for /Temp
    assert publisher.publish(b, '/Temp', 23.5, 'temperature') is True


# ─── Singleton ──────────────────────────────────────────────────────


def test_get_returns_singleton(publisher):
    assert SensorPublisher.get() is publisher
