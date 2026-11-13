"""Vehicle backends (§5.7): common interface and data types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from canav.vehicle.route import Route

# Object classes, identical to ObjectList.cls on CAN (docs/architecture.md)
CLS_UNKNOWN, CLS_CAR, CLS_PEDESTRIAN, CLS_CYCLIST, CLS_STATIC = 0, 1, 2, 3, 4
TL_STATES = {"none": 0, "red": 1, "yellow": 2, "green": 3}


@dataclass
class Control:
    """Actuator command applied by the backend: steer in [-1, 1], throttle/brake in [0, 1]."""

    steer: float = 0.0
    throttle: float = 0.0
    brake: float = 0.0


@dataclass
class VehicleState:
    """Ground truth after a step. Pose = rear-axle position (m) and yaw (rad), world frame."""

    t_sim: float
    x: float
    y: float
    yaw: float
    speed: float
    ax: float
    ay: float
    yaw_rate: float
    s: float  # arc length of the rear axle along the route
    lane_offset: float  # lateral offset from the route (> 0 = left)
    lane_heading_err: float
    objects: list[dict] = field(default_factory=list)  # id, cls, x, y, vx, vy, yaw, length, width
    tl: dict = field(default_factory=dict)  # state, dist_stop (front bumper -> stop line)
    collisions: list[int] = field(default_factory=list)  # object ids currently in contact
    goal_reached: bool = False


class VehicleBackend(ABC):
    @abstractmethod
    def reset(self, scenario: dict) -> VehicleState: ...

    @abstractmethod
    def step(self, control: Control) -> VehicleState: ...

    @abstractmethod
    def route(self) -> Route: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        pass


__all__ = [
    "CLS_CAR",
    "CLS_CYCLIST",
    "CLS_PEDESTRIAN",
    "CLS_STATIC",
    "CLS_UNKNOWN",
    "Control",
    "Route",
    "TL_STATES",
    "VehicleBackend",
    "VehicleState",
]
