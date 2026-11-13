"""Network variants for the experiments (§5.10): the ``network:`` section of an
experiment YAML, applied in this order to the network loaded with its ID scheme:

``period_factor``  multiply every period (and default deadline D = T) — E2
``dlc``            set the DLC of every periodic message (payload is not simulated) — E2
``target_load``    add synthetic messages until the theoretical load reaches it — E3/E4
``deadlines``      AEB-chain deadlines from the budget config (§5.12.1, same numbers as
                   ``results/budget/aeb_budget.json``)
``topology``       two buses joined by the gateway — E5

Synthetic load (chosen, documented in docs/experiments.md): 8-byte frames, one sender node
per message (independent random offsets), T = ``synthetic_period`` (10 ms), the last message
gets a longer period so the theoretical load hits the target; IDs spread evenly over the
free 11-bit IDs so they interleave with the real traffic under every ID scheme.
"""

from __future__ import annotations

import math
from dataclasses import replace

from canav.netdef import MessageDef, Network
from canav.timing import bus_load_theory, frame_bits, timing_messages
from canav.timing.budget import load_deadlines

SYNTH_DLC = 8
SYNTH_PERIOD = 0.010
ID_LOW, ID_HIGH = 0x011, 0x7FF  # synthetic IDs never outrank the AEB request at 0x010


def scale_periods(net: Network, factor: float) -> Network:
    msgs = tuple(
        replace(
            m,
            period=m.period * factor,
            deadline=None if m.deadline is None else m.deadline * factor,
        )
        if m.is_periodic
        else m
        for m in net.messages
    )
    return replace(net, messages=msgs)


def set_dlc(net: Network, dlc: int) -> Network:
    msgs = tuple(replace(m, dlc=dlc, signals=()) if m.is_periodic else m for m in net.messages)
    return replace(net, messages=msgs)


def add_synthetic_load(
    net: Network, target: float, bitrate: int, period: float = SYNTH_PERIOD
) -> tuple[Network, dict]:
    """Add ``Synth_k`` messages (one node each) so the worst-case theoretical load of the
    single bus reaches ``target``. Returns the network and ``{base, achieved, n}``."""
    (bus,) = net.buses
    base = bus_load_theory(timing_messages(net, "worst", bus), bitrate)
    info = {"load_base": base, "load_target": target, "n_synthetic": 0}
    extra = (target - base) * bitrate  # bit/s
    bits = frame_bits(SYNTH_DLC, "worst")
    periods = []
    if extra > 0:
        per_msg = bits / period
        n_full = math.floor(extra / per_msg)
        periods = [period] * n_full
        rest = extra - n_full * per_msg
        if rest > 1e-6 * bitrate:
            periods.append(round(bits / rest, 7))  # 0.1 µs resolution
    used = {m.can_id for m in net.messages}
    free = [i for i in range(ID_LOW, ID_HIGH + 1) if i not in used]
    n = len(periods)
    if n > len(free):
        raise ValueError(f"not enough free IDs for {n} synthetic messages")
    ids = [free[round(k * (len(free) - 1) / max(1, n - 1))] for k in range(n)]
    nodes = list(net.nodes)
    msgs = list(net.messages)
    for k, (cid, t) in enumerate(zip(ids, periods, strict=True)):
        node = f"Synth{k:02d}"
        nodes.append(node)
        msgs.append(
            MessageDef(
                name=f"Synth_{k:02d}",
                can_id=cid,
                dlc=SYNTH_DLC,
                sender=node,
                receivers=(),
                period=t,
                deadline=t,
                jitter=0.0,
                bus=bus,
                e2e=False,
                signals=(),
            )
        )
    out = replace(net, nodes=tuple(nodes), messages=tuple(msgs))
    info["n_synthetic"] = n
    info["load_achieved"] = bus_load_theory(timing_messages(out, "worst", bus), bitrate)
    return out, info


def apply_deadlines(net: Network, budget_config: str) -> Network:
    """D = min(per-hop budget, T) for the AEB-chain messages of the budget config."""
    per_hop, names = load_deadlines(budget_config)
    msgs = tuple(
        replace(m, deadline=min(per_hop, m.period)) if m.name in names else m for m in net.messages
    )
    return replace(net, messages=msgs)


def two_bus(net: Network, buses: dict[str, int], nodes: dict[str, str], delay: float) -> Network:
    """Put every node on one of two buses; messages with receivers on the other bus get a
    gateway copy there (``forwarded_from`` = source bus, sender = Gateway)."""
    if len(buses) != 2:
        raise ValueError("two_bus needs exactly two buses")
    missing = sorted({m.sender for m in net.messages if m.is_periodic} - set(nodes))
    if missing:
        raise ValueError(f"no bus for nodes {missing}")
    msgs = []
    copies = []
    for m in net.messages:
        if not m.is_periodic:  # diagnostics (event) are not simulated
            msgs.append(m)
            continue
        src = nodes[m.sender]
        msgs.append(replace(m, bus=src))
        for dst in buses:
            rx = tuple(r for r in m.receivers if nodes.get(r) == dst)
            if dst != src and rx:
                copies.append(
                    replace(m, bus=dst, sender="Gateway", receivers=rx, forwarded_from=src)
                )
    return replace(net, buses=dict(buses), messages=tuple(msgs + copies), gateway_delay=delay)


def apply_network_variant(net: Network, spec: dict, bitrate: int | None) -> tuple[Network, dict]:
    """Apply the variant keys of an experiment's ``network:`` section; returns (net, info)."""
    info: dict = {}
    if spec.get("period_factor") not in (None, 1, 1.0):
        net = scale_periods(net, float(spec["period_factor"]))
    if spec.get("dlc") is not None:
        net = set_dlc(net, int(spec["dlc"]))
    if spec.get("target_load") is not None:
        (bus_br,) = net.buses.values()
        net, info["synthetic"] = add_synthetic_load(
            net,
            float(spec["target_load"]),
            bitrate or bus_br,
            float(spec.get("synthetic_period", SYNTH_PERIOD)),
        )
    if spec.get("deadlines"):
        net = apply_deadlines(net, spec["deadlines"])
    topo = spec.get("topology")
    if topo:
        (default_br,) = set(net.buses.values())
        buses = {b: int(br or default_br) for b, br in topo["buses"].items()}
        net = two_bus(net, buses, topo["nodes"], float(topo["gateway_delay"]))
    return net, info
