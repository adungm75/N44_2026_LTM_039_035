"""CARLA backend (P1, §5.7): Town02, synchronous mode + Traffic Manager sync,
NPC vehicles and walkers, ``GlobalRoutePlanner`` route, ground truth from ``world.get_actors()``.

CARLA is left-handed (x forward, y right, yaw clockwise in degrees, steer > 0 = right).
Everything leaving this module is in the project frame (right-handed, y left, yaw CCW in rad,
steer > 0 = left): ``y -> -y``, ``yaw -> -yaw``, ``steer -> -steer``.

``carla`` is imported lazily so the pure helpers below can be tested without it.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from pathlib import Path

from canav.vehicle import (
    CLS_CAR,
    CLS_PEDESTRIAN,
    Control,
    Route,
    VehicleBackend,
    VehicleState,
)
from canav.vehicle.route import wrap_angle

TL_MAP = {"Red": "red", "Yellow": "yellow", "Green": "green"}  # Off/Unknown -> none


# ------------------------------------------------------------------ pure frame helpers
def to_xy(x: float, y: float) -> tuple[float, float]:
    """CARLA world (x, y) -> project frame."""
    return x, -y


def to_yaw(yaw_deg: float) -> float:
    """CARLA yaw [deg, clockwise] -> project yaw [rad, CCW]."""
    return wrap_angle(-math.radians(yaw_deg))


def to_carla_steer(steer: float) -> float:
    return -min(max(steer, -1.0), 1.0)


def route_points(points: list[tuple[float, float]], min_step: float = 1e-3) -> list:
    """Drop consecutive duplicates (``Route`` rejects zero-length segments)."""
    out = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > min_step:
            out.append(p)
    return out


def stops_on_route(
    route: Route,
    stops: list[tuple[object, float, float, float]],
    max_lat: float = 1.5,
    max_heading: float = math.radians(30),
) -> list[tuple[float, object]]:
    """Stop lines on the route: ``stops`` = (key, x, y, yaw) in the project frame.

    A stop line counts if it lies on the route (|lat| <= ``max_lat``) and faces the driving
    direction. Returns sorted (s, key).
    """
    out = []
    for key, x, y, yaw in stops:
        s, lat = route.project(x, y)
        if abs(lat) > max_lat or not 0.0 < s < route.length:  # off the route or beyond its ends
            continue
        if abs(wrap_angle(yaw - route.heading_at(s))) > max_heading:
            continue
        out.append((s, key))
    return sorted(out, key=lambda t: t[0])


def next_stop(stops: list[tuple[float, object]], front_s: float, visible_range: float):
    """First stop line ahead of the front bumper within range: (dist, key) or None."""
    for s, key in stops:
        if s >= front_s:
            return (s - front_s, key) if s - front_s <= visible_range else None
    return None


# ------------------------------------------------------------------------------ backend
class CarlaBackend(VehicleBackend):
    def __init__(self, scenario: dict):
        self._actor_ids: list[int] = []
        self._walker_ctrls: list = []
        self._sensor = None
        self._orig_settings = None
        self._tm = None
        self._closed = False
        try:
            self.reset(scenario)
        except BaseException:
            self.close()
            raise

    # ----------------------------------------------------------------- setup
    def reset(self, scenario: dict) -> VehicleState:
        import carla

        sc = self.sc = scenario
        self.carla = carla
        root = Path(os.path.expanduser(os.environ.get("CARLA_ROOT", sc["carla_root"])))
        sys.path.insert(0, str(root / "PythonAPI" / "carla"))  # agents.navigation
        from agents.navigation.global_route_planner import GlobalRoutePlanner

        self.dt = float(sc["dt"])
        seed = int(sc.get("seed", 0))
        rng = random.Random(seed)
        self.client = carla.Client(sc.get("host", "localhost"), int(sc.get("port", 2000)))
        self.client.set_timeout(float(sc.get("timeout", 60.0)))
        world = self.client.get_world()
        if not world.get_map().name.endswith(sc["map"]):
            world = self.client.load_world(sc["map"])
        self.world = world
        self._orig_settings = world.get_settings()
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = self.dt
        world.apply_settings(settings)
        self._tm = self.client.get_trafficmanager(int(sc.get("tm_port", 8000)))
        self._tm.set_synchronous_mode(True)
        self._tm.set_random_device_seed(seed)
        world.set_pedestrians_seed(seed)
        world.set_pedestrians_cross_factor(float(sc["walkers"]["cross_factor"]))
        cmap = world.get_map()
        spawns = cmap.get_spawn_points()
        bl = world.get_blueprint_library()

        # ego + route
        start, goal = spawns[sc["start_spawn"]], spawns[sc["goal_spawn"]]
        bp = bl.find(sc["ego_blueprint"])
        bp.set_attribute("role_name", "hero")
        self.ego = world.spawn_actor(bp, start)
        self._actor_ids.append(self.ego.id)
        grp = GlobalRoutePlanner(cmap, float(sc.get("route_resolution", 1.0)))
        trace = grp.trace_route(start.location, goal.location)
        pts = [to_xy(wp.transform.location.x, wp.transform.location.y) for wp, _ in trace]
        self._route = Route(route_points(pts))
        self.goal = tuple(self._route.points[-1])
        self.goal_radius = float(sc.get("goal_radius", 2.0))
        stops = []
        for tl in world.get_actors().filter("traffic.traffic_light"):
            for wp in tl.get_stop_waypoints():
                x, y = to_xy(wp.transform.location.x, wp.transform.location.y)
                stops.append((tl, x, y, to_yaw(wp.transform.rotation.yaw)))
        self._stops = stops_on_route(self._route, stops)
        self.visible_range = float(sc.get("tl_visible_range", 60.0))
        self.object_range = float(sc.get("object_range", 60.0))

        self._spawn_npcs(sc, spawns, bl, rng, start)
        self._spawn_walkers(sc, bl, rng)
        self._hits: list[int] = []
        col_bp = bl.find("sensor.other.collision")
        self._sensor = world.spawn_actor(col_bp, carla.Transform(), attach_to=self.ego)
        self._actor_ids.append(self._sensor.id)
        self._sensor.listen(self._on_collision)
        if sc.get("recorder"):
            self.client.start_recorder(str(sc["recorder"]))
        for _ in range(int(sc.get("settle_ticks", 10))):
            world.tick()
        for c in self._walker_ctrls:
            c.start()
            c.go_to_location(world.get_random_location_from_navigation())
            c.set_max_speed(float(sc["walkers"]["speed"]))
        world.tick()

        self._ids: dict[int, int] = {}  # CARLA actor id -> small object id (ObjectList.obj_id u8)
        self._others = [
            a
            for a in list(world.get_actors().filter("vehicle.*"))
            + list(world.get_actors().filter("walker.pedestrian.*"))
            if a.id != self.ego.id
        ]
        n_walkers = sum(a.type_id.startswith("walker.") for a in self._others)
        print(
            f"carla: route {self._route.length:.1f} m, {len(self._stops)} stop lines, "
            f"{len(self._others) - n_walkers} vehicles, {n_walkers} walkers",
            flush=True,
        )
        self._measure_vehicle()
        self.t0 = world.get_snapshot().timestamp.elapsed_seconds
        self.t = 0.0
        self.goal_reached = False
        return self._state()

    def _spawn_npcs(self, sc, spawns, bl, rng, start) -> None:
        carla = self.carla
        n = int(sc["npc_vehicles"]["count"])
        min_d = float(sc["npc_vehicles"]["min_distance_to_ego"])
        bps = [b for b in bl.filter("vehicle.*") if b.get_attribute("base_type").as_str() == "car"]
        excl = {sc["start_spawn"], sc["goal_spawn"]}
        free = [
            tf
            for i, tf in enumerate(spawns)
            if i not in excl and tf.location.distance(start.location) > min_d
        ]
        port = self._tm.get_port()
        cmds = []
        for tf in rng.sample(free, min(n, len(free))):
            b = rng.choice(bps)
            b.set_attribute("role_name", "npc")
            cmds.append(
                carla.command.SpawnActor(b, tf).then(
                    carla.command.SetAutopilot(carla.command.FutureActor, True, port)
                )
            )
        for r in self.client.apply_batch_sync(cmds, True):
            if not r.error:
                self._actor_ids.append(r.actor_id)

    def _spawn_walkers(self, sc, bl, rng) -> None:
        carla = self.carla
        n = int(sc["walkers"]["count"])
        bps = list(bl.filter("walker.pedestrian.*"))
        walkers: list[int] = []
        for _ in range(5):  # random navigation points often collide: retry the missing ones
            cmds = []
            for _ in range(n - len(walkers)):
                loc = self.world.get_random_location_from_navigation()
                if loc is None:
                    continue
                b = rng.choice(bps)
                if b.has_attribute("is_invincible"):
                    b.set_attribute("is_invincible", "false")
                cmds.append(carla.command.SpawnActor(b, carla.Transform(loc)))
            res = self.client.apply_batch_sync(cmds, True)
            walkers += [r.actor_id for r in res if not r.error]
            if len(walkers) >= n:
                break
        self._actor_ids += walkers
        cbp = bl.find("controller.ai.walker")
        cmds = [carla.command.SpawnActor(cbp, carla.Transform(), w) for w in walkers]
        ctrl_ids = [r.actor_id for r in self.client.apply_batch_sync(cmds, True) if not r.error]
        self._actor_ids += ctrl_ids
        self._walker_ctrls = list(self.world.get_actors(ctrl_ids))

    def _measure_vehicle(self) -> None:
        """Ego geometry from CARLA physics (wheel positions in cm, world frame)."""
        pc = self.ego.get_physics_control()
        tf = self.ego.get_transform()
        f = tf.get_forward_vector()
        r = tf.get_right_vector()
        loc = tf.location

        def along(w, axis):
            p = w.position / 100.0
            return (p.x - loc.x) * axis.x + (p.y - loc.y) * axis.y

        front = [w for w in pc.wheels if w.max_steer_angle > 0]
        rear = [w for w in pc.wheels if w.max_steer_angle == 0]
        x_front = sum(along(w, f) for w in front) / len(front)
        x_rear = sum(along(w, f) for w in rear) / len(rear)
        bb = self.ego.bounding_box
        self.rear_offset = -x_rear  # actor origin -> rear axle, backwards [m]
        self.front_offset = self.rear_offset + bb.location.x + bb.extent.x
        self.measured = {
            "blueprint": self.ego.type_id,
            "wheelbase": x_front - x_rear,
            "track": abs(along(rear[0], r) - along(rear[1], r)),
            "max_steer": math.radians(max(w.max_steer_angle for w in front)),
            "length": 2 * bb.extent.x,
            "width": 2 * bb.extent.y,
            "rear_to_center": self.rear_offset + bb.location.x,
            "front_offset": self.front_offset,
        }
        print("carla vehicle (measured):", json.dumps(self.measured), flush=True)
        veh = self.sc["vehicle"]
        for k in ("length", "width", "rear_to_center"):
            if abs(veh[k] - self.measured[k]) > 0.05:
                print(f"WARNING scenario vehicle.{k}={veh[k]} != measured {self.measured[k]:.3f}")

    def _on_collision(self, event) -> None:
        other = event.other_actor
        if other.id not in self._hits:
            print(f"carla collision t_sim={self.t:.2f} with {other.type_id} ({other.id})")
        self._hits.append(other.id)

    def route(self) -> Route:
        return self._route

    # ------------------------------------------------------------------ step
    def step(self, control: Control) -> VehicleState:
        c = self.carla.VehicleControl(
            throttle=min(max(control.throttle, 0.0), 1.0),
            steer=to_carla_steer(control.steer),
            brake=min(max(control.brake, 0.0), 1.0),
        )
        self.ego.apply_control(c)
        self.world.tick()
        self.t = self.world.get_snapshot().timestamp.elapsed_seconds - self.t0
        return self._state()

    def _oid(self, actor_id: int) -> int:
        if actor_id not in self._ids:
            self._ids[actor_id] = min(len(self._ids) + 1, 255)
        return self._ids[actor_id]

    def _state(self) -> VehicleState:
        tf = self.ego.get_transform()
        yaw = to_yaw(tf.rotation.yaw)
        cy, sy = math.cos(yaw), math.sin(yaw)
        lx, ly = to_xy(tf.location.x, tf.location.y)
        x, y = lx - self.rear_offset * cy, ly - self.rear_offset * sy  # rear axle
        v = self.ego.get_velocity()
        vx, vy = to_xy(v.x, v.y)
        a = self.ego.get_acceleration()
        ax_w, ay_w = to_xy(a.x, a.y)
        yaw_rate = -math.radians(self.ego.get_angular_velocity().z)
        speed = max(0.0, cy * vx + sy * vy)
        s, lat = self._route.project(x, y)

        objects = []
        for act in self._others:
            atf = act.get_transform()
            if atf.location.distance(tf.location) > self.object_range:
                continue
            bb = act.bounding_box
            ctr = atf.transform(bb.location)
            ox, oy = to_xy(ctr.x, ctr.y)
            av = act.get_velocity()
            ovx, ovy = to_xy(av.x, av.y)
            walker = act.type_id.startswith("walker.")
            objects.append(
                {
                    "id": self._oid(act.id),
                    "cls": CLS_PEDESTRIAN if walker else CLS_CAR,
                    "x": ox,
                    "y": oy,
                    "vx": ovx,
                    "vy": ovy,
                    "yaw": to_yaw(atf.rotation.yaw),
                    "length": 2 * bb.extent.x,
                    "width": 2 * bb.extent.y,
                }
            )

        tl = {"state": "none", "dist_stop": 0.0}
        nxt = next_stop(self._stops, s + self.front_offset, self.visible_range)
        if nxt is not None:
            dist, actor = nxt
            tl = {"state": TL_MAP.get(str(actor.get_state()), "none"), "dist_stop": dist}

        hits, self._hits = self._hits, []
        if math.hypot(x - self.goal[0], y - self.goal[1]) <= self.goal_radius:
            self.goal_reached = True
        return VehicleState(
            t_sim=self.t,
            x=x,
            y=y,
            yaw=yaw,
            speed=speed,
            ax=cy * ax_w + sy * ay_w,
            ay=-sy * ax_w + cy * ay_w,
            yaw_rate=yaw_rate,
            s=s,
            lane_offset=lat,
            lane_heading_err=wrap_angle(yaw - self._route.heading_at(s)),
            objects=objects,
            tl=tl,
            collisions=sorted({self._oid(h) for h in hits}),
            goal_reached=self.goal_reached,
        )

    # ----------------------------------------------------------------- close
    def close(self) -> None:
        """Destroy everything we spawned and give the server back in asynchronous mode."""
        if self._closed:
            return
        self._closed = True
        carla = getattr(self, "carla", None)
        if carla is None:
            return
        steps = [
            lambda: self._sensor is not None and self._sensor.stop(),
            lambda: [c.stop() for c in self._walker_ctrls],
            lambda: self.sc.get("recorder") and self.client.stop_recorder(),
            lambda: self.client.apply_batch_sync(  # reversed: sensor and controllers first
                [carla.command.DestroyActor(i) for i in reversed(self._actor_ids)], True
            ),
            lambda: self._tm is not None and self._tm.set_synchronous_mode(False),
            lambda: (
                self._orig_settings is not None and self.world.apply_settings(self._orig_settings)
            ),
        ]
        for f in steps:
            try:
                f()
            except Exception as e:  # keep going: the server must not stay in sync mode
                print(f"carla close: {e!r}", file=sys.stderr)
