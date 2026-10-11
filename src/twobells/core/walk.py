"""Walking times to each station or stop, and when to leave for the next departure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .model import Departure, Status, minutes_between, shift

DEFAULT_STOP = "*"


@dataclass(frozen=True)
class WalkTimes:
    train: int | None = None
    # Minutes by stop label; "*" applies to any stop not listed.
    bus: Mapping[str, int] = field(default_factory=dict)

    def minutes(self, dep: Departure) -> int | None:
        if dep.mode == "train":
            return self.train
        return self.stop(dep.stop)

    def stop(self, label: str | None) -> int | None:
        """Minutes to the bus stop with this label, or None if no walk is set."""
        return self.bus.get(label or DEFAULT_STOP, self.bus.get(DEFAULT_STOP))


def leave_time(dep: Departure, walk: WalkTimes) -> str | None:
    """When to set off to catch `dep`, or None if cancelled or the walk is unknown."""
    minutes = walk.minutes(dep)
    if minutes is None or dep.status is Status.CANCELLED:
        return None
    return shift(dep.departs_at, -minutes)


def leave_by(departures: list[Departure], walk: WalkTimes, now: str) -> tuple[str, Departure] | None:
    """Leave time for the first departure that can still be reached on foot."""
    for dep in departures:
        leave = leave_time(dep, walk)
        if leave is not None and minutes_between(now, leave) >= 0:
            return leave, dep
    return None
