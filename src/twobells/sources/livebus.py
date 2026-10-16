"""Live bus departures: the timetable for your stops, adjusted by where the buses are.

Works the way bustimes.org does. Each live BODS vehicle is matched to the
timetabled journey it says it is running, placed on the road between that
journey's stops, and its delay is when it was there against when the timetable
has it there. A bus that is late at that point is shown that late at your stop;
one that is early is shown on time, since buses wait at timing points.

Everything here is pure: the caller passes the time, the timetable and the
vehicles, so nothing reads the clock or the network.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise

from ..board.route import MAX_HEADING_OFF, MAX_OFF_ROUTE_M, MAX_OVERSHOOT_M, where
from ..core.model import Departure, Status, Strip
from ..timetable.model import Call, Journey, Schedule, StopPosition
from ..timetable.roads import shape_key
from .bods import Vehicle

RunsOn = Callable[[Journey, date], bool]

# A timetabled departure stays this long after its time, in case the bus is just late.
GRACE = timedelta(minutes=1)
# Times show without a date, and model.minutes_between reads them as within 12 hours.
HORIZON = timedelta(hours=12)
# A bus this far (in stops) past your stop has left; nearer, it may still be at the pole.
PASSED_MARGIN = 0.1
_DAY = 24 * 60


def service_minute(when: datetime, service_day: date, tz: tzinfo) -> int:
    """Minutes after midnight of service_day, by the local clock, of an aware time."""
    local = when.astimezone(tz)
    return (local.date() - service_day).days * _DAY + local.hour * 60 + local.minute


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def match(vehicle: Vehicle, journeys: Iterable[Journey], service_day: date, tz: tzinfo) -> Journey | None:
    """The journey a vehicle is running, out of those running on service_day.

    Matched on line, first stop and timetabled time from it, with the operator
    only breaking ties: Bee Network timetables and live feeds can give one
    operator different codes. Without those, on the ticket machine's journey
    code and operator. None when nothing or more than one journey fits, as a
    wrong match is worse than none.
    """
    candidates = [j for j in journeys if j.calls and _same(j.line, vehicle.line)]
    if vehicle.origin_ref and vehicle.origin_aimed is not None:
        minute = service_minute(vehicle.origin_aimed, service_day, tz)
        found = [j for j in candidates if j.origin.atco == vehicle.origin_ref and j.origin.minute == minute]
        if len(found) > 1:
            found = [j for j in found if _same(j.operator, vehicle.operator)]
    elif vehicle.journey_ref:
        found = [
            j for j in candidates if _same(j.operator, vehicle.operator) and j.journey_code == vehicle.journey_ref
        ]
    else:
        return None
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Progress:
    """Where a matched bus is on its journey and how it compares to the timetable."""

    at: float  # along the journey's calls: 2.5 is halfway from the third call to the fourth
    delay: float  # seconds behind the timetable at that point; negative is early


def _recorded(vehicle: Vehicle) -> datetime | None:
    try:
        recorded = datetime.fromisoformat(vehicle.recorded_at)
    except ValueError:
        return None
    # SIRI-VM times carry an offset; a bare one is read as UTC, as sources/bods.py does.
    return recorded if recorded.tzinfo is not None else recorded.replace(tzinfo=UTC)


def _minute_at(calls: tuple[Call, ...], at: float) -> float:
    """Timetabled minute at a point along the calls, assuming steady progress between stops."""
    i = min(int(at), len(calls) - 1)
    if i == len(calls) - 1:
        return float(calls[i].minute)
    return calls[i].minute + (at - i) * (calls[i + 1].minute - calls[i].minute)


# Placing a bus. Each pair of consecutive calls whose stops have positions is a
# leg: the road the timetable saved between the two stops (timetable/roads.py),
# or a straight line where it saved none. A bus is on a leg when it is near
# enough to the road and heading along it, as route.position_on has it for a
# strip. Of the legs it is on, the nearest wins; where others are about as near
# (a road the journey takes twice, or both ways), the one nearest the
# timetable does. Nearest first, so a bus on a parallel road, or late by just
# the time to a road further on, isn't placed there.

# A saved road is the road itself, to within a few metres, so a bus this far
# from it is somewhere else; the rest allows for a poor GPS fix, as on
# bustimes.org. A straight line is close only to a straight road, so it keeps
# route.MAX_OFF_ROUTE_M: a bus on a bending road with no saved road is left
# unplaced rather than guessed at.
ROAD_TOLERANCE_M = 300
# A leg this much further from the bus than the nearest is as likely as it.
TIE_M = 50
_M_PER_DEG = 111_320
_NO_ROADS: Mapping[str, Sequence[tuple[float, float]]] = {}


def _on_road(
    vehicle: Vehicle, road: list[tuple[float, float]], tolerance: float, first: bool, last: bool
) -> tuple[float, float] | None:
    """How far a bus is from a road ((lat, lon) points in order), in metres,
    and how far along the road the nearest point is, from 0 to 1 by distance.
    None if it is further than `tolerance`, or heading more than
    MAX_HEADING_OFF from the road's direction there. Before the start of the
    `first` road of a stretch, or past the end of the `last`, it has not got
    there yet or has gone, so only MAX_OVERSHOOT_M counts there."""
    # A flat grid in metres with the bus at (0, 0): fine over a few kilometres.
    across = _M_PER_DEG * math.cos(math.radians(vehicle.lat))
    grid = [((lon - vehicle.lon) * across, (lat - vehicle.lat) * _M_PER_DEG) for lat, lon in road]
    # The same point twice has no direction to check a heading against.
    pieces = [(a, b) for a, b in pairwise(grid) if a != b]
    final = len(pieces) - 1
    best = None  # (metres off, metres along, the road's bearing there)
    length = 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(pieces):
        dx, dy = bx - ax, by - ay
        piece = math.hypot(dx, dy)
        raw = -(ax * dx + ay * dy) / (piece * piece)
        t = max(0.0, min(1.0, raw))
        off = math.hypot(ax + t * dx, ay + t * dy)
        overshoot = (first and i == 0 and raw < 0) or (last and i == final and raw > 1)
        if off <= (MAX_OVERSHOOT_M if overshoot else tolerance) and (best is None or off < best[0]):
            best = (off, length + t * piece, math.degrees(math.atan2(dx, dy)) % 360)
        length += piece
    if best is None:
        return None
    off, along, bearing = best
    if vehicle.bearing is not None and abs((vehicle.bearing - bearing + 180) % 360 - 180) > MAX_HEADING_OFF:
        return None
    return off, along / length


def _fits(
    vehicle: Vehicle,
    calls: tuple[Call, ...],
    positions: Mapping[str, StopPosition],
    shapes: Mapping[str, Sequence[tuple[float, float]]],
) -> list[tuple[float, float]]:
    """Each leg of the calls the bus is on: how far it is from the road, in
    metres, and where it is along the calls (2.5 is halfway along the third leg)."""
    fits = []
    for k, (a, b) in enumerate(pairwise(calls)):
        if a.atco not in positions or b.atco not in positions:
            continue
        saved = shapes.get(shape_key((a.atco, b.atco)), ())
        start, end = positions[a.atco], positions[b.atco]
        road = [(start.lat, start.lon), *saved, (end.lat, end.lon)]
        first = k == 0 or calls[k - 1].atco not in positions
        last = k + 2 == len(calls) or calls[k + 2].atco not in positions
        found = _on_road(vehicle, road, ROAD_TOLERANCE_M if saved else MAX_OFF_ROUTE_M, first, last)
        if found is not None:
            fits.append((found[0], k + found[1]))
    return fits


def progress(
    vehicle: Vehicle,
    journey: Journey,
    positions: Mapping[str, StopPosition],
    service_day: date,
    tz: tzinfo,
    shapes: Mapping[str, Sequence[tuple[float, float]]] = _NO_ROADS,
) -> Progress | None:
    """Place a bus on its journey and say how late it is there.

    Only the legs of the journey between stops with positions can place it,
    along `shapes`, the timetable's saved roads (see above). None if it is on
    none of them, or its report time can't be read.
    """
    recorded = _recorded(vehicle)
    if recorded is None:
        return None
    fits = _fits(vehicle, journey.calls, positions, shapes)
    if not fits:
        return None
    seen = recorded.astimezone(tz).replace(tzinfo=None)
    midnight = datetime.combine(service_day, time())
    nearest = min(off for off, _ in fits)
    best = None
    for off, at in fits:
        if off > nearest + TIE_M:
            continue
        due = midnight + timedelta(minutes=_minute_at(journey.calls, at))
        delay = (seen - due).total_seconds()
        if at <= 0 and delay < 0:
            delay = 0.0  # waiting at the first stop: it won't leave early
        if best is None or abs(delay) < abs(best.delay):
            best = Progress(at, delay)
    return best


def heading_to(destination: str, directions: Iterable[str] | None) -> bool:
    """Whether a bus to `destination` goes one of the board's `directions`
    (any, without them): the rows and the last buses keep only those."""
    if not directions:
        return True
    return any(d.lower() in destination.lower() for d in directions)


def _late_minutes(delay: float) -> int:
    """Whole minutes late, rounding halves up; early counts as on time."""
    return math.floor(max(delay, 0.0) / 60 + 0.5)


def _live(
    vehicles: Iterable[Vehicle],
    running: dict[date, list[Journey]],
    positions: Mapping[str, StopPosition],
    tz: tzinfo,
    shapes: Mapping[str, Sequence[tuple[float, float]]] = _NO_ROADS,
) -> dict[tuple[date, int], tuple[Progress, Vehicle]]:
    """Where the bus is on each journey a vehicle could be matched to and placed
    on (along `shapes`, the saved roads), and the bus itself, keyed by
    (service day, id of the journey)."""
    live: dict[tuple[date, int], tuple[datetime, Progress, Vehicle]] = {}
    for vehicle in vehicles:
        hits = [(day, j) for day, journeys in running.items() if (j := match(vehicle, journeys, day, tz))]
        if len(hits) != 1:
            continue  # unmatched, or could be yesterday's journey or today's
        day, journey = hits[0]
        placed = progress(vehicle, journey, positions, day, tz, shapes)
        recorded = _recorded(vehicle)  # readable whenever progress places the bus
        if placed is None or recorded is None:
            continue
        key = (day, id(journey))
        if key in live and live[key][0] >= recorded:
            continue  # two buses on one journey: keep the latest report
        live[key] = (recorded, placed, vehicle)
    return {key: (placed, vehicle) for key, (_, placed, vehicle) in live.items()}


@dataclass(frozen=True)
class LiveDeparture:
    """A board row with what it was worked out from: the journey, the stop on
    it, and the bus running it if one was matched and placed."""

    departure: Departure
    journey: Journey
    service_day: date
    call_index: int  # your stop's place in journey.calls
    leaves: datetime  # timetabled, board-local without a zone
    expected: datetime  # leaves plus any lateness, board-local without a zone
    progress: Progress | None  # None when no bus was placed on the journey
    vehicle: Vehicle | None  # the bus behind progress

    @property
    def key(self) -> str:
        """Names this departure the same way every fetch, even after the
        timetable is read again: service day, operator, line, first stop and
        time, your stop, its time (a loop can call there twice) and its label."""
        origin = self.journey.origin
        call = self.journey.calls[self.call_index]
        return "|".join(
            (
                self.service_day.isoformat(),
                self.journey.operator,
                self.journey.line,
                origin.atco,
                str(origin.minute),
                call.atco,
                str(call.minute),
                self.departure.stop or "",
            )
        )


def departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[Departure]:
    """The next `rows` buses from your stops, soonest first. See live_departures."""
    return [live.departure for live in live_departures(schedule, now, vehicles, runs_on, rows, directions)]


@dataclass(frozen=True)
class LiveJourney:
    """A timetabled journey with a live bus placed on it, whatever stop it
    ends at. Journeys that end at your stops (or inbound-only ones, at a
    stand near them) never give a departure row, but their buses often go on
    to work the next journey from there (see enrich.inbound)."""

    journey: Journey
    service_day: date
    progress: Progress
    vehicle: Vehicle


def live_departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[LiveDeparture]:
    """The next `rows` buses from your stops, soonest first. See live_board."""
    return live_board(schedule, now, vehicles, runs_on, rows, directions)[0]


def live_board(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> tuple[list[LiveDeparture], list[LiveJourney]]:
    """The next `rows` buses from your stops, soonest first, each with its
    journey and the bus running it; and every journey (today's or
    yesterday's) a live bus was placed on, including ones that end at your
    stops and inbound-only ones, which give no departure row. `directions`
    filters only the rows.

    `now` must be timezone-aware in the board's zone, e.g.
    datetime.now(ZoneInfo("Europe/London")); its zone is used to read BODS times.
    Journeys come from today's timetable, yesterday's (for buses still out
    after midnight) and tomorrow's (for its first buses, before midnight), as
    runs_on says; tomorrow's are timetabled only. A bus placed before your stop is shown
    with its delay and stays on the board however late; one that has passed
    your stop is dropped. Without a placed bus the timetabled time is shown as
    UNKNOWN until a minute after it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo
    local_now = now.replace(tzinfo=None)
    today = local_now.date()
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    running = {day: [j for j in schedule.journeys if j.calls and runs_on(j, day)] for day in days}
    # Buses are matched to yesterday's and today's journeys only: none is out on
    # tomorrow's yet, and a journey code that fits tomorrow too would make
    # today's match ambiguous.
    live = _live(vehicles, {day: running[day] for day in days[:2]}, schedule.positions, tz, schedule.shapes)
    labels: dict[str, list[str]] = {}
    for label, atco in schedule.stops.items():
        labels.setdefault(atco, []).append(label)

    found = []
    for day, journeys in running.items():
        midnight = datetime.combine(day, time())
        for journey in journeys:
            if journey.inbound_only or not heading_to(journey.destination, directions):
                continue  # inbound-only journeys are there for enrich.inbound, never as rows
            placed, vehicle = live.get((day, id(journey)), (None, None))
            for k, call in enumerate(journey.calls[:-1]):  # the last call is where it terminates
                leaves = midnight + timedelta(minutes=call.minute)
                if placed is not None and placed.at > k + PASSED_MARGIN:
                    continue  # the bus has been past this stop
                if placed is None and leaves < local_now - GRACE:
                    continue
                if leaves > local_now + HORIZON:
                    continue
                for label in labels.get(call.atco, ()):
                    found.append(_departure(journey, day, k, leaves, placed, vehicle, label))
    found.sort(key=lambda live: (live.expected, live.leaves, live.departure.stop, live.journey.line))
    journeys = {id(j): j for js in running.values() for j in js}
    placed = [
        LiveJourney(journeys[journey_id], day, progress, vehicle)
        for (day, journey_id), (progress, vehicle) in live.items()
    ]
    return found[:rows], placed


def _departure(
    journey: Journey,
    day: date,
    k: int,
    leaves: datetime,
    placed: Progress | None,
    vehicle: Vehicle | None,
    label: str,
) -> LiveDeparture:
    """A board row for the k-th call of a journey, and what it came from."""
    late = _late_minutes(placed.delay) if placed is not None else 0
    expected = leaves + timedelta(minutes=late)
    if placed is None:
        status = Status.UNKNOWN
    else:
        status = Status.LATE if late > 0 else Status.ON_TIME
    departure = Departure(
        mode="bus",
        service=journey.line,
        destination=journey.destination,
        scheduled=leaves.strftime("%H:%M"),
        expected=expected.strftime("%H:%M") if status is Status.LATE else None,
        status=status,
        stop=label,
    )
    return LiveDeparture(departure, journey, day, k, leaves, expected, placed, vehicle)


@dataclass(frozen=True)
class Approach:
    """The road up to your stop for one departure, for a small route strip."""

    stops: tuple[StopPosition, ...]  # in calling order
    yours: int  # your stop's index in stops
    bus_at: float | None  # stops along from the first; None if not on the strip yet


def approach(live: LiveDeparture, positions: dict[str, StopPosition], before: int = 6) -> Approach | None:
    """Your stop, the positioned stops the bus calls at before it (at most
    `before` of them) and the next stop after it if positioned, with where the
    bus is along them.

    Calls without a position are left out, so the strip joins the stops either
    side of them; a bus among them is placed in proportion to the calls it has
    passed. bus_at is None without a placed bus or when the bus has not reached
    the first stop. Past the last stop it counts on in calls, so more than
    len(stops) - 1 means it has gone beyond the strip. None if your stop has no
    position.
    """
    calls = live.journey.calls
    yours = live.call_index
    if not 0 <= yours < len(calls) or calls[yours].atco not in positions:
        return None
    earlier = [i for i in range(yours) if calls[i].atco in positions]
    picked = [*earlier[max(len(earlier) - before, 0):], yours]
    if yours + 1 < len(calls) and calls[yours + 1].atco in positions:
        picked.append(yours + 1)
    stops = tuple(positions[calls[i].atco] for i in picked)
    bus_at = _along(picked, live.progress.at) if live.progress is not None else None
    return Approach(stops, picked.index(yours), bus_at)


def _along(picked: list[int], at: float) -> float | None:
    """A point along a journey's calls as stops along the picked calls."""
    if at < picked[0]:
        return None
    for n, (a, b) in enumerate(pairwise(picked)):
        if at <= b:
            return n + (at - a) / (b - a)
    return len(picked) - 1 + (at - picked[-1])


def where_words(live: LiveDeparture, found: Approach | None) -> str:
    """Where the bus is, in words for a row and screen readers, as the route
    strip says it ("3 stops away", "At your stop"). `found` is its approach."""
    if live.progress is None:
        return "No live position"
    if found is None:
        return "Live position known"
    names = tuple(stop.name for stop in found.stops)
    return where(names[: found.yours + 1], found.bus_at, False)


def row_strip(live: LiveDeparture, positions: dict[str, StopPosition]) -> Strip | None:
    """Where the bus is, for a screen's row: the dashboard row's words and
    strip. None without a placed bus or your stop's position, and for anything
    a row can't draw (Strip refuses it): never a guess."""
    found = approach(live, positions)
    if live.progress is None or found is None:
        return None
    last = len(found.stops) - 1
    at = found.bus_at
    if at is not None:
        if at > last + PASSED_MARGIN:
            return None  # past your stop and off the strip: its row has gone
        at = round(min(at, last), 3)  # at your stop, perhaps just past the pole
    words = where_words(live, found)
    if at is None:
        # Not on the strip yet: "Not yet past The Junction" is too long for a
        # row (18 characters), so count the calls to your stop instead.
        away = live.call_index - math.floor(live.progress.at + 0.02)
        words = "1 stop away" if away == 1 else f"{away} stops away"
    try:
        return Strip(words, len(found.stops), found.yours, at)
    except ValueError:
        return None
