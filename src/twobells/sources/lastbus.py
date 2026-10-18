"""The last buses: for each line and destination from your stops, as the
board shows them, the last departure of the day, from the BODS timetable.

A day here is a service day (timetable/model.py): a bus at 00:20 on a
journey that started the evening before is that evening's last bus. So is
one before NIGHT_ENDS that the timetable gives the next day rather than
running on past midnight, as many do: it is the night's, not the next
day's. So just after midnight a line whose evening buses are still to come
shows the last of them, and once that has gone, tonight's. Which journeys run on a day is
timetable.build.runs_on's to say, bank holidays and days of the week as the
timetable gives them.

A day's departures are grouped once (day_groups), reading every journey;
what is left of them at a moment (last_buses) is a look along those groups,
cheap enough for a screen to ask every second.

Everything here is pure: the caller passes the time and the timetable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..core.model import line_order
from ..timetable.build import runs_on
from ..timetable.model import Schedule
from .livebus import GRACE, heading_to

# Dashboard: the last buses show under the buses from this time on, or once
# this few of a line and destination's buses are left.
EVENING = time(19, 0)
FEW_LEFT = 3
# A screen shows the last bus of all on its bottom line this long before it goes.
SCREEN_AHEAD = timedelta(hours=1)
# A call before this many minutes after midnight belongs to the night before.
NIGHT_ENDS = 3 * 60
_DAY = 24 * 60


@dataclass(frozen=True)
class DayGroup:
    """One line and destination's departures from your stops on a service day."""

    minutes: tuple[int, ...]  # after midnight of the service day, soonest first
    stop: str  # the label of your stop the last one leaves from


# By (line, destination), as the board's rows say them.
DayGroups = dict[tuple[str, str], DayGroup]


@dataclass(frozen=True)
class LastBus:
    line: str
    destination: str
    stop: str  # your stop's label
    service_day: date
    leaves: datetime  # timetabled, board-local without a zone
    # The line and destination's departures still to come that day, this
    # one included; 0 once it has gone.
    remaining: int

    def shown(self, now: datetime) -> bool:
        """Whether the dashboard lists it under the buses at `now` (board-local,
        without a zone): in the evening, or once few are left. Just after
        midnight, the evening's lines still running have few left; tonight's
        wait for the evening."""
        return now.time() >= EVENING or self.remaining <= FEW_LEFT


def day_groups(
    schedule: Schedule,
    day: date,
    holiday_names: Mapping[date, Set[str]] = {},
    directions: Iterable[str] | None = None,
) -> DayGroups:
    """Departures from your stops on service day `day`, by line and
    destination, as live_board finds them: journeys that run that day (by
    runs_on, with each day's bank holiday names), calling at one of your
    stops before their last call, heading in one of `directions` if given.
    A call before NIGHT_ENDS is the night before's: one on the next day's
    journeys counts, a day on, and one on the day's own doesn't."""
    labels: dict[str, list[str]] = {}
    for label, atco in sorted(schedule.stops.items()):
        labels.setdefault(atco, []).append(label)
    running: dict[tuple[int, date], bool] = {}  # by calendar, which a timetable's journeys share, and day
    tomorrow = day + timedelta(days=1)
    found: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for journey in schedule.journeys:
        if journey.inbound_only or not heading_to(journey.destination, directions):
            continue
        calendar = journey.calendar
        for call in journey.calls[:-1]:  # the last call is where it terminates
            here = labels.get(call.atco, ())
            if not here:
                continue
            on, minute = (tomorrow, call.minute + _DAY) if call.minute < NIGHT_ENDS else (day, call.minute)
            if (id(calendar), on) not in running:
                running[id(calendar), on] = runs_on(calendar, on, holiday_names)
            if not running[id(calendar), on]:
                continue
            for label in here:
                found.setdefault((journey.line, journey.destination), []).append((minute, label))
    return {
        key: DayGroup(tuple(sorted(minute for minute, _ in items)), min(items, key=lambda i: (-i[0], i[1]))[1])
        for key, items in found.items()
    }


def _last(key: tuple[str, str], group: DayGroup, day: date, now: datetime) -> LastBus:
    midnight = datetime.combine(day, time())
    remaining = sum(1 for minute in group.minutes if midnight + timedelta(minutes=minute) >= now - GRACE)
    leaves = midnight + timedelta(minutes=group.minutes[-1])
    return LastBus(key[0], key[1], group.stop, day, leaves, remaining)


def last_buses(days: Mapping[date, DayGroups], now: datetime) -> list[LastBus]:
    """Each line and destination's last bus at `now` (board-local, without a
    zone), soonest first: yesterday's service day's while it is still to
    come, else today's, gone or not. `days` has the groups for both (a day
    left out has no buses)."""
    today = now.date()
    yesterday = today - timedelta(days=1)
    earlier, later = days.get(yesterday, {}), days.get(today, {})
    found = []
    for key in {*earlier, *later}:
        last = _last(key, earlier[key], yesterday, now) if key in earlier else None
        if last is None or not last.remaining:
            if key not in later:
                continue  # its last bus last night has gone, and none runs today
            last = _last(key, later[key], today, now)
        found.append(last)
    found.sort(key=lambda b: (b.leaves, line_order(b.line), b.destination))
    return found


def last_of_all(found: Iterable[LastBus]) -> LastBus | None:
    """The night's last bus of all those still to come: the latest on the
    earliest service day that has any. None when every one has gone."""
    left = [b for b in found if b.remaining]
    if not left:
        return None
    day = min(b.service_day for b in left)
    return max((b for b in left if b.service_day == day), key=lambda b: b.leaves)


def screen_line(found: Iterable[LastBus], now: datetime) -> str | None:
    """"Last bus 23:05" for a screen's bottom line, in the hour before the
    last bus of all goes (`now` board-local, without a zone); else None."""
    last = last_of_all(found)
    if last is None or last.leaves - now > SCREEN_AHEAD:
        return None
    return f"Last bus {last.leaves.strftime('%H:%M')}"
