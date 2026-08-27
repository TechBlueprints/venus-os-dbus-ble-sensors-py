#!/bin/sh
# Convenience wrapper: run the BLE-charger test suite from the repo root.
#
#   ./tests/run.sh            # all tests, verbose
#   ./tests/run.sh -k history # only history-related tests
#
# Sets PYTHONPATH so the shared module (``ble_charger_common``) is
# importable without installing anything.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DRIVER="$ROOT/src/opt/victronenergy/dbus-ble-sensors-py"
EXT="$DRIVER/ext:$DRIVER/ext/velib_python"

# Pick an interpreter that matches the device.  Venus OS runs Python
# 3.12, and the service uses 3.10+ syntax (dataclass(slots=True)), so a
# plain `python3` that happens to be older silently SKIPS the modules
# that need it — dbus_ble_sensors among them, which is most of the
# service.  A green run against the wrong interpreter is worse than a
# red one, so refuse rather than skip.
if [ -x "$ROOT/.venv-test/bin/python" ]; then
    PY="$ROOT/.venv-test/bin/python"          # created by: make test-venv
elif command -v python3.12 >/dev/null 2>&1; then
    PY=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
    PY=python3.11
elif command -v python3.10 >/dev/null 2>&1; then
    PY=python3.10
else
    PY=python3
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "error: need Python 3.10+ to match the device (Venus runs 3.12);" >&2
    echo "       found $($PY -V 2>&1) at $PY" >&2
    echo "       fix: brew install python@3.12 && ./tests/mkvenv.sh" >&2
    exit 1
fi

PYTHONPATH="$DRIVER:$EXT:$HERE" \
    exec "$PY" -m pytest "$HERE" -v "$@"
