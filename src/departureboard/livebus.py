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

    Matched on operator, line, first stop and timetabled time from it. Without
    those, on the ticket machine's journey code. None when nothing or more than
    one journey fits, as a wrong match is worse than none.
    """
    candidates = [
        j for j in journeys if j.calls and _same(j.operator, vehicle.operator) and _same(j.line, vehicle.line)
    ]
    if vehicle.origin_ref and vehicle.origin_aimed is not None:
        minute = service_minute(vehicle.origin_aimed, service_day, tz)
        found = [j for j in candidates if j.origin.atco == vehicle.origin_ref and j.origin.minute == minute]
    elif vehicle.journey_ref:
        found = [j for j in candidates if j.journey_code == vehicle.journey_ref]
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
) -> dict[tuple[date, int], Progress]:
    """Where the bus is on each journey a vehicle could be matched to and placed
    on, keyed by (service day, id of the journey)."""
    live: dict[tuple[date, int], tuple[datetime, Progress]] = {}
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
        live[key] = (recorded, placed)
    return {key: placed for key, (_, placed) in live.items()}


def departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[Departure]:
    """The next `rows` buses from your stops, soonest first.

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
            placed = live.get((day, id(journey)))
            for k, call in enumerate(journey.calls[:-1]):  # the last call is where it terminates
                leaves = midnight + timedelta(minutes=call.minute)
                if placed is not None and placed.at > k + PASSED_MARGIN:
                    continue  # the bus has been past this stop
                if placed is None and leaves < local_now - GRACE:
                    continue
                if leaves > local_now + HORIZON:
                    continue
                for label in labels.get(call.atco, ()):
                    found.append(_departure(journey, leaves, placed, label))
    found.sort(key=lambda pair: pair[0])
    return [departure for _, departure in found[:rows]]


def _departure(
    journey: Journey, leaves: datetime, placed: Progress | None, label: str
) -> tuple[tuple, Departure]:
    """A board row, with the key that orders it: when it is expected to leave."""
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
    return (expected, leaves, label, journey.line), departure
