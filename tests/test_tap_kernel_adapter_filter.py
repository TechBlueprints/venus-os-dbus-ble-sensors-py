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
LDH, LDW, JA = tap._BPF_LD_H_ABS, tap._BPF_LD_W_ABS, tap._BPF_JMP_JA


def _decode(prog: bytes):
    assert len(prog) % 8 == 0
    return [struct.unpack("HBBI", prog[i:i + 8]) for i in range(0, len(prog), 8)]


def _run(prog: bytes, frame: bytes) -> int:
    """A tiny classic-BPF interpreter for the opcodes we emit, with the
    kernel's semantics: word/halfword loads are big-endian as the bytes
    lie, a load past the end of the packet terminates with 0 (drop),
    jeq advances pc by jt+1 / jf+1, ja by k+1."""
    insns = _decode(prog)
    pc = 0
    A = 0
    while True:
        assert 0 <= pc < len(insns), "jump left the program"
        code, jt, jf, k = insns[pc]
        if code == LDB:
            if k >= len(frame):
                return 0
            A = frame[k]
            pc += 1
        elif code == LDH:
            if k + 2 > len(frame):
                return 0
            A = int.from_bytes(frame[k:k + 2], "big")
            pc += 1
        elif code == LDW:
            if k + 4 > len(frame):
                return 0
            A = int.from_bytes(frame[k:k + 4], "big")
            pc += 1
        elif code == JEQ:
            pc += 1 + (jt if A == k else jf)
        elif code == JA:
            pc += 1 + k
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


def test_adapter_only_program_uses_byte_loads_only_no_endianness_risk() -> None:
    codes = {i[0] for i in _decode(tap.build_adapter_filter({0, 1, 2}))}
    assert codes <= {LDB, JEQ, RET}


def test_empty_known_set_builds_the_adapter_only_program_byte_for_byte() -> None:
    assert tap.build_adapter_filter({0, 1}, set()) == tap.build_adapter_filter({0, 1})
    assert tap.build_adapter_filter({0, 1}, None) == tap.build_adapter_filter({0, 1})


def test_empty_and_oversized_sets_are_rejected() -> None:
    with pytest.raises(ValueError):
        tap.build_adapter_filter(set())
    with pytest.raises(ValueError):
        tap.build_adapter_filter(set(range(201)))


def test_sensors_py_attaches_at_tap_start_and_reattaches_on_change() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    assert "attach_adapter_filter(" in src, "must attach the kernel filter on the tap socket"
    assert "self._tap_sock" in src, "must keep the live socket to re-attach on adapter change"


# ── The in-kernel address gate ─────────────────────────────────────────────
#
# A real single-report legacy frame captured on prod hci1 on 2026-09-11:
# header (event-rx, adapter 1, len), then 3e <plen> 02 01 | 03 01 | address
# ef52ac5f3bec (as it lies, little-endian) = MAC ec3b5fac52ef | len | AD data
# | rssi (the AD payload here is a synthetic Victron one the parser keeps).  The parser and the BPF constants must agree on that MAC.
_KNOWN = "ec3b5fac52ef"
_ADV = bytes.fromhex("020106" "04ffe10200" "050954657374")  # flags, Victron mfg, name "Test"
_LEGACY_TAIL = bytes([0x3E, 0, 0x02, 0x01, 0x03, 0x01]) + bytes.fromhex("ef52ac5f3bec") \
    + bytes([len(_ADV)]) + _ADV + bytes([0xC8])


def _legacy_frame(adapter_idx: int = 1, addr_le: bytes | None = None,
                  num_reports: int = 1) -> bytes:
    body = bytearray(_LEGACY_TAIL)
    body[1] = len(body) - 2
    body[3] = num_reports
    if addr_le is not None:
        body[6:12] = addr_le
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(body)) + bytes(body)


def _extended_frame(adapter_idx: int = 1, addr_le: bytes | None = None) -> bytes:
    # subevent 0x0D: num(1) event_type(2) addr_type(1) addr(6) phys(2) sid(1)
    # txpwr(1) rssi(1) pa_int(2) dir_type(1) dir_addr(6) data_len(1) data
    addr = addr_le if addr_le is not None else bytes.fromhex("ef52ac5f3bec")
    rep = bytes([0x01, 0x13, 0x00, 0x01]) + addr + bytes([1, 0, 0xFF, 0x7F, 0xC8, 0, 0, 0]) \
        + bytes(6) + bytes([len(_ADV)]) + _ADV
    body = bytes([0x3E, len(rep) + 1, 0x0D]) + rep
    return struct.pack("<HHH", tap._OP_HCI_EVENT_RX, adapter_idx, len(body)) + bytes(body)


def test_bpf_constants_and_the_parser_agree_on_what_a_mac_is() -> None:
    advs = tap.parse_monitor_frame(_legacy_frame(), None, None, None, None, None)
    assert [a.mac for a in advs] == [_KNOWN]
    advs = tap.parse_monitor_frame(_extended_frame(), None, None, None, None, None)
    assert [a.mac for a in advs] == [_KNOWN]
    assert tap._mac_to_raw(_KNOWN) == bytes.fromhex("ef52ac5f3bec")
    assert tap._mac_to_raw("EC:3B:5F:AC:52:EF") == bytes.fromhex("ef52ac5f3bec")


def test_gate_passes_known_and_drops_strangers_on_both_report_kinds() -> None:
    prog = tap.build_adapter_filter({0, 1}, {_KNOWN, "001122334455"})
    stranger = bytes.fromhex("665544332211")
    assert _run(prog, _legacy_frame()) == 0xFFFF
    assert _run(prog, _extended_frame()) == 0xFFFF
    assert _run(prog, _legacy_frame(addr_le=bytes.fromhex("554433221100"))) == 0xFFFF
    assert _run(prog, _legacy_frame(addr_le=stranger)) == 0
    assert _run(prog, _extended_frame(addr_le=stranger)) == 0
    # a near miss: first four bytes match, last two do not -> stranger
    assert _run(prog, _legacy_frame(addr_le=bytes.fromhex("ef52ac5f0000"))) == 0
    assert _run(prog, _legacy_frame(addr_le=bytes.fromhex("00000a5f3bec"))) == 0


def test_gate_is_conservative_multi_report_and_other_subevents_pass() -> None:
    prog = tap.build_adapter_filter({0, 1}, {_KNOWN})
    stranger = bytes.fromhex("665544332211")
    # two reports batched: a known device could hide behind the stranger
    assert _run(prog, _legacy_frame(addr_le=stranger, num_reports=2)) == 0xFFFF
    # an LE Meta event that is not an advertising report (connection complete)
    conn = struct.pack("<HHH", tap._OP_HCI_EVENT_RX, 1, 4) + bytes([0x3E, 2, 0x01, 0x00])
    assert _run(prog, conn) == 0xFFFF


def test_gate_never_overrides_the_adapter_and_event_gates() -> None:
    prog = tap.build_adapter_filter({1}, {_KNOWN})
    assert _run(prog, _legacy_frame(adapter_idx=1)) == 0xFFFF
    assert _run(prog, _legacy_frame(adapter_idx=3)) == 0        # foreign card, known MAC
    f = bytearray(_legacy_frame()); f[6] = 0x0E                  # Command Complete
    assert _run(prog, bytes(f)) == 0


def test_every_jump_in_the_gated_program_stays_inside_and_forward() -> None:
    macs = {"%012x" % (0x100000000000 + i) for i in range(tap._MAX_KERNEL_MACS)}
    for known in ({_KNOWN}, macs):
        insns = _decode(tap.build_adapter_filter({0, 1, 9}, known))
        assert len(insns) <= tap._BPF_MAXINSNS
        assert insns[-1][0] == RET
        for pc, (code, jt, jf, k) in enumerate(insns):
            if code == JEQ:
                assert pc + 1 + jt < len(insns) and pc + 1 + jf < len(insns), pc
            if code == JA:
                assert 0 < pc + 1 + k < len(insns), pc
    # the two chains are reachable and the dispatch lands on their heads
    prog = tap.build_adapter_filter({1}, macs)
    assert _run(prog, _legacy_frame(addr_le=tap._mac_to_raw("%012x" % 0x100000000000))) == 0xFFFF
    assert _run(prog, _extended_frame(addr_le=tap._mac_to_raw("%012x" % (0x100000000000 + 379)))) == 0xFFFF
    assert _run(prog, _legacy_frame(addr_le=bytes(6))) == 0
    assert _run(prog, _extended_frame(addr_le=bytes(6))) == 0


def test_too_many_or_malformed_addresses_are_refused_by_the_builder() -> None:
    with pytest.raises(ValueError):
        tap.build_adapter_filter({1}, {"%012x" % i for i in range(tap._MAX_KERNEL_MACS + 1)})
    with pytest.raises(ValueError):
        tap.build_adapter_filter({1}, {"not-a-mac"})
    with pytest.raises(ValueError):
        tap.build_adapter_filter({1}, {"ec3b5fac52"})


def test_attach_falls_back_to_the_adapter_only_program_when_the_gate_is_refused() -> None:
    class Sock:
        def __init__(self):
            self.attached = []
        def setsockopt(self, level, opt, val):
            assert opt == tap._SO_ATTACH_FILTER
            n, _ = struct.unpack("HL", val)
            self.attached.append(n)
    s = Sock()
    assert tap.attach_adapter_filter(s, {0, 1}, {"not-a-mac"}) is True
    assert s.attached == [9]                                  # adapter-only shape
    s = Sock()
    assert tap.attach_adapter_filter(s, {0, 1}, {_KNOWN}) is True
    # gated shape: the adapter-only ACCEPT is replaced by the 8-insn
    # dispatch, then one 5-insn block + DROP per chain
    assert s.attached == [8 + 8 + 2 * (5 + 1)]


def test_sensors_py_carries_the_known_set_into_every_attach() -> None:
    src = open(os.path.join(SRC, "dbus_ble_sensors.py")).read()
    body = src[src.index("def _refresh_tap_known_macs"):]
    body = body[:body.index("\n    def ")]
    assert "attach_adapter_filter(sock, self._scan_adapter_indices," in body, \
        "a change to the known set must re-attach the kernel program"
    # every attach with an adapter set also carries the known set
    import re
    calls = re.findall(r"attach_adapter_filter\((\w+), self\._scan_adapter_indices,\s*self\._tap_known_macs\)", src)
    assert len(calls) == 3, calls
    assert re.search(r"attach_adapter_filter\(\w+, self\._scan_adapter_indices\)", src) is None
    # the seed refresh runs before the socket is published so it cannot attach
    start = src[src.index("tap_sock = create_tap_socket()"):]
    assert start.index("self._refresh_tap_known_macs()") < start.index("self._tap_sock = tap_sock")
