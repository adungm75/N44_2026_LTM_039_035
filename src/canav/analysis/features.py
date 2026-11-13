"""Window features for anomaly detection (§5.11).

Input: a ``candump -L`` log (both tiers, D6) plus, for SimPy runs only, ``errors.jsonl``
(error frames) and ``frames.jsonl`` (queuing delays). Windows of ``window_s`` (100 ms), step =
window, aligned to ``t_start``; only complete windows inside ``[t_start, t_end)`` are kept.

Per window:

- ``load`` (worst-case frame bits / window / bitrate), ``frames``, ``unknown`` (IDs not in the
  DBC), ``counter_err`` / ``crc_err`` (E2E check per ID, like a receiver that sees every
  frame), ``error_frames`` (sim), ``qd_high|mid|low`` max queuing delay per priority class (sim;
  classes chosen: ID < 0x100, 0x100-0x3FF, >= 0x400);
- per periodic DBC ID ``X``: ``n_X`` (frames), ``ia_mean_X``/``ia_std_X``/``ia_max_X`` (same-ID
  inter-arrival times ending in the window), ``gap_X`` = max(``ia_max_X``, time since the last
  frame at the window end) so that a silent ID is visible even without frames;
- aggregates: ``gap_ratio_max`` = max_X gap_X / T_X, ``count_ratio_min|max`` = n_X / (window/T_X).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from canav import e2e
from canav.analysis.candump import read_log
from canav.timing import frame_bits

WINDOW_S = 0.1
QD_CLASSES = (("qd_high", 0x000, 0x100), ("qd_mid", 0x100, 0x400), ("qd_low", 0x400, 0x800))
SCALAR_FEATURES = (
    "load",
    "frames",
    "unknown",
    "counter_err",
    "crc_err",
    "error_frames",
    "qd_high",
    "qd_mid",
    "qd_low",
    "gap_ratio_max",
    "count_ratio_min",
    "count_ratio_max",
)


def periodic_ids(db) -> dict[int, float]:
    """Standard-ID periodic messages of the DBC -> period [s]."""
    return {
        m.frame_id: m.cycle_time / 1000
        for m in db.messages
        if m.cycle_time and not m.is_extended_frame
    }


def load_records(path: str | Path) -> pd.DataFrame:
    recs = [r for r in read_log(path) if not (r.error or r.rtr or r.fd)]
    return pd.DataFrame(
        {
            "t_ns": np.array([r.t_ns for r in recs], dtype=np.int64),
            "can_id": np.array([r.can_id for r in recs], dtype=np.int64),
            "ext": np.array([r.extended for r in recs], dtype=bool),
            "data": [r.data for r in recs],
        }
    )


def window_features(
    recs: pd.DataFrame,
    db,
    bitrate: int,
    t_start: int,
    t_end: int,
    window_s: float = WINDOW_S,
    errors: pd.DataFrame | None = None,
    queue: pd.DataFrame | None = None,
) -> pd.DataFrame:
    w = round(window_s * 1e9)
    n_win = (t_end - t_start) // w
    starts = t_start + np.arange(n_win, dtype=np.int64) * w
    ends = starts + w
    out = pd.DataFrame({"t_start_ns": starts, "t_end_ns": ends})
    t = recs["t_ns"].to_numpy(np.int64)
    ids = recs["can_id"].to_numpy(np.int64)
    ext = recs["ext"].to_numpy(bool)
    win = (t - t_start) // w
    inside = (t >= t_start) & (win < n_win)

    def per_window(mask, values=None) -> np.ndarray:
        sel = inside & mask
        v = np.ones(sel.sum()) if values is None else np.asarray(values)[sel]
        return np.bincount(win[sel], weights=v, minlength=n_win)[:n_win]

    dlc = np.fromiter((len(d) for d in recs["data"]), dtype=np.int64, count=len(recs))
    bits = np.array([frame_bits(int(n), "worst", bool(e)) for n, e in zip(dlc, ext, strict=True)])
    out["load"] = per_window(np.ones(len(t), bool), bits) / (window_s * bitrate)
    out["frames"] = per_window(np.ones(len(t), bool))
    known = {m.frame_id for m in db.messages if not m.is_extended_frame}
    out["unknown"] = per_window(ext | ~np.isin(ids, list(known)))

    # E2E: a receiver that sees every frame (counter state per ID, CRC-failed frames skipped)
    e2e_ids = {
        m.frame_id: m.length
        for m in db.messages
        if any(s.name == e2e.CRC_SIGNAL for s in m.signals)
    }
    cnt_err = np.zeros(len(t), bool)
    crc_err = np.zeros(len(t), bool)
    last: dict[int, int] = {}
    data = recs["data"].tolist()
    for i in np.flatnonzero(np.isin(ids, list(e2e_ids)) & ~ext):
        cid = int(ids[i])
        if len(data[i]) != e2e_ids[cid]:
            continue
        res = e2e.check(cid, data[i], last.get(cid))
        if not res.crc_ok:
            crc_err[i] = True
            continue
        cnt_err[i] = not res.counter_ok
        last[cid] = res.counter
    out["counter_err"] = per_window(cnt_err)
    out["crc_err"] = per_window(crc_err)

    out["error_frames"] = 0.0
    if errors is not None and len(errors):
        te = errors["t_start"].to_numpy(np.int64)
        we = (te - t_start) // w
        ok = (te >= t_start) & (we < n_win)
        out["error_frames"] = np.bincount(we[ok], minlength=n_win)[:n_win].astype(float)
    for col, lo, hi in QD_CLASSES:
        out[col] = np.nan
        if queue is not None and len(queue):
            q = queue[(queue["can_id"] >= lo) & (queue["can_id"] < hi)]
            wq = (q["t_tx_end"].to_numpy(np.int64) - t_start) // w
            ok = (wq >= 0) & (wq < n_win)
            s = pd.Series(q["qd"].to_numpy(float)[ok]).groupby(wq[ok]).max()
            out[col] = 0.0
            out.loc[s.index, col] = s.to_numpy() / 1e9

    periods = periodic_ids(db)
    gap_ratio = np.zeros(n_win)
    count_ratio = []
    cols = {}
    for cid, period in sorted(periods.items()):
        sel = (ids == cid) & ~ext
        tt = t[sel]
        n = per_window(sel)
        cols[f"n_{cid:03X}"] = n
        gaps = np.diff(tt)
        gw = (tt[1:] - t_start) // w
        ok = (tt[1:] >= t_start) & (gw < n_win)
        g = pd.Series(gaps[ok] / 1e9).groupby(gw[ok])
        for name, agg in (("ia_mean", g.mean()), ("ia_std", g.std(ddof=0)), ("ia_max", g.max())):
            col = np.full(n_win, np.nan)
            col[agg.index.to_numpy(int)] = agg.to_numpy()
            cols[f"{name}_{cid:03X}"] = col
        k = np.searchsorted(tt, ends, side="left") - 1  # last frame before each window end
        open_gap = np.where(k >= 0, (ends - tt[np.maximum(k, 0)]) / 1e9, np.nan)
        gap = np.fmax(cols[f"ia_max_{cid:03X}"], open_gap)
        cols[f"gap_{cid:03X}"] = gap
        gap_ratio = np.fmax(gap_ratio, gap / period)
        count_ratio.append(n / (window_s / period))
    out = pd.concat([out, pd.DataFrame(cols)], axis=1)
    out["gap_ratio_max"] = gap_ratio
    out["count_ratio_min"] = np.min(count_ratio, axis=0)
    out["count_ratio_max"] = np.max(count_ratio, axis=0)
    return out


def sim_run_features(run_dir: Path, db, bitrate: int, warmup_s: float) -> pd.DataFrame:
    """Windows of one SimPy run (needs ``can.log``; uses errors/frames.jsonl if present)."""
    m = json.loads((run_dir / "metrics.json").read_text())
    recs = load_records(run_dir / "can.log")
    errors = queue = None
    if (run_dir / "errors.jsonl").exists() and (run_dir / "errors.jsonl").stat().st_size:
        errors = pd.read_json(run_dir / "errors.jsonl", lines=True)
    if (run_dir / "frames.jsonl").exists():
        fr = pd.read_json(run_dir / "frames.jsonl", lines=True)
        fr = fr[fr["outcome"] == "ok"]
        queue = pd.DataFrame(
            {
                "can_id": fr["can_id"].to_numpy(np.int64),
                "t_tx_end": fr["t_tx_end"].to_numpy(np.int64),
                "qd": (fr["t_tx_start"] - fr["t_queued"]).to_numpy(np.int64),
            }
        )
    t_end = round(m["duration_s"] * 1e9)
    return window_features(
        recs, db, bitrate, round(warmup_s * 1e9), t_end, errors=errors, queue=queue
    )
