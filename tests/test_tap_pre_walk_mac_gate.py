"""The tap drops a stranger before the AD walk when adoption is closed.

With the hardware accept list retired the radio is accept-all, so our own
cards deliver every neighbour.  A stranger is anything not in the known
set (configured devices, learned name-device addresses, router-registered
addresses).  With adoption closed we would refuse to adopt it anyway, so
parsing it is pure cost; the gate drops it after the MAC format and one
set lookup, before the TLV walk.  An empty/None set means discovery is
open and everything is walked.
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

OURS = "c120d54f7125"       # a configured device (tap format: lowercase, no colons)
STRANGER = "aabbccddeeff"


def _legacy(mac_hex: str, adapter_idx: int = 1) -> bytes:
    ad = bytes([0x02, 0x01, 0x06, 0x17, 0xFF, 0xE1, 0x02]) + bytes(20)   # Victron mfg data
    mac_le = bytes.fromhex(mac_hex)[::-1]
    rep = bytes([0x00, 0x01]) + mac_le + bytes([len(ad)]) + ad + bytes([0xC8])
    hci = bytes([0x3E, 1 + 1 + len(rep), 0x02, 0x01]) + rep
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(hci)) + hci


def _extended(mac_hex: str, adapter_idx: int = 1) -> bytes:
    """One LE Extended Advertising Report carrying the same Victron payload."""
    ad = bytes([0x02, 0x01, 0x06, 0x17, 0xFF, 0xE1, 0x02]) + bytes(20)
    mac_le = bytes.fromhex(mac_hex)[::-1]
    # event_type(2) addr_type(1) addr(6) pri_phy sec_phy sid tx_pwr rssi
    # periodic_int(2) direct_addr_type direct_addr(6) data_len data
    rep = (struct.pack("<H", 0x0013) + bytes([0x01]) + mac_le + bytes([1, 0, 0xFF, 0x7F, 0xC8])
           + struct.pack("<H", 0) + bytes([0]) + bytes(6) + bytes([len(ad)]) + ad)
    hci = bytes([0x3E, 1 + 1 + len(rep), 0x0D, 0x01]) + rep
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(hci)) + hci


@pytest.mark.parametrize("frame", [_legacy, _extended], ids=["legacy", "extended"])
def test_stranger_is_dropped_before_the_walk_when_a_known_set_is_given(frame) -> None:
    known = {OURS}
    assert tap.parse_monitor_frame(frame(STRANGER), known_macs=known) == []
    advs = tap.parse_monitor_frame(frame(OURS), known_macs=known)
    assert len(advs) == 1 and advs[0].mac == OURS


@pytest.mark.parametrize("frame", [_legacy, _extended], ids=["legacy", "extended"])
def test_empty_or_none_set_means_discovery_open_walk_everything(frame) -> None:
    assert len(tap.parse_monitor_frame(frame(STRANGER), known_macs=None)) == 1
    assert len(tap.parse_monitor_frame(frame(STRANGER), known_macs=set())) == 1


def test_gate_sits_after_ignored_macs_and_before_the_walk() -> None:
    src = open(os.path.join(SRC, "hci_advertisement_tap.py")).read()
    # both parsers carry it, and it precedes the walk in each
    assert src.count("if known_macs and mac not in known_macs:") == 2
    for parser in ("_parse_legacy_reports", "_parse_extended_reports"):
        body = src[src.index(f"def {parser}"):]
        assert body.index("mac not in known_macs") < body.index("_walk_ad_structures(ad_data")


def test_gate_is_plumbed_end_to_end() -> None:
    src = open(os.path.join(SRC, "hci_advertisement_tap.py")).read()
    assert "known_macs: 'set[str] | None' = None" in src
    assert "name_prefixes, allowed_adapters,\n                                       known_macs)" in src
