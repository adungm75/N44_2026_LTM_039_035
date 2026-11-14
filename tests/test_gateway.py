import socket
import struct
import threading
import time
from pathlib import Path

import pytest

from canav import e2e
from canav.ecu import load_dbc
from canav.gateway import CAN_FRAME, Gateway, GatewayConfig, open_can
from canav.gateway.protocol import (
    CAN_EFF_FLAG,
    CAN_RTR_FLAG,
    FLAG_OWN_TX,
    FLAG_SW_TIMESTAMP,
    HEADER,
    MAX_DATAGRAM,
    SEQ_MOD,
    TYPE_DATA,
    TYPE_DIAG_REQ,
    TYPE_HEARTBEAT,
    TYPE_STATS,
    Batcher,
    ProtocolError,
    Record,
    Stats,
    decode,
    encode,
    seq_gap,
)
from canav.monitor import Monitor, run_monitor

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def db():
    return load_dbc(ROOT / "config/dbc/canav.dbc")


def _rec(i: int, n: int = 8, can_id: int = 0x200) -> Record:
    return Record(can_id, 1_700_000_000_000_000_000 + i, 0, bytes(range(n)))


# ------------------------------------------------------------------ protocol
def test_roundtrip_all_lengths_and_flags():
    recs = [_rec(n, n) for n in range(9)]
    recs += [
        Record(CAN_EFF_FLAG | 0x1ABCDEF0, 5, FLAG_OWN_TX, b"\x01\x02"),
        Record(CAN_RTR_FLAG | 0x123, 6, FLAG_SW_TIMESTAMP, b""),
    ]
    buf = encode(TYPE_DATA, 7, 123456789, recs)
    assert len(buf) == HEADER.size + sum(14 + len(r.data) for r in recs)
    dg = decode(buf)
    assert (dg.type, dg.seq, dg.t_send_ns, dg.records) == (TYPE_DATA, 7, 123456789, tuple(recs))
    assert buf[:4] == b"CAV1" and buf[4] == 1  # magic, version


def test_zero_frames_and_stats():
    hb = encode(TYPE_HEARTBEAT, 1, 2)
    assert len(hb) == 20 and decode(hb).records == ()
    st = Stats(10, 1, 3, 2, 0)
    assert decode(encode(TYPE_STATS, 2, 3, stats=st)).stats == st
    with pytest.raises(ProtocolError):
        encode(TYPE_STATS, 2, 3)


def test_full_datagram():
    n_max = (MAX_DATAGRAM - HEADER.size) // 22  # 8-byte frames
    assert len(encode(TYPE_DATA, 0, 0, [_rec(i) for i in range(n_max)])) <= MAX_DATAGRAM
    with pytest.raises(ProtocolError):
        encode(TYPE_DATA, 0, 0, [_rec(i) for i in range(n_max + 1)])
    b = Batcher(max_frames=1000, flush_ns=10**9)
    batches = [x for i in range(n_max + 1) for x in b.add(_rec(i), 0)]
    assert len(batches) == 1 and len(batches[0]) == n_max  # flushed by size, last one pending
    assert len(b.pending) == 1


def test_seq_wrap():
    assert decode(encode(TYPE_HEARTBEAT, SEQ_MOD - 1, 0)).seq == SEQ_MOD - 1
    assert decode(encode(TYPE_HEARTBEAT, SEQ_MOD, 0)).seq == 0
    assert seq_gap(SEQ_MOD - 1, 0) == 0
    assert seq_gap(SEQ_MOD - 2, 1) == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b"XAV1" + b[4:],  # magic
        lambda b: b[:4] + b"\x02" + b[5:],  # version
        lambda b: b[:5] + b"\x09" + b[6:],  # type
        lambda b: b[:-1],  # truncated record
        lambda b: b + b"\x00",  # trailing byte
        lambda b: b[:10],  # truncated header
    ],
)
def test_decode_rejects(mutate):
    buf = encode(TYPE_DATA, 0, 0, [_rec(0)])
    with pytest.raises(ProtocolError):
        decode(mutate(buf))


def test_batcher_count_and_timeout():
    b1 = Batcher(max_frames=1, flush_ns=1)
    assert [len(x) for x in b1.add(_rec(0), 0)] == [1]
    b8 = Batcher(max_frames=8, flush_ns=5_000_000)
    out = [b8.add(_rec(i), 1000 * i) for i in range(8)]
    assert all(o == [] for o in out[:7]) and len(out[7][0]) == 8
    b8.add(_rec(9), 10_000)
    assert not b8.due(10_000 + 4_999_999) and b8.due(10_000 + 5_000_000)
    assert b8.deadline() == 5_010_000 and len(b8.take()) == 1 and b8.deadline() is None


# ------------------------------------------------------------------- gateway
def _udp(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))
    s.settimeout(1.0)
    return s


def test_gateway_batching_and_downlink():
    up = _udp(48510)
    sent = []
    cfg = GatewayConfig(uplink_port=48510, downlink_port=48511, batch_frames=2, flush_ms=1.0)
    gw = Gateway(cfg, can_send=lambda cid, data: sent.append((cid, data)))
    try:
        t = time.monotonic_ns()
        for i in range(3):
            gw.on_frame(0x200, bytes(8), 1000 + i, 0, t)
        dg = decode(up.recv(2048))
        assert dg.type == TYPE_DATA and dg.seq == 0 and len(dg.records) == 2
        gw.tick(t + 1_000_000)  # third frame flushed by timeout
        dg = decode(up.recv(2048))
        assert dg.seq == 1 and [r.t_rx_ns for r in dg.records] == [1002]

        diag = Record(0x7DF, 0, 0, bytes([8, 0x22, 1, 0, 0, 0, 0, 0]))
        gw.on_downlink(encode(TYPE_DIAG_REQ, 0, 0, [diag, Record(0x100, 0, 0, bytes(4))]))
        gw.on_downlink(b"garbage")
        gw.on_downlink(encode(TYPE_DATA, 1, 0, [diag]))  # wrong type on the downlink
        assert sent == [(0x7DF, diag.data)]
        assert (gw.diag_sent, gw.rejected) == (1, 3)
    finally:
        gw.close()
        up.close()


def _e2e_frame(db, name: str, counter: int) -> Record:
    m = db.get_message_by_name(name)
    vals = {s.name: 0 for s in m.signals}
    data = e2e.protect(m.frame_id, m.encode(vals), counter)
    return Record(m.frame_id, time.time_ns(), 0, data)


def test_monitor_udp_loopback(db):
    mon = Monitor(db, 500_000)
    stop = threading.Event()
    th = threading.Thread(target=run_monitor, args=(mon, stop), kwargs={"port": 48520})
    th.start()
    time.sleep(0.2)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        frames = [_e2e_frame(db, "WheelSpeed", c) for c in (0, 1, 3)]  # counter jump at 3
        for seq, recs in ((0, frames[:1]), (1, frames[1:2]), (3, frames[2:])):  # seq 2 lost
            tx.sendto(encode(TYPE_DATA, seq, time.time_ns(), recs), ("127.0.0.1", 48520))
        bad = Record(0x6A1, time.time_ns(), 0, b"\xde\xad")
        tx.sendto(encode(TYPE_DATA, 4, time.time_ns(), [bad]), ("127.0.0.1", 48520))
        t0 = time.monotonic()
        while sum(mon.datagrams.values()) < 4 and time.monotonic() - t0 < 2:
            time.sleep(0.01)
    finally:
        stop.set()
        th.join(timeout=2)
        tx.close()
    s = mon.summary()
    assert s["frames"] == 4 and s["udp_lost"] == 1
    assert s["unknown_ids"] == {"0x6A1": 1}
    assert (s["crc_err"], s["counter_err"]) == (0, 1)
    assert s["latency_us"]["n"] == 4 and s["latency_us"]["p50"] > 0
    ids = {r["can_id"]: r for r in mon.id_rows()}
    assert ids["0x200"]["name"] == "WheelSpeed" and ids["0x200"]["expected_hz"] == 100


# --------------------------------------------------------------- vcan (needs vcan0 up)
@pytest.mark.vcan
def test_gateway_on_vcan(db):
    up = _udp(48530)
    cfg = GatewayConfig(iface="vcan0", uplink_port=48530, downlink_port=48531, batch_frames=1)
    gw = Gateway(cfg)
    stop = threading.Event()
    th = threading.Thread(target=gw.run, args=(stop,))
    th.start()
    peer = open_can("vcan0", [(0, 0)], False)
    peer.setblocking(True)
    try:
        time.sleep(0.1)
        peer.send(CAN_FRAME.pack(0x123, 2, b"\xab\xcd".ljust(8, b"\0")))
        rec = next(r for r in _data_records(up) if r.can_id == 0x123)
        assert rec.data == b"\xab\xcd" and not rec.flags & FLAG_SW_TIMESTAMP
        assert abs(time.time_ns() - rec.t_rx_ns) < 1e9
        down = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        diag = Record(0x7DF, 0, 0, bytes([8, 0x22, 1, 0, 0, 0, 0, 0]))
        down.sendto(encode(TYPE_DIAG_REQ, 0, 0, [diag]), ("127.0.0.1", 48531))
        down.close()
        frame = peer.recv(16)
        can_id, length, data = CAN_FRAME.unpack(frame)
        assert (can_id, data[:length]) == (0x7DF, diag.data)
        own = next(r for r in _data_records(up) if r.can_id == 0x7DF)
        assert own.flags & FLAG_OWN_TX
    finally:
        stop.set()
        th.join(timeout=2)
        gw.close()
        peer.close()
        up.close()


def _data_records(sock):
    while True:
        dg = decode(sock.recv(2048))
        if dg.type == TYPE_DATA:
            yield from dg.records


def test_can_frame_struct():
    assert CAN_FRAME.size == 16
    assert struct.calcsize("=IB3x8s") == 16
