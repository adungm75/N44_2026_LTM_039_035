"""Build and run the SimPy bus model from a :class:`canav.netdef.Network` (engine ``sim``)."""

from __future__ import annotations

import dataclasses
import random
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
import simpy

from canav.analysis.metrics import MessageInfo
from canav.busmodel import (
    SIDECAR_FIELDS,
    Bus,
    ErrorEvent,
    FaultInjector,
    Frame,
    MessageSpec,
    Node,
    PeriodicTask,
    SimLogger,
    s_to_ns,
)
from canav.netdef import Network
from canav.timing import bus_load_theory, davis_wcrt, timing_messages

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


def theory(net: Network, cfg: SimConfig) -> tuple[dict[int, float | None], dict[str, float]]:
    """(WCRT per CAN ID, theoretical load per bus); ``actual`` stuffing uses the worst bound."""
    stuffing = "worst" if cfg.stuffing == "actual" else cfg.stuffing
    wcrt: dict[int, float | None] = {}
    load: dict[str, float] = {}
    for bus, br in bitrates(net, cfg).items():
        msgs = timing_messages(net, stuffing, bus)
        load[bus] = bus_load_theory(msgs, br)
        wcrt.update({r.can_id: r.r for r in davis_wcrt(msgs, br)})
    return wcrt, load


def message_info(net: Network, cfg: SimConfig) -> dict[int, MessageInfo]:
    wcrt, _ = theory(net, cfg)
    act = set(cfg.actuators)
    return {
        m.can_id: MessageInfo(
            name=m.name,
            bus=m.bus,
            period=m.period,
            deadline=m.deadline,
            wcrt=wcrt.get(m.can_id),
            to_actuator=bool(act & set(m.receivers)),
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

    groups: dict[tuple[str, str], dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for m in net.messages:
        if m.is_periodic:
            groups[(m.sender, m.bus)][s_to_ns(m.period)].append(m)

    nodes: dict[str, Node] = {}
    for (sender, bus_name), by_period in sorted(groups.items()):
        node = buses[bus_name].attach(Node(sender, cfg.tx_queue))
        nodes[f"{sender}@{bus_name}"] = node
        node_offset = rng.randrange(max(by_period)) if cfg.offsets == "random" else 0
        task_cfg = cfg.tasks.get(sender)
        inputs = (
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

    duration_ns = s_to_ns(cfg.duration)
    env.run(until=duration_ns)
    return SimResult(logger.frames, logger.errors, nodes, rates, duration_ns)
