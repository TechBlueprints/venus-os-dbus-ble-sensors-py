"""
Persist SmartShunt / BMV-Smart BLE advertisement keys in
``com.victronenergy.settings``.

Kept in a separate namespace from the IP22 and Orion-TR keys so each
product family stays self-contained.  Paths live under
``/Settings/Devices/smartshunt_<mac>/``.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import adapter_identity
from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)


def _mac_key(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s


def advertisement_key_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/smartshunt_{_mac_key(dev_mac)}/AdvertisementKey"


def get_advertisement_key(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    path = advertisement_key_setting_path(dev_mac)
    raw = settings.try_get_value(path)
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
    logger.info("Stored SmartShunt advertisement key for %s", mk)


def firmware_version_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/smartshunt_{_mac_key(dev_mac)}/FirmwareVersion"


def get_firmware_version(settings: DbusSettingsService,
                         dev_mac: str) -> Optional[str]:
    path = firmware_version_setting_path(dev_mac)
    raw = settings.try_get_value(path)
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
    path = firmware_version_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored SmartShunt firmware version %r for %s", s, mk)


def preferred_adapter_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/smartshunt_{_mac_key(dev_mac)}/PreferredAdapter"


def get_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    path = preferred_adapter_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    # Canonicalize on read too, so a value written before this was
    # MAC-keyed is upgraded in flight rather than needing a settings
    # migration.  An unresolvable legacy name passes through unchanged:
    # it may still be correct, and this is a preference, not a pin.
    s = str(raw).strip()
    return adapter_identity.canonical(s) if s else None


def set_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str, adapter: str) -> None:
    """Store which adapter last connected successfully, by MAC.

    Stored as the card's own MAC, never as ``hciN``.  This value outlives
    reboots and replugs in com.victronenergy.settings and hciN numbering
    does not, so a stored ``hci0`` can come to name a different radio
    after a USB reset — at which point a "preferred adapter" sends the
    device to the wrong card.  That is the precise failure MAC identity
    exists to prevent, arriving through a setting meant to help.
    """
    mk = _mac_key(dev_mac)
    s = adapter_identity.canonical(str(adapter).strip())
    if not s:
        return
    path = preferred_adapter_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored preferred adapter %s for SmartShunt %s", s, mk)
