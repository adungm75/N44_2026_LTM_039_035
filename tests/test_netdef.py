from dataclasses import replace
from pathlib import Path

import cantools
import pytest

from canav import e2e
from canav.netdef import (
    NetdefError,
    SignalDef,
    apply_id_scheme,
    check,
    load_id_schemes,
    load_matrix,
    load_network,
    validate,
)
from canav.netdef.dbc import dbc_string

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def net():
    return load_matrix(ROOT / "config/network/matrix.yaml")


def test_matrix_matches_spec(net):
    assert len(net.nodes) == 10
    ids = {m.name: m.can_id for m in net.messages}
    assert ids["AEB_BrakeRequest"] == 0x010
    assert [ids[f"ObjectList_{k}"] for k in range(8)] == list(range(0x300, 0x308))
    assert ids["Diag_Request"] == 0x7DF and ids["Diag_Response"] == 0x7E8
    assert all(m.e2e for m in net.messages if m.is_periodic)
    diag = net.message("Diag_Request")
    assert not diag.is_periodic and set(diag.receivers) == set(net.nodes) - {"Gateway"}


def _with_message(net, name, **changes):
    msgs = tuple(replace(m, **changes) if m.name == name else m for m in net.messages)
    return replace(net, messages=msgs)


def _with_signals(net, name, signals):
    return _with_message(net, name, signals=tuple(signals))


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda n: _with_message(n, "IMU", can_id=0x200), "duplicate CAN ID"),
        (lambda n: _with_message(n, "IMU", dlc=9), "DLC 9"),
        (lambda n: _with_message(n, "IMU", receivers=("Nobody",)), "unknown receiver"),
        (lambda n: _with_message(n, "IMU", sender="Nobody"), "unknown sender"),
        (lambda n: _with_message(n, "IMU", can_id=0x800), "out of range"),
        (
            lambda n: _with_signals(n, "IMU", [SignalDef("a", 0, 8), SignalDef("b", 4, 8)]),
            "overlaps another signal",
        ),
        # bits 48-51 hold the alive counter on an 8-byte frame
        (lambda n: _with_signals(n, "IMU", [SignalDef("a", 44, 8)]), "overlaps E2E"),
        # byte 3 is the CRC on a 4-byte frame
        (lambda n: _with_signals(n, "Steer_Cmd", [SignalDef("a", 24, 8)]), "overlaps E2E"),
        (lambda n: _with_signals(n, "Diag_Request", [SignalDef("a", 60, 8)]), "exceeds DLC"),
        (lambda n: _with_message(n, "VehicleStatus", dlc=6), "E2E requires DLC"),
        (
            lambda n: _with_signals(n, "Steer_Cmd", [SignalDef("s", 0, 8, 0.001, maximum=1.0)]),
            "maximum",
        ),
    ],
)
def test_validator_rejects(net, mutate, needle):
    errors = validate(mutate(net))
    assert any(needle in e for e in errors), errors
    with pytest.raises(NetdefError):
        check(mutate(net))


def test_id_schemes(net):
    schemes = load_id_schemes(ROOT / "config/network/id_schemes.yaml")
    assert set(schemes) == {"safety_first", "naive"}
    assert apply_id_scheme(net, schemes["safety_first"]) == net
    naive = apply_id_scheme(net, schemes["naive"])
    assert naive.message("AEB_BrakeRequest").can_id == 0x600
    assert naive.message("Diag_Request").can_id == 0x7DF
    with pytest.raises(NetdefError):
        apply_id_scheme(net, {"NoSuchMessage": 0x123})
    with pytest.raises(NetdefError):
        apply_id_scheme(net, {"IMU": 0x200})  # collides with WheelSpeed


def _sample_values(db_msg):
    """Deterministic in-range physical values for every non-E2E signal."""
    values = {}
    for i, s in enumerate(db_msg.signals):
        if s.name in (e2e.COUNTER_SIGNAL, e2e.CRC_SIGNAL):
            values[s.name] = 0
            continue
        lo = -(1 << (s.length - 1)) if s.is_signed else 0
        hi = (1 << (s.length - 1)) - 1 if s.is_signed else (1 << s.length) - 1
        raw = [lo, hi, (lo + hi) // 2 + i][i % 3]
        phys = raw * s.scale + s.offset
        if s.minimum is not None:
            phys = max(phys, s.minimum)
        if s.maximum is not None:
            phys = min(phys, s.maximum)
        values[s.name] = phys
    return values


def test_dbc_roundtrip_all_messages(net):
    db = cantools.database.load_string(dbc_string(net), database_format="dbc")
    assert {m.name for m in db.messages} == {m.name for m in net.messages}
    assert {n.name for n in db.nodes} == set(net.nodes)
    for m in net.messages:
        dm = db.get_message_by_name(m.name)
        assert dm.frame_id == m.can_id and dm.length == m.dlc and dm.senders == [m.sender]
        assert dm.cycle_time == (round(m.period * 1000) if m.is_periodic else None)
        values = _sample_values(dm)
        payload = dm.encode(values)
        if m.e2e:
            payload = e2e.protect(m.can_id, payload, 9)
            assert e2e.check(m.can_id, payload).ok
        decoded = dm.decode(payload)
        for s in dm.signals:
            if s.name == e2e.CRC_SIGNAL:
                continue
            want = 9 if s.name == e2e.COUNTER_SIGNAL else values[s.name]
            assert decoded[s.name] == pytest.approx(want, abs=s.scale / 2), (m.name, s.name)


def test_committed_dbc_is_generated():
    """config/dbc/canav.dbc must equal the generator output (never hand-edited)."""
    net = load_network(
        ROOT / "config/network/matrix.yaml", schemes_path=ROOT / "config/network/id_schemes.yaml"
    )
    assert (ROOT / "config/dbc/canav.dbc").read_bytes() == dbc_string(net).encode()
