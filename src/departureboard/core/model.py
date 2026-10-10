"""Provider-neutral departure model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import overload

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


def shift(hhmm: str, minutes: int) -> str:
    """Clock time moved by `minutes`, wrapping at midnight."""
    total = (_to_minutes(hhmm) + minutes) % _DAY
    return f"{total // 60:02d}:{total % 60:02d}"


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
    stop: str | None = None

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

    @property
    def _leaves(self) -> str:
        # A cancelled departure goes from the board at its booked time.
        return self.scheduled if self.status is Status.CANCELLED else self.departs_at

    def is_gone(self, now: str) -> bool:
        return minutes_between(now, self._leaves) < 0

    def has_left(self, now: datetime, fetched: datetime) -> bool:
        """Like is_gone, but on the calendar: the time is read as the one nearest
        to when it was fetched, so this morning's 08:40 never comes back tonight."""
        fetched = fetched.replace(second=0, microsecond=0)
        leaves = fetched + timedelta(minutes=minutes_between(fetched.strftime("%H:%M"), self._leaves))
        return leaves < now.replace(second=0, microsecond=0)


@overload
def iso(moment: datetime) -> str: ...
@overload
def iso(moment: None) -> None: ...
@overload
def iso(moment: datetime | None) -> str | None: ...


def iso(moment: datetime | None) -> str | None:
    """`moment` to the second, as the dashboard's API and history give times."""
    return moment.isoformat(timespec="seconds") if moment is not None else None
