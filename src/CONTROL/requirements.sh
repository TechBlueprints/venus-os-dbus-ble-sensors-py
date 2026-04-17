#!/bin/bash
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")

# Getting velib_python files, as it is not available as a package...
mkdir -p "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python"
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/vedbus.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/vedbus.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/logger.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/logger.py
wget -O "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/velib_python/ve_utils.py" https://raw.githubusercontent.com/victronenergy/velib_python/refs/heads/master/ve_utils.py

# victron_ble: used by the Orion-TR driver for advertisement decryption.
# Install from PyPI then patch base.py to use cryptography (shipped in
# Venus OS) instead of PyCryptodome (not available).
pip3 install victron-ble --no-deps --target "$SCRIPT_DIR/../opt/victronenergy/dbus-ble-sensors-py/ext/"
