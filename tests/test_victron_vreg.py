"""VREG protocol framing.

Pure byte-level checks: these encodings are what a charger accepts or
silently ignores, and a regression here is invisible until a setpoint
stops taking effect on real hardware.
"""
from __future__ import annotations

import victron_vreg as vreg


def test_cbor_uint_widths() -> None:
    assert vreg.cbor_uint(6) == b"\x06"
    assert vreg.cbor_uint(23) == b"\x17"
    assert vreg.cbor_uint(24) == b"\x18\x18"
    assert vreg.cbor_uint(0xFF) == b"\x18\xff"
    assert vreg.cbor_uint(0xEDF0) == b"\x19\xed\xf0"
    assert vreg.cbor_uint(0x00010000) == b"\x1a\x00\x01\x00\x00"


def test_cbor_bstr_short_and_long() -> None:
    assert vreg.cbor_bstr(b"\x2c\x01") == b"\x42\x2c\x01"
    long = bytes(range(30))
    assert vreg.cbor_bstr(long) == b"\x58\x1e" + long


def test_cbor_array_is_indefinite_length() -> None:
    # 0x9F … 0xFF — the peripheral rejects the definite-length form.
    assert vreg.cbor_array([b"\x01", b"\x02"]) == b"\x9f\x01\x02\xff"


def test_encode_write_command_matches_wire_format() -> None:
    # SetValue 0xEDF0 (charge current limit) = 30.0 A, as 300 deci-amps
    # little-endian.  Byte-for-byte what the pre-bcmv2 writer emitted.
    assert vreg.encode_write_command(0xEDF0, b"\x2c\x01").hex() \
        == "06009f19edf0422c01ff"


def test_encode_read_command_uses_the_plain_getvalue_opcode() -> None:
    # 0x05, not 0x25: the privileged variant is only for 0xEC65, and
    # sending it for ordinary registers is not what the firmware expects.
    assert vreg.encode_read_command(0x0140).hex() == "05009f190140ff"


def test_scan_for_key_extracts_sixteen_bytes() -> None:
    key = bytes(range(16))
    frames = [b"\xde\xad", b"\x19\xec\x65\x50" + key + b"\xff"]
    assert vreg.scan_for_key(frames) == key


def test_scan_for_key_needs_a_full_key() -> None:
    # Truncated payload must read as "no key", never as a short one.
    assert vreg.scan_for_key([b"\x19\xec\x65\x50" + bytes(15)]) is None
    assert vreg.scan_for_key([b"nothing here"]) is None


def test_scan_for_vreg_spans_frame_boundaries() -> None:
    # The device chunks its pushes; the marker may straddle two frames.
    assert vreg.scan_for_vreg([b"\x19\xed", b"\xf0\x42\x2c\x01"], 0xEDF0) \
        == b"\x2c\x01"


def test_scan_for_vreg_skips_non_bstr_matches() -> None:
    # A register id that appears as a *value* elsewhere must not be
    # mistaken for the entry we asked for; scanning continues past it.
    blob = b"\x19\x01\x40\x00" + b"\x19\x01\x40\x43abc"
    assert vreg.scan_for_vreg([blob], 0x0140) == b"abc"


def test_scan_for_vreg_returns_none_when_absent() -> None:
    assert vreg.scan_for_vreg([b"\x19\xed\xf0\x42\x2c\x01"], 0x0100) is None
