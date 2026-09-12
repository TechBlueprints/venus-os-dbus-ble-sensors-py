"""Passive tap of BLE advertisements via the Linux HCI monitor channel.

Opens a raw Bluetooth socket bound to HCI_CHANNEL_MONITOR (channel 2) which
receives a read-only copy of ALL HCI traffic between the host and every
Bluetooth controller — the same mechanism that btmon uses.  This bypasses
BlueZ's AdvertisementMonitor1 filtering, allowing us to see advertisements
from devices (such as Mopeka sensors) that omit the Flags AD type.

The tap does not issue any commands and does not interfere with BlueZ or
tie up any adapter.

Packet formats are derived from the Bluetooth Core Specification (public
standard) and Linux kernel UAPI headers (userspace API).
"""

import ctypes
import ctypes.util
import logging
import os
import select
import socket
import struct
from typing import Iterable
import threading
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# ── Socket constants ──────────────────────────────────────────────────────
# From the Linux kernel UAPI (net/bluetooth/bluetooth.h).
# We use raw integer values because CPython is often compiled without
# Bluetooth socket support (socket.AF_BLUETOOTH may not exist).
_BT_FAMILY = 31          # AF_BLUETOOTH
_BT_RAW = socket.SOCK_RAW
_BT_HCI_PROTO = 1        # BTPROTO_HCI
_MONITOR_CHANNEL = 2  # HCI monitor — passive read-only tap of all HCI traffic
_ALL_CONTROLLERS = 0xFFFF  # receive from every adapter

# ── Monitor frame opcodes (kernel UAPI) ──────────────────────────────────
_OP_HCI_EVENT_RX = 3  # HCI event received from controller

# ── HCI event codes (Bluetooth Core Spec Vol 4, Part E, §7.7) ────────────
_EVT_LE_META = 0x3E

# ── LE Meta subevent codes (Bluetooth Core Spec Vol 4, Part E, §7.7.65) ──
_SUB_ADV_REPORT = 0x02
_SUB_EXT_ADV_REPORT = 0x0D

# ── AD type codes (Bluetooth Core Spec Supplement, Part A, §1) ────────────
_AD_TYPE_MANUFACTURER = 0xFF
_AD_TYPE_NAME_SHORT = 0x08
_AD_TYPE_NAME_COMPLETE = 0x09

# ── Monitor frame header: opcode(u16le), adapter(u16le), payload_len(u16le)
_FRAME_HDR = struct.Struct("<HHH")
_FRAME_HDR_SIZE = _FRAME_HDR.size  # 6

# ── Receive buffer ────────────────────────────────────────────────────────
_RECV_BUF = 4096


# ── ctypes structure for sockaddr_hci ─────────────────────────────────────
# Python's socket.bind() for AF_BLUETOOTH/BTPROTO_HCI only accepts (dev_id,)
# and cannot set hci_channel (CPython issue 36132).  We call libc.bind()
# directly via ctypes as a workaround.

class _HciSocketAddress(ctypes.Structure):
    _fields_ = [
        ("family", ctypes.c_ushort),
        ("dev_id", ctypes.c_ushort),
        ("channel", ctypes.c_ushort),
    ]


@dataclass(slots=True)
class TappedAdvertisement:
    """One parsed BLE advertisement from the monitor channel."""
    adapter_index: int
    mac: str  # lowercase no-separator, e.g. "aabbccddeeff"
    address_type: int
    rssi: int
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    # Advertised local name (AD 0x08/0x09), only populated when it matches
    # a registered name prefix — devices like the Micro-Air EasyStart carry
    # no manufacturer data and are identified by name alone.
    local_name: 'str | None' = None


def _format_mac(addr_bytes: bytes) -> str:
    """Convert 6 little-endian address bytes to lowercase hex (no separators)."""
    return addr_bytes[::-1].hex()


def create_tap_socket() -> socket.socket:
    """Open a raw HCI socket bound to the monitor channel.

    Returns a non-blocking Python socket ready for select()/recv().
    Raises OSError if the socket cannot be opened or bound.
    """
    sock = socket.socket(_BT_FAMILY, _BT_RAW, _BT_HCI_PROTO)

    libc_path = ctypes.util.find_library("c")
    if libc_path is None:
        # Embedded Linux (e.g. Venus OS) may lack ldconfig; try well-known paths
        for p in ("/lib/libc.so.6", "/usr/lib/libc.so.6"):
            if os.path.exists(p):
                libc_path = p
                break
    if libc_path is None:
        sock.close()
        raise OSError("libc not found")
    libc = ctypes.CDLL(libc_path, use_errno=True)

    addr = _HciSocketAddress(_BT_FAMILY, _ALL_CONTROLLERS, _MONITOR_CHANNEL)
    rc = libc.bind(
        ctypes.c_int(sock.fileno()),
        ctypes.pointer(addr),
        ctypes.c_int(ctypes.sizeof(addr)),
    )
    if rc != 0:
        errno = ctypes.get_errno()
        sock.close()
        raise OSError(errno, f"bind to monitor channel failed: {os.strerror(errno)}")

    sock.setblocking(False)
    return sock


def _walk_ad_structures(data: bytes,
                        mfg_filter: frozenset[int] | set[int] | None = None,
                        name_prefixes: 'Iterable[str] | None' = None,
                        ) -> 'tuple[dict[int, bytes], str | None]':
    """Parse AD structures: manufacturer-specific data, plus the local name.

    AD structure format (Bluetooth Core Spec Supplement, Part A):
        length (1 byte) — covers ad_type + ad_payload
        ad_type (1 byte)
        ad_payload (length - 1 bytes)

    Manufacturer Specific Data (ad_type 0xFF):
        company_id (2 bytes, little-endian)
        payload (remaining bytes)

    When *mfg_filter* is provided, only matching company IDs are included.

    The local name (AD 0x08/0x09) is decoded only when *name_prefixes* is
    given, and returned only when it starts with one of the prefixes —
    the name is a routing key for name-identified devices, not a general
    metadata field, and decoding every neighbour's name would be pure
    per-advertisement overhead.
    """
    result: dict[int, bytes] = {}
    name: 'str | None' = None
    pos = 0
    end = len(data)
    while pos < end:
        ad_len = data[pos]
        pos += 1
        if ad_len == 0 or pos + ad_len > end:
            break
        ad_type = data[pos]
        if ad_type == _AD_TYPE_MANUFACTURER and ad_len >= 3:
            company = data[pos + 1] | (data[pos + 2] << 8)
            if mfg_filter is None or company in mfg_filter:
                result[company] = bytes(data[pos + 3 : pos + ad_len])
        elif (name_prefixes and ad_len >= 2
                and ad_type in (_AD_TYPE_NAME_SHORT, _AD_TYPE_NAME_COMPLETE)):
            try:
                decoded = bytes(data[pos + 1 : pos + ad_len]).decode('utf-8')
            except UnicodeDecodeError:
                decoded = None
            # str.startswith takes a str or a TUPLE, never a set.  The caller
            # now hands us a live, mutable set (external name_prefix
            # registrations are folded into it in place), so convert at use.
            # A named advert is rare (name-routed devices only), so the
            # per-call tuple() is negligible.  Prod 2026-09-11 13:42Z: a set
            # here raised TypeError and crash-looped the tap thread.
            if decoded and decoded.startswith(tuple(name_prefixes)):
                name = decoded
        pos += ad_len
    return result, name


def _parse_legacy_reports(payload: bytes, offset: int, adapter_idx: int,
                          mfg_filter: frozenset[int] | set[int] | None = None,
                          ignored_macs: set[str] | None = None,
                          name_prefixes: 'Iterable[str] | None' = None,
                          known_macs: 'set[str] | None' = None,
                          ) -> list[TappedAdvertisement]:
    """Parse LE Advertising Report (subevent 0x02).

    Per-report layout (Bluetooth Core Spec Vol 4, Part E, §7.7.65.2):
        event_type   (1 byte)
        address_type (1 byte)
        address      (6 bytes, little-endian)
        data_length  (1 byte)
        data         (data_length bytes)
        rssi         (1 byte, signed)
    """
    if offset >= len(payload):
        return []
    num = payload[offset]
    offset += 1
    results: list[TappedAdvertisement] = []
    for _ in range(num):
        if offset + 10 > len(payload):
            break
        offset += 1  # event_type
        addr_type = payload[offset]
        offset += 1
        addr_bytes = payload[offset : offset + 6]
        offset += 6
        data_len = payload[offset]
        offset += 1
        if offset + data_len + 1 > len(payload):
            break
        ad_data = payload[offset : offset + data_len]
        offset += data_len
        rssi_raw = payload[offset]
        offset += 1
        rssi = rssi_raw - 256 if rssi_raw > 127 else rssi_raw

        mac = _format_mac(addr_bytes)
        if ignored_macs is not None and mac in ignored_macs:
            continue
        # Pre-walk MAC gate.  When the caller supplies a non-empty set of
        # KNOWN addresses (configured devices, learned name-device
        # addresses, router-registered addresses), a report from any other
        # address is dropped here, before the AD walk: with adoption closed
        # we would refuse to adopt it anyway, so parsing it is pure cost.
        # This is what makes an accept-all radio affordable -- a stranger
        # costs the MAC format + one set lookup instead of the TLV walk.
        # An empty/None set means "no gate" (discovery open: walk all).
        if known_macs and mac not in known_macs:
            continue

        mfg, name = _walk_ad_structures(ad_data, mfg_filter, name_prefixes)
        if mfg or name:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=mac,
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
                local_name=name,
            ))
    return results


def _parse_extended_reports(payload: bytes, offset: int, adapter_idx: int,
                            mfg_filter: frozenset[int] | set[int] | None = None,
                            ignored_macs: set[str] | None = None,
                            name_prefixes: 'Iterable[str] | None' = None,
                            known_macs: 'set[str] | None' = None,
                            ) -> list[TappedAdvertisement]:
    """Parse LE Extended Advertising Report (subevent 0x0D).

    Per-report layout (Bluetooth Core Spec Vol 4, Part E, §7.7.65.13):
        event_type               (2 bytes, little-endian)
        address_type             (1 byte)
        address                  (6 bytes, little-endian)
        primary_phy              (1 byte)
        secondary_phy            (1 byte)
        advertising_sid          (1 byte)
        tx_power                 (1 byte, signed)
        rssi                     (1 byte, signed)
        periodic_adv_interval    (2 bytes)
        direct_address_type      (1 byte)
        direct_address           (6 bytes)
        data_length              (1 byte)
        data                     (data_length bytes)
    """
    if offset >= len(payload):
        return []
    num = payload[offset]
    offset += 1
    results: list[TappedAdvertisement] = []
    for _ in range(num):
        if offset + 24 > len(payload):
            break
        event_type_lo = payload[offset]
        offset += 2
        data_status = (event_type_lo >> 5) & 0x03
        addr_type = payload[offset]
        offset += 1
        addr_bytes = payload[offset : offset + 6]
        offset += 6
        offset += 4  # primary_phy + secondary_phy + advertising_sid + tx_power
        rssi_raw = payload[offset]
        offset += 1
        rssi = rssi_raw - 256 if rssi_raw > 127 else rssi_raw
        offset += 9  # periodic_adv_interval + direct_address_type + direct_address
        if offset + 1 > len(payload):
            break
        data_len = payload[offset]
        offset += 1
        if offset + data_len > len(payload):
            break
        ad_data = payload[offset : offset + data_len]
        offset += data_len

        if data_status != 0:
            continue

        mac = _format_mac(addr_bytes)
        if ignored_macs is not None and mac in ignored_macs:
            continue
        # Pre-walk MAC gate.  When the caller supplies a non-empty set of
        # KNOWN addresses (configured devices, learned name-device
        # addresses, router-registered addresses), a report from any other
        # address is dropped here, before the AD walk: with adoption closed
        # we would refuse to adopt it anyway, so parsing it is pure cost.
        # This is what makes an accept-all radio affordable -- a stranger
        # costs the MAC format + one set lookup instead of the TLV walk.
        # An empty/None set means "no gate" (discovery open: walk all).
        if known_macs and mac not in known_macs:
            continue

        mfg, name = _walk_ad_structures(ad_data, mfg_filter, name_prefixes)
        if mfg or name:
            results.append(TappedAdvertisement(
                adapter_index=adapter_idx,
                mac=mac,
                address_type=addr_type,
                rssi=rssi,
                manufacturer_data=mfg,
                local_name=name,
            ))
    return results


def parse_monitor_frame(raw: bytes,
                        mfg_filter: frozenset[int] | set[int] | None = None,
                        ignored_macs: set[str] | None = None,
                        name_prefixes: 'Iterable[str] | None' = None,
                        allowed_adapters: 'set[int] | None' = None,
                        known_macs: 'set[str] | None' = None,
                        ) -> list[TappedAdvertisement]:
    """Parse one monitor channel datagram into advertisement(s).

    Each datagram has a 6-byte header followed by the HCI payload.
    Only HCI events containing LE Advertising Reports are processed;
    all other traffic is silently discarded.

    When *mfg_filter* is provided, only advertisements containing a
    matching manufacturer company ID are returned.

    When *ignored_macs* is provided, advertisements from those MACs are
    dropped before AD structure parsing.
    """
    if len(raw) < _FRAME_HDR_SIZE + 3:
        return []

    # Fast-path: check discriminator bytes before unpacking the header.
    # raw[6] = event_code, raw[8] = subevent (within the HCI payload).
    if raw[6] != _EVT_LE_META:
        return []

    opcode, adapter_idx, payload_len = _FRAME_HDR.unpack_from(raw, 0)
    if opcode != _OP_HCI_EVENT_RX:
        return []

    # Early-drop frames from adapters we do not scan, BEFORE parsing the
    # report body.  The monitor channel (HCI_DEV_NONE) delivers every
    # card's traffic, so another service's active discovery scan on a card
    # we don't own floods us with strangers we would otherwise fully parse
    # (~67 us/report on the Cerbo) only to discard at the mfg/name filter.
    # Empty/None means "no restriction" -- the safe default before any
    # adapter is known.  See dbus_ble_sensors._scan_adapter_indices.
    if allowed_adapters and adapter_idx not in allowed_adapters:
        return []

    subevent = raw[8]
    payload = raw[_FRAME_HDR_SIZE:]

    if subevent == _SUB_ADV_REPORT:
        return _parse_legacy_reports(payload, 3, adapter_idx, mfg_filter,
                                     ignored_macs, name_prefixes, known_macs)
    elif subevent == _SUB_EXT_ADV_REPORT:
        return _parse_extended_reports(payload, 3, adapter_idx, mfg_filter,
                                       ignored_macs, name_prefixes, known_macs)

    return []


# ── Kernel-side adapter filter (classic BPF on the monitor socket) ─────────
#
# The monitor channel delivers EVERY adapter's traffic.  parse_monitor_frame
# drops foreign-adapter frames in ~1 us, but each one still costs a recv()
# wakeup and a GIL acquisition on the tap thread.  A classic BPF program
# attached with SO_ATTACH_FILTER makes the kernel drop those frames before
# they are queued to our socket, so we never wake for them at all.
#
# Built ONLY from byte loads (BPF_LD|BPF_B|BPF_ABS): the monitor header and
# HCI fields are little-endian while BPF's halfword load is big-endian, and
# a wrong-endian compare would drop everything.  Byte loads have no
# endianness.  The program replicates parse_monitor_frame's own gates
# (opcode == event-rx, event == LE Meta) plus "adapter in our set"; its only
# possible error direction is passing a frame the parser then discards,
# never dropping one we need.  Proven on dev-cerbo 2026-09-06 with
# pass-all / drop-all / selective-adapter controls.
_BPF_LD_B_ABS = 0x30   # A = byte at [k]
_BPF_LD_H_ABS = 0x28   # A = 16-bit big-endian (as the bytes lie) at [k]
_BPF_LD_W_ABS = 0x20   # A = 32-bit big-endian (as the bytes lie) at [k]
_BPF_LD_B_IND = 0x50   # A = byte at [X+k]
_BPF_LD_H_IND = 0x48   # A = 16-bit at [X+k]
_BPF_LD_W_IND = 0x40   # A = 32-bit at [X+k]
_BPF_LD_MEM = 0x60     # A = M[k]
_BPF_ST = 0x02         # M[k] = A
_BPF_LDX_IMM = 0x01    # X = k
_BPF_ALU_ADD_K = 0x04  # A += k
_BPF_ALU_ADD_X = 0x0C  # A += X
_BPF_ALU_SUB_X = 0x1C  # A -= X
_BPF_MISC_TAX = 0x07   # X = A
_BPF_JMP_JEQ_K = 0x15  # if A == k: pc += jt+1 else pc += jf+1
_BPF_JMP_JGT_K = 0x25  # if A > k (unsigned)
_BPF_JMP_JGE_K = 0x35  # if A >= k (unsigned)
_BPF_JMP_JA = 0x05     # pc += k+1 (unconditional, 32-bit offset)
_BPF_RET_K = 0x06      # return k  (0 = drop, 0xFFFF = accept whole frame)
_SO_ATTACH_FILTER = 26
_SO_DETACH_FILTER = 27
_BPF_MAXINSNS = 4096   # kernel limit on a classic-BPF program

# Frame geometry for a SINGLE-report advertising datagram (offsets from
# the start of the monitor frame; hdr(6) evt(1) plen(1) sub(1) num(1)):
#   legacy   (subevent 0x02): event_type(1) addr_type(1) addr(6)
#                             data_len(1) data ... rssi(1)
#   extended (subevent 0x0D): event_type(2) addr_type(1) addr(6) phys(2)
#                             sid(1) tx(1) rssi(1) periodic(2) direct(7)
#                             data_len(1) data
_LEGACY_GEOM = (12, 18, 19)     # (addr_off, data_len_off, data_off)
_EXTENDED_GEOM = (13, 33, 34)
_INSNS_PER_MAC = 5
_MAX_KERNEL_MACS = 200          # 2 chains x 5 insns = 2000 of _BPF_MAXINSNS
_AD_WALK_STEPS = 8              # AD structures examined per report; 31-byte
                                # legacy data cannot hold more real ones
_MAX_KERNEL_IDS = 32
_MAX_KERNEL_PREFIXES = 8
_MAX_PREFIX_LEN = 29            # name AD structure payload in 31 bytes


def _bpf_insn(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _mac_to_raw(mac: str) -> bytes:
    """Inverse of _format_mac: the 6 address bytes as they lie in the frame
    (little-endian on the wire).  Raises ValueError for a malformed MAC."""
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError(f"not a 6-byte address: {mac!r}")
    return raw[::-1]


class _Asm:
    """Tiny classic-BPF assembler with symbolic jump targets.

    jt/jf/k may be a label name; resolve() turns each into the relative
    offset the kernel expects and refuses a conditional jump that would
    not fit the 8-bit field, so a program that assembles is a program the
    kernel's checker will accept on that count.
    """

    def __init__(self) -> None:
        self.insns: list[list] = []
        self.labels: dict[str, int] = {}

    def emit(self, code: int, jt=0, jf=0, k=0) -> None:
        self.insns.append([code, jt, jf, k])

    def label(self, name: str) -> None:
        assert name not in self.labels, name
        self.labels[name] = len(self.insns)

    def resolve(self) -> bytes:
        out = []
        for i, (code, jt, jf, k) in enumerate(self.insns):
            if isinstance(jt, str):
                jt = self.labels[jt] - (i + 1)
            if isinstance(jf, str):
                jf = self.labels[jf] - (i + 1)
            if isinstance(k, str):
                k = self.labels[k] - (i + 1)
            if not (0 <= jt <= 255 and 0 <= jf <= 255):
                raise ValueError(f"jump out of range at {i}: jt={jt} jf={jf}")
            if code == _BPF_JMP_JA and k < 0:
                raise ValueError(f"backward ja at {i}")
            out.append(_bpf_insn(code, jt, jf, k))
        if len(out) > _BPF_MAXINSNS:
            raise ValueError(f"program too long: {len(out)} insns")
        return b"".join(out)


def _emit_mac_block(a: _Asm, raw: bytes, addr_off: int) -> None:
    """Five instructions: accept the frame if the address at *addr_off*
    equals *raw*, else fall through.  All jumps local."""
    word = int.from_bytes(raw[0:4], "big")
    half = int.from_bytes(raw[4:6], "big")
    a.emit(_BPF_LD_W_ABS, k=addr_off)
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf=3, k=word)
    a.emit(_BPF_LD_H_ABS, k=addr_off + 4)
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf=1, k=half)
    a.emit(_BPF_RET_K, k=0xFFFF)


def _prefix_chunks(prefix: bytes) -> list[tuple[int, int, int]]:
    """(offset, load opcode, value) triples covering *prefix* left to
    right in word, halfword, byte pieces, compared as the bytes lie."""
    out = []
    off = 0
    while len(prefix) - off >= 4:
        out.append((off, _BPF_LD_W_IND, int.from_bytes(prefix[off:off + 4], "big")))
        off += 4
    if len(prefix) - off >= 2:
        out.append((off, _BPF_LD_H_IND, int.from_bytes(prefix[off:off + 2], "big")))
        off += 2
    if len(prefix) - off == 1:
        out.append((off, _BPF_LD_B_IND, prefix[off]))
    return out


def _emit_report_chain(a: _Asm, name: str, geom: tuple[int, int, int],
                       raws: list[bytes], ids: list[int],
                       prefixes: list[bytes]) -> None:
    """One subevent's gate: address blocks, then the AD-structure walk.

    The walk keeps M[0] = end of data and M[1] = current structure
    length, X = current structure offset.  Each of _AD_WALK_STEPS steps
    reads the length and type, checks manufacturer data (type 0xFF,
    company id as the two bytes lie) against *ids* and a complete or
    shortened name (0x09 / 0x08) against *prefixes* -- a prefix is only
    compared when the structure is long enough to hold it, so no compare
    ever reads past the structure -- then advances X by length + 1.  It
    stops at end of data or at a zero length; anything unmatched after the
    last step is dropped.  A load past the end of the datagram returns
    drop, the kernel's own rule.
    """
    addr_off, dlen_off, data_off = geom
    a.label(name)
    for raw in raws:
        _emit_mac_block(a, raw, addr_off)
    if not ids and not prefixes:
        a.emit(_BPF_RET_K, k=0)
        return
    a.emit(_BPF_LD_B_ABS, k=dlen_off)          # A = data_len
    a.emit(_BPF_ALU_ADD_K, k=data_off)         # A = end
    a.emit(_BPF_ST, k=0)                       # M[0] = end
    a.emit(_BPF_LDX_IMM, k=data_off)           # X = first structure
    end = f"{name}.END"
    for step in range(_AD_WALK_STEPS):
        s = f"{name}.S{step}"
        nxt = f"{name}.S{step + 1}" if step + 1 < _AD_WALK_STEPS else end
        a.label(s)
        a.emit(_BPF_LD_MEM, k=0)               # A = end
        a.emit(_BPF_ALU_SUB_X)                 # A = end - X
        # end-of-data exits jump to a per-step DROP so every conditional
        # jump stays within the 8-bit field however long the chain grows
        a.emit(_BPF_JMP_JGT_K, jt=0, jf=f"{s}.DROP", k=1)   # need len + type
        a.emit(_BPF_LD_B_IND, k=0)             # A = len
        a.emit(_BPF_JMP_JEQ_K, jt=f"{s}.DROP", jf=0, k=0)
        a.emit(_BPF_ST, k=1)                   # M[1] = len
        a.emit(_BPF_LD_B_IND, k=1)             # A = type
        if ids:
            a.emit(_BPF_JMP_JEQ_K, jt=0, jf=f"{s}.NAME", k=_AD_TYPE_MANUFACTURER)
            a.emit(_BPF_LD_H_IND, k=2)         # company id, bytes as they lie
            for cid in ids:
                a.emit(_BPF_JMP_JEQ_K, jt=f"{s}.ACC", jf=0,
                       k=((cid & 0xFF) << 8) | (cid >> 8))
            a.emit(_BPF_JMP_JA, k=f"{s}.ADV")
        a.label(f"{s}.NAME")
        if prefixes:
            a.emit(_BPF_JMP_JEQ_K, jt=f"{s}.P0", jf=0, k=_AD_TYPE_NAME_COMPLETE)
            a.emit(_BPF_JMP_JEQ_K, jt=0, jf=f"{s}.ADV", k=_AD_TYPE_NAME_SHORT)
            for i, pfx in enumerate(prefixes):
                nxt_p = f"{s}.P{i + 1}" if i + 1 < len(prefixes) else f"{s}.ADV"
                a.label(f"{s}.P{i}")
                a.emit(_BPF_LD_MEM, k=1)       # A = len
                a.emit(_BPF_JMP_JGE_K, jt=0, jf=nxt_p, k=len(pfx) + 1)
                chunks = _prefix_chunks(pfx)
                for j, (off, op, val) in enumerate(chunks):
                    a.emit(op, k=2 + off)
                    last = j + 1 == len(chunks)
                    a.emit(_BPF_JMP_JEQ_K, jt=(f"{s}.ACC" if last else 0),
                           jf=nxt_p, k=val)
        a.label(f"{s}.ADV")
        a.emit(_BPF_LD_MEM, k=1)               # A = len
        a.emit(_BPF_ALU_ADD_K, k=1)
        a.emit(_BPF_ALU_ADD_X)                 # A = X + len + 1
        a.emit(_BPF_MISC_TAX)                  # X = A
        a.emit(_BPF_JMP_JA, k=nxt)
        a.label(f"{s}.ACC")
        a.emit(_BPF_RET_K, k=0xFFFF)
        a.label(f"{s}.DROP")
        a.emit(_BPF_RET_K, k=0)
    a.label(end)
    a.emit(_BPF_RET_K, k=0)


def build_adapter_filter(allowed: 'set[int] | frozenset[int]',
                         known_macs: 'Iterable[str] | None' = None,
                         mfg_ids: 'Iterable[int] | None' = None,
                         name_prefixes: 'Iterable[str] | None' = None) -> bytes:
    """Classic-BPF program bytes for the monitor socket.

    Always: accept only LE-Meta event frames from *allowed* adapter
    indices.  With no other argument that is the whole program, byte for
    byte what this function produced before any gate existed.

    With any of *known_macs*, *mfg_ids*, *name_prefixes*, a single-report
    advertising frame must ALSO satisfy one of them to pass:
      * report[0]'s address is in *known_macs* (hex strings in
        _format_mac's spelling);
      * an AD structure carries manufacturer data with a company id in
        *mfg_ids*;
      * an AD structure carries a name starting with one of
        *name_prefixes*.
    Everything else is dropped in the kernel and never wakes the tap.
    That is where an accept-all radio's cost to THIS PROCESS lives: each
    stranger datagram costs a select()+recv() wakeup (~200 us on the
    Cerbo) before Python sees a byte.  It does nothing for the kernel's
    own per-advertisement work, which only the radio can avoid.

    Conservative by construction:
      * a datagram carrying more than one report is ACCEPTED unexamined
        (the userspace gates judge each report);
      * any LE-Meta subevent other than the two advertising-report kinds
        is ACCEPTED (the parser discards it cheaply);
      * legacy and extended reports have their own chains, dispatched
        by subevent, since their offsets differ;
      * the AD walk examines at most _AD_WALK_STEPS structures; a wanted
        structure further in than that is missed, which cannot happen in
        31 bytes of legacy data.
    """
    idx = sorted(int(i) for i in allowed)
    n = len(idx)
    if n == 0 or n > 200:
        raise ValueError("adapter set must be 1..200 entries")
    raws = sorted({_mac_to_raw(m) for m in (known_macs or ())})
    if len(raws) > _MAX_KERNEL_MACS:
        raise ValueError(f"address gate holds at most {_MAX_KERNEL_MACS} "
                         f"addresses, got {len(raws)}")
    ids = sorted({int(i) for i in (mfg_ids or ()) if 0 <= int(i) <= 0xFFFF})
    if len(ids) > _MAX_KERNEL_IDS:
        raise ValueError(f"at most {_MAX_KERNEL_IDS} manufacturer ids, got {len(ids)}")
    prefixes = sorted({str(p).encode("utf-8") for p in (name_prefixes or ()) if p})
    if len(prefixes) > _MAX_KERNEL_PREFIXES:
        raise ValueError(f"at most {_MAX_KERNEL_PREFIXES} name prefixes, got {len(prefixes)}")
    if any(len(p) > _MAX_PREFIX_LEN for p in prefixes):
        raise ValueError("a name prefix longer than an AD structure can hold")

    a = _Asm()
    a.emit(_BPF_LD_B_ABS, k=0)                                   # opcode lo
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf="DROP", k=_OP_HCI_EVENT_RX)
    a.emit(_BPF_LD_B_ABS, k=6)                                   # event code
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf="DROP", k=_EVT_LE_META)
    a.emit(_BPF_LD_B_ABS, k=2)                                   # adapter idx
    for i in idx:
        a.emit(_BPF_JMP_JEQ_K, jt="ADAPTER_OK", jf=0, k=i)
    a.label("DROP")
    a.emit(_BPF_RET_K, k=0)
    a.label("ADAPTER_OK")
    if not raws and not ids and not prefixes:
        a.emit(_BPF_RET_K, k=0xFFFF)
        return a.resolve()

    a.emit(_BPF_LD_B_ABS, k=9)                                   # num_reports
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf="ACCEPT", k=1)
    a.emit(_BPF_LD_B_ABS, k=8)                                   # subevent
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf=1, k=_SUB_ADV_REPORT)
    a.emit(_BPF_JMP_JA, k="LEG")
    a.emit(_BPF_JMP_JEQ_K, jt=0, jf=1, k=_SUB_EXT_ADV_REPORT)
    a.emit(_BPF_JMP_JA, k="EXT")
    a.label("ACCEPT")
    a.emit(_BPF_RET_K, k=0xFFFF)
    _emit_report_chain(a, "LEG", _LEGACY_GEOM, raws, ids, prefixes)
    _emit_report_chain(a, "EXT", _EXTENDED_GEOM, raws, ids, prefixes)
    return a.resolve()


def _attach_program(sock: socket.socket, prog: bytes) -> None:
    buf = ctypes.create_string_buffer(prog)
    fprog = struct.pack("HL", len(prog) // 8, ctypes.addressof(buf))
    sock.setsockopt(socket.SOL_SOCKET, _SO_ATTACH_FILTER, fprog)


def attach_adapter_filter(sock: socket.socket,
                          allowed: 'set[int] | frozenset[int] | None',
                          known_macs: 'Iterable[str] | None' = None,
                          mfg_ids: 'Iterable[int] | None' = None,
                          name_prefixes: 'Iterable[str] | None' = None) -> bool:
    """Install (or, for an empty/None adapter set, remove) the kernel filter.

    With any of *known_macs*, *mfg_ids*, *name_prefixes* the program also
    carries the advertisement gate (see build_adapter_filter).  If the
    gated program is refused -- too many entries, a malformed one, or
    the kernel says no -- the adapter-only program is installed instead
    and the userspace gates carry the strangers, so the tap stays correct
    either way.

    Returns True if the kernel accepted the change.  A failure is logged
    and returns False; parse_monitor_frame's own early-drop still applies,
    so the tap stays correct either way -- this is an optimisation layered
    on an already-safe path.
    """
    try:
        if not allowed:
            sock.setsockopt(socket.SOL_SOCKET, _SO_DETACH_FILTER,
                            struct.pack("I", 0))
            _log.info("tap kernel adapter filter: removed (no restriction)")
            return True
        macs = sorted(set(known_macs or ()))
        ids = sorted(set(mfg_ids or ()))
        pfx = sorted(set(name_prefixes or ()))
        gate = "open (every report from these cards is delivered)"
        if macs or ids or pfx:
            # Degrade in stages: the address blocks go first (userspace
            # gates addresses anyway), then the whole gate.
            try:
                _attach_program(sock, build_adapter_filter(allowed, macs, ids, pfx))
                gate = (f"closed: {len(ids)} manufacturer id(s), {len(pfx)} "
                        f"name prefix(es), {len(macs)} address(es); other "
                        "advertisements dropped in-kernel, never waking the tap")
            except (OSError, ValueError) as e:
                try:
                    if not (ids or pfx):
                        raise
                    _attach_program(sock, build_adapter_filter(allowed, None, ids, pfx))
                    _log.warning("tap kernel address blocks not applied (%r); "
                                 "gate carries ids and prefixes only", e)
                    gate = (f"closed: {len(ids)} manufacturer id(s), {len(pfx)} "
                            "name prefix(es), addresses left to userspace")
                except (OSError, ValueError) as e2:
                    _log.warning("tap kernel advertisement gate not applied (%r); "
                                 "adapter-only filter, userspace gates carry "
                                 "the strangers", e2)
                    _attach_program(sock, build_adapter_filter(allowed))
        else:
            _attach_program(sock, build_adapter_filter(allowed))
        _log.info("tap kernel adapter filter: attached for hci%s "
                  "(LE-Meta events only; other adapters dropped in-kernel); "
                  "advertisement gate %s", sorted(allowed), gate)
        return True
    except (OSError, ValueError) as e:
        _log.warning("tap kernel adapter filter not applied (%r); "
                     "falling back to the userspace early-drop", e)
        return False


def run_tap_loop(sock: socket.socket, callback, stop_event: threading.Event,
                 mfg_filter: frozenset[int] | set[int] | None = None,
                 ignored_macs: set[str] | None = None,
                 name_prefixes: 'Iterable[str] | None' = None,
                 allowed_adapters: 'set[int] | None' = None,
                 known_macs: 'set[str] | None' = None):
    """Read monitor frames and invoke callback for each parsed advertisement.

    Blocks until stop_event is set.  The callback receives a single
    TappedAdvertisement argument and is called on the tap thread — the
    caller is responsible for bridging to the appropriate thread.

    When *mfg_filter* is provided, only advertisements with matching
    manufacturer company IDs are forwarded to the callback.

    When *ignored_macs* is provided, advertisements from those MACs are
    dropped before AD structure parsing.
    """
    while not stop_event.is_set():
        try:
            readable, _, _ = select.select([sock], [], [], 1.0)
        except (OSError, ValueError):
            break
        if not readable:
            continue
        try:
            raw = sock.recv(_RECV_BUF)
        except BlockingIOError:
            continue
        except (OSError, ValueError):
            break
        if not raw:
            break
        for adv in parse_monitor_frame(raw, mfg_filter, ignored_macs,
                                       name_prefixes, allowed_adapters,
                                       known_macs):
            try:
                callback(adv)
            except Exception:
                _log.exception("tap callback error for %s", adv.mac)

    try:
        sock.close()
    except OSError:
        pass
