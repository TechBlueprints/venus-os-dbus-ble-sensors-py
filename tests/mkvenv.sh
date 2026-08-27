#!/bin/sh
# Create the test virtualenv on an interpreter matching the device.
#
# Venus OS runs Python 3.12 and the service uses 3.10+ syntax, so a
# host python3 that is older cannot import dbus_ble_sensors at all —
# the tests for it would skip and the suite would look green.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PY:-python3.12}
command -v "$PY" >/dev/null 2>&1 || {
    echo "error: $PY not found — brew install python@3.12" >&2; exit 1; }
"$PY" -m venv "$ROOT/.venv-test"
"$ROOT/.venv-test/bin/pip" install -q --upgrade pip
"$ROOT/.venv-test/bin/pip" install -q -r "$ROOT/tests/requirements.txt"
echo "created $ROOT/.venv-test ($("$ROOT/.venv-test/bin/python" -V))"
