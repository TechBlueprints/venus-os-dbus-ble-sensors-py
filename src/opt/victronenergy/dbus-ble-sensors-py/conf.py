import os

# Project variables
PROCESS_NAME = os.path.basename(os.path.dirname(__file__))
PROCESS_VERSION = '1.1.1'

# Timeouts
IGNORED_DEVICES_TIMEOUT = 600   # 10 min
DEVICE_SERVICES_TIMEOUT = 3600  # 60 min

# Optional ``[orion] PairingPin=…`` override for Orion-TR BLE pairing (see ``orion_tr_pin.py``).
ORION_OPTIONAL_INI = "/data/conf/dbus-ble-sensors-py-orion.ini"

# --- shared BLE connection-manager location + policy (fleet contract) ---
# Where the shared bleak-connection-manager install lives.  Our config
# key, defaulting to /data/bcm; an EMPTY value means never look (a
# deliberate standalone run on a bare clone).  Sourced in-process by
# ble_stack.ensure_ble_stack — there is no launcher shim and no shared
# config file.  See bleak-connection-manager/CONSUMER_MIGRATION.md §2.
BLUETOOTH_CONNECTION_MANAGER_DIR = os.environ.get(
    "BLUETOOTH_CONNECTION_MANAGER_DIR", "/data/bcm")

# StartNotify policy, consumer-side (was BCM_FORCE_START_NOTIFY on the
# retired shim).  Passed as force_start_notify= at install_bleak_catcher.
# Default true: this fleet forces StartNotify to dodge the BlueZ 5.72
# AcquireNotify UAF.  Override with FORCE_START_NOTIFY=false.
FORCE_START_NOTIFY = os.environ.get(
    "FORCE_START_NOTIFY", "true").strip().lower() not in ("0", "false", "no", "")
