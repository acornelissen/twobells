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

Inbound buses. For a departure that starts at your stop, the bus coming in
to work it (livebus.Inbound, found there: followed by the timetable's
block, or else guessed by line) is shown as a line of its own ("Bus
arriving from Ashton, 4 min late (due 23:29)"), with the facts from its
tracker: where it is and how late. Either way the departure is live, and
livebus carries the bus's lateness on to the departure's time; `followed`
tells the API which it was. The same journey's later stops of yours have
the same bus, named arriving where the journey starts ("Bus arriving at
Henry Street from Ashton, ...").

Ghost buses. A bus due within GHOST_WITHIN with no live bus placed on its
journey is flagged, since trackers usually show up well before then. Not
when live positions failed (every row would be flagged), and not when an
inbound bus with a tracker is on its way to work it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from ..core.model import Status, clock_at, iso, minutes_apart, real, zoned
from ..sources.bodsbus import BusBoard
from ..sources.darwin import CallingPoint, TrainService
from ..sources.livebus import LiveDeparture, LiveJourney
from ..timetable.model import StopPosition

# A bus due this soon with no tracker gets the warning.
GHOST_WITHIN = timedelta(minutes=5)
# A journey longer than this is a mistake in the times, not a long journey.
_LONGEST = 12 * 60
_NAME_LIMIT = 80
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _arrival(name: str, at: datetime, minutes: int) -> dict:
    """The contract's arrival: `at` is aware, in the board's zone."""
    return {"name": name, "at": at.strftime("%H:%M"), "at_iso": iso(at), "minutes": minutes}


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


def train_arrival(service: TrainService, filter_crs: str | None, scheduled_at: datetime,
                  expected_at: datetime | None) -> dict | None:
    """Where and when the train arrives, and the minutes from leaving here,
    or None if that isn't known. `scheduled_at` and `expected_at` are this
    station's departure (aware); expected_at is None if not known."""
    dep = service.departure
    if dep.status is Status.CANCELLED or (dep.status is Status.LATE and expected_at is None):
        return None
    point = _target(service.calling_points, filter_crs)
    if point is None:
        return None
    arrives = _point_time(point)
    if arrives is None:
        return None
    leaves = (expected_at or scheduled_at).replace(second=0, microsecond=0)
    # Within 12 hours of leaving, in the minutes that pass, across the clocks changing too.
    at = clock_at(arrives, leaves)
    minutes = minutes_apart(leaves, at)
    if not 0 < minutes <= _LONGEST:
        return None  # arrives before it leaves: Darwin's times disagree
    return _arrival(point.name, at, minutes)


# Buses


def bus_arrival(live: LiveDeparture, now: datetime) -> dict | None:
    """When the bus reaches its journey's last call: the timetable's time
    from your stop after it leaves, with the delay it has now, in the time
    that passes. `now` is when the board was worked out, in the board's zone.
    None if your stop is its last call."""
    calls = live.journey.calls
    if live.call_index >= len(calls) - 1:
        return None
    minutes = calls[-1].minute - calls[live.call_index].minute
    if not 0 < minutes <= _LONGEST:
        return None
    if now.tzinfo is None:
        return _arrival(live.journey.destination, live.expected + timedelta(minutes=minutes), minutes)
    leaves = zoned(live.expected, now.tzinfo, now)
    at = (real(leaves) + timedelta(minutes=minutes)).astimezone(now.tzinfo)
    return _arrival(live.journey.destination, at, minutes)


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


def _where(placed: LiveJourney, live: LiveDeparture, positions: dict[str, StopPosition]) -> str:
    """Where the inbound bus is, as the route strip says it: "4 stops away"
    from the end of its journey, then "At your stop", or, for a later stop
    of the departure's journey, at the stop it is in at ("At Henry Street")."""
    calls = placed.journey.calls
    away = (len(calls) - 1) - math.floor(placed.progress.at + 0.02)
    if away == 1:
        return "1 stop away"
    if away > 1:
        return f"{away} stops away"
    end = calls[-1].atco
    if live.call_index == 0 and (end == live.journey.origin.atco or end not in positions):
        return "At your stop"
    for atco in (end, live.journey.origin.atco):
        if atco in positions:
            return f"At {positions[atco].name}"
    return "Waiting to start"


def inbound(live: LiveDeparture, positions: dict[str, StopPosition]) -> dict | None:
    """The bus coming in to work a departure that starts at your stop, or a
    later stop of yours on the same journey, as livebus found it, or None
    (see the module's notes). `followed` says whether it was followed by
    the timetable's block rather than guessed by line; either makes the
    departure live."""
    coming = live.inbound
    if coming is None:
        return None
    origin = _origin(coming.bus, positions)
    due = coming.arrives.strftime("%H:%M")
    start = positions.get(live.journey.origin.atco) if live.call_index > 0 else None
    return {
        "from": origin,
        "due": due,
        "late_min": coming.late,
        "where": _where(coming.bus, live, positions),
        "text": inbound_text(origin, coming.late, due, start.name if start is not None else None),
        "followed": coming.followed,
    }


def inbound_text(origin: str | None, late: int, due: str, start: str | None = None) -> str:
    """The inbound bus in words for the row: "Bus arriving from Ashton, 4 min
    late (due 23:29)". Its own lateness and time, never a time for the
    departure. `start` names the stop it arrives at, the journey's first, for
    a row at a later stop: "Bus arriving at Henry Street from Ashton, ..."."""
    coming = "Bus arriving" + (f" at {start}" if start else "") + (f" from {origin}" if origin else "")
    return f"{coming}, {f'{late} min late' if late else 'on time'} (due {due})"


def is_ghost(live: LiveDeparture, now: datetime, vehicles_ok: bool, coming: dict | None) -> bool:
    """A bus due within GHOST_WITHIN (or just gone its time) with no live bus
    placed on it, while live positions are working and no inbound bus is on
    its way. `now` is aware, in the board's zone."""
    if live.live or not vehicles_ok or coming is not None:
        return False
    return live.expected - now.replace(tzinfo=None) <= GHOST_WITHIN


def bus_extras(live: LiveDeparture, board: BusBoard, now: datetime) -> dict:
    """The extra fields of a bus row: arrives, inbound and ghost."""
    coming = inbound(live, board.schedule.positions)
    return {
        "arrives": bus_arrival(live, now),
        "inbound": coming,
        "ghost": is_ghost(live, now, board.vehicles_ok, coming),
    }
