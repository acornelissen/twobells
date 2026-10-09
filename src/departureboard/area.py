"""The area around your stops: every live bus on your lines near them, for
the area map (GET /api/area), and the bus timetable changes coming up.

Both are worked out from what the bus poller already has (its last BODS
fetch and the timetable), so they cost no extra calls.

Timetable changes. The timetable keeps journeys whose calendar starts in
the future (it drops only those that have ended), and runs_on never runs
one before its start. A line has a change on day D, within the next 28
days, when D is a day a journey of any line starts or the day after one
ends, and the line's journeys that run on D (by their stops and times)
differ from those that ran on the same weekday a week before. Each line
is named once, at its first change. Bank holidays are not counted.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Iterable

from .bods import Vehicle
from .bodsbus import BusBoard
from .disruptions import BusArea, plain_text
from .livebus import LiveJourney
from .schedule_model import Journey, Schedule
from .snapshot import iso
from .timetable import runs_on

CHANGE_DAYS = 28
MAX_CHANGES = 20
MAX_VEHICLES = 80
MAX_ROUTES = 40
_NAME_LIMIT = 80


def _line_order(line: str) -> tuple:
    digits = "".join(c for c in line if c.isdigit())
    return (int(digits) if digits else 10**6, line)


def _finite(*values: float | None) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _recorded(vehicle: Vehicle, now: datetime) -> datetime | None:
    try:
        recorded = datetime.fromisoformat(vehicle.recorded_at)
    except (TypeError, ValueError):
        return None
    if recorded.tzinfo is None:
        return None
    return recorded.astimezone(now.tzinfo)


def _next_stop(placed: LiveJourney, schedule: Schedule) -> str | None:
    """The name of the next stop the bus calls at, if the timetable places it."""
    calls = placed.journey.calls
    index = math.floor(placed.progress.at) + 1
    if not 0 <= index < len(calls):
        return None
    position = schedule.positions.get(calls[index].atco)
    return position.name if position else None


def _vehicle(vehicle: Vehicle, placed: LiveJourney | None, schedule: Schedule, now: datetime) -> dict:
    recorded = _recorded(vehicle, now)
    bearing = vehicle.bearing if _finite(vehicle.bearing) else None
    return {
        "line": plain_text(vehicle.line, 20),
        "destination": placed.journey.destination if placed else plain_text(vehicle.destination, _NAME_LIMIT),
        "lat": round(vehicle.lat, 6),
        "lon": round(vehicle.lon, 6),
        "bearing": round(bearing % 360, 1) if bearing is not None else None,
        "recorded_at": iso(recorded),
        "age_s": max(0, int((now - recorded).total_seconds())) if recorded else None,
        "next_stop": _next_stop(placed, schedule) if placed else None,
    }


def _routes(journeys: Iterable[Journey], schedule: Schedule) -> list[dict]:
    """Each line and destination's positioned stops, as one line on the map:
    the journey with the most of them stands for the rest."""
    best: dict[tuple[str, str], tuple[tuple[float, float], ...]] = {}
    for journey in journeys:
        points = tuple((schedule.positions[c.atco].lat, schedule.positions[c.atco].lon)
                       for c in journey.calls if c.atco in schedule.positions)
        key = (journey.line, journey.destination)
        if len(points) >= 2 and len(points) > len(best.get(key, ())):
            best[key] = points
    keys = sorted(best, key=lambda k: (_line_order(k[0]), k[1]))[:MAX_ROUTES]
    return [{"line": line, "destination": destination, "points": [list(p) for p in best[(line, destination)]]}
            for line, destination in keys]


def area_view(board: BusBoard, now: datetime) -> dict:
    """The /api/area answer: live buses on our lines in the timetable's area,
    our stops, and the route lines. `now` is aware, in the board's zone."""
    schedule = board.schedule
    box = BusArea.from_schedule(schedule).box
    placed = {}
    for found in board.journeys:
        placed.setdefault(found.vehicle, found)
    lines = {journey.line.casefold() for journey in schedule.journeys}
    vehicles = []
    for vehicle in board.vehicles:
        if not _finite(vehicle.lat, vehicle.lon) or vehicle.line.casefold() not in lines:
            continue
        if box is not None and not (box[0] <= vehicle.lon <= box[2] and box[1] <= vehicle.lat <= box[3]):
            continue
        vehicles.append(_vehicle(vehicle, placed.get(vehicle), schedule, now))
    vehicles.sort(key=lambda v: (_line_order(v["line"]), v["destination"], v["lat"], v["lon"]))
    stops = [{"label": label, "name": schedule.positions[atco].name, "lat": schedule.positions[atco].lat,
              "lon": schedule.positions[atco].lon}
             for label, atco in sorted(schedule.stops.items()) if atco in schedule.positions]
    return {
        "fetched_at": iso(now),
        "box": [round(v, 6) for v in box] if box else None,
        "vehicles": vehicles[:MAX_VEHICLES],
        "stops": stops,
        "routes": _routes(schedule.journeys, schedule),
    }


# Timetable changes


def _running(journeys: list[Journey], day: date) -> frozenset:
    return frozenset(journey.calls for journey in journeys if runs_on(journey.calendar, day))


def timetable_changes(schedule: Schedule, today: date, days: int = CHANGE_DAYS) -> list[dict]:
    """Lines whose journeys change in the next `days` days, each at its first
    change, soonest first. See the module's notes for the rule."""
    last = today + timedelta(days=days)
    candidates = set()
    for journey in schedule.journeys:
        start, end = journey.calendar.start, journey.calendar.end
        if start and today < start <= last:
            candidates.add(start)
        if end and today <= end < last:
            candidates.add(end + timedelta(days=1))
    by_line: dict[tuple[str, str], list[Journey]] = {}
    for journey in schedule.journeys:
        by_line.setdefault((journey.line, journey.operator), []).append(journey)
    found: dict[tuple[str, str], date] = {}
    for day in sorted(candidates):
        week_before = day - timedelta(days=7)
        for key, journeys in by_line.items():
            if key not in found and _running(journeys, day) != _running(journeys, week_before):
                found[key] = day
    ordered = sorted(found.items(), key=lambda item: (item[1], _line_order(item[0][0]), item[0][1]))
    return [{"line": line, "operator": operator, "from": day.isoformat()}
            for (line, operator), day in ordered[:MAX_CHANGES]]
