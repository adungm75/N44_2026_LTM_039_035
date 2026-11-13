#!/usr/bin/env python3
"""Online experiments of Phase 6 (USER: vcan0 up): sim2d stack on vcan0, processes + gateway.

- ``E0``: baseline, until the goal (driving metrics, 0 CRC/counter errors).
- ``E7_drop5_control``: Control drops 5 % of its frames (§5.10 E7 online).
- ``E7_silent_control``: Control goes silent at 20 s; actuators must enter the safe state
  after the watchdog limit (§5.12.4).
- ``--carla``: also ``E0_carla`` (needs a running CARLA server).

Results: ``results/online_exp/<name>/`` (skipped if ``metrics.json`` exists, ``--force``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from canav.ecu import FaultConfig
from canav.experiments.online import DEFAULTS, run_stack

RUNS = {
    "E0": {"duration": 120.0, "faults": {}},
    "E7_drop5_control": {"duration": 120.0, "faults": {"Control": "drop_prob=0.05"}},
    "E7_silent_control": {
        "duration": 40.0,
        "faults": {"Control": "silent_after_s=20"},
        "stop_on_goal": False,
    },
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", default="results/online_exp")
    p.add_argument("--iface", default="vcan0")
    p.add_argument("--carla", action="store_true")
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    runs = {k: v | {"backend": "sim2d"} for k, v in RUNS.items()}
    if a.carla:
        runs["E0_carla"] = {"duration": 300.0, "faults": {}, "backend": "carla"}
    for name, r in runs.items():
        out = Path(a.out) / name
        if (out / "metrics.json").exists() and not a.force:
            print(f"{name}: cached ({out})")
            continue
        scenario, params = DEFAULTS[r["backend"]]
        print(f"{name}: running {r['backend']} on {a.iface} -> {out}", flush=True)
        m = run_stack(
            out,
            backend=r["backend"],
            iface=a.iface,
            duration=r["duration"],
            scenario=scenario,
            params=params,
            faults={k: FaultConfig.parse(v) for k, v in r["faults"].items()},
            stop_on_goal=r.get("stop_on_goal", True),
        )
        d = m["driving"]
        print(
            json.dumps(
                {
                    "goal_reached": d.get("goal_reached"),
                    "collisions": d.get("collisions"),
                    "errors": m["errors"],
                    "aeb": m["aeb"],
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
