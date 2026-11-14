"""Evaluation criteria -> ``report/tables/criteria_summary.csv``.

Inputs: ``results/summary.parquet`` (SimPy runs), ``results/budget/aeb_budget.json``,
``results/online_exp/*`` (online runs, optional) and ``report/tables/gateway_latency.csv``
(Phase 5). Also writes ``report/tables/criteria_variants.csv``: the per-variant judgement the
breaking points are taken from.

Per variant, over all seeds (a variant fails if any seed fails):

- ``theory_ok``: every periodic message (incl. synthetic and gateway copies) has a Davis
  WCRT and WCRT <= D;
- ``sim_ok``: every message R_max <= D and no instance overwritten (an overwrite means the
  previous instance was not sent before the next release, i.e. R > T >= D);
- ``aeb_ok``: the AEB-chain messages (ObjectList_k, AEB_BrakeRequest, also their gateway
  copies) meet both, and the simulated chain e2e (AEB_BrakeRequest on the bus minus the
  freshest sensing origin) <= (sensing/decision + network share) x L_max (chosen: the brake
  ECU share is not part of the simulated chain);
- ``busoff``: any node went bus-off.

CLI: ``python -m canav.analysis.criteria``
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from canav.analysis.tables import read_table

LOAD_THRESHOLD = 0.50  # CiA "Designing a CAN network" (rule of thumb), level chosen
LOAD_REFERENCE = 0.70  # SAE J1939 / CANopen practice, reported without verdict
SRC_LOAD = 'CiA "Designing a CAN network" (rule of thumb; level chosen for this project)'
SRC_REF = "SAE J1939 / CANopen practice (rule of thumb; reference only)"
SRC_D = "D = T (chosen); AEB chain D from results/budget/aeb_budget.json"
SRC_E2E = "chain e2e limit (0.6 + 0.2) x L_max at design speed (aeb_budget.json; chosen split)"
SRC_WD = "safe state after > 3 missed cycles (watchdog_cycles = 3)"


def _ms(x) -> str:
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 1e3:.3f} ms"


def judge_variants(df: pd.DataFrame, chain: set[str], e2e_limit: float) -> pd.DataFrame:
    rows = []
    for (exp, variant), g in df.groupby(["experiment", "variant"], sort=False):
        per = g[g["period"].notna()]
        th = per.drop_duplicates("msg_key")
        theory_ok = bool((th["wcrt"].notna() & (th["wcrt"] <= th["deadline"] + 1e-12)).all())
        r_ok = per["R_max"].notna() & (per["R_max"] <= per["deadline"] + 1e-12)
        sim_ok = bool((r_ok & (per["n_overwritten"] == 0)).all())
        real = ~per["name"].str.startswith("Synth_")
        real_th = real.loc[th.index]
        real_ok = bool(
            (th["wcrt"].notna() & (th["wcrt"] <= th["deadline"] + 1e-12))[real_th].all()
            and (r_ok & (per["n_overwritten"] == 0))[real].all()
        )
        ch = per[per["name"].isin(chain)]
        ch_th = ch.drop_duplicates("msg_key")
        ch_theory = bool(
            (ch_th["wcrt"].notna() & (ch_th["wcrt"] <= ch_th["deadline"] + 1e-12)).all()
        )
        ch_r = ch["R_max"].notna() & (ch["R_max"] <= ch["deadline"] + 1e-12)
        ch_sim = bool((ch_r & (ch["n_overwritten"] == 0)).all())
        e2e = g.loc[g["name"] == "AEB_BrakeRequest", "e2e_max"].max()
        e2e_ok = bool(pd.notna(e2e) and e2e <= e2e_limit)
        worst = per.assign(ratio=per["R_max"] / per["deadline"]).sort_values("ratio")
        late = per[~r_ok | (per["n_overwritten"] > 0)]["name"].unique().tolist()
        rows.append(
            {
                "experiment": exp,
                "variant": variant,
                "params": g["params"].iloc[0],
                "seeds": int(g["seed"].nunique()),
                "bus_load_theory_max": float(g["bus_load_theory"].max()),
                "bus_load_sim_max": float(g["bus_load"].max()),
                "theory_ok": theory_ok,
                "sim_ok": sim_ok,
                "worst_R_over_D": float(worst["ratio"].max()) if len(worst) else None,
                "late_messages": ";".join(sorted(late)),
                "aeb_theory_ok": ch_theory,
                "aeb_sim_ok": ch_sim,
                "aeb_e2e_max_s": float(e2e) if pd.notna(e2e) else None,
                "aeb_e2e_ok": e2e_ok,
                "aeb_ok": ch_theory and ch_sim and e2e_ok,
                "all_ok": theory_ok and sim_ok,
                "real_ok": real_ok,
                "busoff": bool((g["busoff_total"] > 0).any()),
                "lost_injected": int((g["n_dropped"] + g["n_overwritten"] + g["n_busoff"]).sum()),
            }
        )
    return pd.DataFrame(rows)


def _param(v: pd.DataFrame, key: str) -> pd.Series:
    return v["params"].map(lambda p: json.loads(p).get(key))


def _bracket(values: list, ok: list[bool], highest: bool) -> tuple[object, str]:
    """Last passing value of a monotone sweep and the bracket text."""
    pairs = sorted(zip(values, ok, strict=True), reverse=not highest)
    best, first_fail = None, None
    for val, good in pairs:
        if good and first_fail is None:
            best = val
        elif not good and first_fail is None:
            first_fail = val
    if first_fail is None:
        return best, f"none in sweep (all pass up to {pairs[-1][0]})"
    if best is None:
        return None, f"fails already at {pairs[0][0]}"
    return best, f"breaking point between {best} and {first_fail}"


def build(
    summary: pd.DataFrame,
    budget: dict,
    online_dir: Path | None = None,
    gateway_csv: Path | None = None,
    detector_csv: Path | None = None,
) -> tuple[list[dict], pd.DataFrame]:
    d = budget["design"]
    chain = set(d["deadlines_s"])
    e2e_limit = d["chain_e2e_limit_s"]
    v = judge_variants(summary, chain, e2e_limit)
    rows: list[dict] = []

    def add(crit, exp, item, measured, threshold, source, verdict):
        rows.append(
            {
                "criterion": crit,
                "experiment": exp,
                "item": item,
                "measured": measured,
                "threshold": threshold,
                "threshold_source": source,
                "verdict": verdict,
            }
        )

    pf = lambda b: "pass" if b else "fail"  # noqa: E731
    e0 = summary[summary["experiment"] == "E0"]
    if len(e0):
        b = v[v["experiment"] == "E0"].iloc[0]
        per = e0[e0["period"].notna()]
        th = per.drop_duplicates("msg_key")
        slack = (th["deadline"] - th["wcrt"]).min()
        add(
            1,
            "E0",
            "Davis WCRT <= D, all periodic IDs",
            f"{int((th['wcrt'] <= th['deadline']).sum())}/{len(th)} IDs, min slack {_ms(slack)}",
            "R <= D",
            SRC_D,
            pf(b["theory_ok"]),
        )
        add(
            1,
            "E0",
            f"simulated max R <= D, all IDs ({b['seeds']} seeds)",
            f"max R/D = {b['worst_R_over_D']:.3f}",
            "R <= D",
            SRC_D,
            pf(b["sim_ok"]),
        )
        add(
            1,
            "E0",
            "AEB chain: per-hop R <= D (theory + sim)",
            f"theory {pf(b['aeb_theory_ok'])}, sim {pf(b['aeb_sim_ok'])}",
            f"D(ObjectList_k) = {_ms(d['deadlines_s']['ObjectList_0'])}, "
            f"D(AEB_BrakeRequest) = {_ms(d['deadlines_s']['AEB_BrakeRequest'])}",
            SRC_D,
            pf(b["aeb_theory_ok"] and b["aeb_sim_ok"]),
        )
        add(
            1,
            "E0",
            "AEB chain e2e (sim, sensing origin -> AEB_BrakeRequest on bus)",
            _ms(b["aeb_e2e_max_s"]),
            f"<= {_ms(e2e_limit)}",
            SRC_E2E,
            pf(b["aeb_e2e_ok"]),
        )
        lost = int((e0["n"] - e0["n_ok"]).sum())
        add(
            4,
            "E0",
            "baseline E2E: instances lost / counter gaps (sim)",
            f"{lost} lost, max counter loss {e0['loss_counter'].max():.4f}",
            "0",
            "project spec",
            pf(lost == 0 and e0["loss_counter"].max() == 0),
        )

    e1 = v[v["experiment"] == "E1"]
    if len(e1):
        br = _param(e1, "sim.bitrate").tolist()
        best, text = _bracket(br, e1["all_ok"].tolist(), highest=False)
        add(
            3,
            "E1",
            "lowest bitrate with every deadline met (theory + sim)",
            f"{best} bit/s" if best else "-",
            "all R <= D",
            SRC_D,
            f"breaking point at {best} bit/s ({text})" if best else f"fail ({text})",
        )

    for exp, keys in (
        ("E2", ("network.period_factor", "network.dlc")),
        ("E3", ("network.scheme", "network.target_load")),
    ):
        g = v[v["experiment"] == exp]
        if len(g):
            bad = g[~g["aeb_ok"]]
            text = "; ".join(bad["variant"]) if len(bad) else "none"
            why = "; ".join(
                f"{r.variant}: "
                + ", ".join(
                    s
                    for s, f in (
                        ("theory", not r.aeb_theory_ok),
                        ("sim R", not r.aeb_sim_ok),
                        (f"e2e {_ms(r.aeb_e2e_max_s)}", not r.aeb_e2e_ok),
                    )
                    if f
                )
                for r in bad.itertuples()
            )
            add(
                3,
                exp,
                f"configurations where the AEB chain misses its deadline ({keys})",
                why or "none",
                f"per-hop D, e2e <= {_ms(e2e_limit)}",
                SRC_D + "; " + SRC_E2E,
                f"breaking point at: {text}" if len(bad) else "pass (none)",
            )

    e4 = v[v["experiment"] == "E4"]
    if len(e4):
        loads = _param(e4, "network.target_load").tolist()
        best, text = _bracket(loads, e4["all_ok"].tolist(), highest=True)
        meas = f"{best * 100:.0f} % (theoretical worst-case load)" if best else "-"
        add(
            3,
            "E4",
            "highest load with every deadline met (theory + sim)",
            meas,
            "all R <= D",
            SRC_D,
            f"breaking point at {best} ({text})",
        )
        best_r, text_r = _bracket(loads, e4["real_ok"].tolist(), highest=True)
        add(
            3,
            "E4",
            "highest load with every matrix (non-synthetic) message deadline met",
            f"{best_r * 100:.0f} %" if best_r else "-",
            "all R <= D",
            SRC_D + "; synthetic messages excluded",
            f"breaking point at {best_r} ({text_r})",
        )
        add(
            2,
            "E4",
            "highest schedulable load vs 50 % rule of thumb",
            meas,
            f">= {LOAD_THRESHOLD:.0%}",
            SRC_LOAD,
            pf(best is not None and best >= LOAD_THRESHOLD),
        )
        add(
            2,
            "E4",
            "highest schedulable load vs 70 % reference",
            meas,
            f"{LOAD_REFERENCE:.0%}",
            SRC_REF,
            "n/a (reference: " + ("above" if best and best >= LOAD_REFERENCE else "below") + ")",
        )

    e5 = v[v["experiment"] == "E5"]
    if len(e5):
        e5 = e5.assign(
            delay=_param(e5, "network.topology.gateway_delay"),
            aeb_bus=_param(e5, "network.topology.nodes.AEB"),
        )
        for bus, g in e5.groupby("aeb_bus"):
            best, text = _bracket(g["delay"].tolist(), g["aeb_ok"].tolist(), highest=True)
            add(
                3,
                "E5",
                f"forward delay at which the AEB chain misses (AEB on {bus})",
                f"max e2e {_ms(g['aeb_e2e_max_s'].max())}",
                f"e2e <= {_ms(e2e_limit)}",
                SRC_E2E,
                text if best else f"fail ({text})",
            )

    for exp, key, unit in (
        ("E7_drop", "sim.faults.0.drop_prob", ""),
        ("E7_delay", "sim.faults.0.extra_delay", " s"),
        ("E7_error", "sim.faults.0.error_prob", ""),
    ):
        g = v[v["experiment"] == exp]
        if not len(g):
            continue
        vals = _param(g, key).tolist()
        ok = (g["aeb_ok"] & ~g["busoff"]).tolist()
        best, text = _bracket(vals, ok, highest=True)
        why = "; ".join(
            f"{x}{unit}: " + ("bus-off" if r.busoff else f"AEB chain (e2e {_ms(r.aeb_e2e_max_s)})")
            for x, r in zip(vals, g.itertuples(), strict=True)
            if not (r.aeb_ok and not r.busoff)
        )
        add(
            3,
            exp,
            "highest fault rate before AEB chain miss or bus-off",
            why or "no failure",
            "AEB chain ok and no bus-off",
            SRC_E2E,
            f"{text}" if best is not None else f"fail ({text})",
        )
    ed = summary[summary["experiment"] == "E7_drop"]
    if len(ed):
        detected, between = int(ed["lost_detected"].sum()), int(ed["lost_between"].sum())
        add(
            4,
            "E7_drop",
            "injected losses detected by the alive counter (sim, between delivered frames)",
            f"{detected} detected / {between} lost ({int(ed['n_dropped'].sum())} drops injected)",
            "all",
            "project spec",
            pf(detected == between),
        )

    if online_dir is not None:
        rows += online_rows(online_dir)
    if gateway_csv is not None and gateway_csv.exists():
        gw = read_table(gateway_csv)
        for r in gw.itertuples():
            add(
                6,
                "Phase 5 gateway",
                f"batch {r.batch_frames}, flush {r.flush_ms} ms",
                f"p50 {r.lat_p50_us:.0f} us, p99 {r.lat_p99_us:.0f} us, UDP lost {r.udp_lost}",
                "none",
                "no threshold",
                "n/a",
            )
    if detector_csv is not None and detector_csv.exists():
        rows += detector_rows(read_table(detector_csv))
    add(
        5,
        "E7 online (P1)",
        "maximum avoidance speed vs drop/delay at Control/AEB",
        "not run",
        "-",
        "project spec",
        "n/a (P1, not run in Phase 6)",
    )
    return rows, v


DETECTOR_K = 3  # chosen for the summary rows: middle of the k sweep (classic 3 sigma)


def detector_rows(ev: pd.DataFrame) -> list[dict]:
    """Spoofing detected by the E2E counter (R5) and the detector (no threshold)."""
    rows = []

    def add(crit, exp, item, measured, threshold, source, verdict):
        rows.append(
            dict(
                criterion=crit,
                experiment=exp,
                item=item,
                measured=measured,
                threshold=threshold,
                threshold_source=source,
                verdict=verdict,
            )
        )

    for ds, g in ev.groupby("dataset"):
        exp = f"E8/E9 {ds}"
        sp = g[(g["anomaly"] == "spoof") & (g["k"] == DETECTOR_K)]
        if len(sp):
            r5 = float(sp["tpr_R5"].iloc[0])
            add(
                4,
                exp,
                "spoofed Steer_Cmd windows with E2E counter errors (R5)",
                f"{r5:.3f} of {int(sp['windows'].iloc[0])} windows",
                "1.0 (every window)",
                "every injected fault detected",
                "pass" if r5 >= 1.0 else "fail",
            )
        gk = g[g["k"] == DETECTOR_K]
        for r in gk[gk["anomaly"] != "all"].itertuples():
            add(
                7,
                exp,
                f"TPR {r.anomaly} (k = {DETECTOR_K})",
                f"{r.tpr:.4f} ({r.windows} windows), delay mean {r.delay_mean_s:.3f} s / "
                f"max {r.delay_max_s:.3f} s, missed {int(r.missed)}/{int(r.occurrences)}",
                "none",
                "no threshold",
                "n/a",
            )
        al = gk[gk["anomaly"] == "all"].iloc[0]
        add(
            7,
            exp,
            f"global FPR (k = {DETECTOR_K})",
            f"{al.fpr:.5f} ({int(al.normal_windows)} normal windows)",
            "none",
            "no threshold",
            "n/a",
        )
        roc = g[g["anomaly"] == "all"].sort_values("k")
        add(
            7,
            exp,
            "ROC over k (FPR, TPR)",
            "; ".join(f"k={int(r.k)}: ({r.fpr:.5f}, {r.tpr:.4f})" for r in roc.itertuples()),
            "none",
            "no threshold",
            "n/a",
        )
    return rows


def online_rows(root: Path) -> list[dict]:
    from canav.analysis.online import load_events

    rows = []

    def add(crit, exp, item, measured, threshold, source, verdict):
        rows.append(
            dict(
                criterion=crit,
                experiment=exp,
                item=item,
                measured=measured,
                threshold=threshold,
                threshold_source=source,
                verdict=verdict,
            )
        )

    e0 = root / "E0" / "metrics.json"
    if e0.exists():
        m = json.loads(e0.read_text())
        err = m["errors"]["crc_err"] + m["errors"]["counter_err"]
        add(
            4,
            "E0 online",
            "baseline CRC + counter errors (vcan0)",
            f"{m['errors']['crc_err']} crc, {m['errors']['counter_err']} counter",
            "0",
            "project spec",
            "pass" if err == 0 else "fail",
        )
    drop = root / "E7_drop5_control"
    if (drop / "metrics.json").exists():
        ev = load_events(drop / "ecu")
        detected = injected_runs = 0
        for msg, rx in (
            ("Steer_Cmd", "SteeringActuator"),
            ("ThrottleBrake_Cmd", "ThrottleBrakeActuator"),
        ):
            tx = ev[(ev["ecu"] == "Control") & (ev["msg"] == msg)].sort_values("t_ns")
            kinds = tx["event"].tolist()
            # a run of drops followed by a sent frame is one counter gap at the receiver
            injected_runs += sum(
                1 for i in range(1, len(kinds)) if kinds[i] == "tx" and kinds[i - 1] == "drop"
            )
            detected += int(
                ((ev["ecu"] == rx) & (ev["event"] == "counter_err") & (ev["msg"] == msg)).sum()
            )
        add(
            4,
            "E7 online drop 5 % Control",
            "drop runs detected as counter gaps at actuators",
            f"{detected} detected / {injected_runs} drop runs",
            "all",
            "project spec",
            "pass" if detected == injected_runs else "fail",
        )
    silent = root / "E7_silent_control"
    if (silent / "metrics.json").exists():
        ev = load_events(silent / "ecu")
        params = __import__("yaml").safe_load((silent / "ecu_params.yaml").read_text())
        for act, msg in (
            ("SteeringActuator", "Steer_Cmd"),
            ("ThrottleBrakeActuator", "ThrottleBrake_Cmd"),
        ):
            a = ev[ev["ecu"] == act]
            safe = a[(a["event"] == "use") & (a["msg"] == "safe_state")]
            last = a[(a["event"] == "rx") & (a["msg"] == msg)]["t_ns"]
            limit = params[act]["watchdog_cycles"] * 0.01
            if safe.empty or last.empty:
                add(
                    4,
                    "E7 online silent Control",
                    f"{act} safe state",
                    "not entered",
                    f"> {limit * 1e3:.0f} ms after last command",
                    SRC_WD,
                    "fail",
                )
                continue
            t_safe = int(safe["t_ns"].iloc[0])
            t_last = int(last[last < t_safe].max())
            lat = (t_safe - t_last) / 1e9
            add(
                4,
                "E7 online silent Control",
                f"{act} enters safe state after the watchdog",
                f"{lat * 1e3:.1f} ms after the last command",
                f"> {limit * 1e3:.0f} ms (3 cycles of 10 ms)",
                SRC_WD,
                "pass" if lat > limit else "fail",
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--results", default="results")
    p.add_argument("--tables", default="report/tables")
    a = p.parse_args(argv)
    res = Path(a.results)
    summary = pd.read_parquet(res / "summary.parquet")
    budget = json.loads((res / "budget" / "aeb_budget.json").read_text())
    tables = Path(a.tables)
    rows, variants = build(
        summary,
        budget,
        res / "online_exp",
        tables / "gateway_latency.csv",
        tables / "detector_eval.csv",
    )
    out = Path(a.tables)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "criteria_summary.csv", index=False)
    variants.to_csv(out / "criteria_variants.csv", index=False)
    for r in rows:
        print(
            f"[{r['criterion']}] {r['experiment']}: {r['item']}: {r['measured']} -> {r['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
