"""Network metrics from the sidecar (§5.9). Inputs int ns, outputs float s."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from canav.e2e import COUNTER_MODULO

NS = 1e9


@dataclass(frozen=True)
class MessageInfo:
    """Static per-message data needed to judge measurements."""

    name: str
    bus: str
    period: float | None
    deadline: float | None
    wcrt: float | None = None
    to_actuator: bool = False
    forwarded: bool = False  # gateway copy on another bus


def busy_time(starts: np.ndarray, ends: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Cumulative bus-busy time up to each ``t`` for non-overlapping intervals sorted by start."""
    starts = np.asarray(starts, dtype=np.int64)
    ends = np.asarray(ends, dtype=np.int64)
    t = np.asarray(t, dtype=np.int64)
    cum = np.concatenate(([0], np.cumsum(ends - starts)))
    i = np.searchsorted(starts, t, side="right")  # intervals started at or before t
    out = cum[i].astype(np.float64)
    started = i > 0
    overshoot = np.clip(ends[np.maximum(i - 1, 0)] - t, 0, None)
    out[started] -= overshoot[started]
    return out


def window_load(starts, ends, duration_ns: int, window_ns: int) -> np.ndarray:
    """Busy fraction of each window ``[k*w, (k+1)*w)`` covering ``[0, duration)``."""
    order = np.argsort(starts, kind="stable")
    s, e = np.asarray(starts)[order], np.asarray(ends)[order]
    edges = np.arange(0, duration_ns + window_ns, window_ns, dtype=np.int64)
    edges[-1] = min(edges[-1], duration_ns)
    edges = np.unique(edges)
    b = busy_time(s, e, edges)
    return np.diff(b) / np.diff(edges)


def counter_loss(counters) -> tuple[int, int]:
    """(expected, received) from consecutive alive counters (mod 16).

    A forward step of d means d-1 frames were lost; d = 0 (repeat) counts as no loss.
    More than 15 consecutive losses are undercounted (counter ambiguity).
    """
    c = np.asarray(counters, dtype=np.int64)
    if len(c) == 0:
        return 0, 0
    d = np.mod(np.diff(c), COUNTER_MODULO)
    lost = int(np.sum(np.where(d == 0, 0, d - 1)))
    return len(c) + lost, len(c)


def _stats(x: np.ndarray, prefix: str) -> dict[str, float | None]:
    if len(x) == 0:
        return {f"{prefix}_{k}": None for k in ("mean", "p95", "p99", "max")}
    return {
        f"{prefix}_mean": float(np.mean(x)),
        f"{prefix}_p95": float(np.percentile(x, 95)),
        f"{prefix}_p99": float(np.percentile(x, 99)),
        f"{prefix}_max": float(np.max(x)),
    }


def compute_metrics(
    frames: pd.DataFrame,
    errors: pd.DataFrame,
    info: dict[tuple[str, int], MessageInfo],
    bitrates: dict[str, int],
    duration_ns: int,
    window_s: float = 0.1,
    theory_load: dict[str, float] | None = None,
) -> dict:
    """Bus-, message- and node-level metrics as a JSON-serialisable dict.

    ``frames``: sidecar rows; ``errors``: failed attempts (``t_start, t_end, bus, sender,
    tec, state``). Frames still pending at the end of the run are absent from both.
    ``info`` is keyed by (bus, CAN ID). Message keys are ``0x010``, or ``0x300@bus`` for a
    gateway copy (same ID on the other bus).
    """
    window_ns = round(window_s * NS)
    ok = frames[frames["outcome"] == "ok"]
    out: dict = {"duration_s": duration_ns / NS, "window_s": window_s, "buses": {}, "messages": {}}

    for bus, bitrate in bitrates.items():
        f = ok[ok["bus"] == bus]
        er = errors[errors["bus"] == bus] if len(errors) else errors
        starts = f["t_tx_start"].to_numpy(np.int64)
        ends = f["t_tx_end"].to_numpy(np.int64)
        if len(er):  # aborted attempts + error frames also occupy the bus
            starts = np.concatenate([starts, er["t_start"].to_numpy(np.int64)])
            ends = np.concatenate([ends, er["t_end"].to_numpy(np.int64)])
        order = np.argsort(starts, kind="stable")
        starts, ends = starts[order], ends[order]
        wl = window_load(starts, ends, duration_ns, window_ns) if duration_ns > 0 else np.array([])
        total = float(busy_time(starts, ends, np.array([duration_ns]))[0])
        out["buses"][bus] = {
            "bitrate": bitrate,
            "load": total / duration_ns if duration_ns else None,
            "load_theory": None if theory_load is None else theory_load.get(bus),
            "load_window_mean": float(np.mean(wl)) if len(wl) else None,
            "load_window_p99": float(np.percentile(wl, 99)) if len(wl) else None,
            "load_window_max": float(np.max(wl)) if len(wl) else None,
            "frames_ok": int(len(f)),
            "error_frames": int(len(er)),
        }

    for (can_id, bus_name), g in frames.groupby(["can_id", "bus"]):
        mi = info.get((str(bus_name), int(can_id)))
        gok = g[g["outcome"] == "ok"].sort_values("t_tx_end")
        r = (gok["t_tx_end"] - gok["t_queued"]).to_numpy(np.float64) / NS
        q = (gok["t_tx_start"] - gok["t_queued"]).to_numpy(np.float64) / NS
        ia = np.diff(gok["t_tx_end"].to_numpy(np.float64)) / NS
        orig = gok["origin_ts"].dropna()
        e2e = (gok.loc[orig.index, "t_tx_end"] - orig).to_numpy(np.float64) / NS
        inst = gok["instance"].to_numpy(np.int64)
        expected, received = counter_loss(inst % COUNTER_MODULO)
        # instances missing between the first and last delivered one (exact, no counter wrap)
        lost_between = int(inst.max() - inst.min() + 1 - len(np.unique(inst))) if len(inst) else 0
        deadline = mi.deadline if mi else None
        n = len(g)
        late = int(np.sum(r > deadline)) if deadline is not None else 0
        m = {
            "name": mi.name if mi else str(g["name"].iloc[0]),
            "bus": str(g["bus"].iloc[0]),
            "period": mi.period if mi else None,
            "deadline": deadline,
            "wcrt": mi.wcrt if mi else None,
            "to_actuator": bool(mi.to_actuator) if mi else False,
            "n": int(n),
            "outcomes": {str(k): int(v) for k, v in g["outcome"].value_counts().items()},
            **_stats(r, "R"),
            "Q_mean": float(np.mean(q)) if len(q) else None,
            "Q_max": float(np.max(q)) if len(q) else None,
            "deadline_miss_rate": (n - len(gok) + late) / n if n else None,
            "interarrival_mean": float(np.mean(ia)) if len(ia) else None,
            "jitter_std": float(np.std(ia)) if len(ia) else None,
            "jitter_range": float(np.max(ia) - np.min(ia)) if len(ia) else None,
            "loss_counter": (expected - received) / expected if expected else None,
            "lost_detected": expected - received,  # seen as alive-counter gaps
            "lost_between": lost_between,
            **_stats(e2e, "e2e"),
        }
        key = f"0x{int(can_id):03X}" + (f"@{bus_name}" if mi and mi.forwarded else "")
        out["messages"][key] = m

    nodes: dict[str, dict] = {}
    if len(errors):
        for sender, g in errors.groupby("sender"):
            nodes[str(sender)] = {
                "error_frames": int(len(g)),
                "max_tec": int(g["tec"].max()),
                "busoff_count": int((g["state"] == "busoff").sum()),
            }
    out["nodes"] = nodes
    return out
