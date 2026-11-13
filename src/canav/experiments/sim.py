"""Build and run the SimPy bus model from a :class:`canav.netdef.Network` (engine ``sim``)."""

from __future__ import annotations

import dataclasses
import math
import random
from collections import defaultdict
from dataclasses import dataclass, field, replace

import pandas as pd
import simpy

from canav.analysis.metrics import MessageInfo
from canav.busmodel import (
    SIDECAR_FIELDS,
    Bus,
    ErrorEvent,
    FaultInjector,
    Frame,
    Gateway,
    MessageSpec,
    Node,
    PeriodicTask,
    SimLogger,
    s_to_ns,
)
from canav.experiments import anomalies
from canav.netdef import Network
from canav.timing import WcrtResult, bus_load_theory, davis_wcrt, timing_messages

OFFSET_MODES = ("zero", "random")
FRAME_COLUMNS = [*SIDECAR_FIELDS, "dlc"]
NULLABLE_TIME_COLUMNS = ("t_queued", "t_tx_start", "t_tx_end", "origin_ts")


@dataclass
class SimConfig:
    """``sim:`` section of an experiment YAML. Times in seconds."""

    duration: float = 60.0
    seeds: list[int] = field(default_factory=lambda: [1])
    bitrate: int | None = None  # override every bus bitrate
    stuffing: str = "worst"
    tx_queue: str = "priority"
    overwrite: bool = True
    # zero: every message released at k*T; random: one uniform offset per node in
    # [0, longest period of the node), applied modulo each message period.
    offsets: str = "random"
    error_frame_bits: int = 31
    busoff_recovery: bool = True
    tasks: dict[str, dict] = field(default_factory=dict)  # node -> {exec_time: s}
    actuators: list[str] = field(default_factory=list)
    faults: list[dict] = field(default_factory=list)
    anomalies: list[dict] = field(default_factory=list)  # E8, see canav.experiments.anomalies

    @classmethod
    def from_dict(cls, d: dict | None) -> SimConfig:
        d = dict(d or {})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown sim config keys: {unknown}")
        cfg = cls(**d)
        if cfg.offsets not in OFFSET_MODES:
            raise ValueError(f"offsets must be one of {OFFSET_MODES}")
        for task in cfg.tasks.values():
            extra = set(task) - {"exec_time"}
            if extra:
                raise ValueError(f"unknown task keys: {sorted(extra)}")
        return cfg

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class SimResult:
    frames: list[Frame]
    errors: list[ErrorEvent]
    nodes: dict[str, Node]
    bitrates: dict[str, int]
    duration_ns: int

    def frames_df(self, run_id: str) -> pd.DataFrame:
        df = pd.DataFrame([f.record(run_id) for f in self.frames], columns=FRAME_COLUMNS)
        # nullable integers: log times stay int ns (no float conversion on missing values)
        return df.astype({c: "Int64" for c in NULLABLE_TIME_COLUMNS})

    def errors_df(self, run_id: str) -> pd.DataFrame:
        rows = [dataclasses.asdict(e) | {"run_id": run_id} for e in self.errors]
        return pd.DataFrame(rows)


def bitrates(net: Network, cfg: SimConfig) -> dict[str, int]:
    return {name: cfg.bitrate or br for name, br in net.buses.items()}


def theory(
    net: Network, cfg: SimConfig
) -> tuple[dict[tuple[str, int], float | None], dict[str, float]]:
    """(WCRT per (bus, CAN ID), theoretical load per bus); ``actual`` stuffing uses the worst
    bound. A gateway copy gets release jitter = gateway delay + (R - C) of its source message,
    iterated to a fixed point because both buses can forward to each other."""
    stuffing = "worst" if cfg.stuffing == "actual" else cfg.stuffing
    rates = bitrates(net, cfg)
    msgs = {bus: timing_messages(net, stuffing, bus) for bus in rates}
    load = {bus: bus_load_theory(m, rates[bus]) for bus, m in msgs.items()}
    fwd = [m for m in net.messages if m.forwarded_from is not None and m.is_periodic]
    jit: dict[tuple[str, int], float] = {}
    res: dict[tuple[str, int], WcrtResult] = {}
    for _ in range(50):
        res = {}
        for bus, br in rates.items():
            tms = [replace(t, jitter=jit.get((bus, t.can_id), t.jitter)) for t in msgs[bus]]
            res.update({(bus, r.can_id): r for r in davis_wcrt(tms, br)})
        new = {}
        for m in fwd:
            src = res[(m.forwarded_from, m.can_id)]
            new[(m.bus, m.can_id)] = (
                math.inf if src.r is None else net.gateway_delay + src.r - src.c
            )
        if new == jit or any(math.isinf(v) for v in new.values()):
            break
        jit = new
    wcrt = {k: r.r for k, r in res.items()}
    for k, v in jit.items():  # copy of an unschedulable source: no bound
        if math.isinf(v):
            wcrt[k] = None
    return wcrt, load


def message_info(
    net: Network, cfg: SimConfig, wcrt: dict[tuple[str, int], float | None] | None = None
) -> dict[tuple[str, int], MessageInfo]:
    """Keyed by (bus, CAN ID): gateway copies share the ID of their source message."""
    if wcrt is None:
        wcrt, _ = theory(net, cfg)
    act = set(cfg.actuators)
    return {
        (m.bus, m.can_id): MessageInfo(
            name=m.name,
            bus=m.bus,
            period=m.period,
            deadline=m.deadline,
            wcrt=wcrt.get((m.bus, m.can_id)),
            to_actuator=bool(act & set(m.receivers)),
            forwarded=m.forwarded_from is not None,
        )
        for m in net.messages
    }


def run_sim(net: Network, cfg: SimConfig, seed: int) -> SimResult:
    """Simulate all periodic messages of ``net`` for ``cfg.duration`` seconds."""
    for name in list(cfg.tasks) + list(cfg.actuators):
        if name not in net.nodes:
            raise ValueError(f"unknown node {name!r} in sim config")
    rng = random.Random(seed)
    env = simpy.Environment()
    logger = SimLogger()
    faults = FaultInjector.from_config(cfg.faults, rng) if cfg.faults else None
    silent = anomalies.silent_rules(cfg.anomalies)
    if silent:
        faults = FaultInjector((faults.rules if faults else ()) + tuple(silent), rng)
    rates = bitrates(net, cfg)
    buses = {
        name: Bus(
            env,
            name,
            br,
            cfg.stuffing,
            overwrite=cfg.overwrite,
            error_frame_bits=cfg.error_frame_bits,
            busoff_recovery=cfg.busoff_recovery,
            faults=faults,
            rng=rng,
            logger=logger,
        )
        for name, br in rates.items()
    }

    if len(buses) > 1:  # gateway copies come from the gateway, not from periodic tasks
        if len(buses) != 2:
            raise ValueError("at most two buses (one gateway)")
        a, b = buses
        routes = {
            key: [m.can_id for m in net.messages if m.forwarded_from == src and m.bus == dst]
            for key, src, dst in (("a_to_b", a, b), ("b_to_a", b, a))
        }
        Gateway(env, buses[a], buses[b], routes, net.gateway_delay, tx_queue=cfg.tx_queue)

    groups: dict[tuple[str, str], dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for m in net.messages:
        if m.is_periodic and m.forwarded_from is None:
            groups[(m.sender, m.bus)][s_to_ns(m.period)].append(m)

    nodes: dict[str, Node] = {}
    for (sender, bus_name), by_period in sorted(groups.items()):
        node = buses[bus_name].attach(Node(sender, cfg.tx_queue))
        nodes[f"{sender}@{bus_name}"] = node
        node_offset = rng.randrange(max(by_period)) if cfg.offsets == "random" else 0
        task_cfg = cfg.tasks.get(sender)
        inputs = (  # received on the node's bus, directly or as a gateway copy
            [m.name for m in net.messages if m.bus == bus_name and sender in m.receivers]
            if task_cfg is not None
            else []
        )
        exec_ns = s_to_ns(task_cfg.get("exec_time", 0.0)) if task_cfg else 0
        for period_ns, msgs in sorted(by_period.items()):
            specs = [
                MessageSpec(m.name, m.can_id, m.dlc, m.e2e, s_to_ns(m.jitter), m.extended)
                for m in msgs
            ]
            PeriodicTask(
                env,
                node,
                specs,
                period_ns,
                offset_ns=node_offset % period_ns,
                exec_ns=exec_ns,
                inputs=inputs,
                faults=faults,
                rng=rng,
            )

    if cfg.anomalies:
        anomalies.install(env, next(iter(buses.values())), cfg.anomalies)

    duration_ns = s_to_ns(cfg.duration)
    env.run(until=duration_ns)
    return SimResult(logger.frames, logger.errors, nodes, rates, duration_ns)
