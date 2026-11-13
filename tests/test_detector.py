from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from canav import e2e
from canav.analysis.detector import Detector, evaluate, label_windows, sensitivity
from canav.analysis.features import periodic_ids, window_features
from canav.ecu import load_dbc
from canav.experiments.sim import SimConfig, run_sim
from canav.netdef import load_network

ROOT = Path(__file__).resolve().parents[1]
S = 1_000_000_000


@pytest.fixture(scope="module")
def db():
    return load_dbc(ROOT / "config/dbc/canav.dbc")


def _sim(anomalies=(), seed=1, duration=4.0) -> pd.DataFrame:
    doc = yaml.safe_load((ROOT / "config/experiments/E0_baseline.yaml").read_text())["sim"]
    cfg = SimConfig.from_dict(doc | {"duration": duration, "anomalies": list(anomalies)})
    res = run_sim(load_network(), cfg, seed)
    ok = sorted((f for f in res.frames if f.outcome == "ok"), key=lambda f: f.t_tx_end)
    return pd.DataFrame(
        {
            "t_ns": [f.t_tx_end for f in ok],
            "can_id": [f.can_id for f in ok],
            "ext": [f.extended for f in ok],
            "data": [f.data for f in ok],
        }
    )


def test_features_normal_run(db):
    df = window_features(_sim(), db, 500_000, S, 4 * S)
    assert len(df) == 30  # 3 s of complete 100 ms windows
    assert df["load"].mean() == pytest.approx(0.1758, abs=0.01)
    assert (df["unknown"] == 0).all() and (df["counter_err"] == 0).all()
    assert (df["crc_err"] == 0).all()
    assert df["n_200"].eq(10).all()  # WheelSpeed, 10 ms
    assert df["gap_ratio_max"].max() < 2.5


def test_features_see_anomalies(db):
    iv = {"start": 2.0, "end": 3.0}
    feats = {
        t: window_features(_sim([{"type": t} | iv]), db, 500_000, S, 4 * S)
        for t in ("flood", "silent", "spoof", "unknown_id")
    }
    inside = slice(10, 20)  # windows [2.0, 3.0)
    assert (feats["flood"]["load"].iloc[inside] > 0.9).all()
    assert (feats["flood"]["unknown"].iloc[inside] > 0).all()  # 0x000 is not in the DBC
    assert (feats["silent"]["gap_300"].iloc[inside][1:] > 0.125).all()  # ObjectList_0 silent
    assert (feats["spoof"]["counter_err"].iloc[inside] > 0).all()
    assert feats["unknown_id"]["unknown"].iloc[inside].sum() == 10  # 0x6A1 every 100 ms


def test_detector_rules_and_eval(db):
    train = pd.concat(
        [window_features(_sim(seed=s), db, 500_000, S, 4 * S) for s in (1, 2)],
        ignore_index=True,
    )
    det = Detector(periodic_ids(db), 500_000).fit(train)
    runs = []
    for t in ("flood", "silent", "spoof", "unknown_id"):
        df = window_features(
            _sim([{"type": t, "start": 2.0, "end": 3.0}], seed=3), db, 500_000, S, 4 * S
        )
        runs.append((df, [{"type": t, "start_ns": 2 * S, "end_ns": 3 * S}]))
    runs.append((window_features(_sim(seed=4), db, 500_000, S, 4 * S), []))
    r = det.rules(runs[0][0], 3)
    lab = label_windows(runs[0][0], runs[0][1])
    assert r.loc[lab == "flood", "R4"].all() and r.loc[lab == "flood", "R1"].all()
    ev = evaluate(det, runs, ks=(3,))
    rows = {x["anomaly"]: x for x in ev["rows"]}
    assert set(rows) == {"all", "flood", "silent", "spoof", "unknown_id"}
    for t in ("flood", "spoof", "unknown_id"):
        assert rows[t]["tpr"] == 1.0 and rows[t]["missed"] == 0
    assert rows["silent"]["tpr"] >= 0.9
    assert rows["all"]["fpr_clean_runs"] == 0.0
    sens = {(x["feature"], x["anomaly"]): x["sensitivity"] for x in sensitivity(train, runs)}
    assert sens[("unknown", "unknown_id")] == np.inf and sens[("counter_err", "spoof")] == np.inf


def test_spoof_payload_has_valid_crc_and_frozen_counter():
    data = e2e.protect(0x100, bytes(4), 0)
    assert e2e.check(0x100, data).crc_ok
    assert not e2e.check(0x100, data, last_counter=5).counter_ok
