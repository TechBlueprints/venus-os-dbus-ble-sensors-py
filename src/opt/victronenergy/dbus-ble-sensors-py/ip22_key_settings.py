"""
Persist Blue Smart IP22 BLE advertisement keys in
``com.victronenergy.settings``.

Kept in a separate namespace from the Orion-TR keys so each product
family stays self-contained in the settings tree.  Paths live under
``/Settings/Devices/ip22_<mac>/`` alongside the standard per-device
``CustomName`` / ``Enabled`` entries.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import adapter_identity
from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)

# A stored preference in hciN form is a legacy value from before this
# setting was MAC-keyed.  It is never written this way now.
_HCI_NAME = re.compile(r"^hci\d+$", re.IGNORECASE)

def _mac_key(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s

def advertisement_key_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/AdvertisementKey"

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
    logger.info("Stored IP22 advertisement key for %s", mk)

def firmware_version_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/FirmwareVersion"

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
    logger.info("Stored IP22 firmware version %r for %s", s, mk)

def preferred_adapter_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/PreferredAdapter"

def get_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    path = preferred_adapter_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    # Canonicalize on read too, so a value written before this was
    # MAC-keyed is upgraded in flight rather than needing a settings
    # migration.
    s = str(raw).strip()
    if not s:
        return None

    # ...but an hciN-form value cannot be upgraded, only laundered.
    # canonical("hci0") resolves to whatever card answers to hci0 RIGHT
    # NOW, turning a number recorded at an unknown past moment into a
    # confident, authoritative-looking MAC for hardware that may never
    # have been the one meant.  On dev, hci0 named three different
    # physical cards inside one hour.
    #
    # There is no way to recover which card was intended, so the honest
    # answer is no preference at all: placement falls through to the
    # configured pool and connected-then-bonded ranking, and the next
    # successful connect rewrites this setting as a MAC.  Self-healing
    # beats a settings migration, and beats a confident wrong answer.
    if _HCI_NAME.match(s):
        logger.info("%s: ignoring legacy hciN preference %r — the number "
                    "names whichever card enumerated first, not the card "
                    "it meant; it will be rewritten as a MAC on the next "
                    "successful connect", dev_mac, s)
        return None

    return adapter_identity.canonical(s)

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
    logger.info("Stored preferred adapter %s for IP22 %s", s, mk)
