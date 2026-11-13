import math
import time
from pathlib import Path

import pytest
import yaml

from canav.sidechannel import UdpLatest, UdpSender, decode, encode, state_message
from canav.vehicle import Control
from canav.vehicle.route import Route
from canav.vehicle.sim2d import Sim2D

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario():
    return yaml.safe_load((ROOT / "config/scenarios/sim2d_default.yaml").read_text())


def test_route_l_shape(scenario):
    r = Sim2D(scenario).route()
    assert r.length == pytest.approx(120 + math.pi / 2 * 20 + 80, abs=0.05)
    assert r.point_at(r.length) == pytest.approx((140.0, 100.0), abs=1e-6)
    assert r.curvature_at(135.0) == pytest.approx(1 / 20, rel=0.02)
    assert r.curvature_at(50.0) == pytest.approx(0.0, abs=1e-9)
    s, lat = r.project(60.0, 1.5)
    assert s == pytest.approx(60.0) and lat == pytest.approx(1.5)  # left = positive
    s, lat = r.project(141.0, 60.0)  # on the second straight, 1 m right of the centre line
    assert lat == pytest.approx(-1.0, abs=1e-6)


def test_route_json_roundtrip(tmp_path):
    r = Route.from_segments((0, 0, 0), [{"straight": 10}, {"arc": {"radius": 5, "angle": -1.0}}])
    r.to_json(tmp_path / "r.json")
    assert Route.from_json(tmp_path / "r.json").length == pytest.approx(r.length, abs=1e-3)
    assert r.curvature_at(12.0) == pytest.approx(-0.2, rel=0.05)  # right turn


def test_bicycle_accel_and_turn(scenario):
    sim = Sim2D(scenario)
    for _ in range(100):  # 1 s full throttle: a = 3.0 - 0.1 rolling
        st = sim.step(Control(throttle=1.0))
    assert st.speed == pytest.approx(2.9, abs=0.05)
    assert st.ax == pytest.approx(2.9, abs=0.01)
    for _ in range(100):
        st = sim.step(Control(steer=0.5, throttle=0.1))
    assert st.yaw > 0.1 and st.yaw_rate > 0  # positive steer turns left
    for _ in range(300):
        st = sim.step(Control(brake=1.0))
    assert st.speed == 0.0


def test_pedestrian_trigger_and_collision(scenario):
    sc = scenario | {"parked_car": None, "traffic_light": None}
    sc["pedestrian"] = sc["pedestrian"] | {
        "s": 20.0,
        "trigger_distance": 12.0,
        "lat_start": 0.0,
        "lat_end": 0.0,
    }
    sim = Sim2D(sc)
    hit = False
    for _ in range(2000):
        st = sim.step(Control(throttle=0.6))
        front = st.s + 3.6
        if front < 20.0 - 12.0:
            assert not st.objects  # occluded until the trigger distance
        hit |= 2 in st.collisions
    assert hit  # driving straight into a standing pedestrian is detected


def test_traffic_light_schedule(scenario):
    sim = Sim2D(scenario)
    sim.x, sim.y = sim.route().point_at(80.0)  # front bumper 26.4 m before the stop line
    st = sim.step(Control())
    assert st.tl["state"] == "red" and st.tl["dist_stop"] == pytest.approx(26.4, abs=0.05)
    sim.t = 30.0
    assert sim.step(Control()).tl["state"] == "green"
    sim.x, sim.y = sim.route().point_at(108.0)  # front past the line
    assert sim.step(Control()).tl["state"] == "none"


def test_goal(scenario):
    sim = Sim2D(scenario)
    sim.x, sim.y = sim.route().point_at(sim.route().length - 1.0)
    assert sim.step(Control()).goal_reached


def test_sidechannel_roundtrip(scenario):
    st = Sim2D(scenario).step(Control())
    msg = state_message(7, st)
    assert decode(encode(msg)) == msg
    assert set(msg) == {
        "type",
        "seq",
        "t_sim",
        "t_wall_ns",
        "pose",
        "speed",
        "imu",
        "objects",
        "tl",
    }
    rx = UdpLatest(47390)
    tx = UdpSender()
    try:
        for i in range(3):
            tx.send(47390, {"type": "ctrl", "seq": i})
        t0 = time.monotonic()
        while rx.latest is None or rx.latest["seq"] != 2:
            rx.poll()
            assert time.monotonic() - t0 < 2
        assert rx.received == 3
    finally:
        rx.close()
        tx.close()
