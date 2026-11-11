"""CARLA smoke test: can this machine run CARLA fast enough for the project?

Runs CARLA in synchronous mode (fixed_delta_seconds = 0.05) with an ego vehicle,
a 16-channel LiDAR, an optional 640x360 RGB camera and NPC vehicles on autopilot.
Every 5 s it logs ticks/s, mean tick time, system RAM and GPU memory/utilization,
then writes ``results/smoke/report.json``.

PASS if no crash and mean ticks/s >= 20 without camera (>= 15 with camera).

Example::

    python scripts/carla_smoke_test.py --map Town02 --duration 600 --no-camera --npc 10
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

FIXED_DT = 0.05
SAMPLE_PERIOD_S = 5.0
PASS_TPS_NO_CAMERA = 20.0
PASS_TPS_CAMERA = 15.0


def read_ram_mb() -> dict[str, float]:
    """Return total and used system RAM in MB from /proc/meminfo."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])  # kB
    total = info["MemTotal"] / 1024
    used = (info["MemTotal"] - info["MemAvailable"]) / 1024
    return {"ram_total_mb": total, "ram_used_mb": used}


def read_gpu() -> dict[str, float | None]:
    """Return GPU memory used (MB) and utilization (%) via nvidia-smi, or None."""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        mem, util = (part.strip().split()[0] for part in out.splitlines()[0].split(","))
        return {"vram_used_mb": float(mem), "gpu_util_pct": float(util)}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return {"vram_used_mb": None, "gpu_util_pct": None}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=2000)
    p.add_argument("--tm-port", type=int, default=8000)
    p.add_argument("--map", default="Town02")
    p.add_argument("--duration", type=float, default=600.0, help="wall-clock seconds")
    p.add_argument("--camera", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--npc", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path("results/smoke/report.json"))
    return p.parse_args()


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    """Run the smoke test, appending samples to ``report``."""
    import carla  # imported here so --help works without the carla wheel

    random.seed(args.seed)
    client = carla.Client(args.host, args.port)
    client.set_timeout(30.0)
    report["server_version"] = client.get_server_version()
    report["client_version"] = client.get_client_version()

    world = client.get_world()
    if not world.get_map().name.endswith(args.map):
        world = client.load_world(args.map)

    original_settings = world.get_settings()
    settings = world.get_settings()
    settings.synchronous_mode = True
    settings.fixed_delta_seconds = FIXED_DT
    world.apply_settings(settings)

    tm = client.get_trafficmanager(args.tm_port)
    tm.set_synchronous_mode(True)
    tm.set_random_device_seed(args.seed)

    actors: list[Any] = []
    try:
        bp_lib = world.get_blueprint_library()
        spawn_points = world.get_map().get_spawn_points()
        random.shuffle(spawn_points)

        ego_bp = bp_lib.find("vehicle.tesla.model3")
        ego_bp.set_attribute("role_name", "hero")
        ego = world.spawn_actor(ego_bp, spawn_points[0])
        actors.append(ego)
        ego.set_autopilot(True, tm.get_port())

        lidar_bp = bp_lib.find("sensor.lidar.ray_cast")
        lidar_bp.set_attribute("channels", "16")
        lidar_bp.set_attribute("range", "50")
        lidar_bp.set_attribute("rotation_frequency", str(1.0 / FIXED_DT))
        lidar_bp.set_attribute("points_per_second", "100000")
        lidar = world.spawn_actor(
            lidar_bp, carla.Transform(carla.Location(x=0.0, z=2.4)), attach_to=ego
        )
        actors.append(lidar)
        lidar.listen(lambda _data: None)

        if args.camera:
            cam_bp = bp_lib.find("sensor.camera.rgb")
            cam_bp.set_attribute("image_size_x", "640")
            cam_bp.set_attribute("image_size_y", "360")
            cam = world.spawn_actor(
                cam_bp, carla.Transform(carla.Location(x=1.5, z=2.0)), attach_to=ego
            )
            actors.append(cam)
            cam.listen(lambda _data: None)

        vehicle_bps = bp_lib.filter("vehicle.*")
        report["npc_spawned"] = 0
        for sp in spawn_points[1 : 1 + args.npc]:
            npc = world.try_spawn_actor(random.choice(vehicle_bps), sp)
            if npc is not None:
                npc.set_autopilot(True, tm.get_port())
                actors.append(npc)
                report["npc_spawned"] += 1

        t_start = time.monotonic()
        t_window = t_start
        ticks_window = 0
        tick_time_sum = 0.0
        while time.monotonic() - t_start < args.duration:
            t0 = time.monotonic()
            world.tick()
            tick_time_sum += time.monotonic() - t0
            ticks_window += 1
            report["ticks_total"] += 1

            now = time.monotonic()
            if now - t_window >= SAMPLE_PERIOD_S:
                sample = {
                    "t_s": round(now - t_start, 3),
                    "ticks_per_s": ticks_window / (now - t_window),
                    "tick_time_mean_ms": 1000.0 * tick_time_sum / ticks_window,
                    **read_ram_mb(),
                    **read_gpu(),
                }
                report["samples"].append(sample)
                print(json.dumps(sample), flush=True)
                t_window, ticks_window, tick_time_sum = now, 0, 0.0
        report["wall_s"] = time.monotonic() - t_start
    finally:
        for actor in actors:
            if actor.type_id.startswith("sensor.") and actor.is_listening:
                actor.stop()
        client.apply_batch([carla.command.DestroyActor(a) for a in reversed(actors)])
        tm.set_synchronous_mode(False)
        world.apply_settings(original_settings)


def main() -> int:
    args = parse_args()
    threshold = PASS_TPS_CAMERA if args.camera else PASS_TPS_NO_CAMERA
    report: dict[str, Any] = {
        "args": {k: str(v) for k, v in vars(args).items()},
        "fixed_delta_seconds": FIXED_DT,
        "threshold_ticks_per_s": threshold,
        "ticks_total": 0,
        "samples": [],
        "crashed": False,
        "error": None,
    }
    try:
        run(args, report)
    except Exception:  # noqa: BLE001 - any failure counts as a crash for the smoke test
        report["crashed"] = True
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)

    wall = report.get("wall_s")
    mean_tps = report["ticks_total"] / wall if wall else 0.0
    report["mean_ticks_per_s"] = mean_tps
    report["min_window_ticks_per_s"] = min(
        (s["ticks_per_s"] for s in report["samples"]), default=None
    )
    report["pass"] = (not report["crashed"]) and mean_tps >= threshold

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    verdict = "PASS" if report["pass"] else "FAIL"
    print(f"{verdict}: mean {mean_tps:.1f} ticks/s (threshold {threshold}) -> {args.out}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
