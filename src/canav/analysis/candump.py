"""Read/write the ``candump -L`` text log format, shared by both tiers.

Line format: ``(<sec>.<usec>) <iface> <id>#<hexdata>``; 3 hex digits = 11-bit ID, 8 = 29-bit.
Also accepted: remote frames ``<id>#R[<dlc>]`` and CAN FD ``<id>##<flags><hexdata>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

CAN_EFF_MASK = 0x1FFFFFFF
CAN_ERR_FLAG = 0x20000000
_LINE = re.compile(r"^\((\d+)\.(\d+)\)\s+(\S+)\s+([0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})#(.*)$")


@dataclass(frozen=True)
class CanRecord:
    t_ns: int
    iface: str
    can_id: int
    data: bytes
    extended: bool = False
    rtr: bool = False
    fd: bool = False
    error: bool = False


def format_line(t_ns: int, iface: str, can_id: int, data: bytes, extended: bool = False) -> str:
    """Format a classic data frame; the timestamp is truncated to µs like candump."""
    sec, rem = divmod(t_ns, 1_000_000_000)
    ident = f"{can_id:08X}" if extended else f"{can_id:03X}"
    return f"({sec}.{rem // 1000:06d}) {iface} {ident}#{data.hex().upper()}"


def parse_line(line: str) -> CanRecord:
    m = _LINE.match(line.strip())
    if m is None:
        raise ValueError(f"not a candump -L line: {line!r}")
    sec, frac, iface, ident, rest = m.groups()
    t_ns = int(sec) * 1_000_000_000 + int(frac.ljust(9, "0")[:9])
    raw_id = int(ident, 16)
    extended = len(ident) == 8
    error = extended and bool(raw_id & CAN_ERR_FLAG)
    can_id = raw_id & CAN_EFF_MASK if extended else raw_id
    rtr = fd = False
    if rest.startswith("#"):  # CAN FD: one flags nibble, then data
        fd = True
        data = bytes.fromhex(rest[2:])
    elif rest[:1] in ("R", "r"):
        rtr = True
        data = b""
    else:
        data = bytes.fromhex(rest)
    return CanRecord(t_ns, iface, can_id, data, extended, rtr, fd, error)


def read_log(path: str | Path) -> Iterator[CanRecord]:
    with open(path) as f:
        for line in f:
            if line.strip():
                yield parse_line(line)


def write_log(path: str | Path, records: Iterable[CanRecord]) -> None:
    with open(path, "w") as f:
        for r in records:
            f.write(format_line(r.t_ns, r.iface, r.can_id, r.data, r.extended) + "\n")
