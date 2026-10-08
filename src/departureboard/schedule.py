"""When to poll each source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WINDOW = re.compile(r"^\s*([A-Za-z,\-]+)\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s*$")

# A minutely systemd timer fires with a few seconds of jitter.
_SLACK = timedelta(seconds=15)


def _parse_days(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        if "-" in part:
            first, last = part.split("-")
            days.update(range(_DAYS.index(first), _DAYS.index(last) + 1))
        else:
            days.add(_DAYS.index(part))
    return frozenset(days)


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: time
    end: time

    def contains(self, moment: datetime) -> bool:
        return moment.weekday() in self.days and self.start <= moment.time() <= self.end


def parse_window(spec: str) -> Window:
    """Parse e.g. 'Mon-Fri 06:45-09:00' or 'Sat,Sun 10:00-12:00'."""
    match = _WINDOW.match(spec)
    if not match:
        raise ValueError(f"Unrecognised window: {spec!r}")
    days, start, end = match.groups()
    try:
        return Window(_parse_days(days), time.fromisoformat(start), time.fromisoformat(end))
    except ValueError as error:
        raise ValueError(f"Unrecognised window: {spec!r}") from error


@dataclass(frozen=True)
class PollPolicy:
    windows: tuple[Window, ...]
    interval: timedelta

    def is_active(self, now: datetime) -> bool:
        return any(window.contains(now) for window in self.windows)

    def is_due(self, now: datetime, last_fetch: datetime | None) -> bool:
        if not self.is_active(now):
            return False
        return last_fetch is None or now - last_fetch >= self.interval - _SLACK
