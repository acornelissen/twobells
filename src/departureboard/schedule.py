"""When to poll each source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_SUNDAY = 6
_WINDOW = re.compile(r"^\s*([A-Za-z,\-]+)\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s*$")

# A minutely systemd timer fires with a few seconds of jitter. Short intervals,
# such as bus positions polled every 30 s from a loop, get proportionally less.
_SLACK = timedelta(seconds=15)


def _weekday(day: date, holidays: frozenset[date] = frozenset()) -> int:
    """The day of the week a window sees: a bank holiday runs a Sunday service."""
    return _SUNDAY if day in holidays else day.weekday()


def _parse_days(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for part in spec.lower().split(","):
        if "-" in part:
            first, last = (_DAYS.index(day) for day in part.split("-"))
            # A range wraps round the week: Fri-Mon is Fri, Sat, Sun, Mon.
            days.update(day % 7 for day in range(first, last + 1 if last >= first else last + 8))
        else:
            days.add(_DAYS.index(part))
    return frozenset(days)


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: time
    end: time

    def contains(self, moment: datetime, holidays: frozenset[date] = frozenset()) -> bool:
        day, clock = _weekday(moment.date(), holidays), moment.time()
        if self.start <= self.end:
            return day in self.days and self.start <= clock <= self.end
        # Ends before it starts, so it runs past midnight into the next morning,
        # as the day it started on: after a bank holiday, a Sunday's.
        yesterday = _weekday(moment.date() - timedelta(days=1), holidays)
        return (day in self.days and clock >= self.start) or (yesterday in self.days and clock <= self.end)


def parse_window(spec: str) -> Window:
    """Parse e.g. 'Mon-Fri 06:45-09:00', 'Sat,Sun 10:00-12:00' or 'Fri-Sat 22:00-01:00'."""
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
    holidays: frozenset[date] = frozenset()  # bank holidays, treated like Sundays

    def is_active(self, now: datetime) -> bool:
        return any(window.contains(now, self.holidays) for window in self.windows)

    def is_due(self, now: datetime, last_fetch: datetime | None) -> bool:
        if not self.is_active(now):
            return False
        if last_fetch is None or last_fetch > now:
            # Never fetched, or the clock went back (summer time ending): fetch now
            # rather than wait an hour for the clock to catch up.
            return True
        slack = min(_SLACK, self.interval / 10)
        return now - last_fetch >= self.interval - slack
