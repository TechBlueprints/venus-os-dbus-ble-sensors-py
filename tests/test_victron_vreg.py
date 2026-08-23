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
    # Official GetValue is always 0x05.  0x25 is an Orion-only fallback,
    # not what the HEX encoder emits for any register — including 0xEC65.
    assert vreg.encode_read_command(0x0140).hex() == "05009f190140ff"


def test_parse_push_frame_voltage_and_key() -> None:
    frame = bytes.fromhex("080319ed8d42a105")
    parsed = vreg.parse_push_frame(frame)
    assert parsed is not None
    inst, reg, payload = parsed
    assert inst == 3
    assert reg == 0xED8D
    assert vreg.decode_smartshunt_vreg(reg, payload)["voltage"] == 14.41
    key = bytes(range(16))
    key_frame = bytes([0x08, 0x00, 0x19, 0xEC, 0x65, 0x50]) + key
    _inst, kreg, kpay = vreg.parse_push_frame(key_frame)
    assert kreg == 0xEC65
    assert vreg.decode_smartshunt_vreg(kreg, kpay)["advertisement_key"] == key.hex()


def test_encode_get_devices_and_official_key_batch() -> None:
    assert vreg.encode_get_devices() == b"\x01"
    assert vreg.encode_subscribe_instance(0) == b"\x03\x00"
    assert vreg.encode_read_commands(
        [vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY]
    ).hex() == "05009f19ec6619ec65ff"
    # SmartShunt protocol 0004 wants a definite-length array.
    assert vreg.encode_read_commands(
        [vreg.VREG_BLE_MAC_ADDRESS, vreg.VREG_ADVERTISEMENT_KEY],
        definite=True,
    ).hex() == "05008219ec6619ec65"
    assert vreg.encode_read_command(0xED8D, 0, definite=True).hex() \
        == "05008119ed8d"


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


def test_encode_vedirect_hex_get() -> None:
    # Command 0x07 + register LE + checksum so the sum is 0x55.
    assert vreg.encode_vedirect_hex_get(0xEC65) == b":765ECFD\n"
    assert vreg.encode_vedirect_hex_get(0x0100) == b":700014D\n"


def test_decode_ip22_vreg_voltage_from_live_push() -> None:
    # DATA_LAST 080019ed8d427905 captured on F2:86 — 0x0579 = 14.01 V.
    inst, reg, payload = vreg.parse_push_frame(
        bytes.fromhex("080019ed8d427905"))
    assert inst == 0
    assert reg == vreg.VREG_OUTPUT_VOLTAGE
    assert vreg.decode_ip22_vreg(reg, payload)["output_voltage1"] == 14.01


def test_decode_ip22_vreg_current_from_live_push() -> None:
    # F2:86 GetValue 0xED8F returned bstr 0000 while held at 0 A.
    # Scale is 0.1 A, same as the charger VE.Direct current register.
    assert vreg.decode_ip22_vreg(vreg.VREG_OUTPUT_CURRENT, b"\x00\x00")[
        "output_current1"] == 0.0
    assert vreg.decode_ip22_vreg(vreg.VREG_OUTPUT_CURRENT, b"\x2c\x01")[
        "output_current1"] == 30.0


def test_decode_ip22_vreg_device_state_and_mode() -> None:
    assert vreg.decode_ip22_vreg(vreg.VREG_DEVICE_STATE, b"\x05")[
        "device_state"] == 5
    assert vreg.decode_ip22_vreg(vreg.VREG_BLE_ADVERTISEMENT_MODE, b"\x01")[
        "advertisement_mode"] == 1
