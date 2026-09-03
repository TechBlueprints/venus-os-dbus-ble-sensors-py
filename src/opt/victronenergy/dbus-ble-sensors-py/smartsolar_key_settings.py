"""Persist SmartSolar Instant Readout keys in ``com.victronenergy.settings``.

Own namespace, ``/Settings/Devices/smartsolar_<mac>/...``, mirroring
ip22_key_settings entry for entry: the advertisement key the driver
recovers over its one paired HEX session, the firmware string that
session reads alongside it, and the adapter (by MAC, never ``hciN``)
that session succeeded on, so the next one starts on the same card.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import adapter_identity
from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)

PREFIX = "smartsolar"

# A legacy "hci0"-style value.  See get_preferred_adapter.
_HCI_NAME = re.compile(r"^hci\d+$", re.IGNORECASE)


def _mac_key(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s


def _path(dev_mac: str, suffix: str) -> str:
    return f"/Settings/Devices/{PREFIX}_{_mac_key(dev_mac)}/{suffix}"


def _store(settings: DbusSettingsService, path: str, value: str) -> None:
    settings.set_item(path, value, 0, 0, silent=True)
    settings.set_value(path, value)


# --- advertisement key ------------------------------------------------

def advertisement_key_setting_path(dev_mac: str) -> str:
    return _path(dev_mac, "AdvertisementKey")


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
    _store(settings, advertisement_key_setting_path(dev_mac), s)
    logger.info("Stored SmartSolar advertisement key for %s", mk)


# --- firmware version -------------------------------------------------

def firmware_version_setting_path(dev_mac: str) -> str:
    return _path(dev_mac, "FirmwareVersion")


def get_firmware_version(settings: DbusSettingsService,
                         dev_mac: str) -> Optional[str]:
    raw = settings.try_get_value(firmware_version_setting_path(dev_mac))
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def set_firmware_version(settings: DbusSettingsService,
                         dev_mac: str, version: str) -> None:
    mk = _mac_key(dev_mac)
    s = str(version).strip()
    if not s:
        return
    _store(settings, firmware_version_setting_path(dev_mac), s)
    logger.info("Stored SmartSolar firmware version %r for %s", s, mk)


# --- preferred adapter ------------------------------------------------

def preferred_adapter_setting_path(dev_mac: str) -> str:
    return _path(dev_mac, "PreferredAdapter")


def get_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    raw = settings.try_get_value(preferred_adapter_setting_path(dev_mac))
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # An hciN value names whichever card enumerated first at some past
    # moment, not the card that was meant; it cannot be upgraded, only
    # laundered into a confident wrong MAC.  No preference is the honest
    # answer: placement falls through to the pool, and the next
    # successful session rewrites this as a MAC.  Same rule as ip22.
    if _HCI_NAME.match(s):
        logger.info("%s: ignoring legacy hciN preference %r — the number "
                    "names whichever card enumerated first, not the card "
                    "it meant; it will be rewritten as a MAC on the next "
                    "successful connect", dev_mac, s)
        return None
    return adapter_identity.canonical(s)


def set_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str, adapter: str) -> None:
    """Store which adapter last connected successfully, by MAC, never hciN."""
    mk = _mac_key(dev_mac)
    s = adapter_identity.canonical(str(adapter).strip())
    if not s:
        return
    _store(settings, preferred_adapter_setting_path(dev_mac), s)
    logger.info("Stored preferred adapter %s for SmartSolar %s", s, mk)
