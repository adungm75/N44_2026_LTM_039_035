#!/usr/bin/env python3
"""E8 online (§5.10; USER: vcan0 up, can-utils): one anomaly per run of the
sim2d stack on vcan0. The anomaly starts 20 s after the bridge wrote ``route.json`` and lasts
10 s (chosen, same as the SimPy E8); its CLOCK_REALTIME interval is written to
``results/online_exp/E8_<type>/anomalies.json`` for the window labels (E9).

- ``flood``: ``cangen <if> -I 000 -L 8 -D 0000000000000000 -g 0.2`` (§5.10);
- ``unknown_id``: ``cangen <if> -I 6A1 -L 8 -D 0000000000000000 -g 100`` (100 ms, chosen);
- ``spoof``: raw socket, Steer_Cmd 0x100 every 10 ms, frozen AliveCounter 0 with a valid CRC
  (same attacker as the SimPy E8);
- ``silent``: Perception fault ``silent_after_s=20`` (stops for the rest of the run, the
  ECUs have no "resume" fault); interval = from the first missing ObjectList_0 frame (last
  frame + 50 ms) to the end of the log.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

from canav import e2e
from canav.analysis.features import load_records
from canav.ecu import FaultConfig
from canav.experiments.online import DEFAULTS, run_stack
from canav.gateway import CAN_FRAME

TYPES = ("flood", "silent", "spoof", "unknown_id")
DELAY_S, LENGTH_S, DURATION_S = 20.0, 10.0, 45.0


def _spoof(iface: str, stop: threading.Event) -> None:
    s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    s.bind((iface,))
    data = e2e.protect(0x100, bytes(4), 0)
    nxt = time.monotonic()
    try:
        while not stop.is_set():
            s.send(CAN_FRAME.pack(0x100, 4, data.ljust(8, b"\0")))
            nxt += 0.010
            time.sleep(max(0.0, nxt - time.monotonic()))
    finally:
        s.close()


def attacker(kind: str, iface: str, out: Path, abort: threading.Event) -> None:
    while not (out / "route.json").exists():
        if abort.wait(0.05):
            return
    if abort.wait(DELAY_S):
        return
    start = time.time_ns()
    if kind in ("flood", "unknown_id"):
        cid, gap = ("000", "0.2") if kind == "flood" else ("6A1", "100")
        cmd = ["cangen", iface, "-I", cid, "-L", "8", "-D", "0000000000000000", "-g", gap]
        p = subprocess.Popen(cmd)
        abort.wait(LENGTH_S)
        p.terminate()
        p.wait(timeout=5)
    else:
        stop = threading.Event()
        th = threading.Thread(target=_spoof, args=(iface, stop))
        th.start()
        abort.wait(LENGTH_S)
        stop.set()
        th.join()
    end = time.time_ns()
    iv = [{"type": kind, "start_ns": start, "end_ns": end, "source": "attacker clock"}]
    (out / "anomalies.json").write_text(json.dumps(iv, indent=2))


def silent_interval(out: Path) -> None:
    recs = load_records(out / "can.log")
    obj = recs[recs["can_id"] == 0x300]["t_ns"]
    start = int(obj.max()) + 50_000_000  # first missing ObjectList_0 (T = 50 ms)
    end = int(recs["t_ns"].max()) + 1
    iv = [{"type": "silent", "start_ns": start, "end_ns": end, "source": "can.log"}]
    (out / "anomalies.json").write_text(json.dumps(iv, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", default="results/online_exp")
    p.add_argument("--iface", default="vcan0")
    p.add_argument("--types", nargs="+", default=list(TYPES), choices=TYPES)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    scenario, params = DEFAULTS["sim2d"]
    for kind in a.types:
        out = Path(a.out) / f"E8_{kind}"
        if (out / "anomalies.json").exists() and not a.force:
            print(f"E8_{kind}: cached ({out})")
            continue
        print(f"E8_{kind}: running -> {out}", flush=True)
        abort = threading.Event()
        faults = {}
        th = None
        if kind == "silent":
            faults = {"Perception": FaultConfig.parse(f"silent_after_s={DELAY_S}")}
        else:
            th = threading.Thread(target=attacker, args=(kind, a.iface, out, abort))
            th.start()
        try:
            run_stack(
                out,
                backend="sim2d",
                iface=a.iface,
                duration=DURATION_S,
                scenario=scenario,
                params=params,
                faults=faults,
                stop_on_goal=False,
            )
        finally:
            abort.set()
            if th is not None:
                th.join()
        if kind == "silent":
            silent_interval(out)
        print((out / "anomalies.json").read_text(), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
