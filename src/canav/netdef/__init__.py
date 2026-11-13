"""Network definition: load, validate and transform the CAN message matrix (matrix.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from canav import e2e

DEFAULT_MATRIX = Path("config/network/matrix.yaml")
DEFAULT_SCHEMES = Path("config/network/id_schemes.yaml")
ALL_RECEIVERS = "all"


class NetdefError(ValueError):
    """Raised when a network definition is invalid; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid network definition:\n  " + "\n  ".join(errors))


@dataclass(frozen=True)
class SignalDef:
    name: str
    start: int
    length: int
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None

    def raw_range(self) -> tuple[int, int]:
        if self.signed:
            return -(1 << (self.length - 1)), (1 << (self.length - 1)) - 1
        return 0, (1 << self.length) - 1


@dataclass(frozen=True)
class MessageDef:
    name: str
    can_id: int
    dlc: int
    sender: str
    receivers: tuple[str, ...]
    period: float | None
    deadline: float | None
    jitter: float
    bus: str
    e2e: bool
    signals: tuple[SignalDef, ...]
    extended: bool = False

    @property
    def is_periodic(self) -> bool:
        return self.period is not None


@dataclass(frozen=True)
class Network:
    nodes: tuple[str, ...]
    buses: dict[str, int]  # bus name -> bitrate [bit/s]
    messages: tuple[MessageDef, ...]

    def message(self, name: str) -> MessageDef:
        for m in self.messages:
            if m.name == name:
                return m
        raise KeyError(name)

    def by_id(self) -> dict[int, MessageDef]:
        return {m.can_id: m for m in self.messages}


def _parse_signal(raw: dict[str, Any]) -> SignalDef:
    return SignalDef(
        name=str(raw["name"]),
        start=int(raw["start"]),
        length=int(raw["length"]),
        scale=float(raw.get("scale", 1.0)),
        offset=float(raw.get("offset", 0.0)),
        signed=bool(raw.get("signed", False)),
        unit=raw.get("unit"),
        minimum=None if raw.get("minimum") is None else float(raw["minimum"]),
        maximum=None if raw.get("maximum") is None else float(raw["maximum"]),
    )


def _parse_message(raw: dict[str, Any], nodes: tuple[str, ...], default_bus: str) -> MessageDef:
    sender = str(raw["sender"])
    receivers = raw.get("receivers", [])
    if receivers == ALL_RECEIVERS:
        receivers = [n for n in nodes if n != sender]
    period = raw.get("period")
    period = None if period is None else float(period)
    deadline = raw.get("deadline", period)
    return MessageDef(
        name=str(raw["name"]),
        can_id=int(raw["id"]),
        dlc=int(raw["dlc"]),
        sender=sender,
        receivers=tuple(str(r) for r in receivers),
        period=period,
        deadline=None if deadline is None else float(deadline),
        jitter=float(raw.get("jitter", 0.0)),
        bus=str(raw.get("bus", default_bus)),
        e2e=bool(raw.get("e2e", False)),
        signals=tuple(_parse_signal(s) for s in raw.get("signals", [])),
        extended=bool(raw.get("extended", False)),
    )


def parse_network(doc: dict[str, Any]) -> Network:
    """Build a :class:`Network` from a parsed matrix.yaml document (not validated)."""
    nodes = tuple(str(n) for n in doc["nodes"])
    buses = {str(k): int(v["bitrate"]) for k, v in doc["buses"].items()}
    default_bus = next(iter(buses))
    messages = tuple(_parse_message(m, nodes, default_bus) for m in doc["messages"])
    return Network(nodes=nodes, buses=buses, messages=messages)


def _signal_errors(m: MessageDef) -> list[str]:
    errors: list[str] = []
    limit = e2e.data_bits(m.dlc) if m.e2e and m.dlc in e2e.E2E_DLCS else 8 * m.dlc
    used = 0
    names: set[str] = set()
    for s in m.signals:
        where = f"{m.name}.{s.name}"
        if s.name in names:
            errors.append(f"{where}: duplicate signal name")
        names.add(s.name)
        if m.e2e and s.name in (e2e.COUNTER_SIGNAL, e2e.CRC_SIGNAL):
            errors.append(f"{where}: name reserved for E2E")
        if s.length < 1 or s.start < 0:
            errors.append(f"{where}: invalid start/length {s.start}/{s.length}")
            continue
        if s.start + s.length > limit:
            what = "overlaps E2E counter/CRC" if m.e2e else "exceeds DLC"
            errors.append(f"{where}: bits {s.start}..{s.start + s.length - 1} {what}")
        mask = ((1 << s.length) - 1) << s.start
        if used & mask:
            errors.append(f"{where}: overlaps another signal")
        used |= mask
        if s.scale == 0:
            errors.append(f"{where}: scale must be non-zero")
            continue
        lo, hi = sorted(r * s.scale + s.offset for r in s.raw_range())
        eps = 1e-9 * max(1.0, abs(lo), abs(hi))
        if s.minimum is not None and s.minimum < lo - eps:
            errors.append(f"{where}: minimum {s.minimum} below representable {lo}")
        if s.maximum is not None and s.maximum > hi + eps:
            errors.append(f"{where}: maximum {s.maximum} above representable {hi}")
    return errors


def validate(net: Network) -> list[str]:
    """Return a list of human-readable problems (empty if the network is valid)."""
    errors: list[str] = []
    nodes = set(net.nodes)
    if len(nodes) != len(net.nodes):
        errors.append("duplicate node names")
    seen_names: set[str] = set()
    seen_ids: dict[int, str] = {}
    for m in net.messages:
        if m.name in seen_names:
            errors.append(f"{m.name}: duplicate message name")
        seen_names.add(m.name)
        if m.can_id in seen_ids:
            errors.append(
                f"{m.name}: duplicate CAN ID 0x{m.can_id:03X} (also {seen_ids[m.can_id]})"
            )
        seen_ids[m.can_id] = m.name
        max_id = 0x1FFFFFFF if m.extended else 0x7FF
        if not 0 <= m.can_id <= max_id:
            errors.append(f"{m.name}: CAN ID 0x{m.can_id:X} out of range")
        if not 0 <= m.dlc <= 8:
            errors.append(f"{m.name}: DLC {m.dlc} not in 0..8")
        if m.e2e and m.dlc not in e2e.E2E_DLCS:
            errors.append(f"{m.name}: E2E requires DLC in {e2e.E2E_DLCS}")
        if m.sender not in nodes:
            errors.append(f"{m.name}: unknown sender {m.sender}")
        for r in m.receivers:
            if r not in nodes:
                errors.append(f"{m.name}: unknown receiver {r}")
        if m.sender in m.receivers:
            errors.append(f"{m.name}: sender is also a receiver")
        if m.bus not in net.buses:
            errors.append(f"{m.name}: unknown bus {m.bus}")
        if m.period is not None:
            if m.period <= 0 or m.deadline is None or m.deadline <= 0:
                errors.append(f"{m.name}: period and deadline must be > 0")
        elif m.deadline is not None:
            errors.append(f"{m.name}: event message must not have a deadline")
        if m.jitter < 0:
            errors.append(f"{m.name}: jitter must be >= 0")
        errors.extend(_signal_errors(m))
    return errors


def check(net: Network) -> Network:
    """Validate ``net``; raise :class:`NetdefError` on any problem, else return it."""
    errors = validate(net)
    if errors:
        raise NetdefError(errors)
    return net


def load_matrix(path: str | Path = DEFAULT_MATRIX) -> Network:
    """Load and validate matrix.yaml."""
    with open(path) as f:
        return check(parse_network(yaml.safe_load(f)))


def load_id_schemes(path: str | Path = DEFAULT_SCHEMES) -> dict[str, dict[str, int]]:
    """Load id_schemes.yaml -> {scheme name: {message name: CAN ID}}."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    return {
        name: {str(k): int(v) for k, v in (ids or {}).items()}
        for name, ids in doc["schemes"].items()
    }


def apply_id_scheme(net: Network, ids: dict[str, int]) -> Network:
    """Return a validated copy of ``net`` with CAN IDs replaced per ``ids`` (name -> ID)."""
    names = {m.name for m in net.messages}
    unknown = sorted(set(ids) - names)
    if unknown:
        raise NetdefError([f"ID scheme references unknown message {n}" for n in unknown])
    messages = tuple(replace(m, can_id=ids.get(m.name, m.can_id)) for m in net.messages)
    return check(replace(net, messages=messages))


def load_network(
    matrix: str | Path = DEFAULT_MATRIX,
    scheme: str = "safety_first",
    schemes_path: str | Path = DEFAULT_SCHEMES,
) -> Network:
    """Load matrix.yaml and apply the named ID scheme."""
    schemes = load_id_schemes(schemes_path)
    if scheme not in schemes:
        raise KeyError(f"unknown ID scheme {scheme!r}; available: {sorted(schemes)}")
    return apply_id_scheme(load_matrix(matrix), schemes[scheme])
