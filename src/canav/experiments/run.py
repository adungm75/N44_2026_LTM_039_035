"""Experiment runner.

CLI: ``python -m canav.experiments.run --config config/experiments/E0_baseline.yaml --engine sim``
Writes ``<out>/<experiment>/<run_id>/{can.log, frames.jsonl, errors.jsonl, metrics.json,
config.yaml}``; ``run_id`` = ``seed<N>-<hash of resolved config>``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import yaml

from canav.analysis.candump import format_line
from canav.analysis.metrics import compute_metrics
from canav.experiments.sim import SimConfig, message_info, run_sim, theory
from canav.netdef import DEFAULT_MATRIX, DEFAULT_SCHEMES, load_network
from canav.provenance import git_hash


def resolve(doc: dict, duration: float | None = None) -> dict:
    """Fill defaults: returns ``{name, description, network: {...}, sim: {...}}``."""
    net = {"matrix": str(DEFAULT_MATRIX), "id_schemes": str(DEFAULT_SCHEMES)}
    net |= {"scheme": "safety_first"} | dict(doc.get("network") or {})
    sim = SimConfig.from_dict(doc.get("sim"))
    if duration is not None:
        sim.duration = duration
    return {
        "name": str(doc["name"]),
        "description": doc.get("description", ""),
        "network": net,
        "sim": sim.to_dict(),
    }


def run_id_for(resolved: dict, seed: int) -> str:
    body = {k: v for k, v in resolved.items() if k != "description"}
    body["sim"] = {k: v for k, v in body["sim"].items() if k != "seeds"}
    digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    return f"seed{seed}-{digest}"


def _jsonl(path: Path, df) -> None:
    df.to_json(path, orient="records", lines=True)
    if df.empty:
        path.write_text("")


def run_one(resolved: dict, seed: int, out_root: Path) -> tuple[Path, dict, float]:
    """Run one seed; returns (run dir, metrics, wall seconds of the simulation)."""
    n = resolved["network"]
    net = load_network(n["matrix"], n["scheme"], n["id_schemes"])
    cfg = SimConfig.from_dict(resolved["sim"])
    run_id = run_id_for(resolved, seed)
    run_dir = out_root / resolved["name"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    res = run_sim(net, cfg, seed)
    wall = time.perf_counter() - t0

    frames = res.frames_df(run_id)
    errors = res.errors_df(run_id)
    _, load_th = theory(net, cfg)
    metrics = compute_metrics(
        frames, errors, message_info(net, cfg), res.bitrates, res.duration_ns, theory_load=load_th
    )
    metrics |= {"run_id": run_id, "seed": seed, "engine": "sim", "sim_wall_s": wall}

    ok = sorted((f for f in res.frames if f.outcome == "ok"), key=lambda f: f.t_tx_end)
    with open(run_dir / "can.log", "w") as fh:
        for f in ok:
            fh.write(format_line(f.t_tx_end, f.bus, f.can_id, f.data, f.extended) + "\n")
    _jsonl(run_dir / "frames.jsonl", frames)
    _jsonl(run_dir / "errors.jsonl", errors)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    meta = resolved | {"run_id": run_id, "seed": seed, "engine": "sim", "git": git_hash()}
    (run_dir / "config.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    return run_dir, metrics, wall


def print_summary(metrics: dict) -> None:
    print(
        f"{'ID':>6} {'name':<18} {'n_ok':>6} {'R_mean[us]':>10} {'R_max[us]':>10} "
        f"{'WCRT[us]':>9}  R_max<=WCRT"
    )
    for cid, m in metrics["messages"].items():
        r_max, wcrt = m["R_max"], m["wcrt"]
        within = r_max is not None and wcrt is not None and r_max <= wcrt + 1e-12
        fmt = lambda v: "-" if v is None else f"{v * 1e6:.1f}"  # noqa: E731
        print(
            f"{cid:>6} {m['name']:<18} {m['outcomes'].get('ok', 0):>6} {fmt(m['R_mean']):>10} "
            f"{fmt(r_max):>10} {fmt(wcrt):>9}  {within}"
        )
    for bus, b in metrics["buses"].items():
        print(
            f"bus {bus}: load sim {b['load'] * 100:.2f} % vs theory {b['load_theory'] * 100:.2f} %"
            f" (100 ms windows max {b['load_window_max'] * 100:.2f} %), "
            f"error frames {b['error_frames']}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", required=True)
    p.add_argument("--engine", choices=["sim", "online"], default="sim")
    p.add_argument("--seeds", type=int, nargs="+", default=None, help="override sim.seeds")
    p.add_argument("--duration", type=float, default=None, help="override sim.duration [s]")
    p.add_argument("--out", default="results")
    a = p.parse_args(argv)
    if a.engine != "sim":
        print("engine 'online' is implemented in Phase 3", file=sys.stderr)
        return 2

    resolved = resolve(yaml.safe_load(Path(a.config).read_text()), a.duration)
    seeds = a.seeds or resolved["sim"]["seeds"]
    for seed in seeds:
        run_dir, metrics, wall = run_one(resolved, seed, Path(a.out))
        print(
            f"== {resolved['name']} seed {seed}: {resolved['sim']['duration']:.0f} s simulated "
            f"in {wall:.2f} s wall -> {run_dir}"
        )
        print_summary(metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
