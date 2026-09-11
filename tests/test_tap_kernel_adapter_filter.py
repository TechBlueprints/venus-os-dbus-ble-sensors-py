"""The tap's kernel (classic-BPF) adapter filter is built correctly.

parse_monitor_frame's early-drop makes a foreign-adapter frame cheap; the
kernel filter makes it free -- the frame is never queued to our socket, so
the tap thread never wakes.  The program must never drop a frame we need,
so it is built only from byte loads (no endianness) and replicates the
parser's own gates: monitor opcode == event-rx, event == LE Meta, adapter
in our set.  These decode the program and check every jump lands where it
should.
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

LDB, JEQ, RET = tap._BPF_LD_B_ABS, tap._BPF_JMP_JEQ_K, tap._BPF_RET_K


def _decode(prog: bytes):
    assert len(prog) % 8 == 0
    return [struct.unpack("HBBI", prog[i:i + 8]) for i in range(0, len(prog), 8)]


def _run(prog: bytes, frame: bytes) -> int:
    """A tiny classic-BPF interpreter for the three opcodes we emit."""
    insns = _decode(prog)
    pc = 0
    A = 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == LDB:
            A = frame[k] if k < len(frame) else 0
            pc += 1
        elif code == JEQ:
            pc += 1 + (jt if A == k else jf)
        elif code == RET:
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


def _frame(adapter_idx: int, opcode: int = tap._OP_HCI_EVENT_RX,
           evt: int = tap._EVT_LE_META) -> bytes:
    hdr = struct.pack("<HHH", opcode, adapter_idx, 10)
    return hdr + bytes([evt, 8, 0x02, 0x01]) + bytes(6)


def test_program_shape_for_two_adapters() -> None:
    insns = _decode(tap.build_adapter_filter({0, 1}))
    assert insns[0] == (LDB, 0, 0, 0)                          # opcode lo
    assert insns[1][0] == JEQ and insns[1][3] == tap._OP_HCI_EVENT_RX
    assert insns[2] == (LDB, 0, 0, 6)                          # event code
    assert insns[3][0] == JEQ and insns[3][3] == tap._EVT_LE_META
    assert insns[4] == (LDB, 0, 0, 2)                          # adapter idx
    assert insns[5] == (JEQ, 2, 0, 0) and insns[6] == (JEQ, 1, 0, 1)
    assert insns[7] == (RET, 0, 0, 0)                          # DROP
    assert insns[8] == (RET, 0, 0, 0xFFFF)                     # ACCEPT
    assert len(insns) == 9


@pytest.mark.parametrize("allowed", [{1}, {0, 1}, {0, 1, 5, 9}, set(range(10))])
def test_every_jump_lands_on_drop_or_accept(allowed) -> None:
    prog = tap.build_adapter_filter(allowed)
    for idx in range(12):
        want = 0xFFFF if idx in allowed else 0
        assert _run(prog, _frame(idx)) == want, (allowed, idx)


def test_non_event_and_non_le_meta_frames_are_dropped_even_for_our_adapter() -> None:
    prog = tap.build_adapter_filter({1})
    assert _run(prog, _frame(1)) == 0xFFFF
    assert _run(prog, _frame(1, opcode=0x0004)) == 0        # not event-rx
    assert _run(prog, _frame(1, evt=0x0E)) == 0             # Command Complete, not LE Meta
    assert _run(prog, _frame(1, evt=0x02)) == 0             # ACL-ish, not LE Meta


def test_only_byte_loads_are_used_no_endianness_risk() -> None:
    codes = {i[0] for i in _decode(tap.build_adapter_filter({0, 1, 2}))}
    assert codes <= {LDB, JEQ, RET}


def test_empty_and_oversized_sets_are_rejected() -> None:
    with pytest.raises(ValueError):
        tap.build_adapter_filter(set())
    with pytest.raises(ValueError):
        tap.build_adapter_filter(set(range(201)))


def test_sensors_py_attaches_at_tap_start_and_reattaches_on_change() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    assert "attach_adapter_filter(" in src, "must attach the kernel filter on the tap socket"
    assert "self._tap_sock" in src, "must keep the live socket to re-attach on adapter change"
