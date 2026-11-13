"""Experiment runner (SimPy engine).

CLI: ``python -m canav.experiments.run --config config/experiments/E1_bitrate.yaml [--jobs 4]``

An experiment YAML has ``name``, optional ``base`` (another experiment YAML, deep-merged
under this one), ``network`` (matrix, ID scheme and the variant keys of
:mod:`canav.experiments.variants`), ``sim`` (:class:`SimConfig`), ``traces`` and ``sweep``:
a mapping ``dotted.path -> [values]`` expanded as a cartesian product (list items addressed
by index, e.g. ``sim.faults.0.drop_prob``).

Each (variant, seed) writes ``<out>/<name>/<run_id>/{metrics.json, config.yaml}`` with
``run_id = seed<N>-<hash of the resolved variant>``; runs that already have ``metrics.json``
are skipped (``--force`` reruns). ``traces``: ``full`` = also ``can.log``, ``frames.jsonl``,
``errors.jsonl`` for every seed; ``first`` (default) = ``can.log`` + ``errors.jsonl`` for the
first seed only; ``none``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from canav.analysis.candump import format_line
from canav.analysis.metrics import compute_metrics
from canav.experiments.sim import SimConfig, message_info, run_sim, theory
from canav.experiments.variants import apply_network_variant
from canav.netdef import DEFAULT_MATRIX, DEFAULT_SCHEMES, load_network
from canav.provenance import git_hash

TRACES = ("full", "first", "none")
NETWORK_KEYS = {"matrix", "id_schemes", "scheme"}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_doc(path: str | Path) -> dict:
    doc = yaml.safe_load(Path(path).read_text())
    if "base" in doc:
        base = load_doc(doc.pop("base"))
        for k in ("sweep", "description", "traces"):
            base.pop(k, None)
        doc = _merge(base, doc)
    return doc


def _set_path(doc: dict, path: str, value) -> None:
    keys = path.split(".")
    node = doc
    for k in keys[:-1]:
        node = node[int(k)] if isinstance(node, list) else node.setdefault(k, {})
    last = keys[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


def expand(doc: dict) -> list[tuple[dict, dict]]:
    """Cartesian product of ``sweep`` -> [(params, doc without sweep)]."""
    sweep = doc.get("sweep") or {}
    base = {k: v for k, v in doc.items() if k != "sweep"}
    out = []
    for combo in itertools.product(*sweep.values()):
        params = dict(zip(sweep, combo, strict=True))
        d = copy.deepcopy(base)
        for path, value in params.items():
            _set_path(d, path, value)
        out.append((params, d))
    return out


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
    body = {k: v for k, v in resolved.items() if k not in ("description", "params")}
    body["sim"] = {k: v for k, v in body["sim"].items() if k != "seeds"}
    digest = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    return f"seed{seed}-{digest}"


def build_network(resolved: dict):
    n = resolved["network"]
    net = load_network(n["matrix"], n["scheme"], n["id_schemes"])
    variant = {k: v for k, v in n.items() if k not in NETWORK_KEYS}
    return apply_network_variant(net, variant, resolved["sim"].get("bitrate"))


def _jsonl(path: Path, df) -> None:
    df.to_json(path, orient="records", lines=True)
    if df.empty:
        path.write_text("")


def run_one(
    resolved: dict, seed: int, out_root: Path, traces: str = "full", first: bool = True
) -> tuple[Path, dict, float]:
    """Run one seed; returns (run dir, metrics, wall seconds of the simulation)."""
    net, variant_info = build_network(resolved)
    cfg = SimConfig.from_dict(resolved["sim"])
    run_id = run_id_for(resolved, seed)
    run_dir = out_root / resolved["name"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    res = run_sim(net, cfg, seed)
    wall = time.perf_counter() - t0

    frames = res.frames_df(run_id)
    errors = res.errors_df(run_id)
    wcrt, load_th = theory(net, cfg)
    info = message_info(net, cfg, wcrt)
    metrics = compute_metrics(
        frames, errors, info, res.bitrates, res.duration_ns, theory_load=load_th
    )
    metrics |= {
        "run_id": run_id,
        "seed": seed,
        "engine": "sim",
        "sim_wall_s": wall,
        "params": resolved.get("params", {}),
        "variant": variant_info,
    }

    if traces == "full" or (traces == "first" and first):
        ok = sorted((f for f in res.frames if f.outcome == "ok"), key=lambda f: f.t_tx_end)
        with open(run_dir / "can.log", "w") as fh:
            for f in ok:
                fh.write(format_line(f.t_tx_end, f.bus, f.can_id, f.data, f.extended) + "\n")
        _jsonl(run_dir / "errors.jsonl", errors)
        if traces == "full":
            _jsonl(run_dir / "frames.jsonl", frames)
    meta = resolved | {"run_id": run_id, "seed": seed, "engine": "sim", "git": git_hash()}
    (run_dir / "config.yaml").write_text(yaml.safe_dump(meta, sort_keys=False))
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))  # last: cache marker
    return run_dir, metrics, wall


def _job(args) -> tuple[str, float, str]:
    resolved, seed, out_root, traces, first = args
    run_dir, _, wall = run_one(resolved, seed, Path(out_root), traces, first)
    return str(run_dir), wall, json.dumps(resolved.get("params", {}))


def run_experiment(
    path: str | Path,
    out: str | Path = "results",
    *,
    seeds: list[int] | None = None,
    duration: float | None = None,
    jobs: int = 1,
    force: bool = False,
) -> list[Path]:
    doc = load_doc(path)
    traces = doc.get("traces", "first")
    if traces not in TRACES:
        raise ValueError(f"traces must be one of {TRACES}")
    todo, done = [], []
    for params, d in expand(doc):
        resolved = resolve(d, duration) | {"params": params}
        run_seeds = seeds or resolved["sim"]["seeds"]
        for i, seed in enumerate(run_seeds):
            run_dir = Path(out) / resolved["name"] / run_id_for(resolved, seed)
            if not force and (run_dir / "metrics.json").exists():
                done.append(run_dir)
                continue
            todo.append((resolved, seed, str(out), traces, i == 0))
    name = doc["name"]
    print(f"== {name}: {len(todo)} runs to do, {len(done)} cached", flush=True)
    t0 = time.perf_counter()
    if jobs > 1 and len(todo) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_job, todo))
    else:
        results = [_job(t) for t in todo]
    for run_dir, wall, params in results:
        print(f"   {run_dir}  {params}  sim {wall:.1f} s", flush=True)
    print(f"== {name}: done in {time.perf_counter() - t0:.0f} s", flush=True)
    return done + [Path(r[0]) for r in results]


def print_summary(metrics: dict) -> None:
    print(
        f"{'ID':>10} {'name':<18} {'n_ok':>6} {'R_mean[us]':>10} {'R_max[us]':>10} "
        f"{'WCRT[us]':>9}  R_max<=WCRT"
    )
    for cid, m in metrics["messages"].items():
        r_max, wcrt = m["R_max"], m["wcrt"]
        within = r_max is not None and wcrt is not None and r_max <= wcrt + 1e-12
        fmt = lambda v: "-" if v is None else f"{v * 1e6:.1f}"  # noqa: E731
        print(
            f"{cid:>10} {m['name']:<18} {m['outcomes'].get('ok', 0):>6} {fmt(m['R_mean']):>10} "
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
    p.add_argument("--config", required=True, nargs="+")
    p.add_argument("--engine", choices=["sim"], default="sim")
    p.add_argument("--seeds", type=int, nargs="+", default=None, help="override sim.seeds")
    p.add_argument("--duration", type=float, default=None, help="override sim.duration [s]")
    p.add_argument("--out", default="results")
    p.add_argument("--jobs", type=int, default=1, help="parallel processes")
    p.add_argument("--force", action="store_true", help="rerun cached runs")
    p.add_argument("--print", action="store_true", help="print per-message tables")
    a = p.parse_args(argv)
    jobs = a.jobs if a.jobs > 0 else max(1, (os.cpu_count() or 2) // 2)
    for cfg in a.config:
        dirs = run_experiment(
            cfg, a.out, seeds=a.seeds, duration=a.duration, jobs=jobs, force=a.force
        )
        if a.print:
            for d in dirs:
                print(f"-- {d}")
                print_summary(json.loads((d / "metrics.json").read_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
