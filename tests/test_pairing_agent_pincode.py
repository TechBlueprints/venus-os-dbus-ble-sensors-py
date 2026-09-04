"""The pairing agent must answer both forms of the same secret.

BlueZ asks for a passkey (uint32) or a PIN code (string) depending on
what the peer negotiates.  Implementing only the numeric form means a
device that asks for the string gets no answer, and the pairing fails
as AuthenticationFailed -- indistinguishable from a wrong PIN.

As a string the leading zero is part of the secret: a PIN displayed as
014916 is "014916", not "14916".
"""
from __future__ import annotations

import os
import re

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))


def _agent_src() -> str:
    src = open(os.path.join(SRC, "ble_gatt_dbus.py")).read()
    i = src.index("class _PairingAgent")
    return src[i:src.index("\nclass ", i + 10)]


def test_agent_answers_both_passkey_and_pincode() -> None:
    a = _agent_src()
    assert "def RequestPasskey" in a, "numeric form must stay"
    assert "def RequestPinCode" in a, (
        "a peer that asks for the string form currently gets no answer, "
        "which surfaces as AuthenticationFailed and looks like a wrong PIN")


def test_pincode_is_zero_padded_to_six_digits() -> None:
    a = _agent_src()
    assert '"%06d"' in a, (
        'the PIN string must be zero-padded: 014916 is not 14916')
    assert "%06d" % 14916 == "014916"


def test_pincode_returns_a_string_not_a_number() -> None:
    a = _agent_src()
    m = re.search(r'def RequestPinCode[\s\S]*?out_signature="s"', a) or \
        re.search(r'out_signature="s"\)\s*\n\s*def RequestPinCode', a)
    assert m or 'out_signature="s"' in a, "RequestPinCode must declare a string return"
