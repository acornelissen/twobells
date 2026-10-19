"""Provider-neutral departure model."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum
from typing import TypeGuard, overload

_DAY = 24 * 60
_HALF_DAY = _DAY // 2

# A clock time, 00:00 to 23:59: the only shape a departure's times may take.
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")

# A train Darwin calls "Delayed" (or gives no time for) has no time to go by,
# so it stays until a fetch no longer lists it. If the fetches stop, it goes
# this long after the last one, so it can't sit on the board for ever.
HOLD_UNTIMED = timedelta(hours=1)


def line_order(line: str) -> tuple:
    """A bus line's place in a list: by its number, the rest breaking ties, so
    X57 sorts as 57: 6, 57, X57, 61, 341. A line with no digits goes last."""
    digits = "".join(c for c in line if c.isdigit())
    return (int(digits) if digits else 10**6, line)


def row_id(kind: str, identity: str) -> str:
    """The opaque id of a row: 16 hex characters, the same every time for the same departure."""
    return hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()[:16]


class Status(str, Enum):
    ON_TIME = "on_time"
    LATE = "late"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def is_clock(value: object) -> TypeGuard[str]:
    """Whether value is a clock time as HH:MM."""
    return isinstance(value, str) and _CLOCK.fullmatch(value) is not None


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


def clock_at(hhmm: str, near: datetime) -> datetime:
    """The time `hhmm` nearest to `near` (within 12 hours either way), in
    near's zone. In the hour the clocks go back, which comes twice, it is
    the pass nearer to `near` in the time that passes."""
    base = near.replace(second=0, microsecond=0, tzinfo=None, fold=0)
    local = base + timedelta(minutes=minutes_between(base.strftime("%H:%M"), hhmm))
    if near.tzinfo is None:
        return local
    now = near.astimezone(UTC)
    passes = [local.replace(tzinfo=near.tzinfo, fold=fold) for fold in (0, 1)]  # the same but in that hour
    return min(passes, key=lambda moment: abs(moment.astimezone(UTC) - now))


def zoned(local: datetime, zone: tzinfo, near: datetime) -> datetime:
    """A board-local `local` (naive, with its date) in `zone`, counted on the
    clock from `near` (aware), as a bus board counts its times: in the hour
    the clocks go back, which comes twice, the pass that many clock minutes
    from `near`."""
    clock_now = near.astimezone(zone).replace(tzinfo=None)
    meant = real(near) + (local - clock_now)
    passes = [local.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]  # the same but in that hour
    return min(passes, key=lambda moment: abs(real(moment) - meant))


def real(moment: datetime) -> datetime:
    """An aware `moment` in UTC, to compare or work out a difference in the
    time that passes: between two aware times in one zone, Python goes by
    the clock, the hour the clocks go back counted twice. Naive, as it is."""
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment


def minutes_apart(start: datetime, end: datetime) -> int:
    """Whole minutes from `start` to `end` as they pass, across the clocks changing too."""
    return round((real(end) - real(start)) / timedelta(minutes=1))


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
    # A rail replacement bus, run in a train's place: Darwin's serviceType "bus".
    replacement_bus: bool = False
    # A ferry listed with the trains, a connection of its own: Darwin's
    # serviceType "ferry". Never a replacement bus too.
    ferry: bool = False
    # Darwin's words on the route, like "via Guide Bridge"; None for most.
    via: str | None = None
    # Its platform has changed since an earlier fetch: see mark_platform_changes.
    platform_changed: bool = False
    # A train's "Calling at" line (core/calling.py), from Darwin's calling
    # points; None without them, and for a bus.
    calling_at: str | None = None
    # What a screen's speaker says of a train beyond its row (core/announce.py),
    # as the dashboard says it: the stops it calls at after this station, those
    # it no longer calls at, its coaches, and why it is cancelled (when it is)
    # or late (when it is). Only a screen's board carries them (DarwinSource
    # with_details); e-paper never speaks, so it leaves them empty.
    calling_stops: tuple[str, ...] = ()
    skipped_stops: tuple[str, ...] = ()
    coaches: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        # Every time is worked out from these, so one that isn't HH:MM would
        # stop the board on every run; it is refused here instead.
        if not is_clock(self.scheduled):
            raise ValueError(f"scheduled time is not HH:MM: {self.scheduled!r}")
        if self.expected is not None and not is_clock(self.expected):
            raise ValueError(f"expected time is not HH:MM: {self.expected!r}")
        if not isinstance(self.status, Status):
            raise TypeError("status is not a Status")
        for name in ("mode", "service", "destination"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} is not text")
        for name in ("platform", "stop", "via", "calling_at", "reason"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} is not text")
        for name in ("calling_stops", "skipped_stops"):
            stops = getattr(self, name)
            if not isinstance(stops, tuple) or not all(isinstance(stop, str) for stop in stops):
                raise TypeError(f"{name} is not a list of names")
        # bool is an int in Python, and True coaches is not a train.
        if self.coaches is not None and (not isinstance(self.coaches, int) or isinstance(self.coaches, bool)
                                         or self.coaches < 1):
            raise ValueError(f"coaches is not a number of coaches: {self.coaches!r}")
        for name in ("replacement_bus", "ferry", "platform_changed"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} is not true or false")
        if self.replacement_bus and self.ferry:
            raise ValueError("a departure is a replacement bus or a ferry, not both")

    @property
    def is_train(self) -> bool:
        """A train proper: not a bus, nor a replacement bus or ferry listed
        with the trains, which have no platform or coaches."""
        return self.mode == "train" and not (self.replacement_bus or self.ferry)

    @property
    def departs_at(self) -> str:
        return self.expected or self.scheduled

    @property
    def minutes_late(self) -> int | None:
        """How late on the clock; minutes_late_at for the minutes that pass."""
        if self.status is Status.ON_TIME:
            return 0
        if self.expected is None:
            return None
        return minutes_between(self.scheduled, self.expected)

    def minutes_late_at(self, near: datetime) -> int | None:
        """How late, in the minutes that pass, its times read as nearest
        `near` (times): across the clocks changing too, where booked at
        01:50 BST and expected at 01:10 GMT is 20 minutes late."""
        if self.status is Status.ON_TIME:
            return 0
        if self.expected is None:
            return None
        scheduled_at = clock_at(self.scheduled, near)
        return minutes_apart(scheduled_at, clock_at(self.expected, scheduled_at))

    def times(self, near: datetime) -> tuple[datetime, datetime | None]:
        """When it is booked to leave, read as the time nearest `near`, and
        when it is expected to (nearest the booked time); None when late
        with no time, cancelled, or not known."""
        scheduled_at = clock_at(self.scheduled, near)
        if self.status is Status.ON_TIME:
            return scheduled_at, scheduled_at
        if self.status is Status.LATE and self.expected:
            return scheduled_at, clock_at(self.expected, scheduled_at)
        return scheduled_at, None

    def can_be_next(self, near: datetime, now: datetime) -> bool:
        """Whether a train can be the next to depart at `now`, its times read
        as nearest `near`: not cancelled, not late with no time to go by (it
        could go at any moment, or not for long), and not gone by its
        expected time, or else its booked one. The dashboard's rows, the wall
        tablet (pa.js due(), which trusts the "next" announcement this
        decides) and a screen's speaker all go by it."""
        if self.status is Status.CANCELLED:
            return False
        scheduled_at, expected_at = self.times(near)
        if self.status is Status.LATE and expected_at is None:
            return False
        return real(expected_at or scheduled_at) >= real(now)

    @property
    def _leaves(self) -> str:
        # A cancelled departure goes from the board at its booked time.
        return self.scheduled if self.status is Status.CANCELLED else self.departs_at

    @property
    def _untimed(self) -> bool:
        """A train that is late with no time to go by. A bus without a live
        time is different: it goes by its timetable."""
        return self.mode == "train" and self.expected is None and self.status in (Status.LATE, Status.UNKNOWN)

    def is_gone(self, now: str) -> bool:
        if self._untimed:
            return False
        return minutes_between(now, self._leaves) < 0

    def has_left(self, now: datetime, fetched: datetime) -> bool:
        """Like is_gone, but on the calendar: the time is read as the one nearest
        to when it was fetched, so this morning's 08:40 never comes back tonight."""
        fetched = fetched.replace(second=0, microsecond=0)
        if self._untimed:
            return now.replace(second=0, microsecond=0) > fetched + HOLD_UNTIMED
        leaves = fetched + timedelta(minutes=minutes_between(fetched.strftime("%H:%M"), self._leaves))
        return leaves < now.replace(second=0, microsecond=0)


def _same_train(dep: Departure) -> tuple[str, str, str, str, bool, bool]:
    return dep.mode, dep.service, dep.scheduled, dep.destination, dep.replacement_bus, dep.ferry


def mark_platform_changes(before: list[Departure], after: list[Departure]) -> list[Departure]:
    """`after`, a new fetch, with platform_changed set on each departure whose
    platform isn't the one `before` (the last fetch, less what has left
    since) gave it, and kept while it stays on the new one. Only a platform
    given both times counts: one appearing, or going, is no change, and a
    replacement bus or ferry has none, nor has a train cancelled."""
    previous = {_same_train(dep): dep for dep in before}
    marked = []
    for dep in after:
        old = previous.get(_same_train(dep))
        changed = (dep.is_train and dep.status is not Status.CANCELLED and old is not None
                   and dep.platform is not None and old.platform is not None
                   and (old.platform != dep.platform or old.platform_changed))
        marked.append(replace(dep, platform_changed=changed) if changed != dep.platform_changed else dep)
    return marked


# The most stops a row's strip draws: the dashboard's five before yours, yours
# and the next, with room to spare.
STRIP_MOST_STOPS = 8
WHERE_MOST_CHARS = 60


@dataclass(frozen=True)
class Strip:
    """Where a departure's train or bus is, for a screen's row: the words, and
    a little line of `stops` stops with yours at `yours` and the vehicle `at`
    its place in stop units along them (None draws no vehicle). Kept beside the
    departures, not in them: it changes every few seconds, and the departures
    are saved only when new ones come."""

    where: str  # "At Dinting", "3 stops away"
    stops: int
    yours: int
    at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.where, str) or len(self.where) > WHERE_MOST_CHARS:
            raise ValueError("where is not short text")
        if isinstance(self.stops, bool) or not isinstance(self.stops, int) or not 2 <= self.stops <= STRIP_MOST_STOPS:
            raise ValueError(f"stops must be 2 to {STRIP_MOST_STOPS}, not {self.stops!r}")
        if isinstance(self.yours, bool) or not isinstance(self.yours, int) or not 0 <= self.yours < self.stops:
            raise ValueError(f"yours must be a stop on the strip, not {self.yours!r}")
        if self.at is not None and (isinstance(self.at, bool) or not isinstance(self.at, (int, float))
                                    or not 0 <= self.at <= self.stops - 1):
            raise ValueError(f"at must be on the strip, not {self.at!r}")


def strip_key(dep: Departure) -> tuple[str, str, str, str]:
    """Which departure a Strip is for: the same train or bus from one fetch to
    the next, whatever its live time says."""
    return dep.mode, dep.service, dep.stop or "", dep.scheduled

@overload
def iso(moment: datetime) -> str: ...
@overload
def iso(moment: None) -> None: ...
@overload
def iso(moment: datetime | None) -> str | None: ...


def iso(moment: datetime | None) -> str | None:
    """`moment` to the second, as the dashboard's API and history give times."""
    return moment.isoformat(timespec="seconds") if moment is not None else None
