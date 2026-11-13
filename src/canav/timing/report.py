"""Theoretical timing report: per-ID C, T, D, WCRT and schedulability, plus bus load.

CLI: ``python -m canav.timing.report --bitrate 500000 --out report/tables/theory_baseline.csv``
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from canav.netdef import DEFAULT_MATRIX, DEFAULT_SCHEMES, load_network
from canav.timing import WcrtResult, bus_load_theory, davis_wcrt, timing_messages

COLUMNS = ["can_id", "name", "bits", "C_s", "T_s", "D_s", "J_s", "R_s", "ok"]


def rows(results: list[WcrtResult]) -> list[dict[str, object]]:
    return [
        {
            "can_id": f"0x{r.can_id:03X}",
            "name": r.name,
            "bits": r.bits,
            "C_s": r.c,
            "T_s": r.period,
            "D_s": r.deadline,
            "J_s": r.jitter,
            "R_s": "" if r.r is None else r.r,
            "ok": r.schedulable,
        }
        for r in results
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    p.add_argument("--schemes", default=str(DEFAULT_SCHEMES))
    p.add_argument("--scheme", default="safety_first")
    p.add_argument("--bus", default=None, help="bus name (default: the only/first bus)")
    p.add_argument("--bitrate", type=int, default=None, help="override bus bitrate [bit/s]")
    p.add_argument("--stuffing", choices=["worst", "none"], default="worst")
    p.add_argument("--out", default=None, help="CSV output path")
    a = p.parse_args(argv)

    net = load_network(a.matrix, a.scheme, a.schemes)
    bus = a.bus or next(iter(net.buses))
    bitrate = a.bitrate or net.buses[bus]
    msgs = timing_messages(net, a.stuffing, bus)
    results = davis_wcrt(msgs, bitrate)
    load = bus_load_theory(msgs, bitrate)

    table = rows(results)
    print(f"bus={bus} bitrate={bitrate} bit/s stuffing={a.stuffing} scheme={a.scheme}")
    print(f"{'ID':>6} {'name':<18} {'C[us]':>8} {'T[ms]':>7} {'D[ms]':>7} {'R[us]':>9}  ok")
    for r in results:
        rv = "diverge" if r.r is None else f"{r.r * 1e6:.1f}"
        print(
            f"0x{r.can_id:03X}  {r.name:<18} {r.c * 1e6:8.1f} {r.period * 1e3:7.1f} "
            f"{r.deadline * 1e3:7.1f} {rv:>9}  {r.schedulable}"
        )
    load_bps = load * bitrate
    print(f"load (periodic, theory) = {load_bps:.0f} bit/s = {load * 100:.2f} %")
    n_bad = sum(not r.schedulable for r in results)
    print(f"deadlines met: {len(results) - n_bad}/{len(results)}")

    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(table)
        print(f"wrote {out}")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
