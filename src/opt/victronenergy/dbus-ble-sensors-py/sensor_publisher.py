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
                force: bool = False) -> bool:
        """Write *value* (precise, unrounded) to ``role_service[path]``
        only when:

        - the *rounded* value differs from the last published one, OR
        - the heartbeat interval has elapsed since the last publish, OR
        - *force* is True.

        Rounding is applied **only for the change-detection comparison**.
        The value actually written to D-Bus is the original precise
        value the driver passed in, so downstream consumers (VRM, MQTT,
        gui-v2) still see full resolution when an emit happens.  This
        is the same pattern used in ``dbus-aggregate-smartshunts``
        (commits c8ecb18 / 90cb683) and ``dbus-virtual-dcsystem``
        (ecd2659): coarse comparison gates noisy emits, but the data
        on the bus stays precise.

        ``value=None`` is published the same way any other value is:
        if the cache already holds ``None`` for this path (and we're
        inside the heartbeat window), the write is skipped; if the
        cache holds a real value, ``None`` is written through to
        clear the stale reading.  This matches what drivers like the
        IP22 charger do when a device transitions to ``Off`` and we
        want stale voltage/current readings to vanish from the GUI
        rather than linger.

        Returns ``True`` if a write happened, ``False`` if skipped.
        """
        rounded = self._policy.round_value(value, sensor_type, override)

        now = time.monotonic()
        cache = self._last.setdefault(role_service, {})
        last = cache.get(path)
        if not force and last is not None:
            last_rounded, last_t = last
            if rounded == last_rounded:
                hb = self._policy.heartbeat_seconds
                # ``hb <= 0`` disables heartbeat: never republish
                # an unchanged value.  Otherwise republish once the
                # interval has elapsed.
                if hb <= 0 or (now - last_t) < hb:
                    return False

        # Emit the *precise* value, cache the *rounded* one for the
        # next comparison.  Storing precise would make us re-emit on
        # every sub-rounding flicker — the whole point of the rounded
        # comparison is to avoid that.
        role_service[path] = value
        cache[path] = (rounded, now)
        return True
