import json
import threading
import uuid
from pathlib import Path

import can
import numpy as np
import pandas as pd
import pytest
import yaml

from canav import e2e
from canav.analysis.online import OriginResolver, hop_latency
from canav.ecu import EcuBase, FaultConfig, load_dbc, make_bus

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def db():
    return load_dbc(ROOT / "config/dbc/canav.dbc")


@pytest.fixture
def params():
    return yaml.safe_load((ROOT / "config/online/ecu_params.yaml").read_text())


class _Sender(EcuBase):
    def cycle(self, t_ns):
        self.send("Steer_Cmd", {"steer": 0.25}, {"TargetPath_0": 3})


class _Receiver(EcuBase):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.values = []

    def on_rx(self, msg, values, counter, t_ns):
        self.values.append((msg, values["steer"], counter))


def _run(ecus, seconds):
    """Run ECUs for ``seconds`` each; ECUs listed first get 0.2 s extra (receivers listen
    until the last frame of the senders has arrived, whatever the thread start skew)."""
    stop = threading.Event()
    threads = [
        threading.Thread(target=e.run, args=(stop, seconds + (0.2 if i < len(ecus) - 1 else 0)))
        for i, e in enumerate(ecus)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=seconds + 5)
    for e in ecus:
        e.bus.shutdown()


def _events(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_fault_config_parse():
    f = FaultConfig.parse("drop_prob=0.05,extra_delay_ms=2,silent_after_s=10")
    assert (f.drop_prob, f.extra_delay_ms, f.silent_after_s) == (0.05, 2.0, 10.0)
    assert not FaultConfig.parse("").active()
    with pytest.raises(ValueError):
        FaultConfig.parse("bogus=1")


def test_tx_rx_e2e_and_no_drift(tmp_path, db, params):
    ch = uuid.uuid4().hex
    tx = _Sender("Control", make_bus("virtual", ch), db, params, tmp_path / "Control.jsonl")
    rx = _Receiver(
        "SteeringActuator", make_bus("virtual", ch), db, params, tmp_path / "Steer.jsonl"
    )
    _run([rx, tx], 0.5)
    assert tx.period_ns == 10_000_000  # GenMsgCycleTime of Control's messages
    sent = [e for e in _events(tmp_path / "Control.jsonl") if e["msg"] == "Steer_Cmd"]
    assert 45 <= len(sent) <= 51  # 0.5 s at 10 ms on absolute deadlines
    assert sent[0]["src"] == {"TargetPath_0": 3}
    assert [e["counter"] for e in sent[:17]] == list(range(16)) + [0]
    t = np.array([e["t_ns"] for e in sent])
    assert abs(np.mean(np.diff(t)) - 10e6) < 0.3e6  # no drift
    got = [v for v in rx.values if v[0] == "Steer_Cmd"]
    assert len(got) >= len(sent) - 1 and got[0][1] == pytest.approx(0.25, abs=1e-4)
    assert rx.stats["crc_err"] == rx.stats["counter_err"] == 0
    assert json.loads((tmp_path / "Steer.stats.json").read_text())["rx"] == rx.stats["rx"]


def test_rx_detects_crc_and_counter_errors(tmp_path, db, params):
    ch = uuid.uuid4().hex
    rx = _Receiver(
        "SteeringActuator", make_bus("virtual", ch), db, params, tmp_path / "Steer.jsonl"
    )
    inject = make_bus("virtual", ch)
    msg = db.get_message_by_name("Steer_Cmd")
    good = [
        e2e.protect(0x100, msg.encode({"steer": 0.1, "AliveCounter": 0, "CRC8": 0}), c)
        for c in (0, 1, 3)
    ]
    bad = bytearray(good[1])
    bad[0] ^= 0xFF  # corrupt payload, CRC no longer matches
    stop = threading.Event()
    th = threading.Thread(target=rx.run, args=(stop, 1.0))
    th.start()
    for data in (good[0], bytes(bad), good[1], good[2]):
        inject.send(can.Message(arbitration_id=0x100, data=data, is_extended_id=False))
    th.join(timeout=5)
    inject.shutdown()
    rx.bus.shutdown()
    kinds = [e["event"] for e in _events(tmp_path / "Steer.jsonl")]
    assert kinds == ["rx", "crc_err", "rx", "counter_err", "rx"]  # counter 1 -> 3: one lost
    assert len(rx.values) == 3  # the corrupted frame is not used


def test_faults_drop_and_silent(tmp_path, db, params):
    ch = uuid.uuid4().hex
    f = FaultConfig(drop_prob=0.5, silent_after_s=0.3)
    tx = _Sender("Control", make_bus("virtual", ch), db, params, tmp_path / "C.jsonl", fault=f)
    _run([tx], 0.6)
    ev = [e for e in _events(tmp_path / "C.jsonl") if e["msg"] == "Steer_Cmd"]
    kinds = {e["event"] for e in ev}
    assert kinds == {"tx", "drop"}
    assert max(e["t_ns"] for e in ev) - min(e["t_ns"] for e in ev) < 0.32e9  # silent after 0.3 s
    counters = [e["counter"] for e in ev]
    assert counters == [i % 16 for i in range(len(counters))]  # drops still consume counters


def test_hop_latency_and_e2e_chain():
    ev = pd.DataFrame(
        [
            # sensor tx -> planning uses it -> control uses planning -> actuator applies
            {
                "t_ns": 1000,
                "ecu": "S",
                "event": "tx",
                "msg": "WheelSpeed",
                "counter": 4,
                "src": None,
            },
            {
                "t_ns": 1300,
                "ecu": "P",
                "event": "rx",
                "msg": "WheelSpeed",
                "counter": 4,
                "src": None,
            },
            {
                "t_ns": 2000,
                "ecu": "S",
                "event": "tx",
                "msg": "WheelSpeed",
                "counter": 5,
                "src": None,
            },
            {
                "t_ns": 3000,
                "ecu": "P",
                "event": "tx",
                "msg": "TargetPath_0",
                "counter": 1,
                "src": {"WheelSpeed": 4},
            },
            {
                "t_ns": 3500,
                "ecu": "C",
                "event": "tx",
                "msg": "Steer_Cmd",
                "counter": 9,
                "src": {"TargetPath_0": 1, "WheelSpeed": 5},
            },
            {
                "t_ns": 3800,
                "ecu": "SteeringActuator",
                "event": "use",
                "msg": "Steer_Cmd",
                "counter": 9,
                "src": {"Steer_Cmd": 9},
            },
        ]
    )
    hops = hop_latency(ev)
    assert hops["latency_s"].tolist() == [300e-9]
    e2e_df = OriginResolver(ev).e2e(ev[ev["event"] == "use"])
    # freshest origin among the chain inputs = WheelSpeed #5 at t=2000
    assert e2e_df["e2e_s"].tolist() == [pytest.approx(1800e-9)]
