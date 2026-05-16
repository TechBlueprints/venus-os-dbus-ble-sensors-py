from __future__ import annotations

"""Round + dedup + heartbeat-aware D-Bus property publisher.

Drivers should publish all sensor values through
:meth:`SensorPublisher.publish` rather than writing to
``role_service[path]`` directly.  The publisher tracks the
last-written rounded value per ``(role_service, path)`` in RAM and
skips redundant writes within the heartbeat window.

Two layers of dedup live in this codebase, both keyed off the same
heartbeat setting at ``/Settings/SensorRounding/HeartbeatSeconds``:

1. **Byte-level** in ``dbus_ble_sensors.py`` — drops re-broadcast
   identical raw advertisement blobs, saving CPU on parse/decrypt.
2. **Publish-level** here — drops writes whose rounded value matches
   what we last sent, saving D-Bus signal traffic.

The two are complementary, not redundant: byte-level catches
identical encrypted blobs (Orion-TR idle re-broadcast); publish-level
catches noisy values that round to the same display number.
"""

import time
import weakref
from typing import TYPE_CHECKING

from sensor_rounding import SensorRoundingPolicy

if TYPE_CHECKING:
    from dbus_role_service import DbusRoleService


class SensorPublisher:
    """Round + dedup + heartbeat publisher.  Singleton; access via :meth:`get`.

    The cache is a :class:`weakref.WeakKeyDictionary` keyed on the
    role-service object — when a service is destroyed (device
    disappeared), its entries vanish automatically.
    """

    _INSTANCE: 'SensorPublisher | None' = None

    def __init__(self, policy: SensorRoundingPolicy):
        SensorPublisher._INSTANCE = self
        self._policy = policy
        # role_service -> {path: (rounded_value, monotonic_t)}
        self._last: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()

    @staticmethod
    def get() -> 'SensorPublisher | None':
        return SensorPublisher._INSTANCE

    def publish(self, role_service: 'DbusRoleService', path: str, value,
                sensor_type: 'str | None' = None,
                override: 'int | None' = None,
                deadband: 'float | None' = None,
                force: bool = False) -> bool:
        """Write *value* (precise, unrounded) to ``role_service[path]``.

        Whether a write actually happens depends on **one of two
        change-detection modes**, picked by the parameters:

        **Rounded-equality mode** (default — ``deadband`` is None).
        The new value is rounded per ``sensor_type`` / ``override``
        and compared for equality against the last cached rounded
        value.  A write happens when:

          - the rounded value differs, OR
          - the heartbeat interval has elapsed, OR
          - *force* is True.

        Works well when the source value naturally clusters away from
        rounding boundaries.  Fails when the value sits at a boundary
        (e.g. ``/Dc/In/V`` hovering at 13.5 V with ``round(_, 0)``
        flipping between 13 and 14 on every advertisement).

        **Deadband mode** (``deadband`` set to a positive float).
        ``sensor_type`` and ``override`` are ignored for comparison.
        Instead the new precise value is compared against the last
        precise value cached, and a write happens when:

          - ``abs(value - last_value) >= deadband``, OR
          - the heartbeat interval has elapsed, OR
          - *force* is True.

        Robust against boundary-sitting values: a Orion-TR input
        voltage flickering 13.43 V ↔ 13.53 V (10 mV swing) is
        within any reasonable 0.5 V deadband and stays silent.

        In **both modes** the value actually written to D-Bus is the
        precise input; rounding/deadband only gates whether to emit.

        ``value=None`` is published the same way any other value is:
        if the cache already holds ``None`` for this path (and we're
        inside the heartbeat window), the write is skipped; if the
        cache holds a real value, ``None`` is written through to
        clear the stale reading.

        Returns ``True`` if a write happened, ``False`` if skipped.
        """
        # Pick the comparison key:
        #   deadband mode → cache the precise value
        #   rounded mode  → cache the rounded value
        # That way the next call's comparison reads the right thing.
        if deadband is not None and deadband > 0:
            cmp_key = value
        else:
            cmp_key = self._policy.round_value(value, sensor_type, override)

        now = time.monotonic()
        cache = self._last.setdefault(role_service, {})
        last = cache.get(path)
        if not force and last is not None:
            last_key, last_t = last
            unchanged = False
            if deadband is not None and deadband > 0:
                # Deadband: compare precise values numerically.
                # Treat None on either side as "different" so a
                # stale-to-fresh transition (or vice versa) always
                # emits.
                if value is None or last_key is None:
                    unchanged = (value is None and last_key is None)
                else:
                    try:
                        unchanged = abs(value - last_key) < deadband
                    except TypeError:
                        unchanged = (value == last_key)
            else:
                unchanged = (cmp_key == last_key)

            if unchanged:
                hb = self._policy.heartbeat_seconds
                # ``hb <= 0`` disables heartbeat: never republish
                # an unchanged value.  Otherwise republish once the
                # interval has elapsed.
                if hb <= 0 or (now - last_t) < hb:
                    return False

        role_service[path] = value
        cache[path] = (cmp_key, now)
        return True
