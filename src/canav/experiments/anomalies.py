"""Labelled anomalies for E8 (§5.10, threat model §5.12.7) in the SimPy model.

``sim.anomalies`` items: ``{type, start, end}`` [s] plus optional overrides. Defaults:

- ``flood``: ID 0x000 every 0.2 ms, 8 zero bytes (§5.10); wins every arbitration.
- ``silent``: every frame of ``node`` (Perception, §5.10) dropped at the sender.
- ``spoof``: extra Steer_Cmd 0x100 every 10 ms, 5 ms after the legitimate phase (chosen),
  frozen AliveCounter 0 with a *valid* CRC (chosen: CRC8 and DataID are not secret, the
  attacker does not track the counter state, §5.12.7).
- ``unknown_id``: ID 0x6A1 every 100 ms (period chosen), 8 zero bytes.

The interval ``[start, end)`` is the ground truth for window labels (also kept as the
``label`` of every injected frame in the sidecar).
"""

from __future__ import annotations

from canav import e2e
from canav.busmodel import Bus, FaultRule, InjectTask, Node, s_to_ns

TYPES = ("flood", "silent", "spoof", "unknown_id")
DEFAULTS = {
    "flood": {"can_id": 0x000, "dlc": 8, "period": 0.0002},
    "silent": {"node": "Perception"},
    "spoof": {"can_id": 0x100, "dlc": 4, "period": 0.010, "phase": 0.005, "counter": 0},
    "unknown_id": {"can_id": 0x6A1, "dlc": 8, "period": 0.100},
}


def spec(item: dict) -> dict:
    t = item["type"]
    if t not in TYPES:
        raise ValueError(f"unknown anomaly type {t!r}, expected one of {TYPES}")
    return DEFAULTS[t] | item


def silent_rules(items: list[dict]) -> list[FaultRule]:
    return [
        FaultRule(nodes=(s["node"],), drop_prob=1.0, start=float(s["start"]), end=float(s["end"]))
        for s in map(spec, items)
        if s["type"] == "silent"
    ]


def install(env, bus: Bus, items: list[dict]) -> None:
    """Attach one attacker node per injecting anomaly to ``bus``."""
    for i, s in enumerate(map(spec, items)):
        if s["type"] == "silent":
            continue
        node = bus.attach(Node(f"Attacker{i}", "priority"))
        dlc, cid = int(s["dlc"]), int(s["can_id"])
        if s["type"] == "spoof":
            frozen = e2e.protect(cid, bytes(dlc), int(s["counter"]))

            def payload(_k, data=frozen):
                return data
        else:

            def payload(_k, data=bytes(dlc)):
                return data

        start = float(s["start"]) + float(s.get("phase", 0.0))
        InjectTask(
            env,
            node,
            cid,
            s["type"],
            payload,
            s_to_ns(float(s["period"])),
            s_to_ns(start),
            s_to_ns(float(s["end"])),
            label=s["type"],
        )
