"""Gateway UDP protocol "CAV1" (§5.8, docs/gateway_protocol.md).

All fields in network byte order::

    Header 20 B : magic "CAV1" 4s | version u8 | type u8 | count u16 | seq u32 | t_send_ns u64
    DATA record : can_id u32 | t_rx_ns u64 | len u8 | flags u8 | data[len]   (14 + len B)
    STATS body  : rx_frames u64 | rx_dropped u32 | datagrams u32 | diag_sent u32 | rejected u32

``can_id`` keeps the SocketCAN flags (EFF bit 31, RTR bit 30, ERR bit 29). Times are
CLOCK_REALTIME ns. ``seq`` counts every datagram of one sender and wraps at 2^32.
"""

from __future__ import annotations

import struct
from dataclasses import astuple, dataclass

MAGIC = b"CAV1"
VERSION = 1
TYPE_DATA, TYPE_DIAG_REQ, TYPE_HEARTBEAT, TYPE_STATS = 1, 2, 3, 4
TYPES = {TYPE_DATA, TYPE_DIAG_REQ, TYPE_HEARTBEAT, TYPE_STATS}
MAX_DATAGRAM = 1400
SEQ_MOD = 1 << 32

HEADER = struct.Struct("!4sBBHIQ")
RECORD = struct.Struct("!IQBB")
STATS = struct.Struct("!QIIII")
assert HEADER.size == 20 and RECORD.size == 14 and STATS.size == 24

# record flags
FLAG_OWN_TX = 0x01  # frame sent by the gateway itself (MSG_CONFIRM)
FLAG_SW_TIMESTAMP = 0x02  # no kernel timestamp: t_rx_ns from time.time_ns() in the gateway

CAN_EFF_FLAG, CAN_RTR_FLAG, CAN_ERR_FLAG = 0x80000000, 0x40000000, 0x20000000
CAN_SFF_MASK, CAN_EFF_MASK = 0x7FF, 0x1FFFFFFF


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Record:
    can_id: int  # with SocketCAN flags
    t_rx_ns: int
    flags: int
    data: bytes

    @property
    def size(self) -> int:
        return RECORD.size + len(self.data)


@dataclass(frozen=True, slots=True)
class Stats:
    rx_frames: int
    rx_dropped: int
    datagrams: int
    diag_sent: int
    rejected: int


@dataclass(frozen=True, slots=True)
class Datagram:
    type: int
    seq: int
    t_send_ns: int
    records: tuple[Record, ...] = ()
    stats: Stats | None = None


def encode(type_: int, seq: int, t_send_ns: int, records=(), stats: Stats | None = None) -> bytes:
    if type_ not in TYPES:
        raise ProtocolError(f"unknown type {type_}")
    parts = [HEADER.pack(MAGIC, VERSION, type_, len(records), seq % SEQ_MOD, t_send_ns)]
    for r in records:
        if len(r.data) > 8:
            raise ProtocolError("classic CAN only: len <= 8")
        parts.append(RECORD.pack(r.can_id, r.t_rx_ns, len(r.data), r.flags))
        parts.append(bytes(r.data))
    if type_ == TYPE_STATS:
        if stats is None:
            raise ProtocolError("STATS needs a body")
        parts.append(STATS.pack(*astuple(stats)))
    out = b"".join(parts)
    if len(out) > MAX_DATAGRAM:
        raise ProtocolError(f"datagram {len(out)} B > {MAX_DATAGRAM} B")
    return out


def decode(buf: bytes) -> Datagram:
    if len(buf) < HEADER.size:
        raise ProtocolError("truncated header")
    magic, version, type_, count, seq, t_send = HEADER.unpack_from(buf)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ProtocolError(f"unsupported version {version}")
    if type_ not in TYPES:
        raise ProtocolError(f"unknown type {type_}")
    off = HEADER.size
    records = []
    for _ in range(count):
        if off + RECORD.size > len(buf):
            raise ProtocolError("truncated record")
        can_id, t_rx, length, flags = RECORD.unpack_from(buf, off)
        off += RECORD.size
        if length > 8 or off + length > len(buf):
            raise ProtocolError("bad record length")
        records.append(Record(can_id, t_rx, flags, bytes(buf[off : off + length])))
        off += length
    stats = None
    if type_ == TYPE_STATS:
        if off + STATS.size > len(buf):
            raise ProtocolError("truncated STATS body")
        stats = Stats(*STATS.unpack_from(buf, off))
        off += STATS.size
    if off != len(buf):
        raise ProtocolError(f"{len(buf) - off} trailing bytes")
    return Datagram(type_, seq, t_send, tuple(records), stats)


def seq_gap(prev: int, cur: int) -> int:
    """Datagrams missing between two received sequence numbers (wraps at 2^32)."""
    return (cur - prev - 1) % SEQ_MOD


class Batcher:
    """DATA batching: flush at ``max_frames`` records, when the next record would exceed
    ``max_datagram``, or when the oldest pending record is ``flush_ns`` old.

    Pure logic (times are passed in) so it can be tested without sockets.
    """

    def __init__(
        self, max_frames: int = 32, flush_ns: int = 5_000_000, max_datagram: int = MAX_DATAGRAM
    ):
        if max_frames < 1:
            raise ValueError("max_frames >= 1")
        self.max_frames = max_frames
        self.flush_ns = flush_ns
        self.max_datagram = max_datagram
        self.pending: list[Record] = []
        self.size = HEADER.size
        self.first_ns: int | None = None  # monotonic time of the oldest pending record

    def add(self, rec: Record, now_ns: int) -> list[list[Record]]:
        """Add a record; returns the batches that are now complete (usually none or one)."""
        batches = []
        if self.pending and self.size + rec.size > self.max_datagram:
            batches.append(self.take())
        self.pending.append(rec)
        self.size += rec.size
        if self.first_ns is None:
            self.first_ns = now_ns
        if len(self.pending) >= self.max_frames:
            batches.append(self.take())
        return batches

    def due(self, now_ns: int) -> bool:
        return self.first_ns is not None and now_ns - self.first_ns >= self.flush_ns

    def deadline(self) -> int | None:
        return None if self.first_ns is None else self.first_ns + self.flush_ns

    def take(self) -> list[Record]:
        out, self.pending, self.size, self.first_ns = self.pending, [], HEADER.size, None
        return out
