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

# The register holding the advertisement encryption key.
VREG_ADVERTISEMENT_KEY = 0xEC65

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


def cbor_array(items: list) -> bytes:
    """Indefinite-length array — 0x9F … 0xFF, as the peripheral expects."""
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


# GetValue.  0x05 is the plain variant; the advertisement-key register
# needs the privileged 0x25 instead (see orion_tr_key_cli), which is why
# this helper is not used for it.
OPCODE_GET_VALUE = 0x05


def encode_read_command(register_id: int) -> bytes:
    """CBOR for "push the current value of *register_id*"."""
    return (cbor_uint(OPCODE_GET_VALUE) + cbor_uint(0)
            + cbor_array([cbor_uint(register_id)]))


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
