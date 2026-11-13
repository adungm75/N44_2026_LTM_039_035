#!/usr/bin/env python3
"""Phase 5 measurement: gateway latency vs batching, monitor vs ``canbusload``, diag on the bus.

    python scripts/measure_gateway.py --window 15      (USER: vcan0 up, can-utils installed)

1. Starts the online stack (sim2d, vcan0, no gateway, no stop at goal) as traffic source.
2. Runs ``canbusload`` (worst-case and ``-e`` exact stuffing) for the whole measurement.
3. For each batch size x flush timeout: starts a gateway process, runs the monitor in this
   process for ``--window`` s (DIAG_REQ to BodyHMI every second), stops the gateway.
4. Writes ``report/tables/gateway_latency.csv``, ``report/tables/monitor_vs_canbusload.csv``
   and ``<out>/summary.json``; the stack's ``can.log`` is checked for 0x7DF / 0x7E8.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from canav.analysis.candump import read_log
from canav.ecu import load_dbc
from canav.monitor import Monitor, run_monitor
from canav.netdef import DEFAULT_MATRIX, load_matrix
from canav.provenance import git_hash

DIAG = {"target": 8, "service": 0x22, "did": 1}  # BodyHMI, ReadDataByIdentifier, odometer
HEAD = re.compile(r"canbusload (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
ROW = re.compile(r"^\s*(\S+)@(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%")


def parse_canbusload(path: Path) -> list[dict]:
    """``canbusload -t`` blocks -> [{t (epoch s, interval end), frames, bits}]."""
    rows, t = [], None
    for line in path.read_text().splitlines():
        if m := HEAD.search(line):
            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        elif (m := ROW.match(line)) and t is not None:
            rows.append({"t": t, "frames": int(m.group(3)), "bits": int(m.group(4))})
    return rows


def _between(rows: list[dict], t0: float, t1: float, key: str) -> float | None:
    vals = [r[key] for r in rows if t0 <= r["t"] <= t1]
    return float(np.mean(vals)) if vals else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", default=f"results/gateway/{time.strftime('%Y%m%d-%H%M%S')}")
    p.add_argument("--iface", default="vcan0")
    p.add_argument("--window", type=float, default=15.0, help="[s] per configuration")
    p.add_argument("--batches", type=int, nargs="+", default=[1, 8, 32])
    p.add_argument("--flush-ms", type=float, nargs="+", default=[1.0, 5.0])
    p.add_argument("--port", type=int, default=48100, help="uplink port (downlink = +1)")
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    db = load_dbc("config/dbc/canav.dbc")
    bitrate = next(iter(load_matrix(DEFAULT_MATRIX).buses.values()))
    configs = [(b, f) for b in a.batches for f in a.flush_ms]
    sim_s = 15 + len(configs) * (a.window + 3)

    stack_dir = out / "stack"
    stack_cmd = [py, "scripts/launch_stack.py", "--backend", "sim2d", "--iface", a.iface]
    stack_cmd += ["--duration", str(sim_s), "--out", str(stack_dir)]
    stack_cmd += ["--no-stop-on-goal", "--no-gateway"]
    log = open(out / "stack.log", "w")  # noqa: SIM115 - owned by the child
    stack = subprocess.Popen(stack_cmd, stdout=log, stderr=subprocess.STDOUT)
    loads = {}
    for mode, flag in (("worst", []), ("exact", ["-e"])):
        path = out / f"canbusload_{mode}.txt"
        cmd = ["stdbuf", "-oL", "canbusload", "-t", *flag, f"{a.iface}@{bitrate}"]
        loads[mode] = (subprocess.Popen(cmd, stdout=open(path, "w")), path)  # noqa: SIM115
    rows_lat, results = [], {}
    try:
        t0 = time.monotonic()
        while not (stack_dir / "route.json").exists():
            if stack.poll() is not None or time.monotonic() - t0 > 60:
                raise RuntimeError(f"stack did not start, see {out / 'stack.log'}")
            time.sleep(0.1)
        time.sleep(3.0)  # ECUs up and sending
        for batch, flush in configs:
            name = f"b{batch}_f{flush:g}"
            mon = Monitor(db, bitrate)
            stop = threading.Event()
            th = threading.Thread(
                target=run_monitor,
                args=(mon, stop),
                kwargs={
                    "port": a.port,
                    "diag": DIAG,
                    "gateway_downlink": ("127.0.0.1", a.port + 1),
                },
            )
            th.start()
            time.sleep(0.2)
            gw_cmd = [py, "-m", "canav.gateway", "--iface", a.iface, "--batch", str(batch)]
            gw_cmd += ["--flush-ms", str(flush), "--uplink-port", str(a.port)]
            gw_cmd += ["--downlink-port", str(a.port + 1)]
            gw_cmd += ["--stats-out", str(out / f"{name}_gateway.json")]
            gw = subprocess.Popen(gw_cmd, stdout=subprocess.DEVNULL)
            time.sleep(a.window)
            gw.send_signal(signal.SIGINT)
            gw.wait(timeout=10)
            time.sleep(0.3)
            stop.set()
            th.join(timeout=5)
            s = mon.write(out / name)
            full = mon.full_windows()
            lat = s["latency_us"]
            n_data = s["datagrams"].get("1", 0)
            rows_lat.append(
                {
                    "batch_frames": batch,
                    "flush_ms": flush,
                    "frames": s["frames"],
                    "data_datagrams": n_data,
                    "frames_per_datagram": s["frames"] / n_data if n_data else None,
                    "lat_p50_us": lat["p50"],
                    "lat_p99_us": lat["p99"],
                    "lat_max_us": lat["max"],
                    "udp_lost": s["udp_lost"],
                    "udp_loss_rate": s["udp_loss_rate"],
                }
            )
            results[name] = full
            print(
                f"{name}: lat p50 {lat['p50']:.0f} us p99 {lat['p99']:.0f} us, "
                f"lost {s['udp_lost']}, frames {s['frames']}",
                flush=True,
            )
    finally:
        for proc, _ in loads.values():
            proc.send_signal(signal.SIGINT)
        stack.send_signal(signal.SIGINT)
        stack.wait(timeout=120)
        for proc, _ in loads.values():
            proc.wait(timeout=5)

    cbl = {mode: parse_canbusload(path) for mode, (_, path) in loads.items()}
    rows_cmp = []
    for name, mon_rows in results.items():
        if not mon_rows:
            continue
        t0, t1 = mon_rows[0]["t_start_s"], mon_rows[-1]["t_start_s"] + 1
        # canbusload stamps each 1 s interval at its end: compare from t0 + 1 to t1
        rows_cmp.append(
            {
                "config": name,
                "monitor_frames_per_s": np.mean([w["frames_per_s"] for w in mon_rows]),
                "canbusload_frames_per_s": _between(cbl["worst"], t0 + 1, t1, "frames"),
                "monitor_bits_worst_per_s": np.mean([w["bits_worst_per_s"] for w in mon_rows]),
                "canbusload_bits_worst_per_s": _between(cbl["worst"], t0 + 1, t1, "bits"),
                "monitor_bits_actual_per_s": np.mean([w["bits_actual_per_s"] for w in mon_rows]),
                "canbusload_bits_exact_per_s": _between(cbl["exact"], t0 + 1, t1, "bits"),
            }
        )
    recs = list(read_log(stack_dir / "can.log")) if (stack_dir / "can.log").exists() else []
    diag = {
        "0x7DF_on_candump": sum(r.can_id == 0x7DF for r in recs),
        "0x7E8_on_candump": sum(r.can_id == 0x7E8 for r in recs),
    }
    tables = Path("report/tables")
    tables.mkdir(parents=True, exist_ok=True)
    for path, rows in {
        tables / "gateway_latency.csv": rows_lat,
        tables / "monitor_vs_canbusload.csv": rows_cmp,
    }.items():
        if rows:
            with open(path, "w", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=list(rows[0]))
                wr.writeheader()
                wr.writerows(rows)
    summary = {
        "git": git_hash(),
        "window_s": a.window,
        "latency": rows_lat,
        "monitor_vs_canbusload": rows_cmp,
        "diag": diag,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps({"diag": diag, "compare": rows_cmp}, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
