"""The tap's name-prefix match must accept a set, not only a tuple.

PR #24 made the tap's name-prefix collection a live mutable set so router
registrations could update it in place.  The AD walk matched names with
``str.startswith(name_prefixes)``, which takes a tuple but raises TypeError
on a set.  It only fires on a frame carrying a local name, so the router
and early-drop tests never reached it; prod 2026-09-11 13:42Z did, and the
tap thread crash-looped until rollback.  This is the test that would have
caught it.
"""
from __future__ import annotations

import os
import struct
import sys

import pytest

SRC = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import hci_advertisement_tap as tap  # noqa: E402


def _named_frame(local_name: str, adapter_idx: int = 1) -> bytes:
    """A legacy advertising report whose AD carries a Complete Local Name."""
    nm = local_name.encode()
    ad = bytes([0x02, 0x01, 0x06]) + bytes([len(nm) + 1, 0x09]) + nm   # flags + name
    rep = bytes([0x00, 0x00]) + bytes.fromhex("aabbccddeeff") + bytes([len(ad)]) + ad + bytes([0xC8])
    hci = bytes([0x3E, 1 + 1 + len(rep), 0x02, 0x01]) + rep
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(hci)) + hci


@pytest.mark.parametrize("prefixes", [
    ("EasyStart_",),                 # the historical tuple
    {"EasyStart_"},                  # the live mutable set from #24
    {"EasyStart_", "WD_", "PM"},     # internal + external registrations
    frozenset({"EasyStart_"}),
])
def test_a_named_advert_matches_whatever_collection_type_is_passed(prefixes) -> None:
    advs = tap.parse_monitor_frame(_named_frame("EasyStart_1234"), None, None, prefixes)
    assert len(advs) == 1
    assert advs[0].local_name == "EasyStart_1234"


def test_a_set_of_prefixes_does_not_raise_on_a_non_matching_name() -> None:
    advs = tap.parse_monitor_frame(_named_frame("Nothing_here"), None, None, {"EasyStart_"})
    # no match -> no name; must not raise either way
    assert all(a.local_name in (None, "") for a in advs)


def test_the_walk_no_longer_hands_a_raw_collection_to_startswith() -> None:
    src = open(os.path.join(SRC, "hci_advertisement_tap.py")).read()
    assert "decoded.startswith(tuple(name_prefixes))" in src
    assert "decoded.startswith(name_prefixes)" not in src
