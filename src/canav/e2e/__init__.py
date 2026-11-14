"""End-to-end protection: CRC8 SAE J1850 + 4-bit alive counter.

Layout for a DLC-``n`` frame (n in {4, 8}):
- byte ``n-1``          : CRC8 over bytes ``0..n-2`` followed by DataID (low byte of CAN ID)
- low nibble of ``n-2`` : AliveCounter (0..15, wraps)
- data bits ``0 .. 8*(n-2)-1`` carry signals; the high nibble of byte ``n-2`` is unused.
"""

from __future__ import annotations

from dataclasses import dataclass

E2E_DLCS = (4, 8)
COUNTER_SIGNAL = "AliveCounter"
CRC_SIGNAL = "CRC8"
COUNTER_MODULO = 16


def crc8_j1850(data: bytes, init: int = 0xFF, xorout: int = 0xFF) -> int:
    """CRC-8 SAE J1850: poly 0x1D, init 0xFF, xorout 0xFF, no reflection."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ xorout


def layout(dlc: int) -> tuple[int, int]:
    """Return (counter_byte, crc_byte) indices for an E2E-protected frame of ``dlc`` bytes."""
    if dlc not in E2E_DLCS:
        raise ValueError(f"E2E supports DLC {E2E_DLCS}, got {dlc}")
    return dlc - 2, dlc - 1


def data_bits(dlc: int) -> int:
    """Number of payload bits available for signals (bits 0 .. data_bits-1)."""
    return 8 * layout(dlc)[0]


def compute_crc(can_id: int, data: bytes) -> int:
    """CRC8 over all bytes except the CRC byte, then DataID = low byte of ``can_id``."""
    _, crc_byte = layout(len(data))
    return crc8_j1850(bytes(data[:crc_byte]) + bytes([can_id & 0xFF]))


def next_counter(counter: int) -> int:
    """Alive counter successor (mod 16)."""
    return (counter + 1) % COUNTER_MODULO


def protect(can_id: int, data: bytes, counter: int) -> bytes:
    """Write ``counter`` and the CRC into ``data``; returns the protected payload."""
    buf = bytearray(data)
    counter_byte, crc_byte = layout(len(buf))
    buf[counter_byte] = (buf[counter_byte] & 0xF0) | (counter % COUNTER_MODULO)
    buf[crc_byte] = compute_crc(can_id, buf)
    return bytes(buf)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of an E2E check on one received frame."""

    crc_ok: bool
    counter: int
    counter_ok: bool

    @property
    def ok(self) -> bool:
        return self.crc_ok and self.counter_ok


def check(
    can_id: int, data: bytes, last_counter: int | None = None, max_delta: int = 1
) -> CheckResult:
    """Verify CRC and counter progression.

    The counter is accepted if ``last_counter`` is None (first frame) or the forward
    distance ``(counter - last_counter) mod 16`` is in ``1..max_delta``.
    """
    counter_byte, crc_byte = layout(len(data))
    counter = data[counter_byte] & 0x0F
    crc_ok = data[crc_byte] == compute_crc(can_id, data)
    if last_counter is None:
        counter_ok = True
    else:
        delta = (counter - last_counter) % COUNTER_MODULO
        counter_ok = 1 <= delta <= max_delta
    return CheckResult(crc_ok=crc_ok, counter=counter, counter_ok=counter_ok)
