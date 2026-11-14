"""Classic CAN frame timing, theoretical bus load and Davis WCRT.

Times are exact :class:`fractions.Fraction` internally and returned as float seconds.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction

STUFFING_MODES = ("worst", "actual", "none")
FIXED_TAIL_BITS = 13  # CRC delimiter 1 + ACK 2 + EOF 7 + IFS 3 (not stuffed)
CRC15_POLY = 0x4599


def _g(extended: bool) -> int:
    """Stuffable header+CRC bits excluding payload: 34 (11-bit ID) / 54 (29-bit ID)."""
    return 54 if extended else 34


def frame_bits(dlc: int, stuffing: str = "worst", extended: bool = False) -> int:
    """Bits on the wire for a classic CAN data frame with ``dlc`` payload bytes (incl. IFS).

    ``worst``: Davis et al. bound; ``none``: no stuff bits. ``actual`` needs the payload,
    use :func:`frame_bits_actual`.
    """
    if not 0 <= dlc <= 8:
        raise ValueError(f"dlc {dlc} not in 0..8")
    g = _g(extended)
    if stuffing == "worst":
        return g + 8 * dlc + FIXED_TAIL_BITS + (g + 8 * dlc - 1) // 4
    if stuffing == "none":
        return g + 8 * dlc + FIXED_TAIL_BITS
    if stuffing == "actual":
        raise ValueError("stuffing='actual' needs the payload; use frame_bits_actual()")
    raise ValueError(f"unknown stuffing mode {stuffing!r}; expected one of {STUFFING_MODES}")


def _bits(value: int, width: int) -> list[int]:
    return [(value >> i) & 1 for i in range(width - 1, -1, -1)]


def crc15(bits: Iterable[int]) -> int:
    """CAN CRC-15 (poly 0x4599, init 0) over a bit sequence."""
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= CRC15_POLY
    return crc


def stuffed_region(can_id: int, data: bytes, extended: bool = False) -> list[int]:
    """Unstuffed bit string SOF..CRC sequence of a data frame (dominant = 0)."""
    if len(data) > 8:
        raise ValueError("classic CAN payload is at most 8 bytes")
    if extended:
        hdr = [0] + _bits(can_id >> 18, 11) + [1, 1] + _bits(can_id & 0x3FFFF, 18) + [0, 0, 0]
    else:
        hdr = [0] + _bits(can_id, 11) + [0, 0, 0]  # SOF, ID, RTR, IDE, r0
    bits = hdr + _bits(len(data), 4)
    for byte in data:
        bits += _bits(byte, 8)
    return bits + _bits(crc15(bits), 15)


def count_stuff_bits(bits: Sequence[int]) -> int:
    """Number of stuff bits inserted after every run of 5 equal bits (stuff bits count)."""
    if not bits:
        return 0
    stuff, run, last = 0, 1, bits[0]
    for b in bits[1:]:
        if b == last:
            run += 1
            if run == 5:
                stuff += 1
                last, run = 1 - b, 1  # the stuff bit starts a new run
        else:
            last, run = b, 1
    return stuff


def frame_bits_actual(can_id: int, data: bytes, extended: bool = False) -> int:
    """Exact bits on the wire for this frame (stuff bits counted, incl. IFS)."""
    region = stuffed_region(can_id, data, extended)
    return len(region) + count_stuff_bits(region) + FIXED_TAIL_BITS


def frame_time(dlc: int, bitrate: float, stuffing: str = "worst", extended: bool = False) -> float:
    """Transmission time C [s] of a frame."""
    return frame_bits(dlc, stuffing, extended) / bitrate


def _q(x: float | int | Fraction) -> Fraction:
    """Exact rational of a decimal value (0.01 -> 1/100, not the binary float)."""
    return (
        x if isinstance(x, Fraction) else Fraction(repr(x)) if isinstance(x, float) else Fraction(x)
    )


@dataclass(frozen=True)
class TimingMessage:
    """Input to the response-time analysis. ``period`` None = event message (blocking only)."""

    name: str
    can_id: int
    bits: int
    period: float | None
    deadline: float | None
    jitter: float = 0.0


@dataclass(frozen=True)
class WcrtResult:
    name: str
    can_id: int
    bits: int
    c: float
    period: float
    deadline: float
    jitter: float
    r: float | None  # None = busy period diverges (load >= 100 %)

    @property
    def schedulable(self) -> bool:
        return self.r is not None and self.r <= self.deadline


def bus_load_theory(msgs: Iterable[TimingMessage], bitrate: float) -> float:
    """Utilisation sum(C/T) of periodic messages (fraction, 1.0 = 100 %)."""
    return float(
        sum(Fraction(m.bits) / _q(m.period) for m in msgs if m.period is not None) / _q(bitrate)
    )


def davis_wcrt(msgs: Sequence[TimingMessage], bitrate: float) -> list[WcrtResult]:
    """Worst-case response times per Davis, Burns, Bril, Lukkien (2007).

    Lower CAN ID = higher priority. Event messages (period None) only contribute blocking.
    Results are returned for periodic messages, in ascending ID order.
    """
    tau = 1 / _q(bitrate)
    ordered = sorted(msgs, key=lambda m: m.can_id)
    cost = {m.can_id: Fraction(m.bits) * tau for m in ordered}
    results: list[WcrtResult] = []
    for i, m in enumerate(ordered):
        if m.period is None:
            continue
        c, t_m, j_m = cost[m.can_id], _q(m.period), _q(m.jitter)
        d_m = _q(m.deadline if m.deadline is not None else m.period)
        blocking = max((cost[k.can_id] for k in ordered[i + 1 :]), default=Fraction(0))
        hp = [
            (cost[k.can_id], _q(k.period), _q(k.jitter))
            for k in ordered[:i]
            if k.period is not None
        ]
        hep = hp + [(c, t_m, j_m)]
        r: Fraction | None = None
        if sum(ck / tk for ck, tk, _ in hep) < 1:
            t = c
            while True:
                nxt = blocking + sum(math.ceil((t + jk) / tk) * ck for ck, tk, jk in hep)
                if nxt == t:
                    break
                t = nxt
            r = Fraction(0)
            for q in range(math.ceil((t + j_m) / t_m)):
                w = blocking + q * c
                while True:
                    nxt = blocking + q * c
                    nxt += sum(math.ceil((w + jk + tau) / tk) * ck for ck, tk, jk in hp)
                    if nxt == w:
                        break
                    w = nxt
                r = max(r, j_m + w - q * t_m + c)
        results.append(
            WcrtResult(
                name=m.name,
                can_id=m.can_id,
                bits=m.bits,
                c=float(c),
                period=float(t_m),
                deadline=float(d_m),
                jitter=float(j_m),
                r=None if r is None else float(r),
            )
        )
    return results


def timing_messages(net, stuffing: str = "worst", bus: str | None = None) -> list[TimingMessage]:
    """Convert a :class:`canav.netdef.Network` (optionally one bus) to timing inputs."""
    return [
        TimingMessage(
            name=m.name,
            can_id=m.can_id,
            bits=frame_bits(m.dlc, stuffing, m.extended),
            period=m.period,
            deadline=m.deadline,
            jitter=m.jitter,
        )
        for m in net.messages
        if bus is None or m.bus == bus
    ]
