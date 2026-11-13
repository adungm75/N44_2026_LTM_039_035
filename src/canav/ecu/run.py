"""Run one ECU as a process: ``python -m canav.ecu.run --ecu Control --iface vcan0 ...``.

SIGINT/SIGTERM stop the ECU cleanly (log flushed, bus closed). Faults via ``--fault`` or the
``CANAV_FAULT`` environment variable (``drop_prob=0.05,extra_delay_ms=2,silent_after_s=10``).
"""

from __future__ import annotations

import argparse
import signal
import threading

import yaml

from canav.ecu import FaultConfig, build_ecu, load_dbc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ecu", required=True)
    p.add_argument("--iface", default="vcan0")
    p.add_argument("--dbc", default="config/dbc/canav.dbc")
    p.add_argument("--params", required=True, help="resolved ecu_params.yaml (with route path)")
    p.add_argument("--log-dir", required=True)
    p.add_argument("--fault", default=None)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--duration", type=float, default=None)
    a = p.parse_args(argv)

    fault = FaultConfig.parse(a.fault) if a.fault is not None else FaultConfig.from_env()
    params = yaml.safe_load(open(a.params))
    ecu = build_ecu(a.ecu, a.iface, load_dbc(a.dbc), params, a.log_dir, fault=fault, seed=a.seed)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        ecu.run(stop, a.duration)
    finally:
        ecu.bus.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
