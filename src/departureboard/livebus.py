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
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, Iterable

from .bods import Vehicle
from .model import Departure, Status
from .route import RouteStop, RouteStrip, position_on
from .schedule_model import Call, Journey, Schedule, StopPosition

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
    # SIRI-VM times carry an offset; a bare one is read as UTC, as bods.py does.
    return recorded if recorded.tzinfo is not None else recorded.replace(tzinfo=timezone.utc)


def _runs(calls: tuple[Call, ...], positions: dict[str, StopPosition]) -> list[range]:
    """Stretches of two or more consecutive calls whose stops have known positions."""
    runs, start = [], None
    for i, call in enumerate(calls + (None,)):
        if call is not None and call.atco in positions:
            start = i if start is None else start
            continue
        if start is not None and i - start >= 2:
            runs.append(range(start, i))
        start = None
    return runs


def _minute_at(calls: tuple[Call, ...], at: float) -> float:
    """Timetabled minute at a point along the calls, assuming steady progress between stops."""
    i = min(int(at), len(calls) - 1)
    if i == len(calls) - 1:
        return float(calls[i].minute)
    return calls[i].minute + (at - i) * (calls[i + 1].minute - calls[i].minute)


def progress(
    vehicle: Vehicle,
    journey: Journey,
    positions: dict[str, StopPosition],
    service_day: date,
    tz: tzinfo,
) -> Progress | None:
    """Place a bus on its journey and say how late it is there.

    Only the stretches of the journey whose stops have positions can place it,
    using route.position_on (within 150 m of the road, heading the right way).
    None if it is not on any of them, or its report time can't be read. If it
    fits more than one stretch, as on a loop, the one nearest the timetable wins.
    """
    recorded = _recorded(vehicle)
    if recorded is None:
        return None
    seen = recorded.astimezone(tz).replace(tzinfo=None)
    midnight = datetime.combine(service_day, time())
    best = None
    for run in _runs(journey.calls, positions):
        places = [positions[call.atco] for call in journey.calls[run.start : run.stop]]
        strip = RouteStrip(journey.line, "", tuple(RouteStop(p.name, p.lat, p.lon) for p in places))
        along = position_on(strip, vehicle)
        if along is None:
            continue
        at = run.start + along
        due = midnight + timedelta(minutes=_minute_at(journey.calls, at))
        delay = (seen - due).total_seconds()
        if at <= 0 and delay < 0:
            delay = 0.0  # waiting at the first stop: it won't leave early
        if best is None or abs(delay) < abs(best.delay):
            best = Progress(at, delay)
    return best


def _heading_to(destination: str, directions: Iterable[str] | None) -> bool:
    if not directions:
        return True
    return any(d.lower() in destination.lower() for d in directions)


def _late_minutes(delay: float) -> int:
    """Whole minutes late, rounding halves up; early counts as on time."""
    return math.floor(max(delay, 0.0) / 60 + 0.5)


def _live(
    vehicles: Iterable[Vehicle],
    running: dict[date, list[Journey]],
    positions: dict[str, StopPosition],
    tz: tzinfo,
) -> dict[tuple[date, int], tuple[Progress, Vehicle]]:
    """Where the bus is on each journey a vehicle could be matched to and placed
    on, and the bus itself, keyed by (service day, id of the journey)."""
    live: dict[tuple[date, int], tuple[datetime, Progress, Vehicle]] = {}
    for vehicle in vehicles:
        hits = [(day, j) for day, journeys in running.items() if (j := match(vehicle, journeys, day, tz))]
        if len(hits) != 1:
            continue  # unmatched, or could be yesterday's journey or today's
        day, journey = hits[0]
        placed = progress(vehicle, journey, positions, day, tz)
        if placed is None:
            continue
        key = (day, id(journey))
        recorded = _recorded(vehicle)  # readable, or progress would have been None
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


def live_departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[LiveDeparture]:
    """The next `rows` buses from your stops, soonest first, each with its
    journey and the bus running it.

    `now` must be timezone-aware in the board's zone, e.g.
    datetime.now(ZoneInfo("Europe/London")); its zone is used to read BODS times.
    Journeys come from today's timetable and yesterday's (for buses still out
    after midnight), as runs_on says. A bus placed before your stop is shown
    with its delay and stays on the board however late; one that has passed
    your stop is dropped. Without a placed bus the timetabled time is shown as
    UNKNOWN until a minute after it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo
    local_now = now.replace(tzinfo=None)
    today = local_now.date()
    days = (today - timedelta(days=1), today)
    running = {day: [j for j in schedule.journeys if j.calls and runs_on(j, day)] for day in days}
    live = _live(vehicles, running, schedule.positions, tz)
    labels: dict[str, list[str]] = {}
    for label, atco in schedule.stops.items():
        labels.setdefault(atco, []).append(label)

    found = []
    for day, journeys in running.items():
        midnight = datetime.combine(day, time())
        for journey in journeys:
            if not _heading_to(journey.destination, directions):
                continue
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
    return found[:rows]


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
    picked = earlier[max(len(earlier) - before, 0) :] + [yours]
    if yours + 1 < len(calls) and calls[yours + 1].atco in positions:
        picked.append(yours + 1)
    stops = tuple(positions[calls[i].atco] for i in picked)
    bus_at = _along(picked, live.progress.at) if live.progress is not None else None
    return Approach(stops, picked.index(yours), bus_at)


def _along(picked: list[int], at: float) -> float | None:
    """A point along a journey's calls as stops along the picked calls."""
    if at < picked[0]:
        return None
    for n, (a, b) in enumerate(zip(picked, picked[1:])):
        if at <= b:
            return n + (at - a) / (b - a)
    return len(picked) - 1 + (at - picked[-1])
