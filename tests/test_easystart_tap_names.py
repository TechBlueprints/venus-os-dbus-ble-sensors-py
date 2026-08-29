"""HCI tap: advertised-name extraction for name-identified devices.

The EasyStart advertises no manufacturer data — its advertisement is a
local name and nothing we consume.  These tests pin the tap behaviour
that makes such devices visible at all: names are decoded only when a
prefix filter is supplied, only matching names are surfaced, and ads
carrying neither matching name nor matching manufacturer data are still
dropped.
"""
from __future__ import annotations

import struct

from hci_advertisement_tap import parse_monitor_frame

_FRAME_HDR = struct.Struct("<HHH")
_OP_HCI_EVENT_RX = 3
_EVT_LE_META = 0x3E
_SUB_ADV_REPORT = 0x02


def make_legacy_frame(mac_bytes: bytes, ad_data: bytes, adapter=0,
                      rssi=0xC4, addr_type=1) -> bytes:
    """One monitor datagram containing one legacy advertising report."""
    report = bytes([0x00, addr_type]) + mac_bytes[::-1] \
        + bytes([len(ad_data)]) + ad_data + bytes([rssi])
    payload = bytes([_EVT_LE_META, 0x00, _SUB_ADV_REPORT, 0x01]) + report
    # parse_monitor_frame reads the subevent at raw[8] = payload[2] and
    # hands the report list offset 3 into the payload.
    return _FRAME_HDR.pack(_OP_HCI_EVENT_RX, adapter, len(payload)) + payload


def ad_name(name: str, complete=True) -> bytes:
    encoded = name.encode()
    return bytes([len(encoded) + 1, 0x09 if complete else 0x08]) + encoded


def ad_mfg(company: int, data: bytes) -> bytes:
    body = struct.pack('<H', company) + data
    return bytes([len(body) + 1, 0xFF]) + body


MAC = bytes.fromhex('aabbccddeeff')


def test_matching_name_is_surfaced():
    frame = make_legacy_frame(MAC, ad_name('EasyStart_7F3A'))
    advs = parse_monitor_frame(frame, name_prefixes=('EasyStart_',))
    assert len(advs) == 1
    adv = advs[0]
    assert adv.local_name == 'EasyStart_7F3A'
    assert adv.manufacturer_data == {}
    assert adv.mac == 'aabbccddeeff'


def test_bare_prefix_name_matches():
    frame = make_legacy_frame(MAC, ad_name('EasyStart_'))
    advs = parse_monitor_frame(frame, name_prefixes=('EasyStart_',))
    assert len(advs) == 1
    assert advs[0].local_name == 'EasyStart_'


def test_shortened_name_ad_type_also_matches():
    frame = make_legacy_frame(MAC, ad_name('EasyStart_7F3A', complete=False))
    advs = parse_monitor_frame(frame, name_prefixes=('EasyStart_',))
    assert len(advs) == 1
    assert advs[0].local_name == 'EasyStart_7F3A'


def test_non_matching_name_is_dropped():
    frame = make_legacy_frame(MAC, ad_name('KitchenSpeaker'))
    advs = parse_monitor_frame(frame, name_prefixes=('EasyStart_',))
    assert advs == []


def test_names_not_decoded_without_prefix_filter():
    # No name_prefixes: pre-existing behaviour, mfg data only.
    frame = make_legacy_frame(MAC, ad_name('EasyStart_7F3A'))
    assert parse_monitor_frame(frame) == []


def test_mfg_data_still_flows_alongside_name_filter():
    frame = make_legacy_frame(MAC, ad_mfg(0x02E1, b'\x10\x02\x01\x02'))
    advs = parse_monitor_frame(frame, mfg_filter={0x02E1},
                               name_prefixes=('EasyStart_',))
    assert len(advs) == 1
    assert advs[0].manufacturer_data == {0x02E1: b'\x10\x02\x01\x02'}
    assert advs[0].local_name is None


def test_name_and_mfg_in_one_advertisement():
    frame = make_legacy_frame(
        MAC, ad_name('EasyStart_7F3A') + ad_mfg(0x1234, b'\x01'))
    advs = parse_monitor_frame(frame, mfg_filter={0x1234},
                               name_prefixes=('EasyStart_',))
    assert len(advs) == 1
    assert advs[0].local_name == 'EasyStart_7F3A'
    assert advs[0].manufacturer_data == {0x1234: b'\x01'}


def test_ignored_mac_drops_named_advertisement():
    frame = make_legacy_frame(MAC, ad_name('EasyStart_7F3A'))
    advs = parse_monitor_frame(frame, name_prefixes=('EasyStart_',),
                               ignored_macs={'aabbccddeeff'})
    assert advs == []


def test_undecodable_name_bytes_do_not_crash():
    bad = bytes([5, 0x09, 0xFF, 0xFE, 0x80, 0x81])
    frame = make_legacy_frame(MAC, bad)
    assert parse_monitor_frame(frame, name_prefixes=('EasyStart_',)) == []
