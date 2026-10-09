"""Extras on the dashboard's rows: when each train and bus gets where it's
going, which bus will probably work a departure that starts at your stop,
and a warning for a bus due soon with no tracker.

Everything here is pure and works from what the feeds already gave, so
nothing reads the clock or the network. All of it is a best guess on top of
the row, never a change to it: a row's own times and status words stay as
the board worked them out.

Arrivals. A train arrives at the filter station (`[train] filter_crs`) if it
calls there, else at its last calling point; Darwin's expected time is used
where it is a time, else the booked one. "Delayed" with no time means the
booked time is wrong and the right one unknown, so there is no arrival then,
nor for a cancelled train or stop. A bus arrives at its journey's last call
at the timetabled time plus the delay it has now.

Inbound buses. BODS sends no block numbers here (its BlockRef is just the
line), so, as bustimes.org does, a departure whose journey starts at your
stop is paired with a journey of the same line that ends at your stop (or
at a stop within NEAR_M of it) up to INBOUND_WINDOW before it leaves. Only
journeys with a live bus placed on them count, and only when exactly one
fits: two is a guess too far. A bus that would sooner work an earlier
departure of the same line from the same stop is left to that one.

The inbound bus is shown as a line of its own ("Bus arriving from Ashton, 4
min late (due 23:29)"). It never changes the departure's expected time or
status words, even when it is due after the departure: it is an inference,
drivers make up time on layovers, and another bus may take the journey. The
facts in the line (where that bus is, how late it is) come from its tracker.

Ghost buses. A bus due within GHOST_WITHIN with no live bus placed on its
journey is flagged, since trackers usually show up well before then. Not
when live positions failed (every row would be flagged), and not when an
inbound bus with a tracker is on its way to work it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, time, timedelta, tzinfo

from .bodsbus import BusBoard
from .darwin import CallingPoint, TrainService
from .livebus import LiveDeparture, LiveJourney
from .model import Status
from .schedule_model import NEAR_M, StopPosition, metres

# An inbound bus arrives at most this long before the departure it works.
INBOUND_WINDOW = timedelta(minutes=30)
# A bus due this soon with no tracker gets the warning.
GHOST_WITHIN = timedelta(minutes=5)
# A journey longer than this is a mistake in the times, not a long journey.
_LONGEST = 12 * 60
_DAY = 24 * 60
_NAME_LIMIT = 80
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _arrival(name: str, at: datetime, minutes: int) -> dict:
    """The contract's arrival: `at` is aware, in the board's zone."""
    return {"name": name, "at": at.strftime("%H:%M"), "at_iso": _iso(at), "minutes": minutes}


# Trains


def _target(points: tuple[CallingPoint, ...], filter_crs: str | None) -> CallingPoint | None:
    """The filter station if the train calls there, else its last calling point."""
    if filter_crs:
        for point in points:
            if point.crs == filter_crs:
                return point
    return points[-1] if points else None


def _point_time(point: CallingPoint) -> str | None:
    """When the train gets to a calling point as HH:MM: Darwin's time if it
    gave one, else the booked time; None if cancelled there or "Delayed"."""
    if point.cancelled or "Cancelled" in (point.expected, point.actual):
        return None
    for value in (point.actual, point.expected):
        if value and _CLOCK.fullmatch(value):
            return value
    if point.expected == "Delayed":
        return None
    return point.scheduled


def _minute_of_day(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def train_arrival(service: TrainService, filter_crs: str | None, scheduled_at: datetime,
                  expected_at: datetime | None) -> dict | None:
    """Where and when the train arrives, and the minutes from leaving here,
    or None if that isn't known. `scheduled_at` and `expected_at` are this
    station's departure (aware); expected_at is None if not known."""
    dep = service.departure
    if dep.status is Status.CANCELLED or (dep.status is Status.LATE and expected_at is None):
        return None
    point = _target(service.calling_points, filter_crs)
    arrives = _point_time(point) if point is not None else None
    if arrives is None:
        return None
    leaves = (expected_at or scheduled_at).replace(second=0, microsecond=0)
    minutes = (_minute_of_day(arrives) - (leaves.hour * 60 + leaves.minute)) % _DAY
    if not 0 < minutes <= _LONGEST:
        return None  # arrives before it leaves: Darwin's times disagree
    at = (leaves.replace(tzinfo=None) + timedelta(minutes=minutes)).replace(tzinfo=leaves.tzinfo)
    return _arrival(point.name, at, minutes)


# Buses


def bus_arrival(live: LiveDeparture, tz: tzinfo) -> dict | None:
    """When the bus reaches its journey's last call: the timetabled time plus
    the delay it has now. None if your stop is its last call."""
    calls = live.journey.calls
    if live.call_index >= len(calls) - 1:
        return None
    minutes = calls[-1].minute - calls[live.call_index].minute
    if not 0 < minutes <= _LONGEST:
        return None
    at = live.expected + timedelta(minutes=minutes)
    return _arrival(live.journey.destination, at.replace(tzinfo=tz), minutes)


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _ends_at(journey_end: str, stop: str, positions: dict[str, StopPosition]) -> bool:
    """Whether a journey ending at `journey_end` ends at `stop`, or within NEAR_M of it."""
    if journey_end == stop:
        return True
    a, b = positions.get(journey_end), positions.get(stop)
    return a is not None and b is not None and metres(a, b) <= NEAR_M


def _arrives(placed: LiveJourney) -> datetime:
    """When the journey reaches its last call, by the timetable (board-local, no zone)."""
    return datetime.combine(placed.service_day, time()) + timedelta(minutes=placed.journey.calls[-1].minute)


def _late_minutes(delay: float) -> int:
    """Whole minutes late, rounding halves up; early counts as on time (as livebus does)."""
    return math.floor(max(delay, 0.0) / 60 + 0.5)


def _clean(text: str) -> str | None:
    """A place name from a feed as plain text: BODS's underscores as spaces,
    on one line, printable, cut short."""
    words = " ".join(text.replace("_", " ").split())
    words = "".join(c for c in words if c.isprintable())
    return words[:_NAME_LIMIT].rstrip() or None


def _origin(placed: LiveJourney, positions: dict[str, StopPosition]) -> str | None:
    """Where the inbound journey started: the timetable's stop name if it has
    one, else the name the bus's tracker sends."""
    first = positions.get(placed.journey.calls[0].atco)
    if first is not None:
        return first.name
    return _clean(placed.vehicle.origin_name)


def _where(placed: LiveJourney, stop: str, positions: dict[str, StopPosition]) -> str:
    """Where the inbound bus is, as the route strip says it: "4 stops away"."""
    calls = placed.journey.calls
    away = (len(calls) - 1) - math.floor(placed.progress.at + 0.02)
    if away == 1:
        return "1 stop away"
    if away > 1:
        return f"{away} stops away"
    end = calls[-1].atco
    if end == stop or end not in positions:
        return "At your stop"
    return f"At {positions[end].name}"


def _taken_earlier(arrives: datetime, live: LiveDeparture, rows: list[LiveDeparture], stop: str) -> bool:
    """Whether a departure of the same line from the same stop leaves between
    the inbound bus arriving and `live`: that one is the bus's next working."""
    return any(
        other is not live
        and other.call_index == 0
        and other.journey.calls[0].atco == stop
        and _same(other.journey.line, live.journey.line)
        and arrives <= other.leaves < live.leaves
        for other in rows
    )


def inbound(live: LiveDeparture, rows: list[LiveDeparture], placed: list[LiveJourney],
            positions: dict[str, StopPosition]) -> dict | None:
    """The bus probably coming to work a departure that starts at your stop,
    or None (see the module's notes). `rows` are the board's departures and
    `placed` every journey with a live bus on it."""
    if live.call_index != 0:
        return None
    stop = live.journey.calls[0].atco
    found = [
        p for p in placed
        if not (p.journey is live.journey and p.service_day == live.service_day)
        and _same(p.journey.line, live.journey.line)
        and _ends_at(p.journey.calls[-1].atco, stop, positions)
        and timedelta(0) <= live.leaves - _arrives(p) <= INBOUND_WINDOW
    ]
    if len(found) != 1:
        return None  # none, or more than one: a guess too far
    bus = found[0]
    arrives = _arrives(bus)
    if _taken_earlier(arrives, live, rows, stop):
        return None
    late = _late_minutes(bus.progress.delay)
    origin = _origin(bus, positions)
    due = (arrives + timedelta(minutes=late)).strftime("%H:%M")
    return {
        "from": origin,
        "due": due,
        "late_min": late,
        "where": _where(bus, stop, positions),
        "text": inbound_text(origin, late, due),
    }


def inbound_text(origin: str | None, late: int, due: str) -> str:
    """The inbound bus in words for the row: "Bus arriving from Ashton, 4 min
    late (due 23:29)". Its own lateness and time, never a time for the departure."""
    coming = f"Bus arriving from {origin}" if origin else "Bus arriving"
    return f"{coming}, {f'{late} min late' if late else 'on time'} (due {due})"


def is_ghost(live: LiveDeparture, now: datetime, vehicles_ok: bool, coming: dict | None) -> bool:
    """A bus due within GHOST_WITHIN (or just gone its time) with no live bus
    placed on it, while live positions are working and no inbound bus is on
    its way. `now` is aware, in the board's zone."""
    if live.progress is not None or not vehicles_ok or coming is not None:
        return False
    return live.expected - now.replace(tzinfo=None) <= GHOST_WITHIN


def bus_extras(live: LiveDeparture, board: BusBoard, now: datetime) -> dict:
    """The extra fields of a bus row: arrives, inbound and ghost."""
    coming = inbound(live, board.departures, board.journeys, board.schedule.positions)
    return {
        "arrives": bus_arrival(live, now.tzinfo),
        "inbound": coming,
        "ghost": is_ghost(live, now, board.vehicles_ok, coming),
    }
