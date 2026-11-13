"""Metrics of an online run from the ECU event logs, ``can.log`` and the bridge trace (§5.9).

- hop latency = rx log time - tx log time of the same (msg, counter) (latest tx before the rx)
- jitter = std and max-min of same-message intervals at the sender (tx) and receiver (rx)
- loss = counter gaps per (receiver, msg); crc/counter errors from the logs
- e2e = actuator ``use`` time - freshest sensor origin, following ``src`` chains backwards
- bus load from ``can.log`` (worst-case and actual stuffing), run and 100 ms windows
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path

import numpy as np
import pandas as pd

from canav.analysis.candump import read_log
from canav.analysis.metrics import counter_loss, window_load
from canav.ecu.nodes import ACTUATORS
from canav.timing import frame_bits, frame_bits_actual

NS = 1e9


def load_events(ecu_dir: str | Path) -> pd.DataFrame:
    rows = []
    for path in sorted(Path(ecu_dir).glob("*.jsonl")):
        with open(path) as f:
            rows.extend(json.loads(line) for line in f if line.strip())
    df = pd.DataFrame(rows, columns=["t_ns", "ecu", "event", "msg", "counter", "src"])
    return df.sort_values("t_ns", kind="stable").reset_index(drop=True)


def _stats(x: np.ndarray, prefix: str) -> dict:
    if len(x) == 0:
        return {f"{prefix}_{k}": None for k in ("mean", "p50", "p99", "max")}
    return {
        f"{prefix}_mean": float(np.mean(x)),
        f"{prefix}_p50": float(np.percentile(x, 50)),
        f"{prefix}_p99": float(np.percentile(x, 99)),
        f"{prefix}_max": float(np.max(x)),
    }


def hop_latency(ev: pd.DataFrame) -> pd.DataFrame:
    """One row per rx event matched to its tx: ``msg, receiver, latency_s``."""
    tx = ev[ev["event"] == "tx"][["t_ns", "msg", "counter"]].rename(columns={"t_ns": "t_tx"})
    rx = ev[ev["event"] == "rx"][["t_ns", "msg", "counter", "ecu"]]
    if tx.empty or rx.empty:
        return pd.DataFrame(columns=["msg", "receiver", "latency_s"])
    tx = tx.assign(counter=tx["counter"].fillna(-1).astype(np.int64)).sort_values("t_tx")
    rx = rx.assign(counter=rx["counter"].fillna(-1).astype(np.int64)).sort_values("t_ns")
    m = pd.merge_asof(
        rx, tx, left_on="t_ns", right_on="t_tx", by=["msg", "counter"], direction="backward"
    )
    m = m.dropna(subset=["t_tx"])
    return pd.DataFrame(
        {"msg": m["msg"], "receiver": m["ecu"], "latency_s": (m["t_ns"] - m["t_tx"]) / NS}
    )


def _interval_stats(t_ns: np.ndarray, period: float | None) -> dict:
    d = np.diff(np.sort(t_ns)) / NS
    if len(d) == 0:
        return dict.fromkeys(
            ("interval_mean", "jitter_std", "jitter_range", "interval_p1", "interval_p99")
        )
    out = {
        "interval_mean": float(np.mean(d)),
        "jitter_std": float(np.std(d)),
        "jitter_range": float(np.max(d) - np.min(d)),
        "interval_p1": float(np.percentile(d, 1)),
        "interval_p99": float(np.percentile(d, 99)),
    }
    if period:
        out["interval_max_over_T"] = float(np.max(d) / period)
    return out


class OriginResolver:
    """origin(msg, counter, before) = tx time of a source message, or the freshest origin of
    the inputs listed in the consumer's ``src`` (recursively)."""

    def __init__(self, ev: pd.DataFrame):
        tx = ev[ev["event"] == "tx"]
        self.index: dict[tuple[str, int], tuple[list[int], list]] = {}
        for (msg, counter), g in tx.groupby(["msg", "counter"]):
            self.index[(msg, int(counter))] = (g["t_ns"].tolist(), g["src"].tolist())
        self.memo: dict[tuple[str, int, int], int | None] = {}

    def origin(self, msg: str, counter: int | None, before: int, depth: int = 0) -> int | None:
        if counter is None or depth > 8:
            return None
        entry = self.index.get((msg, int(counter)))
        if entry is None:
            return None
        times, srcs = entry
        i = bisect.bisect_right(times, before) - 1
        if i < 0:
            return None
        key = (msg, int(counter), i)
        if key not in self.memo:
            t_tx, src = times[i], srcs[i]
            if not isinstance(src, dict) or not src:
                self.memo[key] = t_tx
            else:
                cands = [self.origin(m, c, t_tx, depth + 1) for m, c in src.items()]
                cands = [c for c in cands if c is not None]
                self.memo[key] = max(cands) if cands else None
        return self.memo[key]

    def e2e(self, uses: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for t, ecu, src in zip(uses["t_ns"], uses["ecu"], uses["src"], strict=True):
            if not isinstance(src, dict) or not src:
                continue
            cands = [self.origin(m, c, t) for m, c in src.items()]
            cands = [c for c in cands if c is not None]
            if cands:
                rows.append(
                    {"ecu": ecu, "src": "+".join(sorted(src)), "e2e_s": (t - max(cands)) / NS}
                )
        return pd.DataFrame(rows, columns=["ecu", "src", "e2e_s"])


def bus_load(can_log: Path, bitrate: int, window_s: float = 0.1) -> dict:
    recs = list(read_log(can_log)) if can_log.exists() else []
    if len(recs) < 2:
        return {"frames": len(recs)}
    t = np.array([r.t_ns for r in recs], dtype=np.int64)
    worst = np.array([frame_bits(len(r.data), "worst", r.extended) for r in recs])
    cache: dict = {}
    actual = np.array(
        [
            cache.setdefault(
                (r.can_id, r.data, r.extended), frame_bits_actual(r.can_id, r.data, r.extended)
            )
            for r in recs
        ]
    )
    t0 = int(t.min())
    duration = int(t.max() - t0)
    rel = t - t0
    ends = rel
    starts = rel - np.round(actual * NS / bitrate).astype(np.int64)
    wl = window_load(starts, ends, duration, round(window_s * NS))
    return {
        "frames": len(recs),
        "duration_s": duration / NS,
        "load_worst": float(worst.sum() / bitrate / (duration / NS)),
        "load_actual": float(actual.sum() / bitrate / (duration / NS)),
        "load_actual_window_max": float(np.max(wl)) if len(wl) else None,
        "frames_per_s": len(recs) / (duration / NS),
    }


def aeb_stop(ev: pd.DataFrame, trace_path: Path) -> dict | None:
    """Speed and distance from the first AEB override at the actuator to standstill."""
    uses = ev[(ev["event"] == "use") & (ev["ecu"] == "ThrottleBrakeActuator")]
    first = next(
        (
            t
            for t, src in zip(uses["t_ns"], uses["src"], strict=True)
            if isinstance(src, dict) and "AEB_BrakeRequest" in src
        ),
        None,
    )
    if first is None or not trace_path.exists():
        return None
    tr = pd.read_json(trace_path, lines=True)
    after = tr[tr["t_ns"] >= first]
    if after.empty:
        return None
    s0, v0 = float(after["s"].iloc[0]), float(after["v"].iloc[0])
    stopped = after[after["v"] < 0.05]
    if stopped.empty:
        return {"t_ns": int(first), "v_at_activation": v0, "stopping_distance_m": None}
    return {
        "t_ns": int(first),
        "v_at_activation": v0,
        "stopping_distance_m": float(stopped["s"].iloc[0]) - s0,
        "stopping_time_s": (int(stopped["t_ns"].iloc[0]) - int(first)) / NS,
    }


def compute_online_metrics(out_dir: str | Path, db, bitrate: int) -> dict:
    out = Path(out_dir)
    ev = load_events(out / "ecu")
    periods = {m.name: m.cycle_time / 1000 for m in db.messages if m.cycle_time}
    counts = {
        ecu: {k: int(v) for k, v in g["event"].value_counts().items()}
        for ecu, g in ev.groupby("ecu")
    }
    hops = hop_latency(ev)
    messages: dict[str, dict] = {}
    for name in sorted(set(ev.loc[ev["event"] == "tx", "msg"])):
        tx_t = ev.loc[(ev["event"] == "tx") & (ev["msg"] == name), "t_ns"].to_numpy()
        rx = ev[(ev["event"] == "rx") & (ev["msg"] == name)]
        h = hops.loc[hops["msg"] == name, "latency_s"].to_numpy(np.float64)
        entry = {"period": periods.get(name), "tx": int(len(tx_t)), "rx": int(len(rx))}
        entry |= {f"tx_{k}": v for k, v in _interval_stats(tx_t, periods.get(name)).items()}
        if len(rx):
            first_rx = sorted(rx["ecu"].unique())[0]
            rx_t = rx.loc[rx["ecu"] == first_rx, "t_ns"].to_numpy()
            entry["rx_ecu"] = first_rx
            entry |= {f"rx_{k}": v for k, v in _interval_stats(rx_t, periods.get(name)).items()}
            loss = []
            for _, g in rx.groupby("ecu"):
                if g["counter"].notna().all():
                    exp, rec = counter_loss(g["counter"].astype(np.int64).to_numpy())
                    loss.append((exp - rec) / exp if exp else 0.0)
            entry["loss_counter_max"] = max(loss) if loss else None
        entry |= _stats(h, "hop")
        messages[name] = entry

    uses = ev[(ev["event"] == "use") & ev["ecu"].isin(ACTUATORS)]
    e2e = OriginResolver(ev).e2e(uses)
    e2e_summary = {
        f"{ecu}:{src}": {"n": int(len(g))} | _stats(g["e2e_s"].to_numpy(np.float64), "e2e")
        for (ecu, src), g in e2e.groupby(["ecu", "src"])
    }
    ecu_stats = {}
    for p in sorted((out / "ecu").glob("*.stats.json")):
        ecu_stats[p.name.removesuffix(".stats.json")] = json.loads(p.read_text())
    driving = (
        json.loads((out / "driving.json").read_text()) if (out / "driving.json").exists() else {}
    )
    all_hops = hops["latency_s"].to_numpy(np.float64)
    return {
        "bus": bus_load(out / "can.log", bitrate),
        "bitrate": bitrate,
        "errors": {
            "crc_err": int((ev["event"] == "crc_err").sum()),
            "counter_err": int((ev["event"] == "counter_err").sum()),
            "drop": int((ev["event"] == "drop").sum()),
        },
        "hop_latency": _stats(all_hops, "hop") | {"n": int(len(all_hops))},
        "messages": messages,
        "e2e": e2e_summary,
        "events": counts,
        "ecu_stats": ecu_stats,
        "driving": driving,
        "aeb": aeb_stop(ev, out / "bridge.jsonl"),
        "logs": {
            "ecu_logs": sorted(p.name for p in (out / "ecu").glob("*.jsonl")),
            "can_log": (out / "can.log").exists(),
            "bridge_trace": (out / "bridge.jsonl").exists(),
        },
    }
