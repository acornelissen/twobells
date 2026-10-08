"""Walking times to each station or stop, and when to leave for the next departure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

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
        return self.bus.get(dep.stop or DEFAULT_STOP, self.bus.get(DEFAULT_STOP))


def leave_by(departures: list[Departure], walk: WalkTimes, now: str) -> tuple[str, Departure] | None:
    """Leave time for the first departure that can still be reached on foot."""
    for dep in departures:
        minutes = walk.minutes(dep)
        if minutes is None or dep.status is Status.CANCELLED:
            continue
        leave = shift(dep.departs_at, -minutes)
        if minutes_between(now, leave) >= 0:
            return leave, dep
    return None
