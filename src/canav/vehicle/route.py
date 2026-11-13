"""Route geometry: a polyline with arc-length parametrisation (shared by sim2d and Planning)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np


def wrap_angle(a: float) -> float:
    """Wrap to (-pi, pi]."""
    return math.atan2(math.sin(a), math.cos(a))


class Route:
    """Polyline route. Lateral offsets are positive to the left of the driving direction."""

    def __init__(self, points):
        pts = np.asarray(points, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2:
            raise ValueError("route needs at least two (x, y) points")
        self.points = pts
        seg = np.diff(pts, axis=0)
        self.seg_len = np.hypot(seg[:, 0], seg[:, 1])
        if np.any(self.seg_len <= 0):
            raise ValueError("route has repeated points")
        self.seg_dir = seg / self.seg_len[:, None]
        self.s = np.concatenate(([0.0], np.cumsum(self.seg_len)))
        self.length = float(self.s[-1])

    @classmethod
    def from_segments(
        cls, start: tuple[float, float, float], segments: list[dict], step: float = 0.5
    ) -> Route:
        """Build from ``[{"straight": L}, {"arc": {"radius": R, "angle": a}}, ...]``.

        Angles in rad, positive = left turn. ``start`` = (x, y, heading).
        """
        x, y, h = start
        pts = [(x, y)]
        for seg in segments:
            if "straight" in seg:
                length = float(seg["straight"])
                n = max(1, math.ceil(length / step))
                for i in range(1, n + 1):
                    d = length * i / n
                    pts.append((x + d * math.cos(h), y + d * math.sin(h)))
                x, y = pts[-1]
            elif "arc" in seg:
                r, a = float(seg["arc"]["radius"]), float(seg["arc"]["angle"])
                side = 1.0 if a > 0 else -1.0
                cx, cy = x - side * r * math.sin(h), y + side * r * math.cos(h)
                n = max(1, math.ceil(abs(a) * r / step))
                phi0 = h - side * math.pi / 2
                for i in range(1, n + 1):
                    phi = phi0 + a * i / n
                    pts.append((cx + r * math.cos(phi), cy + r * math.sin(phi)))
                x, y = pts[-1]
                h += a
            else:
                raise ValueError(f"unknown route segment {seg!r}")
        return cls(pts)

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"points": self.points.round(4).tolist()}))

    @classmethod
    def from_json(cls, path: str | Path) -> Route:
        return cls(json.loads(Path(path).read_text())["points"])

    def project(self, x: float, y: float) -> tuple[float, float]:
        """(arc length s, lateral offset) of the closest point on the route."""
        p = np.array([x, y])
        rel = p - self.points[:-1]
        t = np.clip(np.einsum("ij,ij->i", rel, self.seg_dir), 0.0, self.seg_len)
        foot = self.points[:-1] + self.seg_dir * t[:, None]
        d2 = np.sum((p - foot) ** 2, axis=1)
        i = int(np.argmin(d2))
        dx, dy = rel[i]
        ux, uy = self.seg_dir[i]
        lat = ux * dy - uy * dx  # cross product: > 0 left of the route
        return float(self.s[i] + t[i]), float(lat)

    def _locate(self, s: float) -> tuple[int, float]:
        s = min(max(s, 0.0), self.length)
        i = int(np.clip(np.searchsorted(self.s, s, side="right") - 1, 0, len(self.seg_len) - 1))
        return i, s - self.s[i]

    def point_at(self, s: float) -> tuple[float, float]:
        """Point at arc length ``s`` (clamped to the route)."""
        i, t = self._locate(s)
        px, py = self.points[i] + self.seg_dir[i] * t
        return float(px), float(py)

    def heading_at(self, s: float) -> float:
        i, _ = self._locate(s)
        return float(math.atan2(self.seg_dir[i, 1], self.seg_dir[i, 0]))

    def curvature_at(self, s: float, ds: float = 1.0) -> float:
        """Signed curvature (1/m, > 0 = left) from the heading change over ``[s-ds, s+ds]``."""
        a, b = max(0.0, s - ds), min(self.length, s + ds)
        if b - a <= 0:
            return 0.0
        return wrap_angle(self.heading_at(b) - self.heading_at(a)) / (b - a)

    def max_abs_curvature(self, s0: float, s1: float, step: float = 1.0) -> float:
        return max(abs(self.curvature_at(s)) for s in np.arange(s0, max(s0, s1) + step, step))
