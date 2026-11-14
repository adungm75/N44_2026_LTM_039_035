"""AEB chain end-to-end time budget.

At the AEB trigger the gap is d = v·TTC. Stopping without impact needs v·L + v²/(2a) <= d,
so the end-to-end budget is ``L_max(v) = TTC - v/(2a)`` (kinematic, no brake ramp-up).
``L_max`` is split by processing-load weight (advisor): sensing/decision (Perception + AEB),
network (CAN hops), brake ECU (ThrottleBrakeActuator). The network share is split equally
over the CAN hops of the chain (chosen). Each per-hop deadline is capped at the period T
(chosen: with overwrite, an instance not sent before the next release is lost anyway).

CLI: ``python -m canav.timing.budget --config config/experiments/aeb_budget.yaml``
writes ``results/budget/aeb_budget.json``. Every input is read from config with its source.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from canav.ecu.base import DEFAULT_PERIOD_S
from canav.netdef import Network, load_network

DEFAULT_CONFIG = Path("config/experiments/aeb_budget.yaml")
DEFAULT_OUT = Path("results/budget/aeb_budget.json")
KMH = 1 / 3.6


def l_max(v: float, ttc: float, decel: float) -> float:
    """End-to-end latency budget [s] at speed ``v`` [m/s]; <= 0 means no latency is enough."""
    return ttc - v / (2 * decel)


def compute(cfg: dict) -> dict:
    params = yaml.safe_load(Path(cfg["ecu_params"]).read_text())
    sim = yaml.safe_load(Path(cfg["sim_config"]).read_text())["sim"]
    scenario = yaml.safe_load(Path(cfg["scenario"]).read_text())
    net: Network = load_network(cfg["matrix"], cfg["scheme"], cfg["id_schemes"])
    ch = cfg["chain"]
    ttc = float(params["AEB"]["ttc_threshold"])
    decel = float(params["AEB"]["decel_req"])
    shares = {k: float(v) for k, v in cfg["shares"].items()}
    if abs(sum(shares.values()) - 1.0) > 1e-9:
        raise ValueError("budget shares must sum to 1")

    sensing = [net.message(n) for n in ch["sensing_messages"]]
    aeb_msg = net.message(ch["aeb_message"])
    hops = [*sensing, aeb_msg]  # CAN hops of the chain (one per message)
    n_net_hops = 2  # sensing frame -> AEB, AEB_BrakeRequest -> actuator
    t_obj = max(m.period for m in sensing)
    aeb_exec = float(sim["tasks"][ch["aeb_node"]]["exec_time"])
    act_period = float((params.get(ch["actuator"]) or {}).get("period", DEFAULT_PERIOD_S))
    bridge_tick = float(scenario["dt"])
    terms = {
        "sensing_decision": {
            "sensing_sample_age_max_s": t_obj,
            "aeb_period_s": aeb_msg.period,
            "aeb_exec_s": aeb_exec,
        },
        "brake_ecu": {"actuator_period_s": act_period, "bridge_tick_s": bridge_tick},
    }

    def at(v_mps: float) -> dict:
        lm = l_max(v_mps, ttc, decel)
        net_budget = shares["network"] * lm
        return {
            "v_kmh": v_mps / KMH,
            "v_mps": v_mps,
            "l_max_s": lm,
            "network_budget_s": net_budget,
            "per_hop_budget_s": net_budget / n_net_hops,
        }

    table = [at(v * KMH) for v in cfg["speeds_kmh"]]
    d = at(float(cfg["design_speed_kmh"]) * KMH)
    budgets = {k: shares[k] * d["l_max_s"] for k in shares}
    checks = {k: {"terms_sum_s": sum(terms[k].values()), "budget_s": budgets[k]} for k in terms}
    for c in checks.values():
        c["ok"] = c["terms_sum_s"] <= c["budget_s"]
    per_hop = d["per_hop_budget_s"]
    deadlines = {m.name: min(per_hop, m.period) for m in hops}
    return {
        "inputs": {
            "ttc_s": ttc,
            "decel_mps2": decel,
            "design_speed_kmh": float(cfg["design_speed_kmh"]),
            "shares": shares,
            "network_split": "equal over 2 CAN hops (chosen)",
            "sources": {
                "ttc_s": f"{cfg['ecu_params']}: AEB.ttc_threshold",
                "decel_mps2": f"{cfg['ecu_params']}: AEB.decel_req (USER decision 2026-11-13)",
                "design_speed_kmh": f"{cfg['_path']}: design_speed_kmh (chosen)",
                "shares": f"{cfg['_path']}: shares (advisor 2026-11-13)",
                "sensing_sample_age_max_s": f"{cfg['matrix']}: max period of the sensing msgs",
                "aeb_period_s": f"{cfg['matrix']}: period of {ch['aeb_message']}",
                "aeb_exec_s": f"{cfg['sim_config']}: sim.tasks.{ch['aeb_node']}.exec_time"
                " (assumption, not measured)",
                "actuator_period_s": f"{cfg['ecu_params']}: {ch['actuator']}.period or "
                "canav.ecu.base.DEFAULT_PERIOD_S",
                "bridge_tick_s": f"{cfg['scenario']}: dt",
            },
        },
        "l_max_by_speed": table,
        "design": d
        | {
            "budgets_s": budgets,
            "terms": terms,
            "share_checks": checks,
            "chain_e2e_limit_s": (shares["sensing_decision"] + shares["network"]) * d["l_max_s"],
            "deadlines_s": deadlines,
            "deadline_ids": {m.name: m.can_id for m in hops},
        },
    }


def load_deadlines(config: str | Path) -> tuple[float, dict[str, float]]:
    """(uncapped per-hop budget, per-message deadlines) computed from a budget config YAML
    (same numbers as ``results/budget/aeb_budget.json``, without depending on that file)."""
    cfg = yaml.safe_load(Path(config).read_text()) | {"_path": str(config)}
    d = compute(cfg)["design"]
    return d["per_hop_budget_s"], d["deadlines_s"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    a = p.parse_args(argv)
    cfg = yaml.safe_load(Path(a.config).read_text()) | {"_path": a.config}
    res = compute(cfg)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    d = res["design"]
    print(
        f"v = {d['v_kmh']:.0f} km/h: L_max = {d['l_max_s'] * 1e3:.1f} ms, network "
        f"{d['network_budget_s'] * 1e3:.1f} ms, per hop {d['per_hop_budget_s'] * 1e3:.1f} ms, "
        f"chain e2e limit {d['chain_e2e_limit_s'] * 1e3:.1f} ms -> {out}"
    )
    for k, c in d["share_checks"].items():
        print(
            f"  {k}: terms {c['terms_sum_s'] * 1e3:.1f} ms <= budget "
            f"{c['budget_s'] * 1e3:.1f} ms: {c['ok']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
