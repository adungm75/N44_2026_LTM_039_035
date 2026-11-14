import math
from pathlib import Path

import pytest
import yaml

from canav.vehicle import Control
from canav.vehicle.bridge import make_backend
from canav.vehicle.carla_bridge import (
    next_stop,
    route_points,
    stops_on_route,
    to_carla_steer,
    to_xy,
    to_yaw,
)
from canav.vehicle.route import Route

ROOT = Path(__file__).resolve().parents[1]


def test_frame_conversion():
    # CARLA is left-handed: +y is to the right of a car heading +x, yaw grows clockwise
    assert to_xy(3.0, 2.0) == (3.0, -2.0)
    assert to_yaw(90.0) == pytest.approx(-math.pi / 2)  # facing CARLA +y = facing right
    assert to_yaw(-90.0) == pytest.approx(math.pi / 2)
    assert abs(to_yaw(180.0)) == pytest.approx(math.pi)
    assert to_carla_steer(0.3) == pytest.approx(-0.3)  # project: > 0 = left
    assert to_carla_steer(2.0) == -1.0


def test_route_points_drops_duplicates():
    pts = route_points([(0, 0), (0, 0), (1, 0), (1, 0.0000001), (2, 0)])
    assert pts == [(0, 0), (1, 0), (2, 0)]
    Route(pts)  # no zero-length segment


def test_stops_on_route():
    r = Route([(0.0, 0.0), (100.0, 0.0)])
    stops = [
        ("b", 60.0, 0.5, 0.0),  # on the route, same direction
        ("a", 40.0, -0.8, 0.1),
        ("opposite", 50.0, 0.0, math.pi),  # stop line of the other driving direction
        ("side", 30.0, 5.0, 0.0),  # another road
        ("beyond", 120.0, 0.0, 0.0),
    ]
    found = stops_on_route(r, stops)
    assert [k for _, k in found] == ["a", "b"]
    assert found[0][0] == pytest.approx(40.0)
    assert next_stop(found, 30.0, 60.0) == (pytest.approx(10.0), "a")
    assert next_stop(found, 30.0, 5.0) is None  # beyond the visible range
    assert next_stop(found, 45.0, 60.0) == (pytest.approx(15.0), "b")
    assert next_stop(found, 61.0, 60.0) is None


def test_unknown_backend():
    with pytest.raises(ValueError):
        make_backend("nope", {})


@pytest.mark.carla
def test_carla_backend_smoke():
    """Needs a running CARLA server. Spawns, drives straight, cleans up."""
    import carla

    sc = yaml.safe_load((ROOT / "config/scenarios/carla_town02.yaml").read_text())
    b = make_backend("carla", sc)
    try:
        assert b.route().length > 100
        assert b._stops  # signalized stop lines on the route
        for _ in range(60):
            st = b.step(Control(throttle=0.6))
        assert st.speed > 1.0 and st.t_sim == pytest.approx(3.0, abs=0.06)
        assert abs(st.lane_offset) < 1.0
        assert st.tl["state"] in ("red", "yellow", "green", "none")
    finally:
        b.close()
    w = carla.Client(sc["host"], sc["port"]).get_world()
    assert not w.get_settings().synchronous_mode  # server handed back in async mode
    assert not w.get_actors().filter("vehicle.*") and not w.get_actors().filter("walker.*")
