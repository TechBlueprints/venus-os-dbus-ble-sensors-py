"""The key request must be asked in both CBOR array dialects.

victron_vreg.cbor_array records the split: the IP22 and Orion want the
indefinite form (0x9F ... 0xFF), while a SmartShunt rejects that on the
control channel and answers only definite-length arrays.

Asking in one dialect only means a device of the other kind is queried
in a form it will not answer.  It stays silent through a perfectly good
PUK+PIN auth and the link dies with 0xEC65 unread -- observed on the
SmartSolar MPPT 75/15, which pairs, returns a good PUK CRC, accepts the
PIN, and then never pushes the key.

Indefinite must stay first so devices that already work are asked
exactly as they were.
"""
from __future__ import annotations

import os
import re

SRC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "opt", "victronenergy",
                                    "dbus-ble-sensors-py"))


def test_both_encodings_are_distinct_on_the_wire() -> None:
    import sys
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    import victron_vreg as v
    batch = [v.VREG_BLE_MAC_ADDRESS, v.VREG_ADVERTISEMENT_KEY]
    indef = v.encode_read_commands(batch, instance=0, definite=False)
    defin = v.encode_read_commands(batch, instance=0, definite=True)
    assert indef != defin
    assert indef.startswith(b"\x05\x00\x9f") and indef.endswith(b"\xff")
    assert defin.startswith(b"\x05\x00\x82"), "definite-length array header"


def test_key_read_asks_in_both_dialects_indefinite_first() -> None:
    src = open(os.path.join(SRC, "hex_key_session.py")).read()
    loops = re.findall(r"for definite in \((.*?)\):", src)
    assert len(loops) >= 2, (
        "both the pre-auth and post-auth instance loops must try both forms")
    for order in loops:
        assert order.replace(" ", "") == "False,True", (
            "indefinite must be tried first so working devices are "
            f"unchanged; got ({order})")
