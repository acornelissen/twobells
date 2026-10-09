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
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Callable, Mapping

from .bodsbus import BusBoard
from .darwin import CallingPoint, ServiceDetails, TrainBoard, TrainService, train_position
from .enrich import bus_extras, train_arrival
from .history import Observed, day_type
from .livebus import LiveDeparture, approach
from .model import Departure, Status, minutes_between
from .route import where
from .schedule_model import Schedule, StopPosition
from .walk import WalkTimes
from .weather import Conditions, Hour, rain_line

PARTS = ("trains", "buses", "weather", "disruptions")
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


def row_id(kind: str, identity: str) -> str:
    """The opaque id of a row: 16 hex characters, the same every time for the same departure."""
    return hashlib.sha256(f"{kind}|{identity}".encode()).hexdigest()[:16]


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment is not None else None


def status_block(last_success: datetime | None = None, stale: bool = False,
                 error: str | None = None, note: str | None = None) -> dict:
    return {"last_success": iso(last_success), "stale": stale, "error": error, "note": note}


def clock_at(hhmm: str, near: datetime) -> datetime:
    """The time `hhmm` nearest to `near` (within 12 hours either way), in near's zone."""
    base = near.replace(second=0, microsecond=0, tzinfo=None)
    local = base + timedelta(minutes=minutes_between(base.strftime("%H:%M"), hhmm))
    return local.replace(tzinfo=near.tzinfo)


def status_text(departure: Departure) -> str:
    """The status in words, so colour is never the only clue."""
    if departure.status is Status.CANCELLED:
        return "Cancelled"
    if departure.status is Status.ON_TIME:
        return "On time"
    if departure.status is Status.LATE:
        return f"Expected {departure.expected}" if departure.expected else "Delayed"
    return "Timetable only" if departure.mode == "bus" else "No report"


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


def train_route(prefix: str, destination: str) -> str:
    """The history's name for a regular train, like "GLO>MAN|Manchester Piccadilly"."""
    return f"{prefix}|{destination}"


def _train_times(service: TrainService, fetched: datetime) -> tuple[datetime, datetime | None]:
    dep = service.departure
    scheduled_at = clock_at(dep.scheduled, fetched)
    if dep.status is Status.ON_TIME:
        return scheduled_at, scheduled_at
    if dep.status is Status.LATE and dep.expected:
        return scheduled_at, clock_at(dep.expected, scheduled_at)
    return scheduled_at, None


def _train_identity(service: TrainService) -> str:
    dep = service.departure
    return service.service_id or f"{dep.scheduled}+{dep.destination}"


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


def train_row(service: TrainService, fetched: datetime, walk: WalkTimes, route_prefix: str,
              phrases: Phrases = no_history, details: ServiceDetails | None = None,
              now: datetime | None = None) -> tuple[dict, dict]:
    """A train's row and its server-side lookup (the history key). With the
    train's service details, the row says where it is at `now` (else when
    the board was fetched) and the stops it calls at before this station."""
    dep = service.departure
    scheduled_at, expected_at = _train_times(service, fetched)
    history = _history_entry("train", train_route(route_prefix, dep.destination), dep.scheduled,
                             f"{dep.scheduled} to {dep.destination}", scheduled_at.date(), phrases)
    row = {
        "id": row_id("train", _train_identity(service)),
        "service": dep.service,
        "destination": dep.destination,
        "origin": service.origin,
        "scheduled": dep.scheduled,
        "scheduled_at": iso(scheduled_at),
        "expected": expected_at.strftime("%H:%M") if expected_at else None,
        "expected_at": iso(expected_at),
        "status": dep.status.value,
        "status_text": status_text(dep),
        "minutes_late": _minutes_late(dep),
        "platform": dep.platform,
        "length": service.length,
        "cancel_reason": service.cancel_reason,
        "delay_reason": service.delay_reason,
        "leave_at": iso(leave_at(dep, scheduled_at, expected_at, walk)),
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


def calling_point(point: CallingPoint) -> dict:
    """One calling point for the train dialog, times as HH:MM or null."""
    cancelled = point.cancelled or point.expected == "Cancelled" or point.actual == "Cancelled"
    return {
        "name": point.name, "crs": point.crs, "scheduled": point.scheduled,
        "expected": None if cancelled else _clock_or_none(point.expected, point.scheduled),
        "actual": None if cancelled else _clock_or_none(point.actual, point.scheduled),
        "cancelled": cancelled,
    }


def trains_part(board: TrainBoard | None, fetched: datetime | None, status: dict, station: str,
                filter_crs: str | None, walk: WalkTimes, route_prefix: str,
                phrases: Phrases = no_history, details: Mapping[str, ServiceDetails] | None = None,
                now: datetime | None = None) -> tuple[dict, dict[str, dict]]:
    """The trains section and its lookups by row id. `fetched` is when the
    board was fetched (aware), which its HH:MM times are read against.
    `details` are the service details known, by Darwin service ID; `now` is
    when to place the trains (else `fetched`)."""
    rows, lookups = [], {}
    details = details or {}
    if board is not None and fetched is not None:
        for service in board.services:
            found = details.get(service.service_id) if service.service_id else None
            row, lookup = train_row(service, fetched, walk, route_prefix, phrases, found, now)
            row["arrives"] = train_arrival(service, filter_crs, *_train_times(service, fetched))
            rows.append(row)
            lookups[row["id"]] = lookup
    return {
        "station": (board.location if board is not None and board.location else None) or station,
        "filter": filter_crs,
        "status": status,
        "messages": list(board.messages) if board is not None else [],
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
            route=train_route(route_prefix, dep.destination),
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


def _recorded(value: str, tz: tzinfo) -> datetime | None:
    try:
        recorded = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)  # as bods.py reads a bare time
    return recorded.astimezone(tz)


def _bearing(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value % 360, 1)


def _vehicle(live: LiveDeparture, now: datetime, with_position: bool) -> dict | None:
    vehicle = live.vehicle
    if vehicle is None or live.progress is None:
        return None
    recorded = _recorded(vehicle.recorded_at, now.tzinfo)
    found = {
        "age_s": max(0, int((now - recorded).total_seconds())) if recorded else None,
        "recorded_at": iso(recorded),
        "bearing": _bearing(vehicle.bearing),
    }
    if with_position:
        found = {"lat": vehicle.lat, "lon": vehicle.lon, **found}
    return found


def _where(live: LiveDeparture, names: list[str] | None, yours: int, bus_at: float | None) -> str:
    """Where the bus is, in words for the row and screen readers."""
    if live.progress is None:
        return "No live position"
    if not names:
        return "Live position known"
    return where(tuple(names[: yours + 1]), bus_at, False)


def _strip(live: LiveDeparture, positions: dict[str, StopPosition]) -> dict | None:
    found = approach(live, positions)
    if found is None:
        return None
    names = [stop.name for stop in found.stops]
    bus_at = round(found.bus_at, 3) if found.bus_at is not None else None
    return {"stops": names, "yours": found.yours, "bus_at": bus_at,
            "where": _where(live, names, found.yours, found.bus_at)}


def _call_time(minute: int) -> str:
    minute %= _DAY_MINUTES
    return f"{minute // 60:02d}:{minute % 60:02d}"


def bus_map(row: dict, live: LiveDeparture, positions: dict[str, StopPosition], now: datetime) -> dict:
    """The /api/bus/<id> answer: the journey's positioned stops up to yours
    and one after, and the bus."""
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
        "stops": stops,
        "vehicle": _vehicle(live, now, with_position=True),
        "where": strip["where"] if strip else _where(live, None, 0, None),
    }


def bus_row(live: LiveDeparture, positions: dict[str, StopPosition], now: datetime, walk: WalkTimes,
            phrases: Phrases = no_history, no_shows: NoShows = no_no_shows) -> tuple[dict, dict]:
    """A bus's row and its server-side lookup (the map and the history key).
    `now` is aware, in the board's zone. The extras that need the whole
    board (enrich.bus_extras) are added by buses_part."""
    dep = live.departure
    tz = now.tzinfo
    scheduled_at = live.leaves.replace(tzinfo=tz)
    expected_at = live.expected.replace(tzinfo=tz) if live.progress is not None else None
    late = dep.status is Status.LATE
    history = _history_entry("bus", bus_route(live), dep.scheduled,
                             f"{dep.scheduled} {dep.service} to {dep.destination}", live.leaves.date(), phrases)
    row = {
        "id": row_id("bus", live.key),
        "line": dep.service,
        "destination": dep.destination,
        "stop": dep.stop,
        "scheduled": dep.scheduled,
        "scheduled_at": iso(scheduled_at),
        "expected": expected_at.strftime("%H:%M") if expected_at else None,
        "expected_at": iso(expected_at),
        "status": dep.status.value,
        "status_text": status_text(dep),
        "minutes_late": round((live.expected - live.leaves).total_seconds() / 60) if late else None,
        "live": live.progress is not None,
        "leave_at": iso(leave_at(dep, scheduled_at, expected_at, walk)),
        "vehicle": _vehicle(live, now, with_position=False),
        "strip": _strip(live, positions),
        "history": {
            "phrase": history["phrase"],
            "no_show_phrase": no_shows(history["route"], dep.scheduled, live.leaves.date()),
        },
    }
    return row, {"bus": bus_map(row, live, positions, now), "history": history}


def timetable_block(state: str, schedule: Schedule | None, tz: tzinfo) -> dict:
    if state not in TIMETABLE_STATES:
        raise ValueError(f"Unknown timetable state {state!r}")
    return {
        "state": state,
        "built": iso(schedule.built.replace(tzinfo=tz)) if schedule is not None else None,
        "valid_until": schedule.valid_until.isoformat() if schedule and schedule.valid_until else None,
        "journeys": len(schedule.journeys) if schedule is not None else None,
    }


def stop_names(stops: dict[str, str], schedule: Schedule | None) -> dict[str, str]:
    """Each stop label's name, from the timetable's positions, else "Stop A"."""
    positions = schedule.positions if schedule is not None else {}
    return {label: positions[atco].name if atco in positions else f"Stop {label}"
            for label, atco in stops.items()}


def buses_part(board: BusBoard | None, now: datetime, status: dict, timetable: dict,
               stops: dict[str, str], walk: WalkTimes,
               phrases: Phrases = no_history, no_shows: NoShows = no_no_shows) -> tuple[dict, dict[str, dict]]:
    """The buses section and its lookups by row id. `stops` are labels to ATCO codes."""
    rows, lookups = [], {}
    schedule = board.schedule if board is not None else None
    if board is not None:
        for live in board.departures:
            row, lookup = bus_row(live, board.schedule.positions, now, walk, phrases, no_shows)
            row.update(bus_extras(live, board, now))
            rows.append(row)
            lookups[row["id"]] = lookup
    return {
        "status": status,
        "timetable": timetable,
        "stops": stop_names(stops, schedule),
        "rows": rows,
    }, lookups


def bus_observations(board: BusBoard) -> list[Observed]:
    """What a bus poll showed, for the punctuality history (naive local times)."""
    found = []
    for live in board.departures:
        dep = live.departure
        placed = live.progress is not None
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


def _sun(conditions: Conditions, today: date, tz: tzinfo) -> dict | None:
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


class Hub:
    """The latest snapshot, shared by the pollers (which publish) and the web
    server's threads (which read and wait). Safe to share between threads."""

    def __init__(self, title: str, walk: WalkTimes, clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, tz: tzinfo | None = None) -> None:
        # The board's zone, not the host's: a container's clock is in UTC.
        self._clock = clock or (lambda: datetime.now(tz) if tz else datetime.now().astimezone())
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._static = {"title": title, "walk": {"train": walk.train, "bus": dict(walk.bus)}}
        self._parts: dict[str, dict | None] = {part: None for part in PARTS}
        self._part_lookups: dict[str, dict[str, dict]] = {part: {} for part in PARTS}
        self._lookups: dict[str, dict] = {}
        self._previous: dict[str, dict] = {}
        self._version = 0
        self._canonical: str | None = None
        self._body = b""
        self._rebuilt: float | None = None
        self._touched: float | None = None
        self._closed = False
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

    def publish(self, part: str, data: dict | None, lookups: dict[str, dict] | None = None) -> int:
        """Replace one section (trains, buses, weather or disruptions) and its
        lookups by row id. Returns the version, which only goes up if anything changed."""
        if part not in PARTS:
            raise ValueError(f"Unknown part {part!r}")
        with self._lock:
            self._parts[part] = data
            self._part_lookups[part] = dict(lookups or {})
            self._rebuild()
            return self._version

    def refresh(self) -> None:
        """Rebuild with the time now and nothing else changed, so
        generated_at stays close to the server's clock when the feeds are quiet."""
        with self._lock:
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

