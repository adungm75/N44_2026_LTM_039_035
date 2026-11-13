"""Generate the DBC from the network definition (decision D7: never edit the DBC by hand).

CLI: ``python -m canav.netdef.dbc --out config/dbc/canav.dbc``
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cantools.database.can import Database, Message, Node, Signal
from cantools.database.can.formats.dbc.dbc_specifics import DbcSpecifics
from cantools.database.conversion import BaseConversion

from canav import e2e
from canav.netdef import DEFAULT_MATRIX, DEFAULT_SCHEMES, MessageDef, Network, load_network


def _signals(m: MessageDef) -> list[Signal]:
    receivers = list(m.receivers)
    sigs = [
        Signal(
            name=s.name,
            start=s.start,
            length=s.length,
            byte_order="little_endian",
            is_signed=s.signed,
            conversion=BaseConversion.factory(scale=s.scale, offset=s.offset),
            minimum=s.minimum,
            maximum=s.maximum,
            unit=s.unit,
            receivers=receivers,
        )
        for s in m.signals
    ]
    if m.e2e:
        counter_byte, crc_byte = e2e.layout(m.dlc)
        sigs.append(Signal(e2e.COUNTER_SIGNAL, 8 * counter_byte, 4, receivers=receivers))
        sigs.append(Signal(e2e.CRC_SIGNAL, 8 * crc_byte, 8, receivers=receivers))
    return sigs


def _cycle_time_ms(m: MessageDef) -> int | None:
    if m.period is None:
        return None
    ms = round(m.period * 1000)
    if abs(ms - m.period * 1000) > 1e-6:
        raise ValueError(f"{m.name}: period {m.period} s is not an integer number of ms")
    return ms


def build_database(net: Network) -> Database:
    """Build a cantools database; periodic messages carry ``GenMsgCycleTime`` [ms]."""
    messages = [
        Message(
            frame_id=m.can_id,
            name=m.name,
            length=m.dlc,
            signals=_signals(m),
            senders=[m.sender],
            cycle_time=_cycle_time_ms(m),
            is_extended_frame=m.extended,
        )
        for m in net.messages
    ]
    return Database(
        messages=messages,
        nodes=[Node(n) for n in net.nodes],
        version="",
        dbc_specifics=DbcSpecifics(),
    )


def dbc_string(net: Network) -> str:
    return build_database(net).as_dbc_string()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--matrix", default=str(DEFAULT_MATRIX))
    p.add_argument("--schemes", default=str(DEFAULT_SCHEMES))
    p.add_argument("--scheme", default="safety_first")
    p.add_argument("--out", default="config/dbc/canav.dbc")
    a = p.parse_args(argv)
    net = load_network(a.matrix, a.scheme, a.schemes)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dbc_string(net))
    print(f"wrote {out} ({len(net.messages)} messages, scheme {a.scheme})")


if __name__ == "__main__":
    main()
