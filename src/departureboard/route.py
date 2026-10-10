"""Route strips: the last few stops before yours, and where the next bus is on them.

Positions are matched in a flat local grid in metres around your stop, which
is accurate to well under a metre over the kilometre or so a strip covers.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from itertools import pairwise

from .bods import Vehicle
from .model import Departure

# A vehicle further than this from the strip's line is somewhere else.
MAX_OFF_ROUTE_M = 150
# Beyond either end of the strip it has not reached it yet, or has gone past your stop.
MAX_OVERSHOOT_M = 30
# Bearing further than this from the road's direction means the other way.
MAX_HEADING_OFF = 90
_EARTH_M_PER_DEG = 111_320


@dataclass(frozen=True)
class RouteStop:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class RouteStrip:
    """Stops in the order the bus calls at them; the last one is yours."""

    line: str
    stop: str  # your stop's label on the board, e.g. "A"
    stops: tuple[RouteStop, ...]

    def box(self, margin_deg: float = 0.01) -> tuple[float, float, float, float]:
        """(west, south, east, north) around the strip, for asking BODS."""
        lats = [s.lat for s in self.stops]
        lons = [s.lon for s in self.stops]
        return (min(lons) - margin_deg, min(lats) - margin_deg, max(lons) + margin_deg, max(lats) + margin_deg)


def _xy(strip: RouteStrip, lat: float, lon: float) -> tuple[float, float]:
    home = strip.stops[-1]
    x = (lon - home.lon) * _EARTH_M_PER_DEG * math.cos(math.radians(home.lat))
    y = (lat - home.lat) * _EARTH_M_PER_DEG
    return x, y


def _heading(ax: float, ay: float, bx: float, by: float) -> float:
    """Compass bearing in degrees from a to b."""
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360


def position_on(strip: RouteStrip, vehicle: Vehicle) -> float | None:
    """How far along the strip a vehicle is, in stops: 0 at the first stop,
    len(stops) - 1 at yours. None if it is off the strip or heading the other way."""
    px, py = _xy(strip, vehicle.lat, vehicle.lon)
    points = [_xy(strip, s.lat, s.lon) for s in strip.stops]
    last = len(points) - 2
    best = None
    for i, ((ax, ay), (bx, by)) in enumerate(pairwise(points)):
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy or 1e-9
        raw = ((px - ax) * dx + (py - ay) * dy) / length_sq
        t = max(0.0, min(1.0, raw))
        off = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        overshoot = (i == 0 and raw < 0) or (i == last and raw > 1)
        limit = MAX_OVERSHOOT_M if overshoot else MAX_OFF_ROUTE_M
        if off <= limit and (best is None or off < best[0]):
            best = (off, i, t, _heading(ax, ay, bx, by))
    if best is None:
        return None
    off, i, t, road = best
    if vehicle.bearing is not None:
        turn = abs((vehicle.bearing - road + 180) % 360 - 180)
        if turn > MAX_HEADING_OFF:
            return None
    return i + t


def locate(strip: RouteStrip, vehicles: list[Vehicle], ahead: int) -> float | None:
    """Position of the bus that is `ahead` buses behind the first one due at
    your stop (0 = the first), or None if it is not on the strip yet."""
    positions = sorted(
        (p for v in vehicles if v.line == strip.line and (p := position_on(strip, v)) is not None),
        reverse=True,
    )
    return positions[ahead] if ahead < len(positions) else None


def target(buses: list[Departure], chosen: Departure) -> int:
    """How many buses of the same line are due at the same stop before `chosen`."""
    return sum(
        1
        for dep in buses[: buses.index(chosen)]
        if dep.service == chosen.service and dep.stop == chosen.stop
    )


@dataclass(frozen=True)
class RouteView:
    """The second page: one bus and the stretch of road before your stop."""

    title: str  # "341 to Hyde"
    due: str  # as on the departures page, e.g. "07:24 +4"
    leave: str | None  # "leave in 6 min"
    stops: tuple[str, ...]  # the last one is yours
    bus_at: float | None  # stops along from the first; None if not on the strip
    where: str  # "3 stops away", "At your stop", ...
    clock: str | None = None

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def where(stops: tuple[str, ...], bus_at: float | None, stale: bool) -> str:
    if stale:
        return "Position not updating"
    if bus_at is None:
        return f"Not yet past {stops[0]}"
    away = (len(stops) - 1) - math.floor(bus_at + 0.02)
    if away <= 0:
        return "At your stop"
    return "1 stop away" if away == 1 else f"{away} stops away"
