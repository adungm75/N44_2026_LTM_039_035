"""Vehicle bridge: steps a backend in real time, sends state to ECUs, applies latest control.

CLI: ``python -m canav.vehicle.bridge --scenario out/scenario.yaml --params out/ecu_params.yaml
--out out --duration 120``. Writes ``route.json`` first, then ``bridge.jsonl`` (one line per
tick) and ``driving.json`` (driving metrics) at the end.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import threading
import time
from pathlib import Path

import yaml

from canav.sidechannel import UdpLatest, UdpSender, ctrl_port, state_message, state_port
from canav.vehicle import Control, VehicleBackend, VehicleState

STATE_SUBSCRIBERS = ("ChassisSensors", "GNSS", "Perception", "BodyHMI")


def make_backend(name: str, scenario: dict) -> VehicleBackend:
    if name == "sim2d":
        from canav.vehicle.sim2d import Sim2D

        return Sim2D(scenario)
    if name == "carla":
        from canav.vehicle.carla_bridge import CarlaBackend

        return CarlaBackend(scenario)
    raise ValueError(f"unknown backend {name!r}")


class DrivingMetrics:
    """Collisions, time to goal, lateral error and closest in-path approach per object."""

    def __init__(self, length: float, width: float, rear_to_center: float):
        self.length, self.width, self.r2c = length, width, rear_to_center
        self.collided: set[int] = set()
        self.contact: set[int] = set()
        self.collision_events = 0
        self.lat_sq = 0.0
        self.lat_n = 0
        self.max_lat = 0.0
        self.time_to_goal: float | None = None
        self.min_inpath_gap: dict[int, float] = {}

    def update(self, st: VehicleState) -> None:
        now = set(st.collisions)
        self.collision_events += len(now - self.contact)
        self.contact = now
        self.collided |= now
        if st.speed > 0.5:
            self.lat_sq += st.lane_offset**2
            self.lat_n += 1
            self.max_lat = max(self.max_lat, abs(st.lane_offset))
        if st.goal_reached and self.time_to_goal is None:
            self.time_to_goal = st.t_sim
        c, s = math.cos(st.yaw), math.sin(st.yaw)
        cx, cy = st.x + self.r2c * c, st.y + self.r2c * s
        for o in st.objects:
            dx, dy = o["x"] - cx, o["y"] - cy
            ex, ey = c * dx + s * dy, -s * dx + c * dy
            if ex > 0 and abs(ey) < self.width / 2 + o["width"] / 2:
                gap = ex - self.length / 2 - o["length"] / 2
                oid = int(o["id"])
                self.min_inpath_gap[oid] = min(self.min_inpath_gap.get(oid, math.inf), gap)

    def summary(self, st: VehicleState) -> dict:
        return {
            "goal_reached": st.goal_reached,
            "time_to_goal_s": self.time_to_goal,
            "t_sim_end_s": st.t_sim,
            "s_end_m": st.s,
            "collisions": self.collision_events,
            "collided_object_ids": sorted(self.collided),
            "lateral_rms_m": math.sqrt(self.lat_sq / self.lat_n) if self.lat_n else None,
            "lateral_max_m": self.max_lat,
            "min_inpath_gap_m": {str(k): v for k, v in self.min_inpath_gap.items()},
        }


class Bridge:
    def __init__(
        self,
        backend: VehicleBackend,
        scenario: dict,
        out_dir: str | Path,
        nodes: list[str],
        port_base: int,
        duration_s: float,
        stop_on_goal: bool = True,
        goal_hold_s: float = 1.0,
    ):
        self.backend = backend
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.dt = float(scenario.get("dt", 0.01))
        self.ports = [state_port(nodes.index(n), port_base) for n in STATE_SUBSCRIBERS]
        self.ctrl_port = ctrl_port(port_base)
        self.duration_s = duration_s
        self.stop_on_goal = stop_on_goal
        self.goal_hold_s = goal_hold_s
        veh = scenario["vehicle"]
        self.metrics = DrivingMetrics(veh["length"], veh["width"], veh["rear_to_center"])
        self.overruns = 0
        backend.route().to_json(self.out / "route.json")
        self.ctrl_rx = UdpLatest(self.ctrl_port)  # bind before ECUs start sending

    def run(self, stop: threading.Event) -> dict:
        sender = UdpSender()
        control = Control()
        trace = open(self.out / "bridge.jsonl", "w", buffering=1 << 16)  # noqa: SIM115
        dt_ns = round(self.dt * 1e9)
        start = time.monotonic_ns()
        k = 0
        t_goal: float | None = None
        st = None
        try:
            while not stop.is_set():
                deadline = start + k * dt_ns
                now = time.monotonic_ns()
                if deadline > now:
                    time.sleep((deadline - now) / 1e9)
                elif now - deadline > dt_ns:
                    self.overruns += 1
                for msg in self.ctrl_rx.poll():
                    for key in ("steer", "throttle", "brake"):
                        if msg.get(key) is not None:
                            setattr(control, key, float(msg[key]))
                st = self.backend.step(control)
                state = state_message(k, st)
                for port in self.ports:
                    sender.send(port, state)
                self.metrics.update(st)
                trace.write(
                    json.dumps(
                        {
                            "t_ns": time.monotonic_ns(),
                            "t_sim": round(st.t_sim, 3),
                            "x": round(st.x, 3),
                            "y": round(st.y, 3),
                            "yaw": round(st.yaw, 4),
                            "v": round(st.speed, 3),
                            "s": round(st.s, 3),
                            "lat": round(st.lane_offset, 3),
                            "steer": round(control.steer, 4),
                            "throttle": round(control.throttle, 4),
                            "brake": round(control.brake, 4),
                            "collisions": st.collisions,
                            "tl": st.tl["state"],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                k += 1
                if st.t_sim >= self.duration_s:
                    break
                if st.goal_reached and self.stop_on_goal:
                    t_goal = st.t_sim if t_goal is None else t_goal
                    if st.t_sim - t_goal >= self.goal_hold_s:
                        break
        finally:
            trace.close()
            sender.close()
            self.ctrl_rx.close()
            self.backend.close()
        summary = self.metrics.summary(st) if st is not None else {}
        summary["bridge_ticks"] = k
        summary["bridge_overruns"] = self.overruns
        (self.out / "driving.json").write_text(json.dumps(summary, indent=2))
        return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", default="sim2d")
    p.add_argument("--scenario", required=True)
    p.add_argument("--params", required=True)
    p.add_argument("--dbc", default="config/dbc/canav.dbc")
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, default=120.0)
    p.add_argument("--no-stop-on-goal", action="store_true")
    a = p.parse_args(argv)
    from canav.ecu import load_dbc

    scenario = yaml.safe_load(open(a.scenario))
    params = yaml.safe_load(open(a.params))
    nodes = [n.name for n in load_dbc(a.dbc).nodes]
    bridge = Bridge(
        make_backend(a.backend, scenario),
        scenario,
        a.out,
        nodes,
        params["sidechannel"]["port_base"],
        a.duration,
        not a.no_stop_on_goal,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    bridge.run(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
