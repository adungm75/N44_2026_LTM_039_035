"""Online ECUs on SocketCAN / python-can virtual (§5.5)."""

from __future__ import annotations

from pathlib import Path

from canav.ecu.base import EcuBase, FaultConfig, load_dbc, make_bus
from canav.sidechannel import state_port


def build_ecu(
    name: str,
    iface: str,
    db,
    params: dict,
    log_dir: str | Path,
    *,
    channel: str | None = None,
    fault: FaultConfig | None = None,
    seed: int = 0,
) -> EcuBase:
    """Create the ECU ``name`` with its own bus handle (filtered to the IDs it receives)."""
    from canav.ecu.nodes import ECUS

    cls = ECUS[name]
    bus = make_bus(iface, channel, EcuBase.can_filters(name, db) or None)
    nodes = [n.name for n in db.nodes]
    return cls(
        name,
        bus,
        db,
        params,
        Path(log_dir) / f"{name}.jsonl",
        fault=fault,
        state_port=state_port(nodes.index(name), params["sidechannel"]["port_base"]),
        seed=seed,
    )


__all__ = ["EcuBase", "FaultConfig", "build_ecu", "load_dbc", "make_bus"]
