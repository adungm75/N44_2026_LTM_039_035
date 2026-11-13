import json
from pathlib import Path

import pytest

from canav.experiments import run as runner
from canav.experiments.sim import SimConfig, run_sim, theory
from canav.experiments.variants import (
    add_synthetic_load,
    apply_deadlines,
    scale_periods,
    set_dlc,
    two_bus,
)
from canav.netdef import load_network
from canav.timing import bus_load_theory, timing_messages
from canav.timing.budget import compute, l_max

ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "config/experiments/aeb_budget.yaml"
E5_NODES = {
    "ChassisSensors": "chassis",
    "Control": "chassis",
    "SteeringActuator": "chassis",
    "ThrottleBrakeActuator": "chassis",
    "AEB": "chassis",
    "Perception": "adas",
    "Planning": "adas",
    "BodyHMI": "adas",
    "GNSS": "adas",
}


@pytest.fixture(scope="module")
def net():
    return load_network()


def test_budget_formula_and_split():
    assert l_max(0.0, 1.5, 6.0) == 1.5
    assert l_max(18.0, 1.5, 6.0) == pytest.approx(0.0)  # v = 2 a TTC: no latency is enough
    import yaml

    cfg = yaml.safe_load(BUDGET.read_text()) | {"_path": str(BUDGET)}
    b = compute(cfg)
    d = b["design"]
    v = 60 / 3.6
    assert d["l_max_s"] == pytest.approx(1.5 - v / 12)
    assert d["network_budget_s"] == pytest.approx(0.2 * d["l_max_s"])
    assert d["per_hop_budget_s"] == pytest.approx(d["network_budget_s"] / 2)
    assert d["deadlines_s"]["AEB_BrakeRequest"] == pytest.approx(min(d["per_hop_budget_s"], 0.01))
    assert d["chain_e2e_limit_s"] == pytest.approx(0.8 * d["l_max_s"])
    assert len(b["l_max_by_speed"]) == 11


def test_variants(net):
    half = scale_periods(net, 0.5)
    assert half.message("WheelSpeed").period == pytest.approx(0.005)
    assert half.message("WheelSpeed").deadline == pytest.approx(0.005)
    four = set_dlc(net, 4)
    assert {m.dlc for m in four.messages if m.is_periodic} == {4}
    d = apply_deadlines(net, str(BUDGET))
    assert d.message("ObjectList_3").deadline < d.message("ObjectList_3").period
    assert d.message("WheelSpeed").deadline == d.message("WheelSpeed").period


@pytest.mark.parametrize("target", [0.3, 0.7, 1.1])
def test_synthetic_load_hits_target(net, target):
    out, info = add_synthetic_load(net, target, 500_000)
    assert info["load_achieved"] == pytest.approx(target, abs=1e-5)
    synth = [m for m in out.messages if m.name.startswith("Synth_")]
    assert len({m.sender for m in synth}) == len(synth)  # one node per message
    ids = [m.can_id for m in out.messages]
    assert len(ids) == len(set(ids)) and min(m.can_id for m in synth) > 0x010
    assert bus_load_theory(timing_messages(out, "worst"), 500_000) == pytest.approx(target, 1e-5)


def test_two_bus_copies_and_theory(net):
    tb = two_bus(net, {"chassis": 500_000, "adas": 500_000}, E5_NODES, 0.0005)
    copies = {(m.name, m.bus) for m in tb.messages if m.forwarded_from}
    assert ("ObjectList_0", "chassis") in copies  # Perception (adas) -> AEB (chassis)
    assert ("WheelSpeed", "adas") in copies  # ChassisSensors -> Planning
    assert ("AEB_BrakeRequest", "adas") not in copies  # AEB and its actuator on chassis
    cfg = SimConfig(duration=1.0, offsets="zero", tasks={"AEB": {"exec_time": 0.001}})
    wcrt, load = theory(tb, cfg)
    assert set(load) == {"chassis", "adas"}
    src = wcrt[("adas", 0x300)]
    copy = wcrt[("chassis", 0x300)]
    assert copy is not None and src is not None
    res = run_sim(tb, cfg, seed=1)
    fwd = [f for f in res.frames if f.bus == "chassis" and f.name == "ObjectList_0"]
    orig = [f for f in res.frames if f.bus == "adas" and f.name == "ObjectList_0"]
    assert fwd and fwd[0].t_queued == orig[0].t_tx_end + 500_000  # gateway delay 0.5 ms
    aeb = [f for f in res.frames if f.name == "AEB_BrakeRequest" and f.origin_ts is not None]
    assert aeb  # the AEB task consumes the forwarded ObjectList (origin chain intact)
    for key, r in wcrt.items():  # sim never exceeds the bound (offsets 0, no faults)
        mx = max(
            (f.t_tx_end - f.t_queued for f in res.frames if (f.bus, f.can_id) == key), default=0
        )
        assert r is None or mx <= r * 1e9 + 2_000 + 1e-6, key


def test_sweep_expand_and_cache(tmp_path):
    cfg = tmp_path / "Ex.yaml"
    cfg.write_text(
        f"base: {ROOT / 'config/experiments/E0_baseline.yaml'}\n"
        "name: Ex\ntraces: first\nsim: {faults: [{drop_prob: 0.0}]}\n"
        "sweep:\n  sim.faults.0.drop_prob: [0.0, 0.5]\n  sim.bitrate: [250000, 500000]\n"
    )
    doc = runner.load_doc(cfg)
    variants = runner.expand(doc)
    assert len(variants) == 4
    params, d = variants[3]
    assert d["sim"]["faults"][0]["drop_prob"] == 0.5 and d["sim"]["bitrate"] == 500000
    assert d["network"]["deadlines"]  # inherited from E0
    out = tmp_path / "res"
    dirs = runner.run_experiment(cfg, out, seeds=[1, 2], duration=0.5, jobs=1)
    assert len(dirs) == 8
    m = json.loads((dirs[0] / "metrics.json").read_text())
    assert m["params"] == {"sim.faults.0.drop_prob": 0.0, "sim.bitrate": 250000}
    first = [d for d in dirs if (d / "can.log").exists()]
    assert len(first) == 4 and not any((d / "frames.jsonl").exists() for d in dirs)
    again = runner.run_experiment(cfg, out, seeds=[1, 2], duration=0.5, jobs=1)
    assert sorted(again) == sorted(dirs)  # all cached


def test_bracket():
    from canav.analysis.criteria import _bracket

    assert _bracket([0.3, 0.5, 0.7], [True, True, False], highest=True)[0] == 0.5
    assert _bracket([125, 250, 500], [False, True, True], highest=False)[0] == 250
    best, text = _bracket([0.1, 0.2], [True, True], highest=True)
    assert best == 0.2 and text.startswith("none in sweep")
    assert _bracket([0.1, 0.2], [False, False], highest=True)[0] is None
