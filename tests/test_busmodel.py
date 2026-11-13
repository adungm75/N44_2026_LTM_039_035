import random

import pytest
import simpy

from canav.busmodel import (
    Bus,
    FaultInjector,
    FaultRule,
    Gateway,
    MessageSpec,
    Node,
    PeriodicTask,
    SimLogger,
    s_to_ns,
)
from canav.timing import frame_bits


def _bus(env, bitrate, **kw):
    log = SimLogger()
    return Bus(env, "b", bitrate, "worst", logger=log, **kw), log


def _source(env, bus, node_name, name, can_id, dlc, period_s, offset_s=0.0, **kw):
    node = bus.attach(Node(node_name))
    spec = MessageSpec(name, can_id, dlc)
    return PeriodicTask(env, node, [spec], s_to_ns(period_s), s_to_ns(offset_s), **kw)


def _r_max(log, name):
    return max(f.t_tx_end - f.t_queued for f in log.frames if f.name == name and f.outcome == "ok")


def test_davis_example_exact():
    """§5.4 rule 5: R_C = 3.5 ms exactly (needs releases before arbitration at equal t)."""
    env = simpy.Environment()
    bus, log = _bus(env, 125_000)
    _source(env, bus, "nA", "A", 1, 7, 0.0025)
    _source(env, bus, "nB", "B", 2, 7, 0.0035)
    _source(env, bus, "nC", "C", 3, 7, 0.0035)
    env.run(until=s_to_ns(0.0875))  # 5 hyperperiods of 17.5 ms
    assert _r_max(log, "C") == 3_500_000
    assert _r_max(log, "B") <= 3_000_000
    assert _r_max(log, "A") <= 2_000_000
    assert all(f.outcome == "ok" for f in log.frames)


def test_frame_duration_and_priority():
    env = simpy.Environment()
    bus, log = _bus(env, 500_000)
    _source(env, bus, "lo", "LO", 0x200, 8, 0.01)
    _source(env, bus, "hi", "HI", 0x100, 4, 0.01)
    env.run(until=s_to_ns(0.005))
    hi, lo = (next(f for f in log.frames if f.name == n) for n in ("HI", "LO"))
    assert hi.t_tx_start == 0 and hi.t_tx_end == frame_bits(4) * 2000
    assert lo.t_tx_start == hi.t_tx_end and lo.t_tx_end - lo.t_tx_start == 135 * 2000


def test_non_preemptive_blocking():
    env = simpy.Environment()
    bus, log = _bus(env, 500_000)
    _source(env, bus, "lo", "LO", 0x500, 8, 0.01)
    _source(env, bus, "hi", "HI", 0x010, 4, 0.01, offset_s=0.0001)  # arrives mid-frame
    env.run(until=s_to_ns(0.005))
    hi = next(f for f in log.frames if f.name == "HI")
    assert hi.t_tx_start == 270_000  # waits for the 135-bit frame to finish


def test_overwrite_on_overload():
    # 8-byte frame = 1.08 ms at 125k, released every 1 ms -> instances pile up
    env = simpy.Environment()
    bus, log = _bus(env, 125_000)
    _source(env, bus, "n", "A", 1, 8, 0.001)
    env.run(until=s_to_ns(0.2))
    outcomes = {f.outcome for f in log.frames}
    assert "overwritten" in outcomes and "ok" in outcomes

    env = simpy.Environment()
    bus, log = _bus(env, 125_000, overwrite=False)
    _source(env, bus, "n", "A", 1, 8, 0.001)
    env.run(until=s_to_ns(0.2))
    assert {f.outcome for f in log.frames} == {"ok"}


def test_fifo_queue_head_only():
    env = simpy.Environment()
    bus, log = _bus(env, 500_000)
    n = bus.attach(Node("n", tx_queue="fifo"))
    other = bus.attach(Node("o"))
    PeriodicTask(env, n, [MessageSpec("LOW", 0x300, 8), MessageSpec("HIGH", 0x050, 8)], 10**7)
    PeriodicTask(env, other, [MessageSpec("MID", 0x100, 8)], 10**7)
    env.run(until=s_to_ns(0.005))
    order = [f.name for f in sorted(log.frames, key=lambda f: f.t_tx_start)]
    # LOW blocks HIGH inside the FIFO; MID wins against the FIFO head LOW
    assert order == ["MID", "LOW", "HIGH"]


def test_busoff_with_high_error_prob():
    env = simpy.Environment()
    rng = random.Random(1)
    faults = FaultInjector([FaultRule(nodes=("bad",), error_prob=1.0, end=0.05)], rng)
    bus, log = _bus(env, 500_000, faults=faults, rng=rng)
    _source(env, bus, "bad", "BAD", 0x100, 8, 0.01)
    _source(env, bus, "good", "GOOD", 0x200, 8, 0.01)
    env.run(until=s_to_ns(0.2))
    bad = next(n for n in bus.nodes if n.name == "bad")
    # TEC +8 per error: the 32nd consecutive error (256 > 255) causes bus-off
    assert [e.state for e in log.errors[:32]] == ["active"] * 15 + ["passive"] * 16 + ["busoff"]
    assert bad.max_tec > 255
    # recovery after 128*11 bits (2.8 ms) -> errors again while the fault window lasts
    assert bad.busoff_count == sum(e.state == "busoff" for e in log.errors) > 1
    assert any(f.outcome == "busoff" and f.name == "BAD" for f in log.frames)
    # recovered after 128*11 bits and the fault window ended -> transmits again
    assert bad.state == "active"
    assert any(f.outcome == "ok" and f.name == "BAD" for f in log.frames)
    assert all(f.outcome == "ok" for f in log.frames if f.name == "GOOD")


def test_busoff_without_recovery():
    env = simpy.Environment()
    rng = random.Random(1)
    faults = FaultInjector([FaultRule(nodes=("bad",), error_prob=1.0, end=0.05)], rng)
    bus, log = _bus(env, 500_000, faults=faults, rng=rng, busoff_recovery=False)
    _source(env, bus, "bad", "BAD", 0x100, 8, 0.01)
    env.run(until=s_to_ns(0.2))
    assert not any(f.outcome == "ok" for f in log.frames)


def test_retransmission_after_error():
    env = simpy.Environment()
    rng = random.Random(3)
    faults = FaultInjector([FaultRule(error_prob=0.5)], rng)
    bus, log = _bus(env, 500_000, faults=faults, rng=rng)
    _source(env, bus, "n", "A", 0x100, 8, 0.01)
    env.run(until=s_to_ns(1.0))
    ok = [f for f in log.frames if f.outcome == "ok"]
    assert any(f.attempts > 1 for f in ok) and log.errors
    # every attempt is either a success or a logged error (minus a frame pending at the end)
    assert 0 <= len(ok) + len(log.errors) - sum(f.attempts for f in log.frames) <= 1


def test_drop_and_extra_delay():
    env = simpy.Environment()
    rng = random.Random(0)
    faults = FaultInjector(
        [FaultRule(messages=("A",), drop_prob=1.0), FaultRule(messages=("B",), extra_delay=0.002)],
        rng,
    )
    bus, log = _bus(env, 500_000, faults=faults, rng=rng)
    _source(env, bus, "n1", "A", 0x100, 8, 0.01, faults=faults)
    _source(env, bus, "n2", "B", 0x200, 8, 0.01, faults=faults)
    env.run(until=s_to_ns(0.1))
    assert {f.outcome for f in log.frames if f.name == "A"} == {"dropped"}
    b = [f for f in log.frames if f.name == "B"]
    assert all(f.t_queued - f.t_release == 2_000_000 for f in b)


def test_consumer_inherits_freshest_origin():
    env = simpy.Environment()
    bus, log = _bus(env, 500_000)
    _source(env, bus, "sens", "S", 0x200, 8, 0.01)
    ctrl = bus.attach(Node("ctrl"))
    PeriodicTask(
        env, ctrl, [MessageSpec("CMD", 0x100, 4)], 10**7, 5 * 10**6, exec_ns=10**6, inputs=["S"]
    )
    env.run(until=s_to_ns(0.1))
    cmd = sorted((f for f in log.frames if f.name == "CMD"), key=lambda f: f.instance)
    assert cmd[0].origin_ts == 0  # sensor sample at t=0, consumed at 5 ms
    assert cmd[3].origin_ts == 30_000_000
    assert cmd[3].t_queued == 36_000_000  # release 35 ms + exec 1 ms


def test_gateway_forwards_with_delay():
    env = simpy.Environment()
    log = SimLogger()
    a = Bus(env, "a", 500_000, logger=log)
    b = Bus(env, "b", 500_000, logger=log)
    Gateway(env, a, b, {"a_to_b": [0x100]}, delay=0.0005)
    _source(env, a, "src", "X", 0x100, 8, 0.01)
    _source(env, a, "src2", "Y", 0x101, 8, 0.01)
    env.run(until=s_to_ns(0.05))
    on_a = sorted(
        (f for f in log.frames if f.bus == "a" and f.name == "X"), key=lambda f: f.instance
    )
    on_b = sorted((f for f in log.frames if f.bus == "b"), key=lambda f: f.instance)
    assert {f.name for f in on_b} == {"X"} and on_b[0].sender == "Gateway"
    assert on_b[0].t_queued == on_a[0].t_tx_end + 500_000
    assert on_b[0].origin_ts == on_a[0].origin_ts == 0


def test_fd_not_implemented():
    with pytest.raises(NotImplementedError):
        Bus(simpy.Environment(), "fd", 500_000, fd=True)
