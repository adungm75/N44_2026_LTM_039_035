"""Monitor for the gateway uplink (§5.8).

Decodes CAV1 datagrams and computes: UDP loss by ``seq``, gateway latency
``t_monitor_rx - t_rx_ns`` (both CLOCK_REALTIME; ``t_monitor_rx`` = ``time.time_ns()`` right
after ``recvfrom``), bus load per window from the frame bits (worst-case and actual stuffing,
same formulas as ``canav.timing``), per-ID rate vs the DBC cycle time, unknown IDs and E2E
CRC/counter errors. Windows are keyed by the CAN receive time (``t_rx_ns``), not by datagram
arrival, so batching does not smear the load.

Outputs (``--out``): ``monitor_windows.csv``, ``monitor_ids.csv``, ``monitor_summary.json``.
Options: ``--dashboard`` (rich live table, P1), ``--diag target=8,service=0x22,did=1`` with
``--diag-period`` (sends DIAG_REQ to the gateway downlink).

CLI: ``python -m canav.monitor --out results/monitor/<ts> [--duration 30]``.
"""

from __future__ import annotations

import csv
import json
import socket
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np

from canav import e2e
from canav.gateway.protocol import (
    CAN_EFF_FLAG,
    CAN_EFF_MASK,
    CAN_ERR_FLAG,
    CAN_SFF_MASK,
    SEQ_MOD,
    TYPE_DATA,
    TYPE_DIAG_REQ,
    TYPE_STATS,
    ProtocolError,
    Record,
    decode,
    encode,
    seq_gap,
)
from canav.timing import frame_bits, frame_bits_actual

DIAG_RESPONSE_ID = 0x7E8


def _pct(a, q) -> float | None:
    return float(np.percentile(a, q)) if len(a) else None


def _mean(rows: list[dict], key: str) -> float | None:
    return float(np.mean([r[key] for r in rows])) if rows else None


class Monitor:
    def __init__(self, db, bitrate: int, window_s: float = 1.0):
        self.db = db
        self.bitrate = bitrate
        self.win_ns = round(window_s * 1e9)
        self.msgs = {m.frame_id: m for m in db.messages if not m.is_extended_frame}
        self.e2e_ids = {
            m.frame_id for m in db.messages if any(s.name == e2e.CRC_SIGNAL for s in m.signals)
        }
        self.datagrams = Counter()
        self.last_seq: int | None = None
        self.lost = self.reordered = self.bad = 0
        self.latency_ns: list[int] = []
        self.windows: dict[int, dict] = {}
        self.id_count: Counter = Counter()
        self.unknown: Counter = Counter()
        self.last_counter: dict[int, int] = {}
        self.crc_err = self.counter_err = 0
        self.own_tx = self.sw_ts = 0
        self.t_first: int | None = None
        self.t_last: int | None = None
        self.gw_stats = None
        self.diag_responses: list[dict] = []
        self.diag_sent = 0
        self._bits_cache: dict[tuple, int] = {}

    # --------------------------------------------------------------- decoding
    def process(self, buf: bytes, t_mon_ns: int) -> None:
        try:
            dg = decode(buf)
        except ProtocolError:
            self.bad += 1
            return
        self.datagrams[dg.type] += 1
        if self.last_seq is not None:
            gap = seq_gap(self.last_seq, dg.seq)
            if gap >= SEQ_MOD // 2:  # older than the last one: reordered or duplicate
                self.reordered += 1
                return
            self.lost += gap
        self.last_seq = dg.seq
        if dg.type == TYPE_DATA:
            for r in dg.records:
                self._frame(r, t_mon_ns)
        elif dg.type == TYPE_STATS:
            self.gw_stats = dg.stats

    def _frame(self, r: Record, t_mon_ns: int) -> None:
        if r.can_id & CAN_ERR_FLAG:
            return
        ext = bool(r.can_id & CAN_EFF_FLAG)
        cid = r.can_id & (CAN_EFF_MASK if ext else CAN_SFF_MASK)
        lat = t_mon_ns - r.t_rx_ns
        self.latency_ns.append(lat)
        self.own_tx += bool(r.flags & 0x01)
        self.sw_ts += bool(r.flags & 0x02)
        self.t_first = r.t_rx_ns if self.t_first is None else min(self.t_first, r.t_rx_ns)
        self.t_last = r.t_rx_ns if self.t_last is None else max(self.t_last, r.t_rx_ns)
        key = r.t_rx_ns // self.win_ns
        w = self.windows.get(key)
        if w is None:
            w = self.windows[key] = {
                "frames": 0,
                "bits_worst": 0,
                "bits_actual": 0,
                "unknown": 0,
                "crc_err": 0,
                "counter_err": 0,
                "lat": [],
                "ids": Counter(),
            }
        w["frames"] += 1
        w["bits_worst"] += frame_bits(len(r.data), "worst", ext)
        ck = (cid, r.data, ext)
        bits = self._bits_cache.get(ck)
        if bits is None:
            bits = frame_bits_actual(cid, r.data, ext)
            if len(self._bits_cache) < 100_000:
                self._bits_cache[ck] = bits
        w["bits_actual"] += bits
        w["lat"].append(lat)
        w["ids"][cid] += 1
        self.id_count[cid] += 1
        msg = None if ext else self.msgs.get(cid)
        if msg is None:
            self.unknown[cid] += 1
            w["unknown"] += 1
            return
        if cid in self.e2e_ids and len(r.data) == msg.length:
            res = e2e.check(cid, r.data, self.last_counter.get(cid))
            if not res.crc_ok:
                self.crc_err += 1
                w["crc_err"] += 1
            else:
                if not res.counter_ok:
                    self.counter_err += 1
                    w["counter_err"] += 1
                self.last_counter[cid] = res.counter
        if cid == DIAG_RESPONSE_ID:
            vals = self.db.decode_message(cid, r.data)
            self.diag_responses.append({"t_rx_ns": r.t_rx_ns, **vals})

    # ------------------------------------------------------------------- diag
    def diag_datagram(self, seq: int, target: int, service: int, did: int) -> bytes:
        msg = self.db.get_message_by_name("Diag_Request")
        data = msg.encode({"target": target, "service": service, "did": did})
        now = time.time_ns()
        return encode(TYPE_DIAG_REQ, seq, now, [Record(msg.frame_id, now, 0, data)])

    # ---------------------------------------------------------------- results
    def window_rows(self) -> list[dict]:
        rows = []
        for key in sorted(self.windows):
            w = self.windows[key]
            lat = np.asarray(w["lat"], dtype=np.float64)
            win_s = self.win_ns / 1e9
            rows.append(
                {
                    "t_start_s": key * win_s,
                    "frames": w["frames"],
                    "frames_per_s": w["frames"] / win_s,
                    "bits_worst_per_s": w["bits_worst"] / win_s,
                    "bits_actual_per_s": w["bits_actual"] / win_s,
                    "load_worst": w["bits_worst"] / win_s / self.bitrate,
                    "load_actual": w["bits_actual"] / win_s / self.bitrate,
                    "unknown": w["unknown"],
                    "crc_err": w["crc_err"],
                    "counter_err": w["counter_err"],
                    "lat_p50_us": _pct(lat, 50) / 1e3,
                    "lat_p99_us": _pct(lat, 99) / 1e3,
                }
            )
        return rows

    def full_windows(self) -> list[dict]:
        """Windows completely inside the observation (first and last are partial)."""
        rows = self.window_rows()
        return rows[1:-1]

    def id_rows(self) -> list[dict]:
        dur = self.duration_s()
        rows = []
        for cid in sorted(self.id_count):
            m = self.msgs.get(cid)
            period = m.cycle_time / 1000 if m is not None and m.cycle_time else None
            rows.append(
                {
                    "can_id": f"0x{cid:03X}",
                    "name": m.name if m is not None else "UNKNOWN",
                    "frames": self.id_count[cid],
                    "rate_hz": self.id_count[cid] / dur if dur else None,
                    "expected_hz": 1 / period if period else None,
                }
            )
        return rows

    def duration_s(self) -> float:
        if self.t_first is None or self.t_last is None:
            return 0.0
        return (self.t_last - self.t_first) / 1e9

    def summary(self) -> dict:
        lat = np.asarray(self.latency_ns, dtype=np.float64) / 1e3
        full = self.full_windows()
        frames = sum(self.id_count.values())
        received = sum(self.datagrams.values())
        return {
            "frames": frames,
            "duration_s": self.duration_s(),
            "datagrams": {str(k): v for k, v in sorted(self.datagrams.items())},
            "udp_lost": self.lost,
            "udp_loss_rate": self.lost / (received + self.lost) if received else None,
            "udp_reordered": self.reordered,
            "bad_datagrams": self.bad,
            "latency_us": {
                "n": int(len(lat)),
                "mean": float(lat.mean()) if len(lat) else None,
                "p50": _pct(lat, 50),
                "p99": _pct(lat, 99),
                "max": float(lat.max()) if len(lat) else None,
            },
            "load_worst_mean": _mean(full, "load_worst"),
            "load_actual_mean": _mean(full, "load_actual"),
            "frames_per_s_mean": _mean(full, "frames_per_s"),
            "unknown_ids": {f"0x{k:X}": v for k, v in sorted(self.unknown.items())},
            "crc_err": self.crc_err,
            "counter_err": self.counter_err,
            "own_tx_frames": self.own_tx,
            "sw_timestamp_frames": self.sw_ts,
            "diag_sent": self.diag_sent,
            "diag_responses": len(self.diag_responses),
            "last_diag_response": self.diag_responses[-1] if self.diag_responses else None,
            "gateway_stats": None if self.gw_stats is None else asdict(self.gw_stats),
        }

    def write(self, out: str | Path) -> dict:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        tables = {"monitor_windows.csv": self.window_rows(), "monitor_ids.csv": self.id_rows()}
        for name, rows in tables.items():
            if rows:
                with open(out / name, "w", newline="") as f:
                    wr = csv.DictWriter(f, fieldnames=list(rows[0]))
                    wr.writeheader()
                    wr.writerows(rows)
        s = self.summary()
        (out / "monitor_summary.json").write_text(json.dumps(s, indent=2, default=str))
        return s


def _dashboard_table(mon: Monitor):
    """Only the last complete window is summarised: redrawing must stay cheap, otherwise it
    delays the receive loop and inflates the measured latency."""
    from rich.table import Table

    keys = sorted(mon.windows)
    key = keys[-2] if len(keys) >= 2 else (keys[-1] if keys else None)
    w = mon.windows.get(key) if key is not None else None
    t = Table(title="canav monitor (last complete 1 s window)")
    for col in ("ID", "name", "rate [Hz]", "expected [Hz]"):
        t.add_column(col)
    ids = w["ids"] if w else Counter()
    for cid in sorted(ids):
        m = mon.msgs.get(cid)
        exp = f"{1000 / m.cycle_time:.0f}" if m is not None and m.cycle_time else "-"
        t.add_row(f"0x{cid:03X}", m.name if m else "UNKNOWN", str(ids[cid]), exp)
    load = w["bits_worst"] / (mon.win_ns / 1e9) / mon.bitrate * 100 if w else 0.0
    lat = np.asarray(w["lat"] if w else [], dtype=np.float64) / 1e3
    t.caption = (
        f"load {load:.2f} % (worst) | lat p50 {_pct(lat, 50) or 0:.0f} us "
        f"p99 {_pct(lat, 99) or 0:.0f} us | UDP lost {mon.lost} | "
        f"unknown {sum(mon.unknown.values())} | crc {mon.crc_err} counter {mon.counter_err} | "
        f"diag resp {len(mon.diag_responses)}"
    )
    return t


def run_monitor(
    mon: Monitor,
    stop: threading.Event,
    *,
    host: str = "127.0.0.1",
    port: int = 48000,
    duration_s: float | None = None,
    diag: dict | None = None,
    diag_period_s: float = 1.0,
    gateway_downlink: tuple[str, int] = ("127.0.0.1", 48001),
    dashboard: bool = False,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind((host, port))
    sock.settimeout(0.05)
    down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t_end = None if duration_s is None else time.monotonic() + duration_s
    next_diag = time.monotonic()
    live = None
    if dashboard:
        from rich.live import Live

        live = Live(_dashboard_table(mon), refresh_per_second=1)
        live.start()
    next_draw = time.monotonic() + 1.0
    try:
        while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
            try:
                buf = sock.recv(65535)
                mon.process(buf, time.time_ns())
            except TimeoutError:
                pass
            now = time.monotonic()
            if diag is not None and now >= next_diag:
                down.sendto(mon.diag_datagram(mon.diag_sent, **diag), gateway_downlink)
                mon.diag_sent += 1
                next_diag = now + diag_period_s
            if live is not None and now >= next_draw:
                live.update(_dashboard_table(mon))
                next_draw = now + 1.0
    finally:
        if live is not None:
            live.stop()
        sock.close()
        down.close()


def _parse_diag(spec: str) -> dict:
    out = {}
    for item in spec.split(","):
        k, _, v = item.partition("=")
        out[k.strip()] = int(v, 0)
    if set(out) != {"target", "service", "did"}:
        raise ValueError("--diag needs target=..,service=..,did=..")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal

    from canav.ecu import load_dbc
    from canav.netdef import DEFAULT_MATRIX, load_matrix

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", required=True)
    p.add_argument("--dbc", default="config/dbc/canav.dbc")
    p.add_argument("--port", type=int, default=48000)
    p.add_argument("--gateway-downlink-port", type=int, default=48001)
    p.add_argument("--duration", type=float, default=None, help="[s] default: until SIGINT")
    p.add_argument("--dashboard", action="store_true")
    p.add_argument("--diag", default=None, metavar="target=8,service=0x22,did=1")
    p.add_argument("--diag-period", type=float, default=1.0)
    a = p.parse_args(argv)
    bitrate = next(iter(load_matrix(DEFAULT_MATRIX).buses.values()))
    mon = Monitor(load_dbc(a.dbc), bitrate)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run_monitor(
        mon,
        stop,
        port=a.port,
        duration_s=a.duration,
        diag=_parse_diag(a.diag) if a.diag else None,
        diag_period_s=a.diag_period,
        gateway_downlink=("127.0.0.1", a.gateway_downlink_port),
        dashboard=a.dashboard,
    )
    s = mon.write(a.out)
    print(json.dumps({k: s[k] for k in ("frames", "udp_lost", "latency_us", "load_worst_mean")}))
    return 0
