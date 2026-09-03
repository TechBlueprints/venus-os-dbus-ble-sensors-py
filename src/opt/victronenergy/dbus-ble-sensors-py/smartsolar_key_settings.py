"""Persist SmartSolar Instant Readout keys in ``com.victronenergy.settings``.

Own namespace, ``/Settings/Devices/smartsolar_<mac>/AdvertisementKey``,
mirroring ip22_key_settings.  Deliberately no preferred-adapter or
firmware entries: this driver never opens a GATT link, so it has no
adapter to remember and no firmware to read.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)

PREFIX = "smartsolar"


def _mac_key(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s


def advertisement_key_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/{PREFIX}_{_mac_key(dev_mac)}/AdvertisementKey"


def get_advertisement_key(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    raw = settings.try_get_value(advertisement_key_setting_path(dev_mac))
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        return None
    return s


def set_advertisement_key(settings: DbusSettingsService,
                          dev_mac: str, key_hex: str) -> None:
    mk = _mac_key(dev_mac)
    s = str(key_hex).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("key must be 32 hex characters")
    path = advertisement_key_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored SmartSolar advertisement key for %s", mk)
