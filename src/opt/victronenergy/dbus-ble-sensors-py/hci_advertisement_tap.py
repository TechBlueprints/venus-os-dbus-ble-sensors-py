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
                        name_prefixes: 'tuple[str, ...] | None' = None,
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
            if decoded and decoded.startswith(name_prefixes):
                name = decoded
        pos += ad_len
    return result, name


def _parse_legacy_reports(payload: bytes, offset: int, adapter_idx: int,
                          mfg_filter: frozenset[int] | set[int] | None = None,
                          ignored_macs: set[str] | None = None,
                          name_prefixes: 'tuple[str, ...] | None' = None,
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
                            name_prefixes: 'tuple[str, ...] | None' = None,
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
                        name_prefixes: 'tuple[str, ...] | None' = None,
                        allowed_adapters: 'set[int] | None' = None,
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
                                     ignored_macs, name_prefixes)
    elif subevent == _SUB_EXT_ADV_REPORT:
        return _parse_extended_reports(payload, 3, adapter_idx, mfg_filter,
                                       ignored_macs, name_prefixes)

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
_BPF_JMP_JEQ_K = 0x15  # if A == k: pc += jt+1 else pc += jf+1
_BPF_RET_K = 0x06      # return k  (0 = drop, 0xFFFF = accept whole frame)
_SO_ATTACH_FILTER = 26
_SO_DETACH_FILTER = 27


def _bpf_insn(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def build_adapter_filter(allowed: 'set[int] | frozenset[int]') -> bytes:
    """Classic-BPF program bytes: accept LE-Meta event frames from *allowed*
    adapter indices, drop everything else.  Deterministic; unit-tested."""
    idx = sorted(int(i) for i in allowed)
    n = len(idx)
    if n == 0 or n > 200:
        raise ValueError("adapter set must be 1..200 entries")
    prog = [
        _bpf_insn(_BPF_LD_B_ABS, k=0),                        # 0: opcode lo
        _bpf_insn(_BPF_JMP_JEQ_K, jt=0, jf=3 + n, k=_OP_HCI_EVENT_RX),  # 1
        _bpf_insn(_BPF_LD_B_ABS, k=6),                        # 2: event code
        _bpf_insn(_BPF_JMP_JEQ_K, jt=0, jf=1 + n, k=_EVT_LE_META),      # 3
        _bpf_insn(_BPF_LD_B_ABS, k=2),                        # 4: adapter idx
    ]
    for j, i in enumerate(idx):                               # 5..5+n-1
        prog.append(_bpf_insn(_BPF_JMP_JEQ_K, jt=n - j, jf=0, k=i))
    prog.append(_bpf_insn(_BPF_RET_K, k=0))                   # 5+n: DROP
    prog.append(_bpf_insn(_BPF_RET_K, k=0xFFFF))              # 6+n: ACCEPT
    return b"".join(prog)


def attach_adapter_filter(sock: socket.socket,
                          allowed: 'set[int] | frozenset[int] | None') -> bool:
    """Install (or, for an empty/None set, remove) the kernel adapter filter.

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
        prog = build_adapter_filter(allowed)
        buf = ctypes.create_string_buffer(prog)
        fprog = struct.pack("HL", len(prog) // 8, ctypes.addressof(buf))
        sock.setsockopt(socket.SOL_SOCKET, _SO_ATTACH_FILTER, fprog)
        _log.info("tap kernel adapter filter: attached for hci%s "
                  "(LE-Meta events only; other adapters dropped in-kernel)",
                  sorted(allowed))
        return True
    except (OSError, ValueError) as e:
        _log.warning("tap kernel adapter filter not applied (%r); "
                     "falling back to the userspace early-drop", e)
        return False


def run_tap_loop(sock: socket.socket, callback, stop_event: threading.Event,
                 mfg_filter: frozenset[int] | set[int] | None = None,
                 ignored_macs: set[str] | None = None,
                 name_prefixes: 'tuple[str, ...] | None' = None,
                 allowed_adapters: 'set[int] | None' = None):
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
                                       name_prefixes, allowed_adapters):
            try:
                callback(adv)
            except Exception:
                _log.exception("tap callback error for %s", adv.mac)

    try:
        sock.close()
    except OSError:
        pass
