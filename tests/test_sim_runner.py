import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from canav.analysis.candump import CanRecord, format_line, parse_line, read_log, write_log
from canav.analysis.metrics import counter_loss, window_load
from canav.experiments import run as runner
from canav.experiments.sim import SimConfig, run_sim, theory
from canav.netdef import load_network

ROOT = Path(__file__).resolve().parents[1]
E0 = ROOT / "config/experiments/E0_baseline.yaml"


@pytest.fixture(scope="module")
def net():
    return load_network(
        ROOT / "config/network/matrix.yaml", schemes_path=ROOT / "config/network/id_schemes.yaml"
    )


def _e0_sim(**over) -> SimConfig:
    doc = yaml.safe_load(E0.read_text())["sim"] | over
    return SimConfig.from_dict(doc)


def test_baseline_sim_within_wcrt(net):
    """§5.4 rule 5: offsets 0, J 0, worst stuffing -> max R <= WCRT (+1 tau) for every ID."""
    cfg = _e0_sim(offsets="zero", duration=1.0)
    res = run_sim(net, cfg, seed=1)
    wcrt, _ = theory(net, cfg)
    tau = 1e9 / 500_000
    worst: dict[tuple[str, int], int] = {}
    for f in res.frames:
        assert f.outcome == "ok"
        key = (f.bus, f.can_id)
        worst[key] = max(worst.get(key, 0), f.t_tx_end - f.t_queued)
    assert set(worst) == set(wcrt)
    for key, r in worst.items():
        assert r <= wcrt[key] * 1e9 + tau + 1e-6, key


@pytest.mark.parametrize("offsets", ["zero", "random"])
def test_sim_load_matches_theory(net, offsets):
    cfg = _e0_sim(offsets=offsets, duration=5.0)
    res = run_sim(net, cfg, seed=2)
    busy = sum(f.t_tx_end - f.t_tx_start for f in res.frames if f.outcome == "ok")
    _, load = theory(net, cfg)
    assert busy / res.duration_ns * 100 == pytest.approx(load["main"] * 100, abs=0.5)


def test_seed_reproducible(net):
    cfg = _e0_sim(duration=1.0)
    a = [(f.can_id, f.t_tx_end) for f in run_sim(net, cfg, 7).frames]
    b = [(f.can_id, f.t_tx_end) for f in run_sim(net, cfg, 7).frames]
    assert a == b


def test_e2e_chain_to_actuators(net):
    res = run_sim(net, _e0_sim(duration=1.0), seed=1)
    steer = [f for f in res.frames if f.name == "Steer_Cmd" and f.origin_ts is not None]
    assert steer and all(f.t_tx_end > f.origin_ts for f in steer)


def test_candump_roundtrip(tmp_path):
    recs = [
        CanRecord(1_234_567_000, "vcan0", 0x010, bytes.fromhex("DEADBEEF")),
        CanRecord(2_000_001_000, "main", 0x1ABCDEF0, bytes(8), extended=True),
        CanRecord(3_000_000_000, "can1", 0x7DF, b""),
    ]
    path = tmp_path / "x.log"
    write_log(path, recs)
    assert path.read_text().splitlines()[0] == "(1.234567) vcan0 010#DEADBEEF"
    assert list(read_log(path)) == recs
    assert format_line(1_999, "c", 1, b"\x01") == "(0.000001) c 001#01"  # µs truncation


def test_candump_parse_variants():
    assert parse_line("(1436509052.249713) vcan0 123#R").rtr
    fd = parse_line("(1.000000) can0 123##1AABB")
    assert fd.fd and fd.data == bytes.fromhex("AABB")
    err = parse_line("(1.000000) can0 20000080#0000000000000000")
    assert err.error and err.extended
    with pytest.raises(ValueError):
        parse_line("garbage")


def test_counter_loss():
    assert counter_loss([]) == (0, 0)
    assert counter_loss([0, 1, 2, 3]) == (4, 4)
    assert counter_loss([14, 15, 0, 3]) == (6, 4)  # wrap, then 2 lost
    assert counter_loss([5, 5]) == (2, 2)  # repeat: no loss


def test_window_load():
    # busy [0,50) and [150, 250) in 100-ns windows over 300 ns
    wl = window_load(np.array([0, 150]), np.array([50, 250]), 300, 100)
    assert wl.tolist() == [0.5, 0.5, 0.5]


def test_runner_writes_outputs(tmp_path):
    rc = runner.main(
        ["--config", str(E0), "--seeds", "3", "--duration", "2", "--out", str(tmp_path)]
    )
    assert rc == 0
    (run_dir,) = (tmp_path / "E0").iterdir()
    for name in ("can.log", "frames.jsonl", "errors.jsonl", "metrics.json", "config.yaml"):
        assert (run_dir / name).exists(), name
    m = json.loads((run_dir / "metrics.json").read_text())
    assert m["seed"] == 3 and m["buses"]["main"]["load"] == pytest.approx(0.1758, abs=0.005)
    assert m["messages"]["0x010"]["R_max"] <= m["messages"]["0x010"]["wcrt"]
    cfg = yaml.safe_load((run_dir / "config.yaml").read_text())
    assert cfg["seed"] == 3 and cfg["git"] and cfg["sim"]["duration"] == 2
    row = json.loads((run_dir / "frames.jsonl").read_text().splitlines()[0])
    assert set(runner_fields()) <= set(row)
    rows = [json.loads(line) for line in (run_dir / "frames.jsonl").read_text().splitlines()]
    sensor = next(r for r in rows if r["name"] == "WheelSpeed")  # sources always have origin_ts
    assert all(
        isinstance(sensor[k], int) for k in ("t_release", "t_queued", "t_tx_end", "origin_ts")
    )
    n_ok = sum(
        1
        for line in (run_dir / "frames.jsonl").read_text().splitlines()
        if json.loads(line)["outcome"] == "ok"
    )
    assert sum(1 for _ in read_log(run_dir / "can.log")) == n_ok


def runner_fields():
    from canav.busmodel import SIDECAR_FIELDS

    return SIDECAR_FIELDS


def test_sim_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        SimConfig.from_dict({"bogus": 1})
