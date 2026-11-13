#!/usr/bin/env python3
"""Launch the online stack: vehicle bridge + ECUs (+ ``candump -L`` if available).

    python scripts/launch_stack.py --backend sim2d --iface vcan0 --duration 120 \\
        --out results/online/<ts>

``--iface vcan0``: bridge and each ECU are separate processes on SocketCAN.
``--iface virtual``: everything runs as threads in this process (python-can virtual).
Ctrl+C stops all children cleanly. Faults: ``--fault Control:drop_prob=0.05`` (repeatable).
Process mode also starts the raw-socket gateway (``--no-gateway`` to skip) and, with
``--monitor``, ``canav.monitor`` into ``<out>/monitor``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from canav.ecu import FaultConfig
from canav.experiments.online import DEFAULT_GATEWAY, DEFAULTS, run_stack


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--backend", default="sim2d", choices=["sim2d", "carla"])
    p.add_argument("--iface", default="vcan0", help="SocketCAN interface or 'virtual'")
    p.add_argument("--duration", type=float, default=120.0, help="max simulated seconds")
    p.add_argument("--out", default=None, help="default: results/online/<timestamp>")
    p.add_argument("--scenario", default=None, help="default: per backend (experiments.online)")
    p.add_argument("--params", default=None, help="default: per backend (experiments.online)")
    p.add_argument("--fault", action="append", default=[], metavar="ECU:k=v,...")
    p.add_argument("--no-stop-on-goal", action="store_true")
    p.add_argument("--no-gateway", action="store_true", help="do not start the gateway process")
    p.add_argument("--monitor", action="store_true", help="also run canav.monitor -> out/monitor")
    a = p.parse_args(argv)

    out = Path(a.out or f"results/online/{time.strftime('%Y%m%d-%H%M%S')}")
    scenario = a.scenario or DEFAULTS[a.backend][0]
    params = a.params or DEFAULTS[a.backend][1]
    faults = {}
    for item in a.fault:
        ecu, _, spec = item.partition(":")
        faults[ecu] = FaultConfig.parse(spec)
    print(f"running {a.backend} on {a.iface} for up to {a.duration:.0f} s -> {out}")
    m = run_stack(
        out,
        backend=a.backend,
        iface=a.iface,
        duration=a.duration,
        scenario=scenario,
        params=params,
        faults=faults,
        stop_on_goal=not a.no_stop_on_goal,
        gateway=None if a.no_gateway else DEFAULT_GATEWAY,
        monitor=a.monitor,
    )
    d, bus, hop = m["driving"], m["bus"], m["hop_latency"]
    print(
        json.dumps(
            {
                "goal_reached": d.get("goal_reached"),
                "time_to_goal_s": d.get("time_to_goal_s"),
                "collisions": d.get("collisions"),
                "lateral_rms_m": d.get("lateral_rms_m"),
                "aeb": m["aeb"],
                "errors": m["errors"],
                "bus_load_actual": bus.get("load_actual"),
                "bus_load_worst": bus.get("load_worst"),
                "hop_p50_us": None if hop["hop_p50"] is None else hop["hop_p50"] * 1e6,
                "hop_p99_us": None if hop["hop_p99"] is None else hop["hop_p99"] * 1e6,
            },
            indent=2,
        )
    )
    print(f"metrics: {out / 'metrics.json'}")
    ok = d.get("goal_reached") and d.get("collisions") == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
