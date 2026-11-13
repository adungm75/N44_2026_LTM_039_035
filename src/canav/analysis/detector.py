"""Threshold/statistical detector (§5.11) and the E9 evaluation.

Rules per 100 ms window (μ, σ from the normal training windows):

- R1 ``load > μ + k·max(σ, σ_min)``, σ_min = load of half the smallest frame (4 B, worst-case
  stuffing) in one window (chosen, same reasoning as the count floor of R2);
- R2 any periodic ID with ``|n_X - μ_X| > k·max(σ_X, 0.5)`` (σ floor 0.5 frame, chosen:
  counts are integers and a window boundary shifts a count by ±1);
- R3 any periodic ID with ``gap_X > 2.5·T_X``;
- R4 ``unknown > 0``;
- R5 ``counter_err + crc_err > 0``.

A window is anomalous if it overlaps an anomaly interval. E9 (sim): train on E0 seeds 1-3,
test on E0 seeds 4-5 and E8; online: train on ``results/online_exp/E0``, test on
``results/online_exp/E8_*``. Outputs ``report/tables/detector_eval.csv``,
``report/tables/feature_sensitivity.csv`` and ``results/E9/eval.json``.

CLI: ``python -m canav.analysis.detector``
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from canav.analysis.features import (
    SCALAR_FEATURES,
    load_records,
    periodic_ids,
    sim_run_features,
    window_features,
)
from canav.timing import frame_bits

KS = (2, 3, 4, 5)
COUNT_SIGMA_FLOOR = 0.5
GAP_FACTOR = 2.5
RULES = ("R1", "R2", "R3", "R4", "R5")
SIM_WARMUP_S = 1.0  # chosen: random offsets need up to one longest period to settle
ONLINE_WARMUP_S = 3.0  # chosen: ECU processes start within the first seconds


@dataclass
class Detector:
    periods: dict[int, float]
    bitrate: int
    window_s: float = 0.1
    load_mu: float = 0.0
    load_sigma: float = 0.0
    count_mu: dict[int, float] | None = None
    count_sigma: dict[int, float] | None = None

    def fit(self, train: pd.DataFrame) -> Detector:
        self.load_mu = float(train["load"].mean())
        floor = 0.5 * frame_bits(4, "worst") / (self.window_s * self.bitrate)
        self.load_sigma = max(float(train["load"].std(ddof=0)), floor)
        self.count_mu = {c: float(train[f"n_{c:03X}"].mean()) for c in self.periods}
        self.count_sigma = {
            c: max(float(train[f"n_{c:03X}"].std(ddof=0)), COUNT_SIGMA_FLOOR) for c in self.periods
        }
        return self

    def rules(self, df: pd.DataFrame, k: float) -> pd.DataFrame:
        r = pd.DataFrame(index=df.index)
        r["R1"] = df["load"] > self.load_mu + k * self.load_sigma
        dev = np.zeros(len(df), bool)
        gap = np.zeros(len(df), bool)
        for c, period in self.periods.items():
            dev |= (df[f"n_{c:03X}"] - self.count_mu[c]).abs().to_numpy() > k * self.count_sigma[c]
            gap |= (df[f"gap_{c:03X}"] > GAP_FACTOR * period).to_numpy()
        r["R2"] = dev
        r["R3"] = gap
        r["R4"] = df["unknown"] > 0
        r["R5"] = (df["counter_err"] + df["crc_err"]) > 0
        r["alarm"] = r[list(RULES)].any(axis=1)
        return r


def label_windows(df: pd.DataFrame, intervals: list[dict]) -> pd.Series:
    """Anomaly type of each window (``normal`` if it overlaps no interval)."""
    lab = pd.Series("normal", index=df.index, dtype=object)
    for iv in intervals:
        hit = (df["t_start_ns"] < iv["end_ns"]) & (df["t_end_ns"] > iv["start_ns"])
        lab[hit] = iv["type"]
    return lab


def evaluate(det: Detector, runs: list[tuple[pd.DataFrame, list[dict]]], ks=KS) -> dict:
    rows, delays = [], []
    for k in ks:
        alarms, labels, per_occ = [], [], []
        for df, intervals in runs:
            r = det.rules(df, k)
            lab = label_windows(df, intervals)
            alarms.append(r.assign(label=lab.to_numpy(), clean=not intervals))
            for iv in intervals:
                hit = (df["t_start_ns"] < iv["end_ns"]) & (df["t_end_ns"] > iv["start_ns"])
                first = df.loc[hit & r["alarm"], "t_end_ns"]
                per_occ.append(
                    {
                        "k": k,
                        "type": iv["type"],
                        "delay_s": None
                        if first.empty
                        else (int(first.iloc[0]) - iv["start_ns"]) / 1e9,
                    }
                )
            labels.append(lab)
        a = pd.concat(alarms, ignore_index=True)
        normal = a[a["label"] == "normal"]
        fpr = float(normal["alarm"].mean()) if len(normal) else None
        clean = a[a["clean"]]  # runs without any anomaly (E0 test seeds / online E0)
        fpr_clean = float(clean["alarm"].mean()) if len(clean) else None
        fp_rules = {rr: float(normal[rr].mean()) for rr in RULES} if len(normal) else {}
        anom = a[a["label"] != "normal"]
        rows.append(
            {
                "k": k,
                "anomaly": "all",
                "windows": int(len(anom)),
                "tpr": float(anom["alarm"].mean()) if len(anom) else None,
                "normal_windows": int(len(normal)),
                "fpr": fpr,
                "fpr_clean_runs": fpr_clean,
                **{f"fpr_{rr}": v for rr, v in fp_rules.items()},
            }
        )
        occ = pd.DataFrame(per_occ)
        for typ, g in anom.groupby("label"):
            d = occ[occ["type"] == typ]["delay_s"]
            rows.append(
                {
                    "k": k,
                    "anomaly": typ,
                    "windows": int(len(g)),
                    "tpr": float(g["alarm"].mean()),
                    "normal_windows": int(len(normal)),
                    "fpr": fpr,
                    "fpr_clean_runs": fpr_clean,
                    **{f"tpr_{rr}": float(g[rr].mean()) for rr in RULES},
                    "occurrences": int(len(d)),
                    "missed": int(d.isna().sum()),
                    "delay_mean_s": float(d.dropna().mean()) if d.notna().any() else None,
                    "delay_max_s": float(d.dropna().max()) if d.notna().any() else None,
                }
            )
        delays += per_occ
    return {"rows": rows, "delays": delays}


def sensitivity(train: pd.DataFrame, runs: list[tuple[pd.DataFrame, list[dict]]]) -> list[dict]:
    """(μ_anom - μ_norm) / σ_norm per (feature, anomaly); σ_norm = 0 (within float noise) ->
    ±inf, or 0 if the means are equal."""
    anom = pd.concat([df.assign(label=label_windows(df, iv).to_numpy()) for df, iv in runs])
    out = []
    for feat in SCALAR_FEATURES:
        mu, sd = train[feat].mean(), train[feat].std(ddof=0)
        if pd.isna(mu):
            continue
        for typ, g in anom[anom["label"] != "normal"].groupby("label"):
            diff = g[feat].mean() - mu
            tol = 1e-9 * max(1.0, abs(mu))  # σ below float noise counts as 0
            if sd > tol:
                val = diff / sd
            else:
                val = 0.0 if abs(diff) <= tol else float(np.sign(diff)) * np.inf
            out.append(
                {
                    "feature": feat,
                    "anomaly": typ,
                    "mu_norm": mu,
                    "sigma_norm": sd,
                    "mu_anom": g[feat].mean(),
                    "sensitivity": val,
                }
            )
    return out


# ------------------------------------------------------------------------- E9
def _sim_runs(results: Path, exp: str) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted((results / exp).glob("*/metrics.json")):
        m = json.loads(p.read_text())
        out.append((p.parent, m))
    return out


def _sim_intervals(m: dict) -> list[dict]:
    cfg = __import__("yaml").safe_load((Path(m["_dir"]) / "config.yaml").read_text())
    return [
        {"type": a["type"], "start_ns": round(a["start"] * 1e9), "end_ns": round(a["end"] * 1e9)}
        for a in cfg["sim"].get("anomalies") or []
    ]


def sim_dataset(results: Path, db, bitrate: int):
    e0 = _sim_runs(results, "E0")
    train = [
        sim_run_features(d, db, bitrate, SIM_WARMUP_S) for d, m in e0 if m["seed"] in (1, 2, 3)
    ]
    test = [
        (sim_run_features(d, db, bitrate, SIM_WARMUP_S), []) for d, m in e0 if m["seed"] in (4, 5)
    ]
    for d, m in _sim_runs(results, "E8"):
        test.append(
            (sim_run_features(d, db, bitrate, SIM_WARMUP_S), _sim_intervals(m | {"_dir": d}))
        )
    return pd.concat(train, ignore_index=True), test


def _online_features(run: Path, db, bitrate: int) -> pd.DataFrame:
    recs = load_records(run / "can.log")
    t0 = int(recs["t_ns"].min()) + round(ONLINE_WARMUP_S * 1e9)
    return window_features(recs, db, bitrate, t0, int(recs["t_ns"].max()))


def online_dataset(root: Path, db, bitrate: int):
    if not (root / "E0" / "can.log").exists():
        return None
    train = _online_features(root / "E0", db, bitrate)
    test = []
    for run in sorted(root.glob("E8_*")):
        if (run / "anomalies.json").exists() and (run / "can.log").exists():
            test.append(
                (
                    _online_features(run, db, bitrate),
                    json.loads((run / "anomalies.json").read_text()),
                )
            )
    return (train, test) if test else None


def main(argv: list[str] | None = None) -> int:
    from canav.ecu import load_dbc
    from canav.netdef import DEFAULT_MATRIX, load_matrix

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--results", default="results")
    p.add_argument("--tables", default="report/tables")
    p.add_argument("--dbc", default="config/dbc/canav.dbc")
    a = p.parse_args(argv)
    res, tables = Path(a.results), Path(a.tables)
    db = load_dbc(a.dbc)
    bitrate = next(iter(load_matrix(DEFAULT_MATRIX).buses.values()))
    periods = periodic_ids(db)
    datasets = {"sim": sim_dataset(res, db, bitrate)}
    online = online_dataset(res / "online_exp", db, bitrate)
    if online is not None:
        datasets["online"] = online
    eval_rows, sens_rows, out = [], [], {}
    for name, (train, test) in datasets.items():
        det = Detector(periods, bitrate).fit(train)
        ev = evaluate(det, test)
        eval_rows += [r | {"dataset": name, "train_windows": len(train)} for r in ev["rows"]]
        sens_rows += [r | {"dataset": name} for r in sensitivity(train, test)]
        out[name] = {
            "train_windows": len(train),
            "load_mu": det.load_mu,
            "load_sigma": det.load_sigma,
            "count_mu": {f"0x{c:03X}": v for c, v in det.count_mu.items()},
            "count_sigma": {f"0x{c:03X}": v for c, v in det.count_sigma.items()},
            "delays": ev["delays"],
        }
    tables.mkdir(parents=True, exist_ok=True)
    cols = ["dataset", "k", "anomaly", "windows", "tpr", "normal_windows", "fpr"]
    ev_df = pd.DataFrame(eval_rows)
    ev_df = ev_df[cols + [c for c in ev_df.columns if c not in cols]]
    ev_df.to_csv(tables / "detector_eval.csv", index=False)
    pd.DataFrame(sens_rows).to_csv(tables / "feature_sensitivity.csv", index=False)
    (res / "E9").mkdir(parents=True, exist_ok=True)
    (res / "E9" / "eval.json").write_text(
        json.dumps(out | {"rows": eval_rows}, indent=2, default=str)
    )
    show = ev_df[["dataset", "k", "anomaly", "windows", "tpr", "fpr", "delay_mean_s"]]
    print(show.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
