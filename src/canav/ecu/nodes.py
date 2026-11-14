"""The online ECUs. Each class = one CAN node; logic per spec.

Ego frame: origin at the rear axle, x forward, y left. ObjectList.x is the longitudinal gap
from the front bumper to the near edge of the object (ground-truth perception).
"""

from __future__ import annotations

import math

from canav.ecu.base import EcuBase
from canav.sidechannel import UdpSender, ctrl_message, ctrl_port
from canav.vehicle import TL_STATES
from canav.vehicle.route import Route, wrap_angle

N_OBJECT_FRAMES = 8


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _to_ego(px: float, py: float, x: float, y: float, yaw: float) -> tuple[float, float]:
    dx, dy = px - x, py - y
    c, s = math.cos(yaw), math.sin(yaw)
    return c * dx + s * dy, -s * dx + c * dy


class ChassisSensors(EcuBase):
    """WheelSpeed + IMU from the side-channel state (sample-and-hold), 10 ms."""

    uses_state = True

    def cycle(self, t_ns: int) -> None:
        st = self.state()
        if st is None:
            return
        v, imu = st["speed"], st["imu"]
        half_track = self.veh["track"] / 2
        self.send(
            "WheelSpeed",
            {
                "speed": v,
                "v_left": max(0.0, v - imu["yaw_rate"] * half_track),
                "v_right": max(0.0, v + imu["yaw_rate"] * half_track),
            },
        )
        self.send("IMU", {"ax": imu["ax"], "ay": imu["ay"], "yaw_rate": imu["yaw_rate"]})


class GNSS(EcuBase):
    uses_state = True

    def cycle(self, t_ns: int) -> None:
        st = self.state()
        if st is None:
            return
        pose = st["pose"]
        heading = pose["yaw"] % (2 * math.pi)
        self.send("GNSS_Pose", {"x": pose["x"], "y": pose["y"], "heading": heading})


class Perception(EcuBase):
    """Objects in the corridor ahead, sorted by gap, always 8 frames; traffic light + lane."""

    uses_state = True

    def cycle(self, t_ns: int) -> None:
        st = self.state()
        if st is None:
            return
        x, y, yaw = st["pose"]["x"], st["pose"]["y"], st["pose"]["yaw"]
        front = self.veh["front_offset"]
        rng, half = self.p["range"], self.p["corridor_half_width"]
        c, s = math.cos(yaw), math.sin(yaw)
        objs = []
        for o in st["objects"]:
            ex, ey = _to_ego(o["x"], o["y"], x, y, yaw)
            th = o["yaw"] - yaw
            hx = abs(math.cos(th)) * o["length"] / 2 + abs(math.sin(th)) * o["width"] / 2
            gap = ex - front - hx
            if ex <= 0 or gap > rng or abs(ey) > half:
                continue
            rel_vx = c * o["vx"] + s * o["vy"] - st["speed"]
            objs.append((gap, ey, rel_vx, o))
        objs.sort(key=lambda t: t[0])
        for k in range(N_OBJECT_FRAMES):
            if k < len(objs):
                gap, ey, rel_vx, o = objs[k]
                vals = {
                    "valid": 1,
                    "cls": o["cls"],
                    "obj_id": o["id"],
                    "x": gap,
                    "y": ey,
                    "rel_vx": rel_vx,
                }
            else:
                vals = {"valid": 0, "cls": 0, "obj_id": 0, "x": 0, "y": 0, "rel_vx": 0}
            self.send(f"ObjectList_{k}", vals)
        tl = st["tl"]
        self.send(
            "TrafficLight_Lane",
            {
                "tl_state": TL_STATES.get(tl["state"], 0),
                "dist_stop": tl["dist_stop"],
                "lane_offset": tl["lane_offset"],
                "lane_heading_err": tl["lane_heading_err"],
            },
        )


class Planning(EcuBase):
    """Route following: pose = GNSS + dead reckoning, look-ahead points, target speed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.route = Route.from_json(self.params["route"])
        self.pose: tuple[float, float, float] | None = None
        self.speed = 0.0
        self.yaw_rate = 0.0
        self.t_dr: int | None = None
        self.objects: dict[int, dict] = {}
        self.tl: dict | None = None
        self.used: dict[str, int | None] = {}

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        self.used[msg] = counter
        if msg == "GNSS_Pose":
            self.pose = (values["x"], values["y"], wrap_angle(values["heading"]))
            self.t_dr = t_ns
        elif msg == "WheelSpeed":
            self.speed = values["speed"]
            if self.pose is not None and self.t_dr is not None:
                dt = (t_ns - self.t_dr) / 1e9
                x, y, h = self.pose
                self.pose = (
                    x + self.speed * math.cos(h) * dt,
                    y + self.speed * math.sin(h) * dt,
                    wrap_angle(h + self.yaw_rate * dt),
                )
                self.t_dr = t_ns
        elif msg == "IMU":
            self.yaw_rate = values["yaw_rate"]
        elif msg.startswith("ObjectList_"):
            self.objects[int(msg.rsplit("_", 1)[1])] = values
        elif msg == "TrafficLight_Lane":
            self.tl = values

    def _stop(self, dist: float) -> float:
        """Speed from which a stop within ``dist`` is possible at ``a_comf``, using the
        position predicted ``lead_time`` ahead (compensates the speed-loop lag)."""
        d = dist - self.speed * self.p["lead_time"]
        return math.sqrt(2 * self.p["a_comf"] * max(0.0, d))

    def target_speed(self, s: float) -> float:
        p = self.p
        v = p["v_cruise"]
        kmax = self.route.max_abs_curvature(s, s + max(10.0, 2 * self.speed))
        if kmax > 1e-6:
            v = min(v, math.sqrt(p["a_lat_max"] / kmax))
        for o in self.objects.values():
            if not o["valid"]:
                continue
            if abs(o["y"]) < p["path_half_width"]:
                v = min(v, self._stop(o["x"] - p["obstacle_stop_gap"]))
            elif o["x"] < p["pass_distance"]:
                v = min(v, p["v_pass"])
        if self.tl is not None:
            state, dist = int(self.tl["tl_state"]), self.tl["dist_stop"]
            stop_dist = self.speed**2 / (2 * p["a_comf"])
            if state == TL_STATES["red"] or (state == TL_STATES["yellow"] and dist > stop_dist):
                v = min(v, self._stop(dist - p["tl_stop_margin"]))
        remaining = self.route.length - s
        v = min(v, self._stop(remaining))
        return 0.0 if remaining < 0.3 else v

    def cycle(self, t_ns: int) -> None:
        if self.pose is None:
            return
        x, y, h = self.pose
        s, _ = self.route.project(x, y)
        pts = [_to_ego(*self.route.point_at(s + d), x, y, h) for d in self.p["lookahead"]]
        src = dict(self.used)
        self.send(
            "TargetPath_0",
            {
                "target_speed": self.target_speed(s),
                "curvature": self.route.curvature_at(s + self.p["lookahead"][0]),
                "p1_x": pts[0][0],
                "p1_y": pts[0][1],
            },
            src,
        )
        self.send(
            "TargetPath_1",
            {"p2_x": pts[1][0], "p2_y": pts[1][1], "p3_x": pts[2][0], "p3_y": pts[2][1]},
            src,
        )


class Control(EcuBase):
    """Pure Pursuit on the look-ahead points + speed PI, 10 ms."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.speed = 0.0
        self.tp: dict[str, dict] = {}
        self.used: dict[str, int | None] = {}
        self.integ = 0.0

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        self.used[msg] = counter
        if msg == "WheelSpeed":
            self.speed = values["speed"]
        elif msg.startswith("TargetPath_"):
            self.tp[msg] = values

    def cycle(self, t_ns: int) -> None:
        p, veh = self.p, self.veh
        src = dict(self.used)
        if len(self.tp) < 2:
            self.send("Steer_Cmd", {"steer": 0.0}, src)
            self.send("ThrottleBrake_Cmd", {"throttle": 0.0, "brake": p["hold_brake"]}, src)
            return
        a, b = self.tp["TargetPath_0"], self.tp["TargetPath_1"]
        pts = [(a["p1_x"], a["p1_y"]), (b["p2_x"], b["p2_y"]), (b["p3_x"], b["p3_y"])]
        ld = _clamp(p["ld_gain"] * self.speed + p["ld_min"], p["ld_min"], p["ld_max"])
        px, py = next((q for q in pts if math.hypot(*q) >= ld), pts[-1])
        d2 = px * px + py * py
        delta = math.atan(2 * veh["wheelbase"] * py / d2) if d2 > 0.25 else 0.0
        steer = _clamp(delta / veh["max_steer"], -1.0, 1.0)

        v_t = a["target_speed"]
        dt = self.period_ns / 1e9
        if v_t < 0.05 and self.speed < 0.2:
            throttle, brake, self.integ = 0.0, p["hold_brake"], 0.0
        else:
            err = v_t - self.speed
            self.integ = _clamp(self.integ + err * dt, -2.0, 2.0)
            acc = _clamp(p["kp"] * err + p["ki"] * self.integ, -p["decel_max"], veh["a_max"])
            throttle = acc / veh["a_max"] if acc > 0 else 0.0
            brake = -acc / veh["b_max"] if acc < 0 else 0.0
        self.send("Steer_Cmd", {"steer": steer}, src)
        self.send("ThrottleBrake_Cmd", {"throttle": throttle, "brake": brake}, src)


class AEB(EcuBase):
    """TTC < threshold for an in-path object -> brake request; held while still approaching."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.objects: dict[int, dict] = {}
        self.used: dict[str, int | None] = {}
        self.active = False

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        if msg.startswith("ObjectList_"):
            self.objects[int(msg.rsplit("_", 1)[1])] = values
            self.used[msg] = counter

    def cycle(self, t_ns: int) -> None:
        p = self.p
        trigger = approaching = False
        for o in self.objects.values():
            if not o["valid"] or abs(o["y"]) >= p["path_half_width"]:
                continue
            gap, rv = o["x"], o["rel_vx"]
            if gap <= 0:
                trigger = True
            elif rv < -0.1:
                approaching = True
                if gap / -rv < p["ttc_threshold"]:
                    trigger = True
        if trigger and not self.active:
            self.stats["aeb_activations"] += 1
        self.active = trigger or (self.active and approaching)
        self.send(
            "AEB_BrakeRequest",
            {"active": int(self.active), "decel_req": p["decel_req"] if self.active else 0.0},
            dict(self.used),
        )


class _Actuator(EcuBase):
    """Forwards E2E-checked commands to the bridge over UDP; watchdog -> safe state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.udp = UdpSender()
        self.port = ctrl_port(self.params["sidechannel"]["port_base"])
        self.seq = 0
        self.safe = False

    def forward(self, src_counter: int | None, **fields) -> None:
        self.udp.send(self.port, ctrl_message(self.seq, src_counter or 0, **fields))
        self.seq += 1

    def stale(self, t_rx: int | None, now: int) -> bool:
        return t_rx is None or now - t_rx > self.p["watchdog_cycles"] * self.period_ns

    def close(self) -> None:
        self.udp.close()


class SteeringActuator(_Actuator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.steer = 0.0
        self.t_rx: int | None = None
        self.counter: int | None = None

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        if msg != "Steer_Cmd":
            return
        self.steer, self.t_rx, self.counter, self.safe = values["steer"], t_ns, counter, False
        self.forward(counter, steer=self.steer)
        self.log.write(t_ns, "use", msg, counter, {msg: counter})

    def cycle(self, t_ns: int) -> None:
        if self.t_rx is not None and self.stale(self.t_rx, t_ns):
            if not self.safe:
                self.safe = True
                self.stats["safe_state"] += 1
                self.log.write(t_ns, "use", "safe_state", None)
            self.forward(self.counter, steer=self.steer)  # hold the last valid steering


class ThrottleBrakeActuator(_Actuator):
    """Applies ThrottleBrake_Cmd; an active AEB_BrakeRequest overrides (throttle 0, brake)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd: tuple[float, float, int | None, int] | None = None
        self.aeb: tuple[bool, float, int | None, int] | None = None

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        if msg == "ThrottleBrake_Cmd":
            self.cmd = (values["throttle"], values["brake"], counter, t_ns)
            self.safe = False
            self.apply(t_ns)
        elif msg == "AEB_BrakeRequest":
            self.aeb = (bool(values["active"]), values["decel_req"], counter, t_ns)
            if self.aeb[0]:
                self.apply(t_ns)

    def apply(self, t_ns: int) -> None:
        src: dict[str, int | None] = {}
        if self.cmd is not None and not self.stale(self.cmd[3], t_ns):
            throttle, brake, counter, _ = self.cmd
            src["ThrottleBrake_Cmd"] = counter
        else:
            throttle, brake, counter = 0.0, self.p["safe_brake"], None
        aeb = self.aeb
        if aeb is not None and aeb[0] and not self.stale(aeb[3], t_ns):
            throttle = 0.0
            brake = max(brake, aeb[1] / self.veh["b_max"])
            src["AEB_BrakeRequest"] = aeb[2]
        self.forward(counter, throttle=throttle, brake=min(brake, 1.0))
        self.log.write(t_ns, "use", "ThrottleBrake", counter, src)

    def cycle(self, t_ns: int) -> None:
        if self.cmd is not None and self.stale(self.cmd[3], t_ns):
            if not self.safe:
                self.safe = True
                self.stats["safe_state"] += 1
                self.log.write(t_ns, "use", "safe_state", None)
            self.apply(t_ns)


class BodyHMI(EcuBase):
    """VehicleStatus every 100 ms (mode 1 = autonomous, gear 1 = D); odometer from state.

    Diagnostics (P1): ``Diag_Request`` with ``target`` = this node's index in the DBC node list
    or 0xFF (all), service 0x22 (ReadDataByIdentifier) -> ``Diag_Response`` service 0x62 with
    DID 0x0001 = odometer [0.1 m], 0x0002 = mode; otherwise negative response service 0x7F,
    value = NRC (0x11 service not supported, 0x31 request out of range).
    """

    uses_state = True
    DIDS = {0x0001: "odometer", 0x0002: "mode"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.odometer = 0.0
        self.t_sim: float | None = None
        self.node_index = [n.name for n in self.db.nodes].index(self.name)

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        if msg != "Diag_Request" or int(values["target"]) not in (self.node_index, 0xFF):
            return
        service, did = int(values["service"]), int(values["did"])
        if service != 0x22:
            resp, value = 0x7F, 0x11
        elif did not in self.DIDS:
            resp, value = 0x7F, 0x31
        else:
            resp = 0x62
            value = round(self.odometer * 10) if did == 0x0001 else 1
        self.send(
            "Diag_Response",
            {"source": self.node_index, "service": resp, "did": did, "value": value},
            {"Diag_Request": counter},
        )

    def cycle(self, t_ns: int) -> None:
        st = self.state()
        if st is None:
            return
        if self.t_sim is not None:
            self.odometer += st["speed"] * max(0.0, st["t_sim"] - self.t_sim)
        self.t_sim = st["t_sim"]
        self.send(
            "VehicleStatus",
            {"mode": 1, "gear": 1, "lights": 0, "odometer": self.odometer, "fault_flags": 0},
        )


ECUS: dict[str, type[EcuBase]] = {
    "ChassisSensors": ChassisSensors,
    "GNSS": GNSS,
    "Perception": Perception,
    "Planning": Planning,
    "Control": Control,
    "AEB": AEB,
    "SteeringActuator": SteeringActuator,
    "ThrottleBrakeActuator": ThrottleBrakeActuator,
    "BodyHMI": BodyHMI,
}
ACTUATORS = ("SteeringActuator", "ThrottleBrakeActuator")
