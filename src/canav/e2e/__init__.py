"""End-to-end protection: CRC8 SAE J1850 + 4-bit alive counter.

Layout for a DLC-``n`` frame (n in {4, 8}):
- byte ``n-1``          : CRC8 over bytes ``0..n-2`` followed by DataID (low byte of CAN ID)
- low nibble of ``n-2`` : AliveCounter (0..15, wraps)
- data bits ``0 .. 8*(n-2)-1`` carry signals; the high nibble of byte ``n-2`` is unused.
"""

from __future__ import annotations

from dataclasses import dataclass
# Độ dài khung dữ liệu CAN (DLC) được hỗ trợ bảo vệ E2E (4 byte hoặc 8 byte)
E2E_DLCS = (4, 8)
# Tên tín hiệu bộ đếm sống và mã kiểm tra CRC trong file DBC
COUNTER_SIGNAL = "AliveCounter"
CRC_SIGNAL = "CRC8"
# Bộ đếm 4-bit có giá trị từ 0 đến 15 (quay vòng theo modulo 16)
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
    # Byte áp chót (dlc - 2) chứa AliveCounter; byte cuối cùng (dlc - 1) chứa mã CRC8
    return dlc - 2, dlc - 1


def data_bits(dlc: int) -> int:
    """Number of payload bits available for signals (bits 0 .. data_bits-1)."""
    # 2 byte cuối dành cho Counter và CRC, nên dữ liệu tín hiệu chỉ chiếm (dlc - 2) byte đầu
    return 8 * layout(dlc)[0]


def compute_crc(can_id: int, data: bytes) -> int:
    """CRC8 over all bytes except the CRC byte, then DataID = low byte of ``can_id``."""
    _, crc_byte = layout(len(data))
    # Ghép phần dữ liệu (từ byte 0 đến trước byte CRC) với DataID (8 bit thấp của CAN ID) để tính CRC
    # Giúp chống lỗi nhận nhầm khung tin (masquerading) giữa các CAN ID khác nhau
    return crc8_j1850(bytes(data[:crc_byte]) + bytes([can_id & 0xFF]))


def next_counter(counter: int) -> int:
    """Alive counter successor (mod 16)."""
    return (counter + 1) % COUNTER_MODULO


def protect(can_id: int, data: bytes, counter: int) -> bytes:
    """Write ``counter`` and the CRC into ``data``; returns the protected payload."""
    buf = bytearray(data)
    counter_byte, crc_byte = layout(len(buf))
    # Giữ nguyên 4 bit cao (0xF0) của byte áp chót, ghi giá trị counter (0..15) vào 4 bit thấp (low nibble)
    buf[counter_byte] = (buf[counter_byte] & 0xF0) | (counter % COUNTER_MODULO)
    # Tính mã CRC8 trên toàn bộ payload đã gắn counter + CAN ID, sau đó ghi vào byte cuối cùng
    buf[crc_byte] = compute_crc(can_id, buf)
    return bytes(buf)


@dataclass(frozen=True)
class CheckResult:
    """Outcome of an E2E check on one received frame."""

    crc_ok: bool    # True nếu mã CRC khớp (dữ liệu không bị lỗi bit trên đường truyền)
    counter: int    # Giá trị AliveCounter trích xuất được từ khung tin nhận về
    counter_ok: bool    # True nếu bộ đếm tăng đúng trình tự (không bị mất gói, lặp gói hoặc treo)

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
    # Tách lấy 4 bit thấp (0x0F) của byte áp chót để đọc giá trị AliveCounter
    counter = data[counter_byte] & 0x0F
    crc_ok = data[crc_byte] == compute_crc(can_id, data)
    if last_counter is None:
        counter_ok = True
    else:
        # Tính khoảng cách tiến của bộ đếm có xét đến trường hợp tràn số (wrap-around mod 16)
        delta = (counter - last_counter) % COUNTER_MODULO
        counter_ok = 1 <= delta <= max_delta
    return CheckResult(crc_ok=crc_ok, counter=counter, counter_ok=counter_ok)
