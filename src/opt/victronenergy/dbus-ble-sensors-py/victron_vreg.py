# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The Victron VE.Smart VREG protocol over GATT.

Shared by the in-service setpoint writer (:mod:`orion_tr_gatt`) and the
advertisement-key provisioner (:mod:`orion_tr_key_cli`), which spoke the
same protocol through two separate hand-rolled copies before the two
moved onto bleak.

Wire format: CBOR, on the ``306b`` VE.Smart service.  A register write is
``[6, 0, [<vreg>, <value bytes>]]`` in CBOR's indefinite-length array
encoding, written to the "data last chunk" characteristic after a
two-step flow-control handshake on the control characteristic.  Register
*responses* come back as notifications and are parsed by scanning for the
register id — the device frames several registers per push and we only
ever want one of them.

All byte strings are little-endian, as Victron encodes them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# VE.Smart GATT service and its characteristics.
SERVICE_UUID = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CHAR_CONTROL = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
CHAR_DATA_LAST = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
CHAR_DATA_BULK = "306b0004-b081-4037-83dc-e59fcc3cdfd0"

# The 9758 "VE service" carries PUK/PIN authentication, needed on the
# first provisioning of firmwares that gate VREG reads behind it.
CHAR_PUK = "97580006-ddf1-48be-b73e-182664615d8e"
CHAR_PIN = "97580003-ddf1-48be-b73e-182664615d8e"

# Control-characteristic opcodes.
OPCODE_CHUNK_SIZE = 0xFA
OPCODE_READY_TO_RECV = 0xF9

# The register holding the advertisement encryption key, and the MAC
# the official client fetches in the same GetValues.
VREG_ADVERTISEMENT_KEY = 0xEC65
VREG_BLE_MAC_ADDRESS = 0xEC66
VREG_BLE_ADVERTISEMENT_MODE = 0xEC7D  # 1 = Instant Readout extra mfr data
VREG_DEVICE_STATE = 0x0201
VREG_OUTPUT_VOLTAGE = 0xED8D

# HEX command opcodes (first CBOR uint of a DATA_LAST write).
OPCODE_GET_DEVICES = 0x01
OPCODE_SUBSCRIBE = 0x03
OPCODE_GET_VALUE = 0x05
OPCODE_SET_VALUE = 0x06

# Flow-control payloads, as the Victron app sends them.
_CHUNK_SIZE_VALUE = 0x14
_READY_CREDITS = 0x08

# Beat between the chunk-size write and the command itself.  The
# peripheral drops the command if it arrives inside its own handshake.
HANDSHAKE_SETTLE_S = 0.3

# Beat between the command and teardown, so the write is not cut off by
# our own disconnect.
WRITE_SETTLE_S = 1.0


def cbor_uint(n: int) -> bytes:
    if n < 24:
        return bytes([n])
    if n < 256:
        return bytes([0x18, n])
    if n < 65536:
        return bytes([0x19, (n >> 8) & 0xFF, n & 0xFF])
    return bytes([0x1A, (n >> 24) & 0xFF, (n >> 16) & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])


def cbor_array(items: list, definite: bool = False) -> bytes:
    """CBOR array of already-encoded items.

    IP22 / Orion want the indefinite form ``0x9F … 0xFF``.  SmartShunt
    protocol header ``0004…`` rejects that (CTRL ``F7``) and answers
    definite-length arrays instead.
    """
    if definite:
        n = len(items)
        if n < 24:
            return bytes([0x80 | n]) + b"".join(items)
        if n < 256:
            return bytes([0x98, n]) + b"".join(items)
        return bytes([0x99, (n >> 8) & 0xFF, n & 0xFF]) + b"".join(items)
    return bytes([0x9F]) + b"".join(items) + bytes([0xFF])


def cbor_bstr(data: bytes) -> bytes:
    n = len(data)
    if n < 24:
        return bytes([0x40 | n]) + data
    return bytes([0x58, n]) + data


def encode_write_command(register_id: int, value_bytes: bytes) -> bytes:
    """CBOR for "write *value_bytes* to *register_id*"."""
    return (cbor_uint(6) + cbor_uint(0)
            + cbor_array([cbor_uint(register_id), cbor_bstr(value_bytes)]))


def encode_get_devices() -> bytes:
    """CBOR GetDevices.  Official client sends this before any VREG read."""
    return cbor_uint(OPCODE_GET_DEVICES)


def encode_subscribe_instance(instance: int = 0) -> bytes:
    """CBOR Subscribe for a device-list instance (not a single VREG)."""
    return cbor_uint(OPCODE_SUBSCRIBE) + cbor_uint(instance)


def encode_subscribe_vreg(register_id: int, instance: int = 0) -> bytes:
    """CBOR Subscribe one register on *instance*."""
    return (cbor_uint(OPCODE_SUBSCRIBE) + cbor_uint(instance)
            + cbor_array([cbor_uint(register_id)]))


def encode_read_command(register_id: int, instance: int = 0,
                        definite: bool = False) -> bytes:
    """CBOR GetValue for one register on *instance* (default the unit)."""
    return encode_read_commands([register_id], instance, definite=definite)


def encode_read_commands(register_ids: list[int], instance: int = 0,
                         definite: bool = False) -> bytes:
    """CBOR GetValues.  Official key fetch is instance 0, [0xEC66, 0xEC65]."""
    return (cbor_uint(OPCODE_GET_VALUE) + cbor_uint(instance)
            + cbor_array([cbor_uint(r) for r in register_ids],
                         definite=definite))


def encode_vedirect_hex_get(register_id: int) -> bytes:
    """ASCII VE.Direct HEX Get (``:7<lo><hi><cs>\\n``).

    Last-resort Get for a register (including ``0xEC65``) when a CBOR
    session ACKs but never Pushes.  Prefer GetDevices + official
    GetValues first; this is only a fallback.
    """
    lo = register_id & 0xFF
    hi = (register_id >> 8) & 0xFF
    checksum = (0x55 - (0x07 + lo + hi)) & 0xFF
    return f":7{lo:02X}{hi:02X}{checksum:02X}\n".encode("ascii")


def scan_for_vreg(blobs, vreg: int) -> bytes | None:
    """Extract the byte string a Push response carries for *vreg*.

    Looks for the CBOR encoding of the uint16 register id followed by a
    short (< 24 byte) bstr header, and returns the payload — or ``None``
    when no matching entry appears in *blobs*.
    """
    marker = bytes([0x19, (vreg >> 8) & 0xFF, vreg & 0xFF])
    joined = b"".join(blobs)
    idx = 0
    while True:
        idx = joined.find(marker, idx)
        if idx < 0:
            return None
        after = idx + len(marker)
        if after >= len(joined):
            return None
        header = joined[after]
        # 0x40-0x57: CBOR short byte string, length in the low 5 bits.
        if 0x40 <= header <= 0x57:
            length = header & 0x1F
            start = after + 1
            if start + length <= len(joined):
                return joined[start:start + length]
        idx = after


def _cbor_read_uint(data: bytes, i: int) -> tuple[int | None, int]:
    if i >= len(data):
        return None, i
    b = data[i]
    if b < 24:
        return b, i + 1
    if b == 0x18 and i + 2 <= len(data):
        return data[i + 1], i + 2
    if b == 0x19 and i + 3 <= len(data):
        return (data[i + 1] << 8) | data[i + 2], i + 3
    if b == 0x1A and i + 5 <= len(data):
        return int.from_bytes(data[i + 1:i + 5], "big"), i + 5
    return None, i


def _cbor_read_bstr(data: bytes, i: int) -> tuple[bytes | None, int]:
    if i >= len(data):
        return None, i
    b = data[i]
    if 0x40 <= b <= 0x57:
        n = b & 0x1F
        end = i + 1 + n
        if end <= len(data):
            return data[i + 1:end], end
    if b == 0x58 and i + 2 <= len(data):
        n = data[i + 1]
        end = i + 2 + n
        if end <= len(data):
            return data[i + 2:end], end
    return None, i


def parse_push_frame(frame: bytes) -> tuple[int, int, bytes] | None:
    """Split a HEX Push (``08 <instance> <cbor vreg> <cbor payload>``)."""
    if not frame or frame[0] != 0x08 or len(frame) < 3:
        return None
    instance = frame[1]
    vreg, i = _cbor_read_uint(frame, 2)
    if vreg is None:
        return None
    payload, _ = _cbor_read_bstr(frame, i)
    if payload is not None:
        return instance, vreg, payload
    value, _ = _cbor_read_uint(frame, i)
    if value is None:
        return None
    width = max(1, (value.bit_length() + 7) // 8)
    return instance, vreg, value.to_bytes(width, "little")


def le_sint(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=True)


def le_uint(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=False)


def decode_smartshunt_vreg(vreg_id: int, payload: bytes) -> dict:
    """Map one SmartShunt Push payload to battery-path fields."""
    out: dict = {}
    if vreg_id == 0x0100 and payload:
        out["product_id"] = le_uint(payload[:2]) if len(payload) >= 2 else le_uint(payload)
    elif vreg_id == 0xED8D and len(payload) >= 2:
        out["voltage"] = le_sint(payload[:2]) / 100.0
    elif vreg_id == 0xED8C and len(payload) >= 4:
        raw = le_sint(payload[:4])
        if raw != 0x7FFFFFFF:
            out["current"] = raw / 1000.0
    elif vreg_id == 0xED8F and len(payload) >= 2:
        out.setdefault("current", le_sint(payload[:2]) / 10.0)
    elif vreg_id == 0x0FFF and len(payload) >= 2:
        out["soc"] = le_uint(payload[:2]) / 100.0
    elif vreg_id == 0x0FFE and len(payload) >= 2:
        mins = le_uint(payload[:2])
        if mins not in (0, 0xFFFF):
            out["ttg_s"] = int(mins) * 60
    elif vreg_id == 0xEEFF and len(payload) >= 4:
        out["consumed_ah"] = le_sint(payload[:4]) / 10.0
    elif vreg_id == 0x010A and payload:
        try:
            out["serial"] = payload.split(b"\x00", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            pass
    elif vreg_id == 0x010B and payload:
        try:
            out["model_name"] = payload.split(b"\x00", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            pass
    elif vreg_id == VREG_ADVERTISEMENT_KEY and len(payload) == 16:
        out["advertisement_key"] = payload.hex()
    elif vreg_id == 0x0140:
        out["firmware"] = payload.hex()
    if "voltage" in out and "current" in out:
        out["power"] = round(out["voltage"] * out["current"], 2)
    return out


def decode_ip22_vreg(vreg_id: int, payload: bytes) -> dict:
    """Map one IP22 Push payload to charger-path fields."""
    out: dict = {}
    if vreg_id == VREG_OUTPUT_VOLTAGE and len(payload) >= 2:
        out["output_voltage1"] = le_sint(payload[:2]) / 100.0
    elif vreg_id == VREG_DEVICE_STATE and payload:
        out["device_state"] = payload[0]
    elif vreg_id == VREG_BLE_ADVERTISEMENT_MODE and payload:
        out["advertisement_mode"] = payload[0]
    return out


def scan_for_key(blobs) -> bytes | None:
    """Extract the 16-byte advertisement key from a Push response.

    The fixed-length form of :func:`scan_for_vreg` for 0xEC65: register id
    then a 0x50 bstr header (16 bytes), which is what every firmware we
    have captured emits.
    """
    target = bytes([0x19, 0xEC, 0x65, 0x50])
    joined = b"".join(blobs)
    idx = joined.find(target)
    if idx >= 0 and idx + 4 + 16 <= len(joined):
        return joined[idx + 4:idx + 4 + 16]
    return None


async def send_flow_control(client) -> None:
    """Announce our chunk size and hand the peripheral receive credits."""
    await client.write_gatt_char(
        CHAR_CONTROL, bytes([OPCODE_CHUNK_SIZE, _CHUNK_SIZE_VALUE]),
        response=False)


async def send_ready(client) -> None:
    await client.write_gatt_char(
        CHAR_CONTROL, bytes([OPCODE_READY_TO_RECV, _READY_CREDITS]),
        response=False)


async def write_register(client, register_id: int, value_bytes: bytes) -> None:
    """Full write sequence on a connected client.

    Writes are unacknowledged (``response=False``, BlueZ's "command"
    write type) — the peripheral acknowledges at the protocol layer, not
    the ATT layer, and asking for an ATT response makes it drop the write.
    """
    import asyncio

    await send_flow_control(client)
    await asyncio.sleep(HANDSHAKE_SETTLE_S)
    await send_ready(client)
    await client.write_gatt_char(
        CHAR_DATA_LAST, encode_write_command(register_id, value_bytes),
        response=False)
    logger.info("wrote VREG 0x%04X = %s", register_id, value_bytes.hex())
    await asyncio.sleep(WRITE_SETTLE_S)
