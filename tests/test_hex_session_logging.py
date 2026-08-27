"""Payload tracing must stay at DEBUG, and secrets must never reach INFO.

hex_key_session.py began life as a one-shot CLI where an operator watched
a terminal, so every frame went to INFO via _err().  Moved into a
long-running service that shipped its logs, those dumps became 58% of all
output — 254 of 437 lines in one quiet 7.5-minute window on prod, single
lines carrying ~470 characters of hex.  Volume like that is how a real
fault scrolls off the end of a log before anyone reads it.

Two of them carried credentials: the pairing passkey, and the nonce+PIN
payload.

These are source-level assertions because the failure is "which logger
level did the author pick", which no runtime test observes.
"""
from __future__ import annotations

import os
import re

SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "src", "opt",
    "victronenergy", "dbus-ble-sensors-py", "hex_key_session.py")

# _err() at INFO is for outcomes and decisions.  This one prints device
# response bytes only on a PIN rejection — rare, and the bytes are the
# diagnosis, not a secret.
_ALLOWED_INFO_HEX = "PIN responses="


def _err_calls() -> list[str]:
    src = open(SRC).read()
    return [m.group(0) for m in re.finditer(r"_err\((?:[^()]|\([^()]*\))*\)", src)]


def test_payload_dumps_are_not_logged_at_info() -> None:
    offenders = [c for c in _err_calls()
                 if ".hex()" in c and _ALLOWED_INFO_HEX not in c]
    assert not offenders, (
        "raw payload dumps must use _dbg() (DEBUG), not _err() (INFO):\n"
        + "\n".join(offenders))


def test_passkey_never_reaches_info() -> None:
    offenders = [c for c in _err_calls() if "passkey" in c]
    assert not offenders, (
        "the pairing passkey is a credential and must not be logged at "
        "INFO:\n" + "\n".join(offenders))


def test_dbg_helper_logs_at_debug() -> None:
    src = open(SRC).read()
    body = src.split("def _dbg(")[1].split("def ")[0]
    assert "logger.debug(" in body, "_dbg must log at DEBUG"
    assert "logger.info(" not in body, "_dbg must not log at INFO"
