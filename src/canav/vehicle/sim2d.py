"""sim2d backend (P0): kinematic bicycle on an L-shaped road with a parked car, a crossing
pedestrian and one traffic light. Headless; ``dt`` = 10 ms by default.
"""

from __future__ import annotations

import math

from canav.vehicle import (
    CLS_CAR,
    CLS_PEDESTRIAN,
    Control,
    Route,
    VehicleBackend,
    VehicleState,
)
from canav.vehicle.route import wrap_angle


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


class Sim2D(VehicleBackend):
    def __init__(self, scenario: dict):
        self.reset(scenario)

    # ----------------------------------------------------------------- setup
    def reset(self, scenario: dict) -> VehicleState:
        self.sc = scenario
        self.dt = float(scenario.get("dt", 0.01))
        veh = scenario["vehicle"]
        self.wheelbase = float(veh["wheelbase"])
        self.max_steer = float(veh["max_steer"])
        self.a_max = float(veh["a_max"])
        self.b_max = float(veh["b_max"])
        self.roll_decel = float(veh.get("roll_decel", 0.0))
        self.length = float(veh["length"])
        self.width = float(veh["width"])
        self.rear_to_center = float(veh["rear_to_center"])
        road = scenario["road"]
        self._route = Route.from_segments(
            tuple(road["start"]), road["segments"], road.get("step", 0.5)
        )
        self.goal = self._route.point_at(self._route.length)
        self.goal_radius = float(scenario.get("goal_radius", 2.0))

        self.t = 0.0
        self.x, self.y = self._route.point_at(0.0)
        self.yaw = self._route.heading_at(0.0)
        self.v = 0.0
        self.ax = self.yaw_rate = 0.0
        self.ped_t0: float | None = None
        self.goal_reached = False
        return self._state([])

    def route(self) -> Route:
        return self._route

    # ------------------------------------------------------------- dynamics
    def step(self, control: Control) -> VehicleState:
        dt = self.dt
        delta = _clamp(control.steer, -1.0, 1.0) * self.max_steer
        a = _clamp(control.throttle, 0.0, 1.0) * self.a_max
        a -= _clamp(control.brake, 0.0, 1.0) * self.b_max
        if self.v > 0:
            a -= self.roll_decel
        v_new = max(0.0, self.v + a * dt)
        v_mid = 0.5 * (self.v + v_new)
        self.yaw_rate = v_mid / self.wheelbase * math.tan(delta)
        self.x += v_mid * math.cos(self.yaw) * dt
        self.y += v_mid * math.sin(self.yaw) * dt
        self.yaw = wrap_angle(self.yaw + self.yaw_rate * dt)
        self.ax = (v_new - self.v) / dt
        self.v = v_new
        self.t += dt
        objects = self._objects()
        state = self._state(objects)
        if math.hypot(self.x - self.goal[0], self.y - self.goal[1]) <= self.goal_radius:
            self.goal_reached = True
        state.goal_reached = self.goal_reached
        return state

    # -------------------------------------------------------------- scenario
    def _front_s(self) -> float:
        s, _ = self._route.project(self.x, self.y)
        return s + self.rear_to_center + self.length / 2

    def _at(self, s: float, lat: float) -> tuple[float, float, float]:
        px, py = self._route.point_at(s)
        h = self._route.heading_at(s)
        return px - lat * math.sin(h), py + lat * math.cos(h), h

    def _objects(self) -> list[dict]:
        objs = []
        car = self.sc.get("parked_car")
        if car:
            x, y, h = self._at(car["s"], car["lat"])
            objs.append(
                dict(
                    id=car.get("id", 1),
                    cls=CLS_CAR,
                    x=x,
                    y=y,
                    vx=0.0,
                    vy=0.0,
                    yaw=h,
                    length=car["length"],
                    width=car["width"],
                )
            )
        ped = self.sc.get("pedestrian")
        if ped:
            if self.ped_t0 is None:
                start_time = ped.get("start_time")
                if start_time is not None:
                    if self.t >= start_time:
                        self.ped_t0 = self.t
                elif ped["s"] - self._front_s() <= ped["trigger_distance"]:
                    self.ped_t0 = self.t
            if self.ped_t0 is not None:
                lat0, lat1, speed = ped["lat_start"], ped["lat_end"], ped["speed"]
                direction = 1.0 if lat1 >= lat0 else -1.0
                walked = speed * (self.t - self.ped_t0)
                lat = lat0 + direction * min(walked, abs(lat1 - lat0))
                moving = walked < abs(lat1 - lat0)
                x, y, h = self._at(ped["s"], lat)
                nx, ny = -math.sin(h), math.cos(h)
                v = direction * speed if moving else 0.0
                d = 2 * ped["radius"]
                objs.append(
                    dict(
                        id=ped.get("id", 2),
                        cls=CLS_PEDESTRIAN,
                        x=x,
                        y=y,
                        vx=v * nx,
                        vy=v * ny,
                        yaw=h,
                        length=d,
                        width=d,
                    )
                )
        return objs

    def _tl(self) -> dict:
        tl = self.sc.get("traffic_light")
        if not tl:
            return {"state": "none", "dist_stop": 0.0}
        dist = tl["s_stop"] - self._front_s()
        if dist < 0 or dist > tl.get("visible_range", 60.0):
            return {"state": "none", "dist_stop": 0.0}
        state = "green"
        for phase, until in tl["schedule"]:
            state = phase
            if until is None or self.t < until:
                break
        return {"state": state, "dist_stop": dist}

    def _collisions(self, objects: list[dict]) -> list[int]:
        cx = self.x + self.rear_to_center * math.cos(self.yaw)
        cy = self.y + self.rear_to_center * math.sin(self.yaw)
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        hits = []
        for o in objects:
            dx, dy = o["x"] - cx, o["y"] - cy
            ex, ey = c * dx + s * dy, -s * dx + c * dy
            th = o["yaw"] - self.yaw
            hx = abs(math.cos(th)) * o["length"] / 2 + abs(math.sin(th)) * o["width"] / 2
            hy = abs(math.sin(th)) * o["length"] / 2 + abs(math.cos(th)) * o["width"] / 2
            if abs(ex) < self.length / 2 + hx and abs(ey) < self.width / 2 + hy:
                hits.append(int(o["id"]))
        return hits

    def _state(self, objects: list[dict]) -> VehicleState:
        s, lat = self._route.project(self.x, self.y)
        return VehicleState(
            t_sim=self.t,
            x=self.x,
            y=self.y,
            yaw=self.yaw,
            speed=self.v,
            ax=self.ax,
            ay=self.v * self.yaw_rate,
            yaw_rate=self.yaw_rate,
            s=s,
            lane_offset=lat,
            lane_heading_err=wrap_angle(self.yaw - self._route.heading_at(s)),
            objects=objects,
            tl=self._tl(),
            collisions=self._collisions(objects),
            goal_reached=self.goal_reached,
        )
