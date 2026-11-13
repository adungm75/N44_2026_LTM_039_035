"""Online ECU runtime (§5.5): python-can bus, DBC, absolute-deadline scheduler,
automatic E2E counter/CRC, rx checks and a JSONL event log per ECU.

Event log line: ``{t_ns, ecu, event, msg, counter, src?}`` with ``t_ns = time.monotonic_ns()``
(CLOCK_MONOTONIC is shared by all processes, so logs of different ECUs can be joined).
Events: ``tx | rx | use | drop | crc_err | counter_err``.
"""

from __future__ import annotations

import heapq
import itertools
import json
import os
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path

import can
import cantools

from canav import e2e
from canav.sidechannel import UdpLatest

FAULT_ENV = "CANAV_FAULT"


@dataclass
class FaultConfig:
    """Online fault injection: drop frames, delay them, or go silent after some time."""

    drop_prob: float = 0.0
    extra_delay_ms: float = 0.0
    silent_after_s: float | None = None

    @classmethod
    def parse(cls, text: str | None) -> FaultConfig:
        """Parse ``"drop_prob=0.05,extra_delay_ms=2,silent_after_s=10"`` (empty -> no fault)."""
        cfg = cls()
        names = {f.name for f in fields(cls)}
        for item in filter(None, (t.strip() for t in (text or "").split(","))):
            key, _, value = item.partition("=")
            if key not in names:
                raise ValueError(f"unknown fault key {key!r}; expected one of {sorted(names)}")
            setattr(cfg, key, float(value))
        return cfg

    @classmethod
    def from_env(cls) -> FaultConfig:
        return cls.parse(os.environ.get(FAULT_ENV))

    def active(self) -> bool:
        return bool(self.drop_prob or self.extra_delay_ms or self.silent_after_s is not None)


class EventLog:
    """Buffered JSONL writer; one file per ECU."""

    def __init__(self, path: str | Path, ecu: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", buffering=1 << 16)  # noqa: SIM115 - closed in close()
        self.ecu = ecu
        self.lock = threading.Lock()

    def write(
        self, t_ns: int, event: str, msg: str, counter: int | None, src: dict | None = None
    ) -> None:
        rec = {"t_ns": t_ns, "ecu": self.ecu, "event": event, "msg": msg, "counter": counter}
        if src:
            rec["src"] = src
        line = json.dumps(rec, separators=(",", ":"))
        with self.lock:
            self.f.write(line + "\n")

    def close(self) -> None:
        with self.lock:
            self.f.close()


def make_bus(iface: str, channel: str | None = None, can_filters: list | None = None) -> can.BusABC:
    """``iface='virtual'`` -> in-process python-can virtual bus on ``channel``; else SocketCAN."""
    if iface == "virtual":
        return can.Bus(interface="virtual", channel=channel or "canav", can_filters=can_filters)
    return can.Bus(interface="socketcan", channel=iface, can_filters=can_filters)


def load_dbc(path: str | Path) -> cantools.database.can.Database:
    return cantools.database.load_file(str(path))


def _clamp_signal(sig, value: float) -> float:
    lo_raw = -(1 << (sig.length - 1)) if sig.is_signed else 0
    hi_raw = (1 << (sig.length - 1)) - 1 if sig.is_signed else (1 << sig.length) - 1
    lo, hi = sorted((lo_raw * sig.scale + sig.offset, hi_raw * sig.scale + sig.offset))
    if sig.minimum is not None:
        lo = max(lo, sig.minimum)
    if sig.maximum is not None:
        hi = min(hi, sig.maximum)
    return min(max(float(value), lo), hi)


class EcuBase:
    """Base class: subclasses implement :meth:`cycle` (periodic) and :meth:`on_rx`.

    The cycle period is the shortest ``GenMsgCycleTime`` of the ECU's tx messages, or
    ``params[<ecu>]["period"]`` (default 10 ms) for ECUs without periodic tx (actuators).
    """

    uses_state = False  # subscribe to the side-channel state

    def __init__(
        self,
        name: str,
        bus: can.BusABC,
        db: cantools.database.can.Database,
        params: dict,
        log_path: str | Path,
        *,
        fault: FaultConfig | None = None,
        state_port: int | None = None,
        seed: int = 0,
    ):
        self.name = name
        self.bus = bus
        self.db = db
        self.params = params
        self.p = dict(params.get(name) or {})
        self.veh = dict(params.get("vehicle") or {})
        self.rx_msgs = {
            m.frame_id: m for m in db.messages if any(name in s.receivers for s in m.signals)
        }
        self.tx_msgs = {m.name: m for m in db.messages if name in m.senders and m.cycle_time}
        if self.tx_msgs:
            self.period_ns = min(m.cycle_time for m in self.tx_msgs.values()) * 1_000_000
        else:
            self.period_ns = round(float(self.p.get("period", 0.01)) * 1e9)
        self.tx_counter = dict.fromkeys(self.tx_msgs, 0)
        self.rx_counter: dict[str, int] = {}
        self.log = EventLog(log_path, name)
        self.stats_path = Path(log_path).with_suffix(".stats.json")
        self.fault = fault or FaultConfig()
        self.rng = random.Random(seed)
        self.state_rx = UdpLatest(state_port) if self.uses_state and state_port else None
        self.stats: Counter[str] = Counter()
        self._delayed: list[tuple[int, int, can.Message, str, int, dict | None]] = []
        self._seq = itertools.count()
        self._t_start = time.monotonic_ns()

    @classmethod
    def can_filters(cls, name: str, db) -> list[dict]:
        """Kernel/software filters for the messages this ECU receives."""
        return [
            {
                "can_id": m.frame_id,
                "can_mask": 0x1FFFFFFF if m.is_extended_frame else 0x7FF,
                "extended": m.is_extended_frame,
            }
            for m in db.messages
            if any(name in s.receivers for s in m.signals)
        ]

    # ------------------------------------------------------------- hooks
    def cycle(self, t_ns: int) -> None:
        """Periodic work (called at absolute deadlines)."""

    def on_rx(self, msg: str, values: dict, counter: int | None, t_ns: int) -> None:
        """A received frame passed the CRC check."""

    def close(self) -> None:
        """Release resources (called once after the run loop)."""

    # ----------------------------------------------------------- helpers
    def state(self) -> dict | None:
        """Latest side-channel state (sample-and-hold), or None before the first one."""
        if self.state_rx is None:
            return None
        self.state_rx.poll()
        return self.state_rx.latest

    def send(self, name: str, values: dict, src: dict | None = None) -> None:
        """Encode, add counter + CRC, apply faults, transmit and log ``tx`` (or ``drop``)."""
        msg = self.tx_msgs[name]
        counter = self.tx_counter[name]
        self.tx_counter[name] = e2e.next_counter(counter)
        now = time.monotonic_ns()
        if (
            self.fault.silent_after_s is not None
            and now - self._t_start >= self.fault.silent_after_s * 1e9
        ):
            return
        sigs = {s.name: s for s in msg.signals}
        data = {k: _clamp_signal(sigs[k], v) for k, v in values.items()}
        protected = e2e.CRC_SIGNAL in sigs
        if protected:
            data[e2e.COUNTER_SIGNAL] = counter
            data[e2e.CRC_SIGNAL] = 0
        payload = msg.encode(data, strict=False)
        if protected:
            payload = e2e.protect(msg.frame_id, payload, counter)
        if self.fault.drop_prob and self.rng.random() < self.fault.drop_prob:
            self.log.write(now, "drop", name, counter, src)
            self.stats["drop"] += 1
            return
        frame = can.Message(
            arbitration_id=msg.frame_id, data=payload, is_extended_id=msg.is_extended_frame
        )
        if self.fault.extra_delay_ms:
            due = now + round(self.fault.extra_delay_ms * 1e6)
            heapq.heappush(self._delayed, (due, next(self._seq), frame, name, counter, src))
        else:
            self._transmit(frame, name, counter, src)

    def _transmit(self, frame: can.Message, name: str, counter: int, src: dict | None) -> None:
        t = time.monotonic_ns()
        self.bus.send(frame)
        self.log.write(t, "tx", name, counter, src)
        self.stats["tx"] += 1

    def _handle_rx(self, frame: can.Message) -> None:
        t = time.monotonic_ns()
        msg = self.rx_msgs.get(frame.arbitration_id)
        if msg is None or frame.is_error_frame or frame.is_remote_frame:
            return
        data = bytes(frame.data)
        counter = None
        if any(s.name == e2e.CRC_SIGNAL for s in msg.signals) and len(data) in e2e.E2E_DLCS:
            res = e2e.check(frame.arbitration_id, data, self.rx_counter.get(msg.name))
            if not res.crc_ok:
                self.log.write(t, "crc_err", msg.name, res.counter)
                self.stats["crc_err"] += 1
                return
            counter = res.counter
            if not res.counter_ok:
                self.log.write(t, "counter_err", msg.name, counter)
                self.stats["counter_err"] += 1
            self.rx_counter[msg.name] = counter
        self.log.write(t, "rx", msg.name, counter)
        self.stats["rx"] += 1
        self.on_rx(msg.name, msg.decode(data, decode_choices=False), counter, t)

    # --------------------------------------------------------------- run
    def run(self, stop: threading.Event, duration_s: float | None = None) -> None:
        """Main loop: periodic cycle on absolute deadlines (no drift), rx in between."""
        start = self._t_start = time.monotonic_ns()
        end = None if duration_s is None else start + round(duration_s * 1e9)
        k = 0
        next_cycle = start
        try:
            while not stop.is_set():
                now = time.monotonic_ns()
                if end is not None and now >= end:
                    break
                while self._delayed and self._delayed[0][0] <= now:
                    _, _, frame, name, counter, src = heapq.heappop(self._delayed)
                    self._transmit(frame, name, counter, src)
                if now >= next_cycle:
                    self.cycle(now)
                    k += 1
                    next_cycle = start + k * self.period_ns
                    late = time.monotonic_ns()
                    if next_cycle <= late:  # overrun: skip missed deadlines, stay on the grid
                        skipped = (late - next_cycle) // self.period_ns + 1
                        self.stats["overrun"] += skipped
                        k += skipped
                        next_cycle = start + k * self.period_ns
                    continue
                wake = next_cycle if not self._delayed else min(next_cycle, self._delayed[0][0])
                frame = self.bus.recv(timeout=max(0, wake - now) / 1e9)
                if frame is not None:
                    self._handle_rx(frame)
        finally:
            self.close()
            self.log.close()
            self.stats_path.write_text(json.dumps(dict(self.stats), indent=1))
            if self.state_rx is not None:
                self.state_rx.close()
