"""Provider-neutral departure model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_DAY = 24 * 60
_HALF_DAY = _DAY // 2


class Status(str, Enum):
    ON_TIME = "on_time"
    LATE = "late"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_between(start: str, end: str) -> int:
    """Signed minutes from start to end, assuming they are within 12 hours."""
    diff = (_to_minutes(end) - _to_minutes(start)) % _DAY
    return diff - _DAY if diff > _HALF_DAY else diff


@dataclass(frozen=True)
class Departure:
    mode: str
    service: str
    destination: str
    scheduled: str
    expected: str | None
    status: Status
    platform: str | None = None

    @property
    def departs_at(self) -> str:
        return self.expected or self.scheduled

    @property
    def minutes_late(self) -> int | None:
        if self.status is Status.ON_TIME:
            return 0
        if self.expected is None:
            return None
        return minutes_between(self.scheduled, self.expected)

    def is_gone(self, now: str) -> bool:
        reference = self.scheduled if self.status is Status.CANCELLED else self.departs_at
        return minutes_between(now, reference) < 0
