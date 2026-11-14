"""Non-CAN side channel over UDP 127.0.0.1.

Bridge -> ECUs: ``{"type": "state", ...}`` to ``port_base + 1 + node index`` (47001-47010).
Actuators -> bridge: ``{"type": "ctrl", ...}`` to ``port_base + 100`` (47100).
Raw sensor data never goes on CAN.
"""

from __future__ import annotations

import json
import socket
import time

from canav.vehicle import VehicleState

HOST = "127.0.0.1"
PORT_BASE = 47000
CTRL_OFFSET = 100
MAX_DATAGRAM = 65_507


def state_port(node_index: int, port_base: int = PORT_BASE) -> int:
    """UDP port of the node at ``node_index`` (0-based, matrix.yaml order)."""
    return port_base + 1 + node_index


def ctrl_port(port_base: int = PORT_BASE) -> int:
    return port_base + CTRL_OFFSET


def encode(msg: dict) -> bytes:
    data = json.dumps(msg, separators=(",", ":")).encode()
    if len(data) > MAX_DATAGRAM:
        raise ValueError("side-channel message too large for one datagram")
    return data


def decode(data: bytes) -> dict:
    return json.loads(data)


def state_message(seq: int, st: VehicleState) -> dict:
    return {
        "type": "state",
        "seq": seq,
        "t_sim": round(st.t_sim, 6),
        "t_wall_ns": time.time_ns(),
        "pose": {"x": st.x, "y": st.y, "yaw": st.yaw},
        "speed": st.speed,
        "imu": {"ax": st.ax, "ay": st.ay, "yaw_rate": st.yaw_rate},
        "objects": st.objects,
        "tl": st.tl | {"lane_offset": st.lane_offset, "lane_heading_err": st.lane_heading_err},
    }


def ctrl_message(
    seq: int,
    src_counter: int,
    steer: float | None = None,
    throttle: float | None = None,
    brake: float | None = None,
) -> dict:
    """Partial command: each actuator fills only the fields it owns (others None)."""
    return {
        "type": "ctrl",
        "seq": seq,
        "steer": steer,
        "throttle": throttle,
        "brake": brake,
        "src_counter": src_counter,
    }


class UdpSender:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, port: int, msg: dict) -> None:
        self.sock.sendto(encode(msg), (HOST, port))

    def close(self) -> None:
        self.sock.close()


class UdpLatest:
    """Non-blocking receiver keeping only the newest message (sample-and-hold)."""

    def __init__(self, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((HOST, port))
        self.sock.setblocking(False)
        self.latest: dict | None = None
        self.received = 0

    def poll(self) -> list[dict]:
        """Drain the socket; returns all messages received since the last poll."""
        out = []
        while True:
            try:
                data = self.sock.recv(MAX_DATAGRAM)
            except BlockingIOError:
                break
            msg = decode(data)
            out.append(msg)
        if out:
            self.latest = out[-1]
            self.received += len(out)
        return out

    def close(self) -> None:
        self.sock.close()
