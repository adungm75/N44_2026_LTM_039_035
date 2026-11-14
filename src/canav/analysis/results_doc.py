# ruff: noqa: E501  (Vietnamese prose templates)
"""Generate ``docs/results.md`` (Vietnamese) from the result files — every number in it is
computed here from ``results/`` and ``report/tables``, nothing is typed by hand.

CLI: ``python -m canav.analysis.results_doc`` (run by ``make figures`` after the figures).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from canav.analysis.tables import read_table

FIG = "../report/figures"


def _ms(x: float) -> str:
    return f"{x * 1e3:.2f} ms"


def _pct(x: float, nd: int = 2) -> str:
    return f"{x * 100:.{nd}f} %"


def build(results: Path, tables: Path) -> str:
    s = pd.read_parquet(results / "summary.parquet")
    fd = json.loads((results / "figures_data.json").read_text())
    budget = json.loads((results / "budget" / "aeb_budget.json").read_text())["design"]
    crit = read_table(tables / "criteria_summary.csv")
    out = [
        "# Kết quả",
        "",
        "Tài liệu này được sinh tự động bởi `python -m canav.analysis.results_doc` (`make figures`)",
        "từ `results/` và `report/tables/`; mọi số dưới đây đều tính từ các file đó. Phương pháp và",
        "giới hạn: `docs/experiments.md`.",
        "",
    ]

    def fig(n: int, name: str, title: str, lines: list[str]) -> None:
        out.extend([f"## Hình {n}. {title}", "", f"![{title}]({FIG}/{name}.png)", "", *lines, ""])

    f1 = fd["fig1_load_vs_bitrate"]
    th, sim = f1["theory"], f1["sim"]
    vcan = f1["vcan"]
    fig(
        1,
        "fig1_load_vs_bitrate",
        "Tải bus theo bitrate (E1)",
        [
            f"- Tải lý thuyết (stuffing worst-case) của ma trận baseline là {_pct(th['125000'], 1)} ở "
            f"125 kbit/s, {_pct(th['500000'])} ở 500 kbit/s và {_pct(th['1000000'])} ở 1 Mbit/s, tỉ lệ "
            "nghịch với bitrate.",
            f"- SimPy (trung bình 5 seed) lệch lý thuyết tối đa "
            f"{max(abs(sim[k] - th[k]) for k in th) * 100:.4f} điểm phần trăm."
            + (
                f" Log candump trên vcan0 (E0 online) cho {_pct(vcan)} ở 500 kbit/s."
                if vcan
                else " Chưa có log vcan0 (chạy `make experiments-online`)."
            ),
        ],
    )

    e0 = s[(s["experiment"] == "E0") & s["period"].notna()]
    g = e0.groupby("msg_key").agg(r=("R_max", "max"), w=("wcrt", "first"), d=("deadline", "first"))
    within = int((g["r"] <= g["w"] + 1e-9).sum())
    ratio = (g["r"] / g["d"]).max()
    fig(
        2,
        "fig2_response_times",
        "Thời gian đáp ứng từng ID so với WCRT và deadline (E0)",
        [
            f"- {within}/{len(g)} ID có R_max mô phỏng ≤ WCRT Davis; R_max/D lớn nhất là {ratio:.3f} và "
            f"độ dư nhỏ nhất D − WCRT là {_ms((g['d'] - g['w']).min())}.",
            f"- ObjectList_k mang D = {_ms(budget['deadlines_s']['ObjectList_0'])} (budget chuỗi AEB ở "
            f"{budget['v_kmh']:.0f} km/h) thay vì T = 50 ms; mọi ID khác D = T.",
        ],
    )

    f3 = fd["fig3_latency_vs_load"]
    e4 = s[(s["experiment"] == "E4") & s["period"].notna()]
    e4 = e4.assign(load=e4["params"].map(lambda p: json.loads(p)["network.target_load"]))
    low = e4[e4["msg_key"].str.slice(2, 5).map(lambda h: int(h, 16)) >= 0x400]
    sums = low.groupby("load")[["n_overwritten", "n"]].sum()
    ow = sums["n_overwritten"] / sums["n"]
    hi = f3["high (ID < 0x100)"]
    lo = f3["low (>= 0x400)"]
    fig(
        3,
        "fig3_latency_vs_load",
        "Độ trễ theo tải và lớp ưu tiên (E4)",
        [
            f"- Lớp ưu tiên cao (ID < 0x100) gần như không đổi: p99 {float(hi['0.3']):.2f} ms ở 30 % và "
            f"{float(hi['1.1']):.2f} ms ở 110 %; lớp thấp (≥ 0x400) tăng từ {float(lo['0.3']):.2f} ms lên "
            f"{max(float(v) for v in lo.values()):.2f} ms (lớn nhất trong dải).",
            f"- Từ 100 % tải, frame lớp thấp bị ghi đè trước khi gửi ({_pct(ow.loc[1.0], 1)} ở 100 %, "
            f"{_pct(ow.loc[1.1], 1)} ở 110 %), nên p99 của các frame còn gửi được không còn đại diện"
            + (" (p99 lớp thấp giảm ở 110 %)." if float(lo["1.1"]) < float(lo["1.0"]) else "."),
        ],
    )

    f4 = fd["fig4_id_scheme_aeb"]
    fig(
        4,
        "fig4_id_scheme_aeb",
        "safety_first vs naive cho AEB_BrakeRequest (E3)",
        [
            f"- Với `naive` (AEB ở 0x600), WCRT của AEB_BrakeRequest tăng từ "
            f"{float(f4['wcrt/naive']['0.3']):.2f} ms (30 %) lên {float(f4['wcrt/naive']['0.9']):.2f} ms "
            f"(90 %), vượt D = {_ms(budget['deadlines_s']['AEB_BrakeRequest'])}; R_max mô phỏng ở 90 % là "
            f"{float(f4['R_max/naive']['0.9']):.2f} ms.",
            f"- Với `safety_first` (0x010) WCRT giữ ở {float(f4['wcrt/safety_first']['0.9']):.2f} ms ở "
            "mọi mức tải: chỉ còn phụ thuộc blocking của frame dài nhất.",
        ],
    )

    f5 = fd["fig5_two_bus"]
    fig(
        5,
        "fig5_two_bus",
        "Một bus vs hai bus qua gateway (E5, P1)",
        [
            f"- AEB ở bus chassis: e2e chuỗi AEB lớn nhất {float(f5['chassis']['0.001']):.2f} ms ở mọi độ "
            f"trễ gateway, thấp hơn 1 bus ({_ms(f5['one_bus'])}) vì bus chassis ít tải hơn.",
            f"- AEB ở bus ADAS: AEB_BrakeRequest phải qua gateway, e2e tăng theo độ trễ gateway tới "
            f"{float(f5['adas']['0.001']):.2f} ms ở 1 ms; vẫn dưới giới hạn {_ms(budget['chain_e2e_limit_s'])}.",
        ],
    )

    f6 = fd["fig6_faults"]
    bo = {float(k): v for k, v in f6["busoff"].items()}
    first_bo = min(k for k, v in bo.items() if v > 0)
    last_ok = max(k for k, v in bo.items() if v == 0)
    fig(
        6,
        "fig6_faults",
        "Lỗi so với deadline chuỗi AEB và bus-off (E7)",
        [
            f"- Trễ thêm ở mọi node đẩy e2e chuỗi AEB từ {float(f6['E7_delay']['1.0']):.2f} ms (1 ms) lên "
            f"{float(f6['E7_delay']['20.0']):.2f} ms (20 ms), vượt giới hạn {_ms(budget['chain_e2e_limit_s'])}.",
            f"- Bus-off xuất hiện từ xác suất lỗi {first_bo:g} % (0 ở {last_ok:g} %); ngưỡng trôi TEC "
            f"p = 1/9 ≈ 11.1 % {'nằm' if last_ok < 100 / 9 < first_bo else 'không nằm'} trong khoảng "
            f"này. Trung bình {bo[max(bo)]:.1f} lần bus-off mỗi run 60 s ở {max(bo):g} %.",
            f"- Drop tới 10 % không đổi e2e chuỗi AEB ({float(f6['E7_drop']['10.0']):.2f} ms) vì e2e lấy "
            "origin mới nhất trong 8 frame ObjectList (giới hạn nêu ở `docs/experiments.md`).",
        ],
    )

    f7 = fd["fig7_detector_timeseries"]
    fig(
        7,
        "fig7_detector_timeseries",
        "Đặc trưng theo thời gian và báo động (E8/E9)",
        [
            f"- Nguồn: {f7['source']}. Mỗi loại anomaly làm một đặc trưng nhảy rõ trong khoảng anomaly: "
            "tải (flood), khoảng trống cùng ID / T (silent), lỗi counter (spoof), số ID lạ (unknown_id).",
            "- Vạch đỏ là cửa sổ 100 ms có báo động (k = 3); số cửa sổ báo động nằm hẳn ngoài khoảng "
            "anomaly trong khung hình: "
            + ", ".join(
                f"{k} {v['alarms_outside_interval_in_view']}"
                for k, v in f7.items()
                if k != "source"
            )
            + ".",
        ],
    )

    ev = read_table(tables / "detector_eval.csv")
    al = ev[ev["anomaly"] == "all"].sort_values(["dataset", "k"])
    sim_al = al[al["dataset"] == "sim"]
    fpr_note = (
        f"- SimPy: FPR {'không đổi' if sim_al['fpr'].nunique() == 1 else 'thay đổi'} theo k; FPR trên "
        f"các run không có anomaly là {_pct(sim_al['fpr_clean_runs'].max(), 3)} (báo động sai còn lại "
        "nằm ở các cửa sổ bình thường của run E8, xem `docs/experiments.md`)."
    )
    roc = "; ".join(
        f"{r.dataset} k={int(r.k)}: FPR {_pct(r.fpr, 3)}, TPR {_pct(r.tpr, 2)}"
        for r in al.itertuples()
    )
    fig(
        8,
        "fig8_roc",
        "ROC của bộ phát hiện theo k (E9)",
        [
            f"- {roc}.",
            fpr_note,
        ],
    )

    gw = read_table(tables / "gateway_latency.csv")
    b1 = gw[(gw["batch_frames"] == 1) & (gw["flush_ms"] == 1.0)].iloc[0]
    b32 = gw[(gw["batch_frames"] == 32) & (gw["flush_ms"] == 5.0)].iloc[0]
    fig(
        9,
        "fig9_gateway_latency",
        "Độ trễ gateway theo batching (Phase 5)",
        [
            f"- Batch 1: p50 {b1.lat_p50_us / 1e3:.2f} ms, p99 {b1.lat_p99_us / 1e3:.2f} ms; batch 32 với "
            f"timeout 5 ms: p50 {b32.lat_p50_us / 1e3:.2f} ms, p99 {b32.lat_p99_us / 1e3:.2f} ms nhưng "
            f"{b32.frames_per_datagram:.1f} frame/datagram thay vì 1.",
            f"- Mất gói UDP: {int(gw['udp_lost'].sum())} ở cả {len(gw)} cấu hình.",
        ],
    )

    out += [
        "## Kết luận theo từng tiêu chí (`report/tables/criteria_summary.csv`)",
        "",
        "| # | Thí nghiệm | Mục | Đo được | Ngưỡng | Kết luận |",
        "|---|---|---|---|---|---|",
    ]
    for r in crit.itertuples():
        cells = [
            str(r.criterion),
            r.experiment,
            r.item,
            str(r.measured),
            str(r.threshold),
            str(r.verdict),
        ]
        out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    out += [
        "",
        "Nguồn của mỗi ngưỡng nằm ở cột `threshold_source` của bảng CSV. `n/a`: tiêu chí không có "
        "ngưỡng (gateway, bộ phát hiện) hoặc chưa chạy (P1).",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", default="results")
    p.add_argument("--tables", default="report/tables")
    p.add_argument("--out", default="docs/results.md")
    a = p.parse_args(argv)
    Path(a.out).write_text(build(Path(a.results), Path(a.tables)))
    print(a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
