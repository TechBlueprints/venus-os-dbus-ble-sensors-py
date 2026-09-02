"""The notify path is fleet policy; nothing in this tree may opt out.

On 2026-09-02 the shared BLE stack began forcing StartNotify for every
consumer (``BCM_FORCE_START_NOTIFY=true`` from the /data/bcm shim), because
AcquireNotify is what creates bluetoothd 5.72's ``notify_io`` — the
use-after-free behind ~240 SIGSEGVs on prod in one day.  A caller that
still passes ``bluez={"use_start_notify": False}`` has it rewritten at the
wrapper and earns one warning per device address for asking.

Three call sites in this tree carried that dead opt-out, plus an env-var
escape hatch.  These guards keep them from coming back: a re-added
opt-out changes nothing on the radio, only produces a misleading warning
and a test asserting a request the radio never sees.
"""
from __future__ import annotations

import os
import re

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))

NOTIFY_MODULES = ["hex_key_session.py", "orion_tr_gatt.py", "smartshunt_hex.py"]


def _src(name: str) -> str:
    return open(os.path.join(SRC, name)).read()


def test_no_module_asks_for_acquire_notify() -> None:
    for name in NOTIFY_MODULES:
        code = re.sub(r'"""[\s\S]*?"""', "", _src(name))   # ignore docstrings
        code = re.sub(r"#.*", "", code)                    # and comments
        assert "use_start_notify" not in code, (
            f"{name} passes use_start_notify: the wrapper overrides it "
            f"and logs a warning per device; policy lives in BCM")


def test_the_env_escape_hatch_is_gone() -> None:
    for name in NOTIFY_MODULES:
        assert "HEX_START_NOTIFY" not in _src(name)
        assert "_PREFER_START_NOTIFY" not in _src(name)


def test_the_open_question_is_recorded_where_someone_will_look() -> None:
    """Dropping the opt-out must not drop the unverified finding with it."""
    doc = _src("hex_key_session.py")
    assert "EMPTY payloads" in doc or "empty payloads" in doc
    assert "2026-09-02 13:50Z" in doc, (
        "the first Recovered-key line after the policy took effect is the "
        "test of the empty-payload claim; record the boundary")


def test_install_inherits_the_fleet_setting() -> None:
    """Passing force_start_notify here would pin or override policy."""
    code = re.sub(r'"""[\s\S]*?"""', "", _src("ble_catcher.py"))
    code = re.sub(r"#.*", "", code)
    assert "force_start_notify" not in code
