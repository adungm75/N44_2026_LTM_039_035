"""Closed loop on python-can ``virtual`` + sim2d (no vcan, no CARLA). ~15 s wall."""

from pathlib import Path

import pytest
import yaml

from canav.experiments.online import run_stack

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    sc = yaml.safe_load((ROOT / "config/scenarios/sim2d_default.yaml").read_text())
    sc["road"]["segments"] = [{"straight": 70.0}]
    sc["parked_car"] = None
    sc["traffic_light"] = None
    sc["pedestrian"]["s"] = 35.0
    out = tmp_path_factory.mktemp("online")
    metrics = run_stack(out, iface="virtual", duration=40.0, scenario=sc, port_base=47400)
    return out, metrics


def test_reaches_goal_without_collision(run):
    _, m = run
    assert m["driving"]["goal_reached"]
    assert m["driving"]["collisions"] == 0


def test_aeb_stops_for_pedestrian(run):
    _, m = run
    assert m["ecu_stats"]["AEB"]["aeb_activations"] >= 1
    assert m["aeb"] is not None and m["aeb"]["stopping_distance_m"] is not None
    assert m["driving"]["min_inpath_gap_m"]["2"] > 0.5  # stopped in front of the pedestrian


def test_no_e2e_errors(run):
    _, m = run
    assert m["errors"]["crc_err"] == 0
    assert m["errors"]["counter_err"] == 0


def test_logs_and_metrics_complete(run):
    out, m = run
    assert m["logs"]["can_log"] and m["logs"]["bridge_trace"]
    assert len(m["logs"]["ecu_logs"]) == 9
    for name in ("config.yaml", "route.json", "driving.json", "metrics.json"):
        assert (out / name).exists()
    assert m["hop_latency"]["n"] > 1000 and m["hop_latency"]["hop_p50"] > 0
    assert m["messages"]["WheelSpeed"]["rx_jitter_std"] is not None
    assert m["e2e"]  # actuator chains resolved to sensor origins
    assert m["bus"]["frames"] > 1000
