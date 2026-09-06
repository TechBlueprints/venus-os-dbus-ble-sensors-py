import os

# Project variables
PROCESS_NAME = os.path.basename(os.path.dirname(__file__))
PROCESS_VERSION = '1.1.1'

# Timeouts
IGNORED_DEVICES_TIMEOUT = 600   # 10 min
DEVICE_SERVICES_TIMEOUT = 3600  # 60 min

# Optional ``[orion] PairingPin=…`` override for Orion-TR BLE pairing (see ``orion_tr_pin.py``).
ORION_OPTIONAL_INI = "/data/conf/dbus-ble-sensors-py-orion.ini"

# --- shared BLE connection-manager: three consumer-side keys (fleet ----
# contract, names shared with the other consumers).  See
# bleak-connection-manager/CONSUMERS.md §2.  There is no shared
# config file: each consumer configures its own.
def _envbool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")

# Enable coordination through the shared install.  Off = run uncoordinated
# on plain bleak, no catcher, no "loaded from" line.  Default on.
BLUETOOTH_CONNECTION_MANAGER = _envbool("BLUETOOTH_CONNECTION_MANAGER", True)

# Where the shared install lives.  Default /data/bcm; empty means never
# look.  Sourced in-process by ble_stack.ensure_ble_stack — no launcher shim.
BLUETOOTH_CONNECTION_MANAGER_DIR = os.environ.get(
    "BLUETOOTH_CONNECTION_MANAGER_DIR", "/data/bcm")

# StartNotify policy (was the retired shim's BCM_FORCE_START_NOTIFY).
# Default true: force StartNotify to dodge the BlueZ 5.72 AcquireNotify UAF.
BLUETOOTH_CONNECTION_MANAGER_FORCE_START_NOTIFY = _envbool(
    "BLUETOOTH_CONNECTION_MANAGER_FORCE_START_NOTIFY", True)
