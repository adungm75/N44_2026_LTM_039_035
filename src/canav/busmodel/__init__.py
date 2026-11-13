"""SimPy model of classic CAN: arbitration, bit timing, stuffing, errors (§5.4).

Simulation time is integer nanoseconds (``env.now``), matching the int-ns log convention.
Bit durations are rounded to the nearest ns (exact for 125k/250k/500k/1M).
"""

from __future__ import annotations

import heapq
import itertools
import random
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import simpy
from simpy.events import Event

from canav import e2e
from canav.timing import STUFFING_MODES, frame_bits, frame_bits_actual

NS = 1_000_000_000
LATE = 2  # event priority processed after all NORMAL (1) events of the same instant
TEC_PASSIVE = 127  # error-passive when TEC > 127
TEC_BUSOFF = 255  # bus-off when TEC > 255
BUSOFF_RECOVERY_BITS = 128 * 11
OUTCOMES = ("ok", "overwritten", "dropped", "error", "busoff")
SIDECAR_FIELDS = (
    "run_id",
    "bus",
    "can_id",
    "name",
    "sender",
    "instance",
    "t_release",
    "t_queued",
    "t_tx_start",
    "t_tx_end",
    "bits",
    "attempts",
    "outcome",
    "origin_ts",
    "label",
)


def s_to_ns(seconds: float) -> int:
    return round(seconds * NS)


class _Late(Event):
    """Fires at the current instant, after every normal-priority event of that instant.

    Used before arbitration so that all releases at time t are queued first (§5.4 rule 1).
    """

    def __init__(self, env: simpy.Environment):
        super().__init__(env)
        self._ok = True
        self._value = None
        env.schedule(self, LATE)


@dataclass(slots=True)
class Frame:
    """One message instance; its final state is one sidecar row."""

    can_id: int
    name: str
    sender: str
    instance: int
    data: bytes
    t_release: int
    bus: str = ""
    t_queued: int | None = None
    t_tx_start: int | None = None  # start of the successful attempt
    t_tx_end: int | None = None
    bits: int = 0
    attempts: int = 0
    outcome: str | None = None
    origin_ts: int | None = None
    label: str | None = None
    extended: bool = False
    seq: int = 0

    def record(self, run_id: str) -> dict:
        row = {k: getattr(self, k) for k in SIDECAR_FIELDS if k != "run_id"}
        row["run_id"] = run_id
        row["dlc"] = len(self.data)
        return row


@dataclass(slots=True)
class ErrorEvent:
    """A failed transmission attempt (aborted frame + error frame) on the bus."""

    bus: str
    t_start: int
    t_end: int
    can_id: int
    sender: str
    bits: int
    tec: int  # sender TEC after the error
    state: str  # sender state after the error: active | passive | busoff


class SimLogger:
    """Collects finished frames and error events in completion order."""

    def __init__(self) -> None:
        self.frames: list[Frame] = []
        self.errors: list[ErrorEvent] = []

    def frame(self, f: Frame) -> None:
        self.frames.append(f)

    def error(self, e: ErrorEvent) -> None:
        self.errors.append(e)


@dataclass(frozen=True)
class FaultRule:
    """Fault applied to frames of matching senders/messages inside [start, end) seconds."""

    nodes: tuple[str, ...] = ()  # empty = all senders
    messages: tuple[str, ...] = ()  # empty = all messages
    start: float = 0.0
    end: float | None = None
    drop_prob: float = 0.0  # frame never queued -> outcome "dropped"
    extra_delay: float = 0.0  # [s] added between release and queueing
    error_prob: float = 0.0  # per transmission attempt
    ber: float = 0.0  # bit error rate; p_attempt = 1 - (1 - ber)^bits

    def matches(self, sender: str, name: str, t_ns: int) -> bool:
        if self.nodes and sender not in self.nodes:
            return False
        if self.messages and name not in self.messages:
            return False
        if t_ns < s_to_ns(self.start):
            return False
        return self.end is None or t_ns < s_to_ns(self.end)


class FaultInjector:
    """Evaluates :class:`FaultRule` s with a seeded RNG."""

    def __init__(self, rules: Iterable[FaultRule], rng: random.Random):
        self.rules = tuple(rules)
        self.rng = rng

    @classmethod
    def from_config(cls, items: Iterable[dict], rng: random.Random) -> FaultInjector:
        rules = []
        for item in items:
            d = dict(item)
            for key in ("nodes", "messages"):
                d[key] = tuple(d.get(key, ()))
            rules.append(FaultRule(**d))
        return cls(rules, rng)

    def _active(self, sender: str, name: str, t_ns: int) -> list[FaultRule]:
        return [r for r in self.rules if r.matches(sender, name, t_ns)]

    def drop(self, sender: str, name: str, t_ns: int) -> bool:
        return any(
            r.drop_prob > 0 and self.rng.random() < r.drop_prob
            for r in self._active(sender, name, t_ns)
        )

    def extra_delay_ns(self, sender: str, name: str, t_ns: int) -> int:
        return sum(s_to_ns(r.extra_delay) for r in self._active(sender, name, t_ns))

    def error_prob(self, f: Frame, t_ns: int) -> float:
        p_ok = 1.0
        for r in self._active(f.sender, f.name, t_ns):
            p_ok *= (1.0 - r.error_prob) * (1.0 - r.ber) ** f.bits
        return 1.0 - p_ok


class Node:
    """A CAN controller with a transmit queue (``priority`` = lowest ID first, or ``fifo``)."""

    def __init__(self, name: str, tx_queue: str = "priority"):
        if tx_queue not in ("priority", "fifo"):
            raise ValueError(f"tx_queue must be 'priority' or 'fifo', got {tx_queue!r}")
        self.name = name
        self.tx_queue = tx_queue
        self.bus: Bus | None = None
        self.tec = 0
        self.max_tec = 0
        self.state = "active"
        self.busoff_count = 0
        self._heap: list[tuple[int, int, Frame]] = []
        self._fifo: deque[Frame] = deque()
        self._pending: dict[int, Frame] = {}  # latest queued instance per ID (overwrite)

    def send(self, frame: Frame) -> None:
        if self.bus is None:
            raise RuntimeError(f"node {self.name} is not attached to a bus")
        self.bus.enqueue(self, frame)

    def _push(self, f: Frame, front: bool = False) -> None:
        if self.tx_queue == "priority":
            heapq.heappush(self._heap, (f.can_id, f.seq, f))
        elif front:
            self._fifo.appendleft(f)
        else:
            self._fifo.append(f)

    def head(self) -> Frame | None:
        """Frame this node offers in arbitration (entries with an outcome are stale)."""
        if self.tx_queue == "priority":
            while self._heap and self._heap[0][2].outcome is not None:
                heapq.heappop(self._heap)
            return self._heap[0][2] if self._heap else None
        while self._fifo and self._fifo[0].outcome is not None:
            self._fifo.popleft()
        return self._fifo[0] if self._fifo else None

    def _pop(self) -> Frame:
        f = heapq.heappop(self._heap)[2] if self.tx_queue == "priority" else self._fifo.popleft()
        if self._pending.get(f.can_id) is f:
            del self._pending[f.can_id]
        return f

    def _drain(self) -> list[Frame]:
        live = [e[2] for e in self._heap] + list(self._fifo)
        self._heap.clear()
        self._fifo.clear()
        self._pending.clear()
        return [f for f in live if f.outcome is None]

    def _set_tec(self, tec: int) -> None:
        self.tec = max(0, tec)
        self.max_tec = max(self.max_tec, self.tec)
        if self.tec > TEC_BUSOFF:
            self.state = "busoff"
        elif self.tec > TEC_PASSIVE:
            self.state = "passive"
        else:
            self.state = "active"


class Bus:
    """Classic CAN bus: non-preemptive priority arbitration, errors with retransmission.

    ``listeners`` are called with every successfully transmitted frame at ``t_tx_end``.
    """

    def __init__(
        self,
        env: simpy.Environment,
        name: str,
        bitrate: int,
        stuffing: str = "worst",
        fd: bool = False,
        data_bitrate: int | None = None,
        *,
        overwrite: bool = True,
        error_frame_bits: int = 31,
        busoff_recovery: bool = True,
        faults: FaultInjector | None = None,
        rng: random.Random | None = None,
        logger: SimLogger | None = None,
    ):
        if fd or data_bitrate is not None:
            raise NotImplementedError("CAN FD bus model is P2 (not implemented)")
        if stuffing not in STUFFING_MODES:
            raise ValueError(f"stuffing must be one of {STUFFING_MODES}, got {stuffing!r}")
        self.env = env
        self.name = name
        self.bitrate = bitrate
        self.stuffing = stuffing
        self.overwrite = overwrite
        self.error_frame_bits = error_frame_bits
        self.busoff_recovery = busoff_recovery
        self.faults = faults
        self.rng = rng or random.Random(0)
        self.logger = logger or SimLogger()
        self.nodes: list[Node] = []
        self.listeners: list[Callable[[Frame], None]] = []
        self._seq = itertools.count()
        self._bits_cache: dict[tuple[int, bytes, bool], int] = {}
        self._wakeup: Event | None = None
        self.process = env.process(self._run())

    def attach(self, node: Node) -> Node:
        node.bus = self
        self.nodes.append(node)
        return node

    def duration_ns(self, bits: int) -> int:
        return round(bits * NS / self.bitrate)

    def _frame_bits(self, f: Frame) -> int:
        if self.stuffing != "actual":
            return frame_bits(len(f.data), self.stuffing, f.extended)
        key = (f.can_id, f.data, f.extended)
        bits = self._bits_cache.get(key)
        if bits is None:
            bits = self._bits_cache[key] = frame_bits_actual(f.can_id, f.data, f.extended)
        return bits

    def finish(self, f: Frame, outcome: str) -> None:
        f.bus = f.bus or self.name
        f.outcome = outcome
        self.logger.frame(f)

    def enqueue(self, node: Node, f: Frame) -> None:
        f.bus = self.name
        f.t_queued = self.env.now
        f.seq = next(self._seq)
        f.bits = self._frame_bits(f)
        if node.state == "busoff":
            self.finish(f, "busoff")
            return
        if self.overwrite:
            old = node._pending.get(f.can_id)
            if old is not None:
                self.finish(old, "overwritten")
            node._pending[f.can_id] = f
        node._push(f)
        if self._wakeup is not None and not self._wakeup.triggered:
            self._wakeup.succeed()

    def _arbitrate(self) -> tuple[Node, Frame] | None:
        best: tuple[Node, Frame] | None = None
        for n in self.nodes:
            if n.state == "busoff":
                continue
            f = n.head()
            if f is not None and (
                best is None or (f.can_id, f.seq) < (best[1].can_id, best[1].seq)
            ):
                best = (n, f)
        return best

    def _run(self):
        env = self.env
        while True:
            if self._arbitrate() is None:
                self._wakeup = env.event()
                yield self._wakeup
                self._wakeup = None
            yield _Late(env)
            won = self._arbitrate()
            if won is None:
                continue
            node, _ = won
            f = node._pop()
            f.attempts += 1
            start = env.now
            p_err = self.faults.error_prob(f, start) if self.faults else 0.0
            if p_err > 0 and self.rng.random() < p_err:
                abort_bit = self.rng.randint(1, max(1, f.bits - 3))
                bits = abort_bit + self.error_frame_bits
                yield env.timeout(self.duration_ns(bits))
                self._on_error(node, f, start, bits)
            else:
                yield env.timeout(self.duration_ns(f.bits))
                f.t_tx_start, f.t_tx_end = start, env.now
                node._set_tec(node.tec - 1)
                self.finish(f, "ok")
                for cb in self.listeners:
                    cb(f)

    def _on_error(self, node: Node, f: Frame, start: int, bits: int) -> None:
        node._set_tec(node.tec + 8)
        self.logger.error(
            ErrorEvent(
                self.name, start, self.env.now, f.can_id, node.name, bits, node.tec, node.state
            )
        )
        if node.state == "busoff":
            node.busoff_count += 1
            self.finish(f, "busoff")
            for p in node._drain():
                self.finish(p, "busoff")
            if self.busoff_recovery:
                self.env.process(self._recover(node))
        elif self.overwrite and f.can_id in node._pending:
            self.finish(f, "overwritten")  # a newer instance arrived during the attempt
        else:
            if self.overwrite:
                node._pending[f.can_id] = f
            node._push(f, front=True)

    def _recover(self, node: Node):
        yield self.env.timeout(self.duration_ns(BUSOFF_RECOVERY_BITS))
        node._set_tec(0)


class Gateway:
    """Store-and-forward gateway between two buses.

    ``routes`` = ``{"a_to_b": [ids], "b_to_a": [ids]}``. A frame received on the source bus is
    queued on the destination bus after ``delay`` seconds, keeping ``origin_ts`` and ``label``.
    """

    def __init__(
        self,
        env: simpy.Environment,
        bus_a: Bus,
        bus_b: Bus,
        routes: dict[str, Iterable[int]],
        delay: float,
        name: str = "Gateway",
        tx_queue: str = "priority",
    ):
        self.env = env
        self.delay_ns = s_to_ns(delay)
        for key, src, dst in (("a_to_b", bus_a, bus_b), ("b_to_a", bus_b, bus_a)):
            ids = frozenset(routes.get(key, ()))
            if ids:
                node = dst.attach(Node(name, tx_queue))
                src.listeners.append(self._forwarder(ids, node))

    def _forwarder(self, ids: frozenset[int], node: Node) -> Callable[[Frame], None]:
        def on_rx(f: Frame) -> None:
            if f.can_id not in ids:
                return
            copy = Frame(
                can_id=f.can_id,
                name=f.name,
                sender=node.name,
                instance=f.instance,
                data=f.data,
                t_release=f.t_release,
                origin_ts=f.origin_ts,
                label=f.label,
                extended=f.extended,
            )
            if self.delay_ns:
                ev = self.env.timeout(self.delay_ns)
                ev.callbacks.append(lambda _ev: node.send(copy))
            else:
                node.send(copy)

        return on_rx


@dataclass(frozen=True)
class MessageSpec:
    """What a periodic task needs to know about one of its output messages."""

    name: str
    can_id: int
    dlc: int
    e2e: bool = False
    jitter_ns: int = 0
    extended: bool = False


class PeriodicTask:
    """Releases a group of one node's messages at ``offset + k*period`` (+ jitter per frame).

    Without ``inputs`` the task is a source: ``origin_ts`` = release time. With ``inputs``
    (message names received on the node's bus) it is a consumer: at each release it reads
    the latest inputs, runs ``exec_ns`` and emits outputs inheriting the freshest input
    ``origin_ts`` (None until an input has arrived). Payload = zeros + E2E counter/CRC.
    """

    def __init__(
        self,
        env: simpy.Environment,
        node: Node,
        messages: Sequence[MessageSpec],
        period_ns: int,
        offset_ns: int = 0,
        exec_ns: int = 0,
        inputs: Iterable[str] = (),
        faults: FaultInjector | None = None,
        rng: random.Random | None = None,
    ):
        if node.bus is None:
            raise RuntimeError(f"node {node.name} is not attached to a bus")
        self.env = env
        self.node = node
        self.messages = tuple(messages)
        self.period_ns = period_ns
        self.offset_ns = offset_ns
        self.exec_ns = exec_ns
        self.inputs = frozenset(inputs)
        self.faults = faults
        self.rng = rng or random.Random(0)
        self._latest: dict[str, int] = {}
        self._payloads: dict[tuple[str, int], bytes] = {}
        if self.inputs:
            node.bus.listeners.append(self._on_rx)
        self.process = env.process(self._run())

    def _on_rx(self, f: Frame) -> None:
        if f.name in self.inputs and f.origin_ts is not None:
            self._latest[f.name] = f.origin_ts

    def _payload(self, m: MessageSpec, k: int) -> bytes:
        counter = k % e2e.COUNTER_MODULO if m.e2e else 0
        key = (m.name, counter)
        data = self._payloads.get(key)
        if data is None:
            data = bytes(m.dlc)
            if m.e2e:
                data = e2e.protect(m.can_id, data, counter)
            self._payloads[key] = data
        return data

    def _emit(self, m: MessageSpec, k: int, t_rel: int, origin: int | None) -> None:
        f = Frame(
            can_id=m.can_id,
            name=m.name,
            sender=self.node.name,
            instance=k,
            data=self._payload(m, k),
            t_release=t_rel,
            origin_ts=origin,
            extended=m.extended,
        )
        bus = self.node.bus
        now = self.env.now
        if self.faults and self.faults.drop(f.sender, f.name, now):
            bus.finish(f, "dropped")
            return
        delay = self.rng.randint(0, m.jitter_ns) if m.jitter_ns else 0
        if self.faults:
            delay += self.faults.extra_delay_ns(f.sender, f.name, now)
        if delay:
            ev = self.env.timeout(delay)
            ev.callbacks.append(lambda _ev: self.node.send(f))
        else:
            self.node.send(f)

    def _run(self):
        env = self.env
        k = 0
        while True:
            t_rel = self.offset_ns + k * self.period_ns
            if t_rel > env.now:
                yield env.timeout(t_rel - env.now)
            if self.inputs:
                origin = max(self._latest.values()) if self._latest else None
            else:
                origin = t_rel
            if self.exec_ns:
                yield env.timeout(self.exec_ns)
            for m in self.messages:
                self._emit(m, k, t_rel, origin)
            k += 1


__all__ = [
    "Bus",
    "ErrorEvent",
    "FaultInjector",
    "FaultRule",
    "Frame",
    "Gateway",
    "MessageSpec",
    "Node",
    "OUTCOMES",
    "PeriodicTask",
    "SIDECAR_FIELDS",
    "SimLogger",
    "s_to_ns",
]
