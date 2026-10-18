"""The route strip page: one bus and the stretch of road before your stop
(RouteView), and which of the buses due it is (target). The strips, and
where a bus is on one, are timetable/route.py's."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ..core.model import Departure


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
    due: str  # as on the departures page, e.g. "Exp 07:24"
    leave: str | None  # "Leave in 6 min"
    stops: tuple[str, ...]  # the last one is yours
    bus_at: float | None  # stops along from the first; None if not on the strip
    where: str  # "3 stops away", "At your stop", ...
    clock: str | None = None
    scroll: bool = True  # on a screen, whether a name too long for its space scrolls; off, it is cut short

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
