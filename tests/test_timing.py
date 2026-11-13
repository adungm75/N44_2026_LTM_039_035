import random
from pathlib import Path

import pytest

from canav.netdef import load_network
from canav.timing import (
    TimingMessage,
    bus_load_theory,
    count_stuff_bits,
    davis_wcrt,
    frame_bits,
    frame_bits_actual,
    timing_messages,
)

ROOT = Path(__file__).resolve().parents[1]
NET_PATHS = (
    ROOT / "config/network/matrix.yaml",
    "safety_first",
    ROOT / "config/network/id_schemes.yaml",
)


def test_frame_bits_spec_values():
    assert frame_bits(8) == 135
    assert frame_bits(4) == 95
    assert frame_bits(7) == 125
    assert frame_bits(8, "none") == 111
    assert frame_bits(0, "none") == 47


def test_frame_bits_rejects_bad_input():
    with pytest.raises(ValueError):
        frame_bits(9)
    with pytest.raises(ValueError):
        frame_bits(8, "actual")
    with pytest.raises(ValueError):
        frame_bits(8, "bogus")


def test_count_stuff_bits():
    assert count_stuff_bits([0] * 4) == 0
    assert count_stuff_bits([0] * 5) == 1
    # 0 0 0 0 0 [1] 1 1 1: the stuff bit starts a run of ones of length 4
    assert count_stuff_bits([0] * 5 + [1] * 3) == 1
    # 0 0 0 0 0 [1] 1 1 1 1 [0]: stuff bit + 4 ones = run of 5 -> second stuff bit
    assert count_stuff_bits([0] * 5 + [1] * 4) == 2
    assert count_stuff_bits([0] * 10) == 2


@pytest.mark.parametrize("extended", [False, True])
def test_actual_between_none_and_worst(extended):
    rng = random.Random(1)
    max_id = (1 << 29) - 1 if extended else 0x7FF
    for _ in range(500):
        dlc = rng.randint(0, 8)
        data = bytes(rng.choice([0x00, 0xFF, rng.randrange(256)]) for _ in range(dlc))
        bits = frame_bits_actual(rng.randint(0, max_id), data, extended)
        assert frame_bits(dlc, "none", extended) <= bits <= frame_bits(dlc, "worst", extended)


def _davis_example():
    # 125 kbit/s, three 7-byte frames: 125 bits * 8 us = 1.0 ms
    bits = frame_bits(7)
    return [
        TimingMessage("A", 1, bits, period=0.0025, deadline=0.0025),
        TimingMessage("B", 2, bits, period=0.0035, deadline=0.00325),
        TimingMessage("C", 3, bits, period=0.0035, deadline=0.00325),
    ]


def test_davis_example():
    res = {r.name: r for r in davis_wcrt(_davis_example(), 125_000)}
    assert res["A"].c == pytest.approx(1.0e-3)
    assert res["A"].r == pytest.approx(2.0e-3, abs=1e-12)
    assert res["B"].r == pytest.approx(3.0e-3, abs=1e-12)
    assert res["C"].r == pytest.approx(3.5e-3, abs=1e-12)
    assert res["A"].schedulable and res["B"].schedulable
    assert not res["C"].schedulable


def test_wcrt_diverges_above_full_load():
    bits = frame_bits(8)
    msgs = [TimingMessage(f"m{i}", i, bits, 0.0005, 0.0005) for i in range(2)]
    res = davis_wcrt(msgs, 500_000)  # 2 * 270 us per 500 us > 100 %
    assert res[1].r is None and not res[1].schedulable


@pytest.fixture(scope="module")
def baseline():
    return timing_messages(load_network(*NET_PATHS), "worst")


def test_baseline_load(baseline):
    load = bus_load_theory(baseline, 500_000)
    assert load * 500_000 == pytest.approx(87_900)
    assert load * 100 == pytest.approx(17.58, abs=0.01)


def test_baseline_wcrt(baseline):
    res = davis_wcrt(baseline, 500_000)
    assert len(res) == 18  # diagnostics are event messages: blocking only
    assert res[0].can_id == 0x010
    assert res[0].r == pytest.approx(460e-6, abs=1e-12)
    assert all(r.schedulable for r in res)


def test_naive_scheme_moves_aeb_down(baseline):
    naive = timing_messages(load_network(NET_PATHS[0], "naive", NET_PATHS[2]), "worst")
    r_safe = {r.name: r.r for r in davis_wcrt(baseline, 500_000)}
    r_naive = {r.name: r.r for r in davis_wcrt(naive, 500_000)}
    assert r_naive["AEB_BrakeRequest"] > r_safe["AEB_BrakeRequest"]
