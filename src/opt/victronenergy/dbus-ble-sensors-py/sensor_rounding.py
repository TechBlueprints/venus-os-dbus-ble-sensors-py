from __future__ import annotations

"""Settings-backed sensor rounding & heartbeat policy.

Sensor values arriving from BLE devices typically carry far more
precision than the GUI displays (Ruuvi temperature at 0.005 °C, Mopeka
tank voltage at 0.001 V, etc.).  Sensor noise then flips the bottom
bits on every advertisement, defeating vedbus's per-path
already-published-value dedup and forcing an ItemsChanged emit on the
system bus for every ad.

This module owns the rounding policy (per sensor type) and the
republish heartbeat — both surfaced as user-tunable settings under
``/Settings/SensorRounding/``.  Settings auto-create on first run with
the defaults below; live changes are applied without a service
restart via the per-setting callback.

Companion: :mod:`sensor_publisher` consumes this policy to decide
whether to actually write a value to a vedbus path.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dbus_settings_service import DbusSettingsService


# Per-type defaults: (default decimals, min, max).  The integer is the
# ``ndigits`` argument to Python's ``round()`` — number of digits after
# the decimal point.  Tuned to match what the GUI / VRM actually
# displays; finer precision is sub-display noise.
DEFAULTS: dict[str, tuple[int, int, int]] = {
    'temperature':   (1, 0, 3),   # 0.1 °C
    'humidity':      (1, 0, 3),   # 0.1 %
    'pressure':      (0, 0, 3),   # 1 hPa
    'voltage':       (2, 0, 4),   # 0.01 V
    'current':       (2, 0, 4),   # 0.01 A
    'power':         (0, 0, 3),   # 1 W
    'soc':           (1, 0, 3),   # 0.1 %
    'efficiency':    (2, 0, 4),   # 0.01 %
    'acceleration':  (2, 0, 4),   # 0.01 g
    'luminosity':    (0, 0, 2),   # 1 lux
    'concentration': (0, 0, 3),   # 1 ppm / µg·m⁻³
    'distance':      (1, 0, 3),   # 0.1 cm
    'percent':       (1, 0, 3),   # 0.1 %  (generic fallback)
}

# The republish-heartbeat: maximum interval (s) between ItemsChanged
# emits for a stable value.  Drives both the byte-level dedup in
# ``dbus_ble_sensors.py`` (early reject of identical raw blobs) and the
# publish-level dedup in :class:`sensor_publisher.SensorPublisher`
# (late reject of unchanged rounded values).  ``0`` disables the
# heartbeat — values are emitted only when they actually change.
HEARTBEAT_DEFAULT = 900    # 15 min — preserves prior DEDUP_KEEPALIVE_SECONDS behavior
HEARTBEAT_MIN = 0
HEARTBEAT_MAX = 86400      # 1 day

_HEARTBEAT_KEY = '_heartbeat'   # cache key (underscore-prefixed to avoid collision with sensor types)
_HEARTBEAT_SETTING_PATH = '/Settings/SensorRounding/HeartbeatSeconds'


def _setting_path(sensor_type: str) -> str:
    return f"/Settings/SensorRounding/{sensor_type.title()}"


class SensorRoundingPolicy:
    """Single source of truth for rounding precision and heartbeat.

    Construct once in ``main()`` after the settings service is
    available; downstream code accesses the singleton via
    :meth:`get`.  Tests pass a fake ``settings`` object (any object
    exposing ``set_item(path, default, min_, max_, callback=...)``
    that returns something with a ``get_value()`` method).
    """

    _INSTANCE: 'SensorRoundingPolicy | None' = None

    def __init__(self, settings: 'DbusSettingsService'):
        SensorRoundingPolicy._INSTANCE = self
        self._cache: dict[str, int] = {}

        for ttype, (default, min_, max_) in DEFAULTS.items():
            item = settings.set_item(
                _setting_path(ttype), default, min_, max_,
                callback=self._make_cb(ttype),
            )
            try:
                self._cache[ttype] = int(item.get_value())
            except (TypeError, ValueError):
                self._cache[ttype] = default

        hb_item = settings.set_item(
            _HEARTBEAT_SETTING_PATH,
            HEARTBEAT_DEFAULT, HEARTBEAT_MIN, HEARTBEAT_MAX,
            callback=self._make_cb(_HEARTBEAT_KEY),
        )
        try:
            self._cache[_HEARTBEAT_KEY] = int(hb_item.get_value())
        except (TypeError, ValueError):
            self._cache[_HEARTBEAT_KEY] = HEARTBEAT_DEFAULT

    @staticmethod
    def get() -> 'SensorRoundingPolicy | None':
        return SensorRoundingPolicy._INSTANCE

    def _make_cb(self, key: str):
        def _cb(service_name, change_path, changes):
            try:
                self._cache[key] = int(changes['Value'])
            except (TypeError, ValueError, KeyError):
                pass
        return _cb

    @property
    def heartbeat_seconds(self) -> int:
        return self._cache[_HEARTBEAT_KEY]

    def round_value(self, value, sensor_type: 'str | None' = None,
                    override: 'int | None' = None):
        """Round *value* per the configured precision.

        *override*, when set, takes precedence over the type table —
        for niche per-reg precision needs (e.g. TxPower's discrete
        0.5 dBm steps).  An unrecognised *sensor_type* with no
        *override* returns the value unchanged.  ``None`` input
        returns ``None``.
        """
        if value is None:
            return None
        ndigits = override if override is not None else (
            self._cache.get(sensor_type) if sensor_type else None
        )
        if ndigits is None:
            return value
        try:
            return round(value, ndigits)
        except (TypeError, ValueError):
            return value
