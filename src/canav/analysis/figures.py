"""Figures for the report (§6 Phase 8) -> ``report/figures/*.png``.

Inputs: ``results/summary.parquet``, ``results/budget/aeb_budget.json``, ``report/tables/*.csv``
and, when present, ``results/online_exp`` (vcan0 runs). A figure whose online series is
missing is still drawn from the SimPy data; its caption in docs/results.md says so.

Style (dataviz reference palette, light surface): categorical slots in fixed order (blue,
orange, aqua), at most three series per axis, thin lines and >= 8 px markers, hairline grid,
text in ink colours, reference lines (deadline, limits) dashed in secondary ink.

CLI: ``python -m canav.analysis.figures``
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from canav.analysis.tables import read_table  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, AXIS = (
    "#fcfcfb",
    "#0b0b0b",
    "#52514e",
    "#898781",
    "#e1e0d9",
    "#c3c2b7",
)
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"  # categorical slots 1-3
CRITICAL = "#d03b3b"  # status: alarm
CLASSES = (
    ("high (ID < 0x100)", 0x000, 0x100),
    ("mid (0x100-0x3FF)", 0x100, 0x400),
    ("low (>= 0x400)", 0x400, 0x800),
)


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlecolor": INK,
            "axes.labelcolor": INK2,
            "axes.edgecolor": AXIS,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK2,
            "ytick.labelcolor": INK2,
            "legend.frameon": False,
            "legend.labelcolor": INK2,
            "lines.linewidth": 1.6,
            "lines.markersize": 6,
        }
    )


def _label(ax, x, y, text, color=INK2, dx=4, dy=0) -> None:
    """Direct label next to a point (text stays in ink colour)."""
    ax.annotate(
        text,
        (x, y),
        xytext=(dx, dy),
        textcoords="offset points",
        color=color,
        va="center",
        fontsize=8,
    )


def _ref(ax, y, text) -> None:
    ax.axhline(y, color=INK2, linestyle="--", linewidth=1.0)
    ax.annotate(
        text,
        (0.0, y),
        xycoords=("axes fraction", "data"),
        xytext=(3, 3),
        textcoords="offset points",
        ha="left",
        color=INK2,
        fontsize=8,
    )


def _param(df: pd.DataFrame, key: str) -> pd.Series:
    return df["params"].map(lambda p: json.loads(p).get(key))


def _runs(df: pd.DataFrame) -> pd.DataFrame:
    """One row per run (run-level columns repeat over messages in the summary)."""
    return df.drop_duplicates("run_id")


# ----------------------------------------------------------------------- figures
def fig_load_vs_bitrate(s: pd.DataFrame, online: Path):
    e1 = _runs(s[s["experiment"] == "E1"]).assign(br=lambda d: _param(d, "sim.bitrate"))
    g = e1.groupby("br").agg(sim=("bus_load", "mean"), theory=("bus_load_theory", "first"))
    fig, ax = plt.subplots(figsize=(6, 3.6))
    x = g.index.to_numpy() / 1000
    ax.plot(x, g["theory"] * 100, color=S1, marker="o", label="theory (worst-case stuffing)")
    ax.plot(
        x,
        g["sim"] * 100,
        color=S2,
        marker="s",
        linestyle="none",
        markersize=8,
        label="SimPy (mean of 5 seeds)",
    )
    vcan = None
    m = online / "E0" / "metrics.json"
    if m.exists():
        vcan = json.loads(m.read_text())["bus"]["load_worst"]
        ax.plot(
            [500],
            [vcan * 100],
            color=S3,
            marker="D",
            linestyle="none",
            markersize=8,
            label="vcan0 can.log (500 kbit/s)",
        )
        _label(ax, 500, vcan * 100, f"vcan0 {vcan * 100:.2f} %", dy=-10)
    for xi, yi in zip(x, g["theory"] * 100, strict=True):
        _label(ax, xi, yi, f"{yi:.1f} %", dy=8)
    ax.set_xscale("log", base=2)
    ax.set_xticks(x, [f"{int(v)}" for v in x])
    ax.set_xlabel("bitrate [kbit/s]")
    ax.set_ylabel("bus load [%]")
    ax.set_title("Bus load vs bitrate (E1, baseline matrix)")
    ax.legend(loc="upper right")
    return fig, {"theory": g["theory"].to_dict(), "sim": g["sim"].to_dict(), "vcan": vcan}


def fig_response_times(s: pd.DataFrame):
    e0 = s[(s["experiment"] == "E0") & s["period"].notna()]
    g = (
        e0.groupby("msg_key")
        .agg(
            name=("name", "first"),
            r_mean=("R_mean", "mean"),
            r_max=("R_max", "max"),
            wcrt=("wcrt", "first"),
            d=("deadline", "first"),
        )
        .sort_index()
    )
    fig, ax = plt.subplots(figsize=(8, 3.8))
    x = np.arange(len(g))
    ax.vlines(x, g["r_mean"] * 1e3, g["r_max"] * 1e3, color=S1, linewidth=2)
    ax.plot(x, g["r_max"] * 1e3, "o", color=S1, label="SimPy R: mean-max (5 seeds)")
    ax.plot(x, g["wcrt"] * 1e3, "_", color=S2, markersize=14, markeredgewidth=2, label="Davis WCRT")
    ax.plot(x, g["d"] * 1e3, "v", color=INK2, markersize=6, label="deadline D")
    ax.set_yscale("log")
    ax.set_xticks(
        x,
        [f"{k} {n}" for k, n in zip(g.index, g["name"], strict=True)],
        rotation=60,
        ha="right",
        fontsize=7,
    )
    ax.set_ylabel("response time [ms] (log)")
    ax.set_title("E0: simulated response time vs Davis WCRT and deadline, per CAN ID")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.42), ncol=3)
    return fig, g.to_dict(orient="index")


def fig_latency_vs_load(s: pd.DataFrame):
    e4 = s[(s["experiment"] == "E4") & s["period"].notna()].copy()
    e4["load"] = _param(e4, "network.target_load")
    cid = e4["msg_key"].str.slice(2, 5).map(lambda h: int(h, 16))
    fig, ax = plt.subplots(figsize=(6, 3.6))
    out = {}
    for (name, lo, hi), color, mk in zip(CLASSES, (S1, S2, S3), "osD", strict=True):
        sel = e4[(cid >= lo) & (cid < hi)]
        g = sel.groupby("load")["R_p99"].max() * 1e3
        ax.plot(g.index * 100, g.to_numpy(), color=color, marker=mk, label=name)
        _label(ax, g.index[-1] * 100, g.iloc[-1], name.split(" ")[0])
        out[name] = g.to_dict()
    ax.set_yscale("log")
    ax.set_xlabel("theoretical load (synthetic messages added) [%]")
    ax.set_ylabel("p99 response time, worst message [ms]")
    ax.set_title("E4: latency vs load per priority class (SimPy)")
    ax.legend(loc="upper left")
    return fig, out


def fig_id_scheme(s: pd.DataFrame, d_aeb: float):
    e3 = s[(s["experiment"] == "E3") & (s["name"] == "AEB_BrakeRequest")].copy()
    e3["scheme"] = _param(e3, "network.scheme")
    e3["load"] = _param(e3, "network.target_load")
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.4), sharey=True)
    out = {}
    for ax, col, title in ((axes[0], "wcrt", "Davis WCRT"), (axes[1], "R_max", "SimPy max R")):
        for scheme, color, mk in (("safety_first", S1, "o"), ("naive", S2, "s")):
            g = e3[e3["scheme"] == scheme].groupby("load")[col].max() * 1e3
            ax.plot(g.index * 100, g.to_numpy(), color=color, marker=mk, label=scheme)
            _label(ax, g.index[-1] * 100, g.iloc[-1], scheme)
            out[f"{col}/{scheme}"] = g.to_dict()
        _ref(ax, d_aeb * 1e3, f"D = {d_aeb * 1e3:.0f} ms")
        ax.set_title(f"AEB_BrakeRequest: {title}")
        ax.set_xlabel("total load [%]")
        ax.set_xlim(25, 105)
    axes[0].set_ylabel("response time [ms]")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.suptitle("E3: ID scheme safety_first (AEB 0x010) vs naive (AEB 0x600)", color=INK)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.legend(handles, labels, loc="lower center", ncol=2)
    return fig, out


def fig_two_bus(s: pd.DataFrame, limit: float):
    e5 = s[(s["experiment"] == "E5") & (s["name"] == "AEB_BrakeRequest")].copy()
    e5["delay"] = _param(e5, "network.topology.gateway_delay")
    e5["aeb"] = _param(e5, "network.topology.nodes.AEB")
    base = s[(s["experiment"] == "E0") & (s["name"] == "AEB_BrakeRequest")]["e2e_max"].max()
    fig, ax = plt.subplots(figsize=(6, 3.4))
    out = {"one_bus": base}
    for aeb, color, mk in (("chassis", S1, "o"), ("adas", S2, "s")):
        g = e5[e5["aeb"] == aeb].groupby("delay")["e2e_max"].max() * 1e3
        ax.plot(g.index * 1e3, g.to_numpy(), color=color, marker=mk, label=f"AEB on {aeb} bus")
        _label(ax, g.index[-1] * 1e3, g.iloc[-1], f"AEB on {aeb}")
        out[aeb] = g.to_dict()
    _ref(ax, base * 1e3, f"1 bus (E0) {base * 1e3:.2f} ms")
    ax.set_xlabel("gateway forward delay [ms]")
    ax.set_ylabel("AEB chain e2e, max [ms]")
    ax.set_title(f"E5: 1 vs 2 buses (chain e2e limit {limit * 1e3:.1f} ms, off scale)")
    ax.legend(loc="center right")
    return fig, out


def fig_faults(s: pd.DataFrame, limit: float):
    fig, axes = plt.subplots(2, 2, figsize=(8, 5.6))
    out = {}
    panels = (
        (axes[0, 0], "E7_drop", "sim.faults.0.drop_prob", 100, "drop probability [%]"),
        (axes[0, 1], "E7_delay", "sim.faults.0.extra_delay", 1e3, "extra delay [ms]"),
        (axes[1, 0], "E7_error", "sim.faults.0.error_prob", 100, "frame error probability [%]"),
    )
    for ax, exp, key, scale, xlabel in panels:
        e = s[(s["experiment"] == exp) & (s["name"] == "AEB_BrakeRequest")].copy()
        e["x"] = _param(e, key) * scale
        g = e.groupby("x")["e2e_max"].max() * 1e3
        ax.plot(g.index, g.to_numpy(), color=S1, marker="o")
        _ref(ax, limit * 1e3, f"limit {limit * 1e3:.1f} ms")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("AEB chain e2e, max [ms]")
        ax.set_title(f"{exp}: AEB chain e2e")
        ax.set_ylim(0, max(g.max(), limit * 1e3) * 1.15)
        out[exp] = g.to_dict()
    ax = axes[1, 1]
    e = _runs(s[s["experiment"] == "E7_error"]).copy()
    e["x"] = _param(e, "sim.faults.0.error_prob") * 100
    g = e.groupby("x")["busoff_total"].mean()
    ax.plot(g.index, g.to_numpy(), color=S2, marker="s")
    ax.axvline(100 / 9, color=INK2, linestyle="--", linewidth=1.0)
    ax.annotate(
        "TEC drift limit p = 1/9",
        (100 / 9, g.max() * 0.9),
        xytext=(-4, 0),
        textcoords="offset points",
        ha="right",
        color=INK2,
        fontsize=8,
    )
    ax.set_xlabel("frame error probability [%]")
    ax.set_ylabel("bus-off events per 60 s run (mean)")
    ax.set_title("E7_error: bus-off")
    out["busoff"] = g.to_dict()
    fig.suptitle("E7: faults vs AEB chain deadline and bus-off (SimPy, 5 seeds)", color=INK)
    fig.tight_layout()
    return fig, out


def fig_detector_timeseries(results: Path, db, bitrate: int):
    from canav.analysis.detector import (
        ONLINE_WARMUP_S,
        SIM_WARMUP_S,
        Detector,
        _online_features,
        sim_dataset,
    )
    from canav.analysis.features import periodic_ids

    keys = {
        "flood": "load",
        "silent": "gap_ratio_max",
        "spoof": "counter_err",
        "unknown_id": "unknown",
    }
    root = results / "online_exp"
    runs = {}
    if (root / "E0" / "can.log").exists() and all(
        (root / f"E8_{k}" / "anomalies.json").exists() for k in keys
    ):
        source = "online vcan0"
        train = _online_features(root / "E0", db, bitrate)
        for k in keys:
            runs[k] = (
                _online_features(root / f"E8_{k}", db, bitrate),
                json.loads((root / f"E8_{k}" / "anomalies.json").read_text())[0],
            )
        _ = ONLINE_WARMUP_S
    else:
        source = "SimPy"
        train, test = sim_dataset(results, db, bitrate)
        for df, iv in test:
            if iv and iv[0]["type"] not in runs:
                runs[iv[0]["type"]] = (df, iv[0])
        _ = SIM_WARMUP_S
    det = Detector(periodic_ids(db), bitrate).fit(train)
    fig, axes = plt.subplots(2, 2, figsize=(8, 5.4))
    out = {"source": source}
    for ax, (kind, feat) in zip(axes.flat, keys.items(), strict=True):
        df, iv = runs[kind]
        t = (df["t_start_ns"] - iv["start_ns"]) / 1e9
        r = det.rules(df, 3)
        ax.axvspan(
            0,
            (iv["end_ns"] - iv["start_ns"]) / 1e9,
            color=GRID,
            alpha=0.7,
            label="anomaly interval",
        )
        ax.plot(t, df[feat], color=S1, linewidth=1.2, label="feature value")
        top = np.nanmax(df[feat].to_numpy()) if np.isfinite(np.nanmax(df[feat])) else 1
        al = t[r["alarm"].to_numpy()]
        ax.plot(
            al,
            np.full(len(al), top * 1.08 if top > 0 else 1),
            "|",
            color=CRITICAL,
            markersize=6,
            label="alarm (k = 3)",
        )
        ax.set_title(f"{kind}: {feat}")
        ax.set_xlabel("time from anomaly start [s]")
        ax.set_xlim(-8, 15)
        dur = (iv["end_ns"] - iv["start_ns"]) / 1e9
        outside = (t >= -8) & (t <= 15) & ((t + 0.1 <= 0) | (t >= dur))  # whole window outside
        out[kind] = {
            "alarms_in_view": int(((al >= -8) & (al <= 15)).sum()),
            "alarms_outside_interval_in_view": int((r["alarm"].to_numpy() & outside).sum()),
        }
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.suptitle(f"E8/E9: feature time series with detector alarms ({source})", color=INK)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.legend(handles, labels, loc="lower center", ncol=3)
    return fig, out


def fig_roc(tables: Path):
    ev = read_table(tables / "detector_eval.csv")
    al = ev[ev["anomaly"] == "all"]
    fig, ax = plt.subplots(figsize=(5, 3.6))
    out = {}
    for (ds, g), color, mk in zip(al.groupby("dataset"), (S2, S1), "so", strict=False):
        g = g.sort_values("k")
        ax.plot(g["fpr"] * 100, g["tpr"] * 100, color=color, marker=mk, label=ds)
        for (fpr, tpr), pt in g.groupby(["fpr", "tpr"], sort=False):  # equal points share a label
            ks = ",".join(str(int(k)) for k in pt["k"])
            _label(ax, fpr * 100, tpr * 100, f"k={ks}", dy=-8)
        out[ds] = g[["k", "fpr", "tpr"]].to_dict(orient="records")
    ax.set_xlabel("false positive rate [%]")
    ax.set_ylabel("true positive rate [%]")
    ax.set_title("E9: detector ROC over k (all anomaly types)")
    ax.legend(loc="lower right")
    return fig, out


def fig_gateway(tables: Path):
    gw = read_table(tables / "gateway_latency.csv")
    fig, axes = plt.subplots(1, 2, figsize=(8, 3.2), sharey=True)
    for ax, col, title in ((axes[0], "lat_p50_us", "p50"), (axes[1], "lat_p99_us", "p99")):
        for (flush, g), color, mk in zip(gw.groupby("flush_ms"), (S1, S2), "os", strict=False):
            g = g.sort_values("batch_frames")
            ax.plot(
                g["batch_frames"],
                g[col] / 1e3,
                color=color,
                marker=mk,
                label=f"flush timeout {flush:g} ms",
            )
            _label(ax, g["batch_frames"].iloc[-1], g[col].iloc[-1] / 1e3, f"{flush:g} ms")
        ax.set_xscale("log", base=2)
        ax.set_xticks([1, 8, 32], ["1", "8", "32"])
        ax.set_xlabel("batch size [frames]")
        ax.set_title(f"gateway latency {title}")
    axes[0].set_ylabel("latency [ms]")
    axes[0].legend(loc="upper left")
    fig.suptitle("Phase 5: gateway latency vs batching (vcan0, sim2d traffic)", color=INK)
    fig.tight_layout()
    return fig, gw.to_dict(orient="records")


def main(argv: list[str] | None = None) -> int:
    from canav.ecu import load_dbc
    from canav.netdef import DEFAULT_MATRIX, load_matrix

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--results", default="results")
    p.add_argument("--tables", default="report/tables")
    p.add_argument("--out", default="report/figures")
    a = p.parse_args(argv)
    res, tables, out = Path(a.results), Path(a.tables), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    style()
    s = pd.read_parquet(res / "summary.parquet")
    budget = json.loads((res / "budget" / "aeb_budget.json").read_text())["design"]
    db = load_dbc("config/dbc/canav.dbc")
    bitrate = next(iter(load_matrix(DEFAULT_MATRIX).buses.values()))
    limit = budget["chain_e2e_limit_s"]
    figs = {
        "fig1_load_vs_bitrate": lambda: fig_load_vs_bitrate(s, res / "online_exp"),
        "fig2_response_times": lambda: fig_response_times(s),
        "fig3_latency_vs_load": lambda: fig_latency_vs_load(s),
        "fig4_id_scheme_aeb": lambda: fig_id_scheme(s, budget["deadlines_s"]["AEB_BrakeRequest"]),
        "fig5_two_bus": lambda: fig_two_bus(s, limit),
        "fig6_faults": lambda: fig_faults(s, limit),
        "fig7_detector_timeseries": lambda: fig_detector_timeseries(res, db, bitrate),
        "fig8_roc": lambda: fig_roc(tables),
        "fig9_gateway_latency": lambda: fig_gateway(tables),
    }
    data = {}
    for name, make in figs.items():
        fig, d = make()
        fig.savefig(out / f"{name}.png", dpi=200, bbox_inches="tight")
        plt.close(fig)
        data[name] = d
        print(f"{out / name}.png")
    (res / "figures_data.json").write_text(json.dumps(data, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
