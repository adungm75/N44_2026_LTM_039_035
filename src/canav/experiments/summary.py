"""Collect SimPy experiment results into ``results/summary.parquet`` (one row per run and
message) — the input of the criteria (``canav.analysis.criteria``) and of the figures.

CLI: ``python -m canav.experiments.summary [--results results]``
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

EXPERIMENTS = ("E0", "E1", "E2", "E3", "E4", "E5", "E7_drop", "E7_delay", "E7_error")


def label(params: dict) -> str:
    """Short variant label, e.g. ``bitrate=125000`` (last path element of each key)."""
    if not params:
        return "baseline"
    return ",".join(f"{k.rsplit('.', 1)[-1]}={v}" for k, v in params.items())


def rows_for_run(exp: str, m: dict) -> list[dict]:
    params = m.get("params") or {}
    run = {
        "experiment": exp,
        "variant": label(params),
        "params": json.dumps(params, sort_keys=True),
        "seed": m["seed"],
        "run_id": m["run_id"],
        "sim_wall_s": m.get("sim_wall_s"),
        "load_target": (m.get("variant") or {}).get("synthetic", {}).get("load_target"),
        "n_synthetic": (m.get("variant") or {}).get("synthetic", {}).get("n_synthetic", 0),
        "busoff_total": sum(n["busoff_count"] for n in m["nodes"].values()),
        "max_tec": max((n["max_tec"] for n in m["nodes"].values()), default=0),
    }
    rows = []
    for key, msg in m["messages"].items():
        bus = m["buses"][msg["bus"]]
        oc = msg["outcomes"]
        rows.append(
            run
            | {
                "msg_key": key,
                "name": msg["name"],
                "bus": msg["bus"],
                "bitrate": bus["bitrate"],
                "bus_load": bus["load"],
                "bus_load_theory": bus["load_theory"],
                "bus_error_frames": bus["error_frames"],
                "period": msg["period"],
                "deadline": msg["deadline"],
                "wcrt": msg["wcrt"],
                "n": msg["n"],
                "n_ok": oc.get("ok", 0),
                "n_dropped": oc.get("dropped", 0),
                "n_overwritten": oc.get("overwritten", 0),
                "n_busoff": oc.get("busoff", 0),
                "R_mean": msg["R_mean"],
                "R_p99": msg["R_p99"],
                "R_max": msg["R_max"],
                "deadline_miss_rate": msg["deadline_miss_rate"],
                "loss_counter": msg["loss_counter"],
                "lost_detected": msg.get("lost_detected"),
                "lost_between": msg.get("lost_between"),
                "jitter_std": msg["jitter_std"],
                "e2e_mean": msg["e2e_mean"],
                "e2e_max": msg["e2e_max"],
            }
        )
    return rows


def collect(results: str | Path = "results") -> pd.DataFrame:
    rows = []
    for exp in EXPERIMENTS:
        for path in sorted(Path(results, exp).glob("*/metrics.json")):
            rows += rows_for_run(exp, json.loads(path.read_text()))
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default="results")
    a = p.parse_args(argv)
    df = collect(a.results)
    out = Path(a.results) / "summary.parquet"
    df.to_parquet(out, index=False)
    print(f"{len(df)} rows, {df['run_id'].nunique() if len(df) else 0} runs -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
