"""CAN <-> UDP gateway on raw sockets (no python-can here).

One ``selectors`` loop:

- CAN raw socket bound to the interface: ``CAN_RAW_FILTER`` from config, own frames received
  (``CAN_RAW_RECV_OWN_MSGS``, flagged ``FLAG_OWN_TX``), kernel receive time from
  ``SO_TIMESTAMPNS`` (CLOCK_REALTIME) via ``recvmsg``, kernel drop counter from ``SO_RXQ_OVFL``;
- uplink: DATA datagrams to ``uplink`` (batched, see :class:`~canav.gateway.protocol.Batcher`),
  STATS every ``stats_s``, HEARTBEAT when nothing was sent for ``heartbeat_s``;
- downlink UDP port: DIAG_REQ datagrams; each record whose ID is in ``downlink_ids`` is written
  to the bus, anything else is counted as rejected.

CLI: ``python -m canav.gateway --config config/online/gateway.yaml [--iface vcan0]``.
"""

from __future__ import annotations

import json
import selectors
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from canav.gateway.protocol import (
    CAN_EFF_FLAG,
    CAN_ERR_FLAG,
    CAN_RTR_FLAG,
    FLAG_OWN_TX,
    FLAG_SW_TIMESTAMP,
    MAX_DATAGRAM,
    SEQ_MOD,
    TYPE_DATA,
    TYPE_DIAG_REQ,
    TYPE_HEARTBEAT,
    TYPE_STATS,
    Batcher,
    ProtocolError,
    Record,
    Stats,
    decode,
    encode,
)
# Cấu trúc khung CAN chuẩn (Classic CAN 2.0) trong C/Linux dài đúng 16 byte:
# I (4B): can_id + cờ | B (1B): độ dài payload (DLC) | 3x (3B): padding | 8s (8B): dữ liệu
CAN_FRAME = struct.Struct("=IB3x8s")  # struct can_frame (classic CAN, 16 B)
# Hằng số tùy chọn socket của nhân Linux để lấy dấu thời gian nanosecond từ kernel và đếm gói tràn bộ đệm
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)  # Linux value if Python lacks it
SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
TIMESPEC = struct.Struct("=qq")
ANC_SIZE = socket.CMSG_SPACE(TIMESPEC.size) + socket.CMSG_SPACE(4)
# Kích thước vùng đệm dữ liệu phụ trợ (ancillary data / cmsg) chứa timestamp và bộ đếm drop
DEFAULT_CONFIG = Path("config/online/gateway.yaml")


@dataclass
class GatewayConfig:
    iface: str = "vcan0"
    uplink_host: str = "127.0.0.1"
    uplink_port: int = 48000
    downlink_host: str = "127.0.0.1"
    downlink_port: int = 48001
    batch_frames: int = 32
    flush_ms: float = 5.0
    max_datagram: int = MAX_DATAGRAM
    filters: list[tuple[int, int]] = field(default_factory=lambda: [(0, 0)])  # (id, mask)
    recv_own: bool = True
    downlink_ids: tuple[int, ...] = (0x7DF,)
    heartbeat_s: float = 1.0
    stats_s: float = 1.0

    @classmethod
    def load(cls, path: str | Path, **overrides) -> GatewayConfig:
        raw = yaml.safe_load(Path(path).read_text())
        cfg = cls(
            iface=raw["iface"],
            uplink_host=raw["uplink"]["host"],
            uplink_port=int(raw["uplink"]["port"]),
            downlink_host=raw["downlink"]["host"],
            downlink_port=int(raw["downlink"]["port"]),
            batch_frames=int(raw["batch_frames"]),
            flush_ms=float(raw["flush_ms"]),
            max_datagram=int(raw["max_datagram"]),
            filters=[(int(f["id"]), int(f["mask"])) for f in raw["filters"]],
            recv_own=bool(raw["recv_own"]),
            downlink_ids=tuple(int(i) for i in raw["downlink_ids"]),
            heartbeat_s=float(raw["heartbeat_s"]),
            stats_s=float(raw["stats_s"]),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg


def open_can(iface: str, filters: list[tuple[int, int]], recv_own: bool) -> socket.socket:
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    flt = b"".join(struct.pack("=II", can_id, mask) for can_id, mask in filters)
    s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, flt)
    s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_RECV_OWN_MSGS, int(recv_own))
    s.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    s.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
    s.bind((iface,))
    s.setblocking(False)
    return s


def parse_can(data: bytes, anc: list, msg_flags: int) -> tuple[int, bytes, int, int, int | None]:
    """``recvmsg`` result -> (can_id with flags, payload, t_rx_ns, record flags, drop counter)."""
    can_id, length, payload = CAN_FRAME.unpack(data[: CAN_FRAME.size])
    t_rx = dropped = None
    for level, kind, cdata in anc:
        if level == socket.SOL_SOCKET and kind == SO_TIMESTAMPNS:
            sec, nsec = TIMESPEC.unpack(cdata[: TIMESPEC.size])
            t_rx = sec * 1_000_000_000 + nsec
        elif level == socket.SOL_SOCKET and kind == SO_RXQ_OVFL:
            (dropped,) = struct.unpack("=I", cdata[:4])
    flags = FLAG_OWN_TX if msg_flags & socket.MSG_CONFIRM else 0
    if t_rx is None:
        t_rx, flags = time.time_ns(), flags | FLAG_SW_TIMESTAMP
    return can_id, payload[: min(length, 8)], t_rx, flags, dropped


class Gateway:
    def __init__(self, cfg: GatewayConfig, can_sock=None, can_send=None):
        """``can_sock``/``can_send`` can be injected for tests (no AF_CAN needed)."""
        self.cfg = cfg
        self.batcher = Batcher(cfg.batch_frames, round(cfg.flush_ms * 1e6), cfg.max_datagram)
        self.seq = 0
        self.up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.up_addr = (cfg.uplink_host, cfg.uplink_port)
        self.down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.down.bind((cfg.downlink_host, cfg.downlink_port))
        self.down.setblocking(False)
        self.can = can_sock
        if self.can is None and can_send is None:
            self.can = open_can(cfg.iface, cfg.filters, cfg.recv_own)
        self._can_send = can_send or self._send_raw
        self.allowed = set(cfg.downlink_ids)
        self.rx_frames = self.rx_dropped = self.datagrams = self.data_datagrams = 0
        self.diag_sent = self.rejected = self.sw_timestamps = 0
        self._last_send = self._last_stats = time.monotonic_ns()

    # ------------------------------------------------------------------ uplink
    def _send(self, type_: int, records=(), stats: Stats | None = None) -> None:
        buf = encode(type_, self.seq, time.time_ns(), records, stats)
        self.seq = (self.seq + 1) % SEQ_MOD
        self.up.sendto(buf, self.up_addr)
        self.datagrams += 1
        self.data_datagrams += type_ == TYPE_DATA
        self._last_send = time.monotonic_ns()

    def on_frame(self, can_id: int, data: bytes, t_rx_ns: int, flags: int, now_ns: int) -> None:
        self.rx_frames += 1
        self.sw_timestamps += bool(flags & FLAG_SW_TIMESTAMP)
        for batch in self.batcher.add(Record(can_id, t_rx_ns, flags, data), now_ns):
            self._send(TYPE_DATA, batch)

    def stats(self) -> Stats:
        return Stats(self.rx_frames, self.rx_dropped, self.datagrams, self.diag_sent, self.rejected)

    def tick(self, now_ns: int) -> None:
        if self.batcher.due(now_ns):
            self._send(TYPE_DATA, self.batcher.take())
        if now_ns - self._last_stats >= self.cfg.stats_s * 1e9:
            self._last_stats = now_ns
            self._send(TYPE_STATS, stats=self.stats())
        if now_ns - self._last_send >= self.cfg.heartbeat_s * 1e9:
            self._send(TYPE_HEARTBEAT)

    def next_deadline(self, now_ns: int) -> int:
        cands = [
            self._last_stats + round(self.cfg.stats_s * 1e9),
            self._last_send + round(self.cfg.heartbeat_s * 1e9),
        ]
        if (d := self.batcher.deadline()) is not None:
            cands.append(d)
        return min(cands)

    # ---------------------------------------------------------------- downlink
    def _send_raw(self, can_id: int, data: bytes) -> None:
        self.can.send(CAN_FRAME.pack(can_id, len(data), data.ljust(8, b"\0")))

    def on_downlink(self, buf: bytes) -> None:
        try:
            dg = decode(buf)
        except ProtocolError:
            self.rejected += 1
            return
        if dg.type != TYPE_DIAG_REQ:
            self.rejected += 1
            return
        for r in dg.records:
            plain = not r.can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG)
            if plain and r.can_id in self.allowed:
                try:
                    self._can_send(r.can_id, r.data)
                    self.diag_sent += 1
                except OSError:
                    self.rejected += 1
            else:
                self.rejected += 1

    # -------------------------------------------------------------------- loop
    def _drain_can(self) -> None:
        while True:
            try:
                data, anc, msg_flags, _ = self.can.recvmsg(CAN_FRAME.size, ANC_SIZE)
            except BlockingIOError:
                return
            can_id, payload, t_rx, flags, dropped = parse_can(data, anc, msg_flags)
            if dropped is not None:
                self.rx_dropped = dropped
            self.on_frame(can_id, payload, t_rx, flags, time.monotonic_ns())

    def _drain_down(self) -> None:
        while True:
            try:
                buf, _ = self.down.recvfrom(65535)
            except BlockingIOError:
                return
            self.on_downlink(buf)

    def run(self, stop: threading.Event, duration_s: float | None = None) -> dict:
        sel = selectors.DefaultSelector()
        sel.register(self.can, selectors.EVENT_READ, self._drain_can)
        sel.register(self.down, selectors.EVENT_READ, self._drain_down)
        t_end = None if duration_s is None else time.monotonic_ns() + round(duration_s * 1e9)
        try:
            while not stop.is_set():
                now = time.monotonic_ns()
                if t_end is not None and now >= t_end:
                    break
                # Ngủ chờ sự kiện đọc từ socket CAN/UDP hoặc đến hạn xử lý định kỳ (tối đa 0.1s)
                timeout = max(0.0, min((self.next_deadline(now) - now) / 1e9, 0.1))
                for key, _ in sel.select(timeout):
                    key.data()
                self.tick(time.monotonic_ns())
                # Xả nốt các khung còn tồn trong hàng đợi và gửi thống kê cuối cùng trước khi thoát
            if self.batcher.pending:
                self._send(TYPE_DATA, self.batcher.take())
            self._send(TYPE_STATS, stats=self.stats())
        finally:
            sel.close()
        return self.summary()

    def summary(self) -> dict:
        return {
            "iface": self.cfg.iface,
            "batch_frames": self.cfg.batch_frames,
            "flush_ms": self.cfg.flush_ms,
            "rx_frames": self.rx_frames,
            "rx_dropped_kernel": self.rx_dropped,
            "datagrams": self.datagrams,
            "data_datagrams": self.data_datagrams,
            "diag_sent": self.diag_sent,
            "rejected": self.rejected,
            "sw_timestamps": self.sw_timestamps,
        }

    def close(self) -> None:
        for s in (self.up, self.down, self.can):
            if s is not None:
                s.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--iface", default=None)
    p.add_argument("--batch", type=int, default=None, help="max frames per DATA datagram")
    p.add_argument("--flush-ms", type=float, default=None, help="max age of a pending frame")
    p.add_argument("--uplink-port", type=int, default=None)
    p.add_argument("--downlink-port", type=int, default=None)
    p.add_argument("--duration", type=float, default=None, help="[s] default: until SIGINT")
    p.add_argument("--stats-out", default=None, help="write the final counters (JSON) here")
    a = p.parse_args(argv)
    cfg = GatewayConfig.load(
        a.config,
        iface=a.iface,
        batch_frames=a.batch,
        flush_ms=a.flush_ms,
        uplink_port=a.uplink_port,
        downlink_port=a.downlink_port,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    gw = Gateway(cfg)
    try:
        summary = gw.run(stop, a.duration)
    finally:
        gw.close()
    if a.stats_out:
        Path(a.stats_out).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    return 0
