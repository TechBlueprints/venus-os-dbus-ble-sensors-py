"""Micro-Air EasyStart protocol: framing and payload decoding.

Pure-logic tests against synthetic frames built from the offsets in
``docs/EASYSTART-PROTOCOL.md``.  No dbus, no bleak, no hardware.
"""
from __future__ import annotations

import struct

import easystart_protocol as proto


def make_live(state=0, learned=3, current_a=12.3, period_raw=8333,
              peak_a=45.6, scpt_s=120, faults=2, starts=1234) -> bytes:
    buf = bytearray(20)
    buf[0:2] = b'\x00\x00'
    buf[2] = state
    buf[3] = learned
    struct.pack_into('<HHHHH', buf, 4,
                     int(round(current_a * 10)), period_raw,
                     int(round(peak_a * 10)), scpt_s, faults)
    struct.pack_into('<I', buf, 14, starts)
    return bytes(buf)


def make_config(fw=27, smask=0x02, fmask=0x7F, scpt=5,
                length=1100) -> bytes:
    buf = bytearray(length)
    for offset, value in ((10, fw), (906, smask), (907, fmask), (908, scpt)):
        if offset < length:
            buf[offset] = value
    return bytes(buf)


# ── Commands are literal constants ─────────────────────────────────────

def test_read_live_command_bytes_match_observed_hex():
    # 7B 22 43 6D 64 22 3A 20 52 65 61 64 4C 69 76 65 7D
    assert proto.CMD_READ_LIVE == bytes.fromhex(
        '7B22436D64223A20526561644C6976657D')
    assert len(proto.CMD_READ_LIVE) == 17


def test_read_eep_command_is_ascii_literal():
    assert proto.CMD_READ_EEP == b'{"Cmd": ReadEEP}'


def test_no_mutating_commands_defined():
    # The module must define exactly the two read commands and nothing
    # that could write device state.
    cmds = [name for name in dir(proto) if name.startswith('CMD_')]
    assert sorted(cmds) == ['CMD_READ_EEP', 'CMD_READ_LIVE']
    for name in cmds:
        assert b'Read' in getattr(proto, name)


# ── Terminator classification ──────────────────────────────────────────

def test_success_terminator():
    payload = b'{"Sts": Success}'
    assert proto.is_terminator(payload)
    assert proto.terminator_ok(payload)


def test_failure_terminator_is_terminator_but_not_ok():
    payload = b'{"Sts": Fail}'
    assert proto.is_terminator(payload)
    assert not proto.terminator_ok(payload)


def test_binary_chunk_is_not_terminator():
    # A live block full of ASCII-range bytes would be the trap; the
    # discriminator is whole-payload printability, and real blocks
    # contain NULs and high bytes.
    assert not proto.is_terminator(make_live())
    assert not proto.is_terminator(b'\x00\x01\x02')
    assert not proto.is_terminator(b'')


def test_ascii_looking_binary_with_control_bytes_is_data():
    assert not proto.is_terminator(b'Success\x00')


# ── Reassembly ─────────────────────────────────────────────────────────

def test_reassembles_chunks_in_order_until_success():
    r = proto.Reassembler()
    r.reset()
    assert r.feed(b'\x00\x01') is None
    assert r.feed(b'\x02\x03') is None
    assert r.feed(b'{"Sts": Success}') is True
    assert r.buffer == b'\x00\x01\x02\x03'


def test_failure_terminator_fails_transfer():
    r = proto.Reassembler()
    r.feed(b'\x00\x01')
    assert r.feed(b'{"Sts": Error}') is False


def test_terminator_only_reply_yields_empty_buffer():
    # A read that returns a terminator and no data has failed — the
    # caller sees success=True but an empty buffer, and decode_live
    # rejects it below.
    r = proto.Reassembler()
    assert r.feed(b'{"Sts": Success}') is True
    assert r.buffer == b''
    assert proto.decode_live(r.buffer) is None


def test_oversized_stream_fails():
    r = proto.Reassembler()
    result = None
    for _ in range(50):
        result = r.feed(b'\x00' * 100)
        if result is not None:
            break
    assert result is False


def test_reset_clears_buffer():
    r = proto.Reassembler()
    r.feed(b'\xff\xfe')
    r.reset()
    assert r.buffer == b''
    assert r.length == 0


# ── Live block decode ──────────────────────────────────────────────────

def test_live_decode_nominal():
    live = proto.decode_live(make_live())
    assert live['state'] == 0
    assert live['state_name'] == 'Normal'
    assert live['fault'] is False
    assert live['learned_starts'] == 3
    assert live['current'] == 12.3
    assert abs(live['frequency'] - 60.0) < 0.01
    assert live['peak_current'] == 45.6
    assert live['scpt_remaining'] == 120
    assert live['total_faults'] == 2
    assert live['total_starts'] == 1234


def test_live_decode_50hz_period():
    live = proto.decode_live(make_live(period_raw=10000))
    assert abs(live['frequency'] - 50.0) < 0.01


def test_live_decode_zero_period_yields_no_frequency():
    live = proto.decode_live(make_live(period_raw=0))
    assert live['frequency'] is None


def test_live_decode_fault_states():
    for state in range(4, 10):
        live = proto.decode_live(make_live(state=state))
        assert live['fault'] is True, state
    for state in (0, 1, 2, 3):
        live = proto.decode_live(make_live(state=state))
        assert live['fault'] is False, state


def test_live_decode_unknown_state_surfaced_not_clamped():
    live = proto.decode_live(make_live(state=12))
    assert live['state'] == 12
    assert live['state_name'] == 'Unknown (12)'


def test_live_decode_short_buffer_rejected():
    assert proto.decode_live(make_live()[:19]) is None
    assert proto.decode_live(b'') is None


def test_live_decode_large_start_count():
    live = proto.decode_live(make_live(starts=100000))
    assert live['total_starts'] == 100000


# ── Configuration block decode ─────────────────────────────────────────

def test_config_decode_nominal():
    config = proto.decode_config(make_config())
    assert config['firmware_version'] == 27
    assert config['smask'] == 0x02
    assert config['fmask'] == 0x7F
    assert config['fmask_disarmed'] == []
    assert config['scpt_delay_setting'] == 5


def test_config_decode_disarmed_protections_reported():
    config = proto.decode_config(make_config(fmask=0x3F))
    assert config['fmask_disarmed'] == ['Wiring issue']
    config = proto.decode_config(make_config(fmask=0x00))
    assert len(config['fmask_disarmed']) == 7


def test_config_decode_truncated_rejected():
    # A truncated read appears to succeed; the length gate is what
    # stops offsets 906-908 from reading garbage.
    assert proto.decode_config(make_config(length=908)) is None
    assert proto.decode_config(b'') is None


def test_config_decode_minimum_length_accepted():
    assert proto.decode_config(make_config(length=909)) is not None
