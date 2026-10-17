"""The dashboard's snapshot: what the page shows, as the JSON in docs/dashboard-api.md.

The serialisers here are pure: they turn what the feeds gave (a TrainBoard,
a BusBoard, Conditions) into plain dicts, with the time and any history
phrases passed in. Rows are named by opaque ids, a hash of what identifies
the departure, so Darwin's service IDs and the bus keys stay on the server.

The Hub holds the latest snapshot for the web server: the pollers publish
their part, it is encoded once, and waiting event streams are woken. Its
version goes up only when the content changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Protocol

from ..core import announce, words
from ..core.model import Departure, Status, iso, minutes_between
from ..core.walk import WalkTimes
from ..settings import show as show_items
from ..sources.bodsbus import BusBoard
from ..sources.darwin import (
    CallingPoint,
    Coach,
    Formation,
    Message,
    ServiceDetails,
    TrainBoard,
    TrainService,
    train_position,
)
from ..sources.lastbus import LastBus
from ..sources.livebus import LiveDeparture, approach, where_words
from ..sources.weather import Conditions, Hour, rain_line
from ..timetable.model import Schedule, StopPosition, sign_letter
from ..timetable.roads import path
from .enrich import bus_extras, train_arrival
from .history import Observed, day_type

PARTS = ("trains", "buses", "weather", "disruptions", "timetable_changes")
TIMETABLE_STATES = ("ready", "building", "missing")
# Positioned stops shown on a bus's map before yours.
MAP_STOPS_BEFORE = 20
# Hours of weather sent ahead of now.
WEATHER_HOURS = 24
# How far ahead the rain sentence looks.
RAIN_LOOKAHEAD = timedelta(hours=2)
_DAY_MINUTES = 24 * 60

# (mode, route, scheduled "HH:MM", day) -> (day type, phrase or None), from the history.
Phrases = Callable[[str, str, str, date], "tuple[str, str | None]"]


# (route, scheduled "HH:MM", day) -> "No tracker on 3 of the last 10 weekdays" or None, for buses.
NoShows = Callable[[str, str, date], "str | None"]


def no_history(mode: str, route: str, scheduled: str, day: date) -> tuple[str, str | None]:
    """Phrases when there is no history to ask."""
    return day_type(day), None


def no_no_shows(route: str, scheduled: str, day: date) -> str | None:
    """No-show phrases when there is no history to ask."""
    return None


# (route, scheduled "HH:MM") -> the platform a train usually leaves from, or None, from the history.
UsualPlatforms = Callable[[str, str], "str | None"]


def no_usual_platform(route: str, scheduled: str) -> str | None:
    """Usual platforms when there is no history to ask."""
    return None


def row_id(kind: str, identity: str) -> str:
    """The opaque id of a row: 16 hex characters, the same every time for the same departure."""
    return hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()[:16]


def status_block(last_success: datetime | None = None, stale: bool = False,
                 error: str | None = None, note: str | None = None) -> dict:
    return {"last_success": iso(last_success), "stale": stale, "error": error, "note": note}


def clock_at(hhmm: str, near: datetime) -> datetime:
    """The time `hhmm` nearest to `near` (within 12 hours either way), in near's zone."""
    base = near.replace(second=0, microsecond=0, tzinfo=None)
    local = base + timedelta(minutes=minutes_between(base.strftime("%H:%M"), hhmm))
    return local.replace(tzinfo=near.tzinfo)


def status_text(departure: Departure) -> str:
    """The status in words, so colour is never the only clue: the board's
    words (core.words), so "Exp 07:19" reads the same on both."""
    if departure.mode == "bus" and departure.status is Status.UNKNOWN:
        return words.TIMETABLE_ONLY
    return words.status(departure)


def status_spoken(departure: Departure) -> str:
    """status_text with nothing abbreviated, for screen readers: "Expected 07:19"."""
    if departure.status is Status.LATE and departure.expected:
        return words.expected_spoken(departure.expected)
    return status_text(departure)


def leave_at(departure: Departure, scheduled_at: datetime, expected_at: datetime | None,
             walk: WalkTimes) -> datetime | None:
    """When to set off on foot, or None if cancelled or there is no walk time."""
    minutes = walk.minutes(departure)
    if minutes is None or departure.status is Status.CANCELLED:
        return None
    return (expected_at or scheduled_at) - timedelta(minutes=minutes)


def _history_entry(mode: str, route: str, scheduled: str, label: str, day: date,
                   phrases: Phrases) -> dict:
    kind, phrase = phrases(mode, route, scheduled, day)
    return {"mode": mode, "route": route, "scheduled": scheduled, "label": label,
            "day_type": kind, "phrase": phrase}


# Trains


def train_route(prefix: str, destination: str, replacement_bus: bool = False, ferry: bool = False) -> str:
    """The history's name for a regular train, like "GLO>MAN|Manchester Piccadilly".
    A rail replacement bus is kept apart from the train it stands in for:
    "GLO>MAN bus|Manchester Piccadilly", and a ferry likewise: "PMH
    ferry|Ryde Pier Head". No train's name can be one, as the prefix comes
    from the settings and the destination after it."""
    if replacement_bus:
        return f"{prefix} bus|{destination}"
    return f"{prefix} ferry|{destination}" if ferry else f"{prefix}|{destination}"


def _train_route(prefix: str, departure: Departure) -> str:
    return train_route(prefix, departure.destination, departure.replacement_bus, departure.ferry)


def _train_noun(departure: Departure) -> str:
    """What a departure in the trains is: "train", "replacement bus" or "ferry"."""
    if departure.replacement_bus:
        return words.REPLACEMENT_BUS.lower()
    return words.FERRY.lower() if departure.ferry else "train"


def _train_label(departure: Departure) -> str:
    """"07:12 to Manchester Piccadilly", or "07:12 replacement bus to ..."
    or "07:12 ferry to ..."."""
    noun = _train_noun(departure)
    kind = "" if noun == "train" else f" {noun}"
    return f"{departure.scheduled}{kind} to {departure.destination}"


def _train_times(service: TrainService, fetched: datetime) -> tuple[datetime, datetime | None]:
    dep = service.departure
    scheduled_at = clock_at(dep.scheduled, fetched)
    if dep.status is Status.ON_TIME:
        return scheduled_at, scheduled_at
    if dep.status is Status.LATE and dep.expected:
        return scheduled_at, clock_at(dep.expected, scheduled_at)
    return scheduled_at, None


def _train_id(service: TrainService) -> str:
    """A train's row id; a replacement bus's or a ferry's is never a train's,
    even at the same time."""
    dep = service.departure
    identity = service.service_id or f"{dep.scheduled}+{dep.destination}"
    return row_id(_train_noun(dep), identity)


def _minutes_late(departure: Departure) -> int | None:
    if departure.status is Status.LATE and departure.expected:
        return departure.minutes_late
    return None


def position_block(details: ServiceDetails | None, now: datetime) -> dict | None:
    """Where the train is, for the row's strip; None without its details."""
    found = train_position(details, now)
    if found is None:
        return None
    return {"where": found.where,
            "strip": {"stops": list(found.strip_stops), "yours": found.yours, "train_at": found.train_at}}


def announcement_block(service: TrainService, platform_changed: bool, is_next: bool) -> list[dict]:
    """What a station would announce for the train (core/announce.py), as
    [{"kind", "text"}] in the order to say them. `is_next` says it is the
    next train to depart, not a later one."""
    dep = service.departure
    formation = service.formation
    coaches = service.length if service.length is not None else (
        len(formation.coaches) if formation is not None and formation.coaches else None)
    train = announce.Train(
        time=dep.scheduled, destination=dep.destination, operator=dep.service, via=dep.via, platform=dep.platform,
        replacement_bus=dep.replacement_bus, ferry=dep.ferry,
        calling=tuple(p.name for p in service.calling_points if not p.skipped),
        skipped=tuple(p.name for p in service.calling_points if p.skipped),
        coaches=coaches,
    )
    said = announce.announcements(
        train, is_cancelled=dep.status is Status.CANCELLED, late=dep.status is Status.LATE,
        minutes_late=_minutes_late(dep), platform_changed=platform_changed,
        cancel_reason=service.cancel_reason, delay_reason=service.delay_reason, is_next=is_next)
    return [{"kind": kind, "text": text} for kind, text in said]


def train_row(service: TrainService, fetched: datetime, walk: WalkTimes, route_prefix: str,
              phrases: Phrases = no_history, details: ServiceDetails | None = None,
              now: datetime | None = None, platforms: UsualPlatforms = no_usual_platform,
              is_next: bool = False) -> tuple[dict, dict]:
    """A train's row and its server-side lookup (the history key). With the
    train's service details, the row says where it is at `now` (else when
    the board was fetched) and the stops it calls at before this station.
    Its platform has changed when the last fetch gave another (the
    Departure says so) or it isn't the one `platforms` says it usually is;
    a train cancelled leaves from none, so never.
    `is_next` says it is the next train to depart, for its announcements."""
    dep = service.departure
    scheduled_at, expected_at = _train_times(service, fetched)
    route = _train_route(route_prefix, dep)
    history = _history_entry("train", route, dep.scheduled, _train_label(dep), scheduled_at.date(), phrases)
    usual = platforms(route, dep.scheduled) if dep.is_train else None
    changed = (dep.status is not Status.CANCELLED and dep.platform is not None
               and (dep.platform_changed or (usual is not None and dep.platform != usual)))
    row = {
        "id": _train_id(service),
        "service": dep.service,
        "destination": dep.destination,
        "origin": service.origin,
        "scheduled": dep.scheduled,
        "scheduled_at": iso(scheduled_at),
        "expected": expected_at.strftime("%H:%M") if expected_at else None,
        "expected_at": iso(expected_at),
        "status": dep.status.value,
        "status_text": status_text(dep),
        "status_spoken": status_spoken(dep),
        "minutes_late": _minutes_late(dep),
        "platform": dep.platform,
        "platform_changed": changed,
        "usual_platform": usual,
        "via": dep.via,
        "replacement_bus": dep.replacement_bus,
        "ferry": dep.ferry,
        "length": service.length,
        "formation": formation_block(service),
        "cancel_reason": service.cancel_reason,
        "delay_reason": service.delay_reason,
        "walk_min": walk.minutes(dep),
        "leave_at": iso(leave_at(dep, scheduled_at, expected_at, walk)),
        "calling_at": dep.calling_at,
        "announce": announcement_block(service, changed, is_next),
        "calling_points": [calling_point(p) for p in service.calling_points],
        "previous_points": [calling_point(p) for p in details.previous] if details else [],
        "position": position_block(details, now or fetched),
        "history": {"phrase": history["phrase"]},
    }
    return row, {"history": history}


def _clock_or_none(value: str | None, scheduled: str) -> str | None:
    """Darwin's time words as the contract's HH:MM: "On time" is the scheduled
    time; "Delayed", "No report" and the like say no time."""
    if value == "On time":
        return scheduled
    return value if value and re.fullmatch(r"\d\d:\d\d", value) else None


def message(found: Message) -> dict:
    """One of the station's messages: "station" ones are about its
    facilities, shown quietly and never announced; "service" ones are
    disruption."""
    return {"text": found.text, "category": found.category, "severity": found.severity, "kind": found.kind}


def calling_point(point: CallingPoint) -> dict:
    """One calling point for the train dialog, times as HH:MM or null."""
    cancelled = point.skipped
    return {
        "name": point.name, "crs": point.crs, "scheduled": point.scheduled,
        "expected": None if cancelled else _clock_or_none(point.expected, point.scheduled),
        "actual": None if cancelled else _clock_or_none(point.actual, point.scheduled),
        "cancelled": cancelled,
    }


# How full a coach is, in words: Darwin's loading is 0 to 100.
QUIET_BELOW = 35
VERY_BUSY_FROM = 70
# The ends of a train must differ by this much loading to name the quietest.
LOADING_SPREAD = 15
QUIETEST = {"front": "Quietest at the front", "middle": "Quietest in the middle", "back": "Quietest at the back"}


def busy_word(loading: int) -> str:
    if loading < QUIET_BELOW:
        return "Quiet"
    return "Very busy" if loading >= VERY_BUSY_FROM else "Busy"


def _section_loadings(coaches: tuple[Coach, ...]) -> dict[str, float]:
    """The mean loading of the front, middle and back of the train, from the
    coaches that have one: each end is a third of the train, rounded up, so
    two coaches are a front and a back."""
    end = math.ceil(len(coaches) / 3)
    parts = {"front": coaches[:end], "middle": coaches[end:len(coaches) - end], "back": coaches[len(coaches) - end:]}
    means = {}
    for name, part in parts.items():
        known = [c.loading for c in part if c.loading is not None]
        if known:
            means[name] = sum(known) / len(known)
    return means


def loading_text(formation: Formation) -> str | None:
    """Where to sit, in words: "Quietest at the back", "Much the same all
    along", or how busy it is overall from Darwin's average; None when Darwin
    doesn't say. The front is the front in Darwin's order of the coaches."""
    coaches = formation.coaches
    known = sum(c.loading is not None for c in coaches)
    if len(coaches) >= 2 and known * 2 >= len(coaches):
        means = _section_loadings(coaches)
        if len(means) >= 2:
            if max(means.values()) - min(means.values()) < LOADING_SPREAD:
                return "Much the same all along the train"
            return QUIETEST[min(means, key=lambda name: means[name])]
    if formation.average_loading is not None:
        return f"{busy_word(formation.average_loading)} overall"
    return None


def detach_text(service: TrainService) -> str | None:
    """"Front 2 coaches detach here" when Darwin says the front detaches at
    this station; the count only when the train is shorter at its next stop."""
    if not service.detach_front:
        return None
    after = service.calling_points[0].length if service.calling_points else None
    if service.length is not None and after is not None and after < service.length:
        count = service.length - after
        return f"Front {count} {'coach detaches' if count == 1 else 'coaches detach'} here"
    return "Front coaches detach here"


def formation_block(service: TrainService) -> dict | None:
    """The train's coaches for its sheet, from the front, with where it is
    quietest and Darwin's flags in words; None when Darwin says nothing of
    them, as many operators don't. A replacement bus or a ferry never has one."""
    if not service.departure.is_train:
        return None
    formation = service.formation
    detach = detach_text(service)
    if formation is None and detach is None and not service.reverse_formation:
        return None
    coaches = formation.coaches if formation is not None else ()
    return {
        "coaches": [{
            "number": c.number, "class": c.coach_class, "toilet": c.toilet,
            "toilet_out_of_order": c.toilet_out_of_order, "loading": c.loading,
            "busy": busy_word(c.loading) if c.loading is not None else None,
        } for c in coaches],
        "loading_text": loading_text(formation) if formation is not None else None,
        "detach_text": detach,
        "reverse": service.reverse_formation,
    }


def trains_part(board: TrainBoard | None, fetched: datetime | None, status: dict, station: str,
                filter_crs: str | None, walk: WalkTimes, route_prefix: str,
                phrases: Phrases = no_history, details: Mapping[str, ServiceDetails] | None = None,
                now: datetime | None = None, positions: dict | None = None,
                platforms: UsualPlatforms = no_usual_platform) -> tuple[dict, dict[str, dict]]:
    """The trains section and its lookups by row id. `fetched` is when the
    board was fetched (aware), which its HH:MM times are read against.
    `details` are the service details known, by Darwin service ID; `now` is
    when to place the trains (else `fetched`). `positions` is how asking
    where the trains are is going (a status block), None without its key.
    `platforms` gives the platform each usually leaves from."""
    rows, lookups = [], {}
    details = details or {}
    if board is not None and fetched is not None:
        # The next train to depart is the first not cancelled, as the board lists them by time.
        next_found = False
        for service in board.services:
            found = details.get(service.service_id) if service.service_id else None
            is_next = not next_found and service.departure.status is not Status.CANCELLED
            next_found = next_found or is_next
            row, lookup = train_row(service, fetched, walk, route_prefix, phrases, found, now, platforms, is_next)
            row["arrives"] = train_arrival(service, filter_crs, *_train_times(service, fetched))
            rows.append(row)
            lookups[row["id"]] = lookup
    return {
        "station": (board.location if board is not None and board.location else None) or station,
        "filter": filter_crs,
        "status": status,
        "positions": positions,
        "messages": [message(m) for m in board.messages] if board is not None else [],
        "rows": rows,
    }, lookups


def train_observations(board: TrainBoard, fetched: datetime, route_prefix: str) -> list[Observed]:
    """What a train poll showed, for the punctuality history (naive local times)."""
    found = []
    for service in board.services:
        dep = service.departure
        scheduled_at, expected_at = _train_times(service, fetched)
        departs = scheduled_at if dep.status is Status.CANCELLED else (expected_at or scheduled_at)
        found.append(Observed(
            route=_train_route(route_prefix, dep),
            scheduled=dep.scheduled,
            day=scheduled_at.date(),
            departs_at=departs.replace(tzinfo=None),
            status=dep.status.value,
            delay_min=0 if dep.status is Status.ON_TIME else _minutes_late(dep),
            live=dep.status is not Status.UNKNOWN,
            service=dep.service,
            destination=dep.destination,
            platform=dep.platform,
            reason=service.cancel_reason or service.delay_reason,
            identity=service.service_id,
        ))
    return found


# Buses


def bus_route(live: LiveDeparture) -> str:
    """The history's name for a regular bus, like "A|341|Hyde Bus Station"."""
    dep = live.departure
    return f"{dep.stop or ''}|{dep.service}|{dep.destination}"


def _recorded(value: str, tz: tzinfo | None) -> datetime | None:
    try:
        recorded = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=UTC)  # as sources/bods.py reads a bare time
    return recorded.astimezone(tz)


def _bearing(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value % 360, 1)


def _vehicle(live: LiveDeparture, now: datetime, with_position: bool) -> dict | None:
    """The bus tracked for the departure: its own, or the one followed in to work it."""
    vehicle = live.tracked
    if vehicle is None or not live.live:
        return None
    recorded = _recorded(vehicle.recorded_at, now.tzinfo)
    found = {
        "age_s": max(0, int((now - recorded).total_seconds())) if recorded else None,
        "recorded_at": iso(recorded),
        "bearing": _bearing(vehicle.bearing),
        # Checked as read (bods.fleet_and_registration); the raw VehicleRef never goes out.
        "fleet_number": vehicle.fleet_number or None,
        "registration": vehicle.registration or None,
    }
    if with_position:
        found = {"lat": vehicle.lat, "lon": vehicle.lon, **found}
    return found


def _strip(live: LiveDeparture, positions: dict[str, StopPosition], now: datetime) -> dict | None:
    found = approach(live, positions)
    if found is None:
        return None
    names = [stop.name for stop in found.stops]
    bus_at = round(found.bus_at, 3) if found.bus_at is not None else None
    # The words are livebus's, so a screen's row says the same (livebus.row_strip).
    return {"stops": names, "yours": found.yours, "bus_at": bus_at,
            "where": where_words(live, found, now.replace(tzinfo=None))}


def _call_time(minute: int) -> str:
    minute %= _DAY_MINUTES
    return f"{minute // 60:02d}:{minute % 60:02d}"


def bus_map(row: dict, live: LiveDeparture, positions: dict[str, StopPosition], now: datetime,
            shapes: Mapping[str, tuple] = {}) -> dict:
    """The /api/bus/<id> answer: the journey's positioned stops up to yours
    and one after, the road through them (from the timetable's `shapes`;
    straight between stops it has none for), and the bus."""
    calls = live.journey.calls
    yours = live.call_index
    picked = [i for i in range(yours) if calls[i].atco in positions][-MAP_STOPS_BEFORE:]
    picked += [i for i in (yours, yours + 1) if i < len(calls) and calls[i].atco in positions]
    stops = []
    for i in picked:
        place = positions[calls[i].atco]
        stops.append({"name": place.name, "lat": place.lat, "lon": place.lon,
                      "time": _call_time(calls[i].minute), "yours": i == yours})
    strip = row["strip"]
    return {
        "id": row["id"],
        "line": row["line"],
        "destination": row["destination"],
        "stop": row["stop"],
        "stop_name": row["stop_name"],
        "stops": stops,
        "path": path([calls[i].atco for i in picked], positions, shapes),
        "vehicle": _vehicle(live, now, with_position=True),
        "where": strip["where"] if strip else where_words(live, None, now.replace(tzinfo=None)),
    }


def bus_row(live: LiveDeparture, positions: dict[str, StopPosition], now: datetime, walk: WalkTimes,
            phrases: Phrases = no_history, no_shows: NoShows = no_no_shows,
            shapes: Mapping[str, tuple] = {}) -> tuple[dict, dict]:
    """A bus's row and its server-side lookup (the map and the history key).
    `now` is aware, in the board's zone; `shapes` are the timetable's roads.
    The extras that need the whole board (enrich.bus_extras) are added by
    buses_part."""
    dep = live.departure
    tz = now.tzinfo
    scheduled_at = live.leaves.replace(tzinfo=tz)
    expected_at = live.expected.replace(tzinfo=tz) if live.live else None
    late = dep.status is Status.LATE
    history = _history_entry("bus", bus_route(live), dep.scheduled,
                             f"{dep.scheduled} {dep.service} to {dep.destination}", live.leaves.date(), phrases)
    row = {
        "id": row_id("bus", live.key),
        "line": dep.service,
        "destination": dep.destination,
        "stop": dep.stop,
        "stop_name": stop_name(positions.get(live.journey.calls[live.call_index].atco), dep.stop),
        "scheduled": dep.scheduled,
        "scheduled_at": iso(scheduled_at),
        "expected": expected_at.strftime("%H:%M") if expected_at else None,
        "expected_at": iso(expected_at),
        "status": dep.status.value,
        "status_text": status_text(dep),
        "status_spoken": status_spoken(dep),
        "minutes_late": round((live.expected - live.leaves).total_seconds() / 60) if late else None,
        "live": live.live,
        "walk_min": walk.minutes(dep),
        "leave_at": iso(leave_at(dep, scheduled_at, expected_at, walk)),
        "vehicle": _vehicle(live, now, with_position=False),
        "strip": _strip(live, positions, now),
        "history": {
            "phrase": history["phrase"],
            "no_show_phrase": no_shows(history["route"], dep.scheduled, live.leaves.date()),
        },
    }
    return row, {"bus": bus_map(row, live, positions, now, shapes), "history": history}


def timetable_block(state: str, schedule: Schedule | None, tz: tzinfo) -> dict:
    if state not in TIMETABLE_STATES:
        raise ValueError(f"Unknown timetable state {state!r}")
    return {
        "state": state,
        "built": iso(schedule.built.replace(tzinfo=tz)) if schedule is not None else None,
        "valid_until": schedule.valid_until.isoformat() if schedule and schedule.valid_until else None,
        # Journeys calling at your stops; inbound-only ones give no departures.
        "journeys": len(schedule.departure_journeys) if schedule is not None else None,
    }


def next_stops(atcos: set[str], schedule: Schedule) -> dict[str, str]:
    """The stop buses call at next after each of `atcos`, by name: the one
    most timetabled journeys go on to (ties by name); a next stop with the
    stop's own name doesn't count. Two stops across a road from each other
    share a name, but not the stop after. Left out where no next stop has one."""
    counts: dict[str, Counter[str]] = {atco: Counter() for atco in atcos}
    positions = schedule.positions
    for journey in schedule.departure_journeys:
        calls = journey.calls
        for index in range(len(calls) - 1):
            atco = calls[index].atco
            if atco not in counts:
                continue
            after = positions.get(calls[index + 1].atco)
            here = positions.get(atco)
            if after is not None and (here is None or after.name != here.name):
                counts[atco][after.name] += 1
    return {atco: min(found, key=lambda name: (-found[name], name)) for atco, found in counts.items() if found}


def stop_details(stops: dict[str, str], schedule: Schedule | None, walk: WalkTimes) -> dict[str, dict]:
    """Each stop label's name and walk, with what tells it from a stop of the
    same name: the letter on its sign, and the stop buses go on to. Nothing is
    made up: a stop the stop data doesn't have is all nulls but its walk."""
    positions = schedule.positions if schedule is not None else {}
    towards = next_stops(set(stops.values()), schedule) if schedule is not None else {}
    found = {}
    for label, atco in stops.items():
        place = positions.get(atco)
        found[label] = {
            "name": place.name if place is not None else None,
            "letter": sign_letter(place.indicator) if place is not None else None,
            "towards": towards.get(atco),
            "walk_min": walk.stop(label),
        }
    return found


def stop_name(place: StopPosition | None, label: str | None) -> str:
    """A stop's name from the stop data, with the letter on its sign when known
    ("Henry Street (Stop A)"), else "Stop A" from the board's label."""
    if place is None:
        return f"Stop {label}" if label else "Your stop"
    return f"{place.name} ({place.indicator})" if place.indicator else place.name


def last_bus(last: LastBus, now: datetime) -> dict:
    """A line and destination's last bus today, for the page (see
    sources/lastbus.py). `now` is aware, in the board's zone."""
    return {
        "line": last.line,
        "destination": last.destination,
        "stop": last.stop,
        "scheduled": last.leaves.strftime("%H:%M"),
        "scheduled_at": iso(last.leaves.replace(tzinfo=now.tzinfo)),
        "remaining": last.remaining,
        "show": last.shown(now.replace(tzinfo=None)),
    }


def buses_part(board: BusBoard | None, now: datetime, status: dict, timetable: dict,
               stops: dict[str, str], walk: WalkTimes,
               phrases: Phrases = no_history, no_shows: NoShows = no_no_shows,
               positions: dict | None = None, last: Iterable[LastBus] = ()) -> tuple[dict, dict[str, dict]]:
    """The buses section and its lookups by row id. `stops` are labels to ATCO
    codes. `positions` is how asking where the buses are is going (a status
    block), None where none are asked for. `last` are the last buses today."""
    rows, lookups = [], {}
    schedule = board.schedule if board is not None else None
    if board is not None:
        for live in board.departures:
            row, lookup = bus_row(live, board.schedule.positions, now, walk, phrases, no_shows, board.schedule.shapes)
            row.update(bus_extras(live, board, now))
            rows.append(row)
            lookups[row["id"]] = lookup
    return {
        "status": status,
        "positions": positions,
        "timetable": timetable,
        "stops": stop_details(stops, schedule, walk),
        "rows": rows,
        "last_buses": [last_bus(found, now) for found in last],
    }, lookups


def bus_observations(board: BusBoard) -> list[Observed]:
    """What a bus poll showed, for the punctuality history (naive local times)."""
    found = []
    for live in board.departures:
        dep = live.departure
        placed = live.live
        found.append(Observed(
            route=bus_route(live),
            scheduled=dep.scheduled,
            day=live.leaves.date(),
            departs_at=live.expected,
            status=dep.status.value,
            delay_min=round((live.expected - live.leaves).total_seconds() / 60) if placed else None,
            live=placed,
            service=dep.service,
            destination=dep.destination,
            platform=None,
            reason=None,
            identity=live.key,
        ))
    return found


# Weather


def _round(value: float | None, digits: int | None = None):
    if value is None:
        return None
    return round(value, digits) if digits is not None else round(value)


def _current(conditions: Conditions) -> dict | None:
    now = conditions.now
    if now is None:
        return None
    return {
        "temperature": _round(now.temperature, 1),
        "feels_like": _round(now.feels_like, 1),
        "code": now.code,
        "text": now.text,
        "wind": _round(now.wind),
        "gust": _round(now.gust),
        "direction": _round(now.direction),
        "is_day": now.is_day,
    }


def _sun(conditions: Conditions, today: date, tz: tzinfo | None) -> dict | None:
    days = [entry for entry in conditions.sun if entry[0] == today] or conditions.sun[:1]
    if not days:
        return None
    _, rise, set_ = days[0]
    return {"sunrise": iso(rise.replace(tzinfo=tz)), "sunset": iso(set_.replace(tzinfo=tz))}


def rain_sentence(hours: list[Hour], now: datetime, until: datetime, threshold: int) -> str | None:
    """The rain sentence, worded so it only changes when the weather does:
    "Rain now (92%)" through an hour of rain already under way, otherwise the
    board's "Rain from 23:00 (80%)" at the start of the hour it is due."""
    line = rain_line(hours, now, until, threshold, live=False)
    if line is None:
        return None
    first = next(h for h in hours if h.start + timedelta(hours=1) > now and h.probability is not None
                 and h.probability >= threshold)
    return f"Rain now ({first.probability}%)" if first.start <= now else line


def weather_part(conditions: Conditions | None, now: datetime, status: dict, threshold: int) -> dict:
    """The weather section. `now` is aware, in the zone the forecast was asked for."""
    if conditions is None:
        return {"status": status, "rain": None, "now": None, "hours": [], "sun": None}
    tz = now.tzinfo
    local = now.replace(tzinfo=None)
    ahead = [hour for hour in conditions.hours if hour.start + timedelta(hours=1) > local][:WEATHER_HOURS]
    hours = [Hour(h.start, h.probability, h.precipitation) for h in conditions.hours]
    return {
        "status": status,
        "rain": rain_sentence(hours, local, local + RAIN_LOOKAHEAD, threshold),
        "now": _current(conditions),
        "hours": [
            {"start": iso(h.start.replace(tzinfo=tz)), "temperature": _round(h.temperature),
             "probability": h.probability, "precipitation": _round(h.precipitation, 1), "code": h.code,
             "text": h.text, "wind": _round(h.wind), "is_day": h.is_day}
            for h in ahead
        ],
        "sun": _sun(conditions, local.date(), tz),
    }


# The hub


def _encode(value) -> str:
    # ASCII only and no NaN: valid JSON on one line, safe in an event stream.
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


class SnapshotSource(Protocol):
    """What the web server reads the snapshot from: the Hub the pollers
    publish to, in the same process (Docker, a desktop), or on a Pi a mirror
    of the snapshot the fetch service hands over (handoff.SnapshotMirror)."""

    def current(self) -> tuple[int, bytes]: ...

    def wait(self, after_version: int, timeout: float) -> int: ...

    def lookup(self, row_id: str) -> dict | None: ...

    def get(self, name: str) -> object | None: ...

    def touch(self) -> None: ...

    def active(self, within: float) -> bool: ...

    def age(self) -> float: ...

    def close(self) -> None: ...

    @property
    def closed(self) -> bool: ...


class Hub:
    """The latest snapshot, shared by the pollers (which publish) and the web
    server's threads (which read and wait). Safe to share between threads."""

    def __init__(self, title: str, walk: WalkTimes, clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, tz: tzinfo | None = None,
                 row_strips: bool = True, show: Iterable[str] | None = None) -> None:
        # The board's zone, not the host's: a container's clock is in UTC.
        self._clock = clock or (lambda: datetime.now(tz) if tz else datetime.now().astimezone())
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        # `quiet` says whether a quiet window is on; the collector's heartbeat sets it.
        # `row_strips` says whether the page draws each row's position strip,
        # and `show` what it shows of what it can leave out ([dashboard] show).
        shown = show_items.default("dashboard") if show is None else show
        self._static = {"title": title, "walk": {"train": walk.train, "bus": dict(walk.bus)}, "quiet": False,
                        "row_strips": row_strips, "show": show_items.ordered("dashboard", shown)}
        self._parts: dict[str, dict | list | None] = dict.fromkeys(PARTS)
        self._part_lookups: dict[str, dict[str, dict]] = {part: {} for part in PARTS}
        self._lookups: dict[str, dict] = {}
        self._previous: dict[str, dict] = {}
        self._version = 0
        self._canonical: str | None = None
        self._body = b""
        self._rebuilt = 0.0  # _rebuild, below, sets it
        self._touched: float | None = None
        self._closed = False
        self._resources: dict[str, object] = {}  # served on their own, not in the snapshot
        with self._lock:
            self._rebuild()

    def _rebuild(self) -> None:
        """Encode the snapshot again; call with the lock held."""
        content = {**self._static, **self._parts}
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
        lookups = {key: value for part in PARTS for key, value in self._part_lookups[part].items()}
        if canonical != self._canonical:
            self._version += 1
            self._canonical = canonical
            self._previous = self._lookups
            self._changed.notify_all()
        self._lookups = lookups
        snapshot = {"version": self._version, "generated_at": iso(self._clock()), **content}
        self._body = _encode(snapshot).encode("ascii")
        self._rebuilt = self._monotonic()

    def publish(self, part: str, data: dict | list | None, lookups: dict[str, dict] | None = None) -> int:
        """Replace one section (trains, buses, weather or disruptions) and its
        lookups by row id. Returns the version, which only goes up if anything changed."""
        if part not in PARTS:
            raise ValueError(f"Unknown part {part!r}")
        with self._lock:
            self._parts[part] = data
            self._part_lookups[part] = dict(lookups or {})
            self._rebuild()
            return self._version

    def refresh(self, quiet: bool | None = None) -> None:
        """Rebuild with the time now, so generated_at stays close to the
        server's clock when the feeds are quiet. With `quiet`, say too whether
        a quiet window is on: the wall-tablet page dims in one."""
        with self._lock:
            if quiet is not None:
                self._static["quiet"] = quiet
            self._rebuild()

    def current(self) -> tuple[int, bytes]:
        """(version, the snapshot as JSON bytes)."""
        with self._lock:
            return self._version, self._body

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def wait(self, after_version: int, timeout: float) -> int:
        """Wait up to `timeout` seconds for a version other than `after_version`
        (or for close); returns the version then."""
        with self._lock:
            self._changed.wait_for(lambda: self._version != after_version or self._closed, timeout)
            return self._version

    def lookup(self, row_id: str) -> dict | None:
        """What the server keeps about a row of the current or previous snapshot."""
        with self._lock:
            return self._lookups.get(row_id) or self._previous.get(row_id)

    def export(self) -> dict:
        """Everything another process needs to serve this snapshot, taken
        together: the version, the snapshot as sent, the lookups of this and
        the previous snapshot, and the area map (None without one)."""
        with self._lock:
            version, body, area = self._version, self._body, self._resources.get("area")
            lookups, previous = dict(self._lookups), dict(self._previous)
        return {"version": version, "snapshot": json.loads(body), "lookups": lookups, "previous": previous,
                "area": area}

    def put(self, name: str, value) -> None:
        """Keep a resource the server answers with on its own (the area map's
        "area"), outside the snapshot and its version. None removes it."""
        with self._lock:
            if value is None:
                self._resources.pop(name, None)
            else:
                self._resources[name] = value

    def get(self, name: str) -> object | None:
        """A resource kept by put(), or None."""
        with self._lock:
            return self._resources.get(name)

    def touch(self) -> None:
        """Note that a browser is using the dashboard."""
        with self._lock:
            self._touched = self._monotonic()

    def active(self, within: float) -> bool:
        """Whether a browser has been seen in the last `within` seconds."""
        with self._lock:
            return self._touched is not None and self._monotonic() - self._touched <= within

    def age(self) -> float:
        """Seconds since the snapshot was last rebuilt."""
        with self._lock:
            return self._monotonic() - self._rebuilt

    def close(self) -> None:
        """Wake every waiting stream so it can finish; for shutting down."""
        with self._lock:
            self._closed = True
            self._changed.notify_all()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

