# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Micro-Air EasyStart BLE protocol: framing and payload decoding.

Pure functions and small state — no bleak, no dbus — so every byte-level
decision is unit-testable.  The protocol itself is documented in
``docs/EASYSTART-PROTOCOL.md``; offsets and semantics here follow it.

**This module is read-only by design.**  The only commands it defines are
the two read commands, as literal byte constants.  The device's single
write characteristic also accepts settings changes and firmware blocks,
distinguished purely by content — so nothing here, or in any caller,
may construct a command string from variables.  See the protocol doc's
"Firmware update mode" section for why this is a hard rule.
"""
from __future__ import annotations

import struct

# GATT layout.  NOTE: this is STMicroelectronics' stock template service
# (the firmware is built from ST's BLE example), so the UUID identifies
# the firmware template, not the product — never use it for discovery.
# Discovery is by advertised name (``EasyStart_``...); see the driver.
SERVICE_UUID = 'd973f2e0-b19e-11e2-9e96-0800200c9a66'
NOTIFY_CHAR_UUID = 'd973f2e1-b19e-11e2-9e96-0800200c9a66'
WRITE_CHAR_UUID = 'd973f2e2-b19e-11e2-9e96-0800200c9a66'

# Advertised-name prefix.  Two lengths occur in the wild: bare
# 'EasyStart_' and 'EasyStart_XXXX' with a four-character unit suffix.
ADV_NAME_PREFIX = 'EasyStart_'

# The two read commands — ASCII, JSON-looking but not JSON (values are
# unquoted).  Sent verbatim as these literal constants, never built.
CMD_READ_LIVE = b'{"Cmd": ReadLive}'
CMD_READ_EEP = b'{"Cmd": ReadEEP}'

# The device streams a reply as binary chunks followed by an ASCII text
# terminator.  A successful terminator contains this substring (full
# observed form: {"Sts": Success}).
_TERMINATOR_SUCCESS = b'Success'

# Live block is 20 bytes; the configuration block reassembles to ~1100.
LIVE_BLOCK_MIN_LEN = 20
# Settings live at offsets 906-908; anything shorter is a truncated read
# and indexing it yields garbage that looks plausible.
CONFIG_BLOCK_MIN_LEN = 909
# Reassembly cap — nothing legitimate exceeds this; a runaway stream
# should fail the transfer, not grow the buffer unboundedly.
REASSEMBLY_MAX_LEN = 4096

# Poll interval the device is designed around.  Slower is safe.
POLL_INTERVAL_S = 5.0

SYSTEM_STATES = {
    0: 'Normal',
    1: 'Unexpected current',
    2: 'Short cycle delay',
    3: 'Power interruption',
    4: 'Stall fault',
    5: 'Stuck start relay fault',
    6: 'Open overload fault',
    7: 'Overcurrent fault',
    8: 'Bad wiring fault',
    9: 'Wrong voltage fault',
}

# States 4-9 are latched faults; 1-3 are transient conditions.
FAULT_STATES = frozenset(range(4, 10))

# Fault-enable mask (config offset 907): a SET bit means that protection
# is armed.  A clear bit is worth surfacing — the cutout is disarmed on
# the unit right now.  Bit 7 unidentified.
FMASK_BITS = {
    0x01: 'Unexpected current',
    0x02: 'Power interruption',
    0x04: 'Compressor stall',
    0x08: 'Start hardware failed',
    0x10: 'Open overload',
    0x20: 'Overcurrent',
    0x40: 'Wiring issue',
}


def is_terminator(payload: bytes) -> bool:
    """Whether a notification payload is the transfer terminator.

    Per the protocol: data chunks are binary, the terminator is a
    non-empty ASCII text string.  Decodable-and-printable is the
    discriminator; the live block's binary content (counts, currents)
    routinely contains bytes that are valid ASCII, so printability of
    the WHOLE payload is what separates the two.
    """
    if not payload:
        return False
    try:
        text = payload.decode('ascii')
    except UnicodeDecodeError:
        return False
    return all(32 <= ord(c) <= 126 for c in text)


def terminator_ok(payload: bytes) -> bool:
    """Whether a terminator payload reports success."""
    return _TERMINATOR_SUCCESS in payload


class Reassembler:
    """Accumulate binary chunks until the text terminator arrives.

    ``feed`` returns ``None`` while the transfer is in progress, ``True``
    on a successful terminator, ``False`` on a failure terminator or an
    oversized stream.  The buffer must be reset *before* the command is
    written, not after the reply — the device may start streaming before
    the write call returns.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._length = 0

    def reset(self) -> None:
        self._parts = []
        self._length = 0

    @property
    def buffer(self) -> bytes:
        return b''.join(self._parts)

    @property
    def length(self) -> int:
        return self._length

    def feed(self, payload: bytes) -> 'bool | None':
        if is_terminator(payload):
            return terminator_ok(payload)
        self._parts.append(bytes(payload))
        self._length += len(payload)
        if self._length > REASSEMBLY_MAX_LEN:
            return False
        return None


def decode_live(buf: bytes) -> 'dict | None':
    """Decode the 20-byte live telemetry block.

    Returns ``None`` for a short buffer — a read that produced no data
    chunks has failed, not "succeeded with nothing to say".
    """
    if len(buf) < LIVE_BLOCK_MIN_LEN:
        return None

    state = buf[2]
    learned_starts = buf[3]
    (current_raw, period_raw, peak_raw, scpt_s, total_faults) = \
        struct.unpack_from('<HHHHH', buf, 4)
    (total_starts,) = struct.unpack_from('<I', buf, 14)

    return {
        'state': state,
        'state_name': SYSTEM_STATES.get(state, f'Unknown ({state})'),
        'fault': state in FAULT_STATES,
        'learned_starts': learned_starts,
        'current': current_raw / 10.0,                      # A
        'frequency': (500000.0 / period_raw) if period_raw else None,  # Hz
        'peak_current': peak_raw / 10.0,                    # A
        'scpt_remaining': scpt_s,                           # s
        'total_faults': total_faults,
        'total_starts': total_starts,
    }


def decode_config(buf: bytes) -> 'dict | None':
    """Decode the identified offsets of the configuration/history block.

    Validates the reassembled length before indexing — a truncated read
    appears to succeed and would otherwise yield garbage settings.
    """
    if len(buf) < CONFIG_BLOCK_MIN_LEN:
        return None

    fmask = buf[907]
    disarmed = [name for bit, name in FMASK_BITS.items() if not (fmask & bit)]
    return {
        'firmware_version': buf[10],
        'smask': buf[906],
        'fmask': fmask,
        'fmask_disarmed': disarmed,
        'scpt_delay_setting': buf[908],  # minutes (setting; live block is s)
    }
