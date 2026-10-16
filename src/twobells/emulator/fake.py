"""Deterministic fake sources and a simulated clock, for the emulator and demos.

Statuses are drawn from a seeded RNG keyed on the service and a six-minute
time bucket, so they evolve over a simulated morning but repeat exactly for
the same seed.

The sample data is a real place, so the demo looks like a real board: trains
from Glossop to Manchester Piccadilly and buses from Glossop's Henry Street
stops. It is only sample data; nothing outside the demo and emulator uses it.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from ..board.route import RouteStop, RouteStrip, where
from ..core import calling
from ..core.model import Departure, Status, Strip, minutes_between, shift, strip_key
from ..sources.bods import Vehicle
from ..sources.darwin import CallingPoint, ServiceDetails, ServiceGone, StationCall, TrainService
from ..sources.weather import Hour

_BUCKET_MINUTES = 6


class SimClock:
    def __init__(self, start: datetime, step: timedelta = timedelta(minutes=1)) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> datetime:
        return self._now

    def advance(self) -> None:
        self._now += self._step


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _upcoming(now: datetime, headway: int, offset: int, count: int) -> list[datetime]:
    """Next `count` times at `offset` past the hour, every `headway` minutes."""
    hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    times: list[datetime] = []
    moment = hour + timedelta(minutes=offset)
    while len(times) < count:
        if moment >= now:
            times.append(moment)
        moment += timedelta(minutes=headway)
    return times


def _rng(seed: int, key: str, now: datetime) -> random.Random:
    bucket = (now.hour * 60 + now.minute) // _BUCKET_MINUTES
    return random.Random(f"{seed}|{key}|{now.date()}|{bucket}")  # noqa: S311 - sample data, not secrets


class _FakeSource:
    name: str  # each kind of source says which it is

    def __init__(self, clock, seed: int = 1, rows: int = 3, failing_when=None) -> None:
        self._clock = clock
        self._seed = seed
        self._rows = rows
        self._failing_when = failing_when
        self.failing = False

    def fetch(self) -> list[Departure]:
        now = self._clock()
        if self.failing or (self._failing_when and self._failing_when(now)):
            raise OSError(f"fake {self.name} source is failing")
        return self._departures(now)

    def _departures(self, now: datetime) -> list[Departure]:
        raise NotImplementedError


# The sample board's heading (Glossop's own board, as examples/glossop.toml has it).
DEMO_TITLE = "Glossop > Manchester Picc"

DEMO_NOTICE = (
    "Disruption between Glossop and Manchester Piccadilly: trains may be cancelled "
    "or delayed by up to 20 minutes. Check before you travel."
)


class FakeTrainSource(_FakeSource):
    """Trains, and like Darwin, the station's disruption messages while
    `notice_when(now)` holds, and each train's service ID."""

    name = "train"

    def __init__(self, clock, seed: int = 1, rows: int = 3, failing_when=None, notice_when=None) -> None:
        super().__init__(clock, seed, rows, failing_when)
        self._notice_when = notice_when
        self.messages: list[str] = []
        self.services: list[TrainService] = []

    def fetch(self) -> list[Departure]:
        departures = super().fetch()
        self.messages = [DEMO_NOTICE] if self._notice_when and self._notice_when(self._clock()) else []
        self.services = [
            TrainService(dep, f"FAKE{dep.scheduled.replace(':', '')}GLO", "Hadfield", "NT", 3, None, None,
                         _calling_points(dep))
            for dep in departures
        ]
        return departures

    def _departures(self, now: datetime) -> list[Departure]:
        departures = []
        for scheduled in _upcoming(now, headway=30, offset=12, count=self._rows):
            std = _hhmm(scheduled)
            roll = _rng(self._seed, std, now).random()
            if roll < 0.08:
                status, expected = Status.CANCELLED, None
            elif roll < 0.14:
                status, expected = Status.LATE, None
            elif roll < 0.38:
                late = _rng(self._seed, std + "late", now).randint(2, 14)
                status, expected = Status.LATE, _hhmm(scheduled + timedelta(minutes=late))
            else:
                status, expected = Status.ON_TIME, None
            calling = None if status is Status.CANCELLED else DEMO_CALLING
            departures.append(
                Departure("train", "Northern", "Manchester Piccadilly", std, expected, status, "1", calling_at=calling)
            )
        return departures


def _calling_points(dep: Departure) -> tuple[CallingPoint, ...]:
    """The stops after Glossop, as the board with calling points lists them."""
    cancelled = dep.status is Status.CANCELLED
    return tuple(CallingPoint(name, crs, shift(dep.scheduled, minutes), None, None, cancelled, 3)
                 for name, crs, minutes in _AFTER)


_BUS_ROUTES = (
    # line, destination, stop, headway, offset
    ("341", "Hyde", "A", 20, 5),
    ("237", "Ashton", "A", 30, 15),
    ("61", "Buxton", "B", 60, 35),
    ("393", "Hadfield", "B", 30, 8),
)
# The stops' names, as the bus timetable gives them: Glossop's two are both
# Henry Street, on either side of the road, so a wide screen says which.
DEMO_STOP_NAMES = {"A": "Henry Street", "B": "Henry Street"}


class FakeBusSource(_FakeSource):
    name = "bus"

    def __init__(self, clock, seed: int = 1, rows: int = 2, failing_when=None) -> None:
        super().__init__(clock, seed, rows, failing_when)

    def stop_names(self) -> dict[str, str]:
        """The stops' names by label, as BodsBusSource gives them from its timetable."""
        return dict(DEMO_STOP_NAMES)

    def live_strips(self, reuse: bool = False) -> dict[tuple[str, str, str, str], Strip]:
        """Where each bus with a tracker is, as BodsBusSource says it: along the
        demo strip's stops to yours, two minutes a stop, reaching yours when
        it is due. Fails while the bus feed does."""
        now = self._clock()
        if self.failing or (self._failing_when and self._failing_when(now)):
            raise OSError(f"fake {self.name} source is failing")
        names = tuple(stop.name for stop in DEMO_STRIP.stops)
        yours = len(names) - 1
        found = {}
        for dep in self._departures(now):
            if dep.status is Status.UNKNOWN:
                continue  # no tracker, so no position
            minutes = minutes_between(_hhmm(now), dep.departs_at) - now.second / 60
            along = yours - minutes / 2
            if along >= 0:
                at = round(min(along, yours), 3)
                found[strip_key(dep)] = Strip(where(names, at, False), len(names), yours, at)
            else:  # not on the strip yet: counted on in stops, as livebus.row_strip does
                found[strip_key(dep)] = Strip(f"{yours - math.floor(along)} stops away", len(names), yours)
        return found

    def _departures(self, now: datetime) -> list[Departure]:
        departures = []
        for line, destination, stop, headway, offset in _BUS_ROUTES:
            for scheduled in _upcoming(now, headway, offset, count=self._rows):
                aimed = _hhmm(scheduled)
                roll = _rng(self._seed, line + aimed, now).random()
                if roll < 0.3:
                    status, expected = Status.UNKNOWN, None
                elif roll < 0.55:
                    late = _rng(self._seed, line + aimed + "late", now).randint(1, 8)
                    status, expected = Status.LATE, _hhmm(scheduled + timedelta(minutes=late))
                else:
                    status, expected = Status.ON_TIME, None
                departures.append(
                    Departure("bus", line, destination, aimed, expected, status, stop=stop)
                )
        departures.sort(key=lambda d: d.departs_at)
        return departures[: self._rows]


# The fake trains' journey: (name, CRS, minutes from leaving Glossop). Each
# comes from Hadfield by Dinting, reaches Glossop a minute before it leaves,
# and goes back by Dinting to Manchester.
_BEFORE = (("Hadfield", "HDF", -8), ("Dinting", "DTG", -4))
_AFTER = (("Dinting", "DTG", 3), ("Broadbottom", "BDB", 7), ("Hattersley", "HTY", 10), ("Godley", "GDL", 12),
          ("Manchester Piccadilly", "MAN", 32))
# Their "Calling at" line, as Darwin's calling points make it.
DEMO_CALLING = calling.line([name for name, _, _ in _AFTER], coaches=3)


class FakeServiceDetails:
    """Darwin's Service Details for the fake trains: the stops a train has
    been to have their times, the rest are on time or as late as it is."""

    def __init__(self, trains: FakeTrainSource, clock) -> None:
        self._trains = trains
        self._clock = clock

    def details(self, service_id: str) -> ServiceDetails:
        service = next((s for s in self._trains.services if s.service_id == service_id), None)
        if service is None:
            raise ServiceGone("Darwin no longer knows this train")
        dep, now = service.departure, _hhmm(self._clock())
        late = dep.minutes_late

        def times(offset: int) -> tuple[str, str | None, str | None]:
            """(booked, expected, actual) at `offset` minutes from leaving Glossop."""
            booked = shift(dep.scheduled, offset)
            if late is None:
                return booked, "Delayed", None
            due = shift(booked, late)
            if minutes_between(now, due) <= 0:
                return booked, None, due  # been and gone
            return booked, "On time" if late == 0 else due, None

        def point(name: str, crs: str, offset: int) -> CallingPoint:
            return CallingPoint(name, crs, *times(offset), cancelled=False, length=3)

        sta, eta, ata = times(-1)
        std, etd, atd = times(0)
        here = StationCall("Glossop", "GLO", std, etd, atd, dep.status is Status.CANCELLED, 3,
                           sta=sta, eta=eta, ata=ata, std=std, etd=etd, atd=atd)
        return ServiceDetails(tuple(point(*p) for p in _BEFORE), tuple(point(*p) for p in _AFTER), here,
                              None, None, 3, None)


# Chance of rain by hour of the day: a wet hour from 07:00, dry otherwise.
_DEMO_RAIN = {7: 70, 8: 30}


class FakeWeatherSource:
    """An hourly forecast for the clock's day and the next, the same every day."""

    name = "weather"

    def __init__(self, clock, chances: dict[int, int] | None = None) -> None:
        self._clock = clock
        self._chances = _DEMO_RAIN if chances is None else chances

    def fetch(self) -> list[Hour]:
        midnight = self._clock().replace(hour=0, minute=0, second=0, microsecond=0)
        hours = []
        for offset in range(48):
            start = midnight + timedelta(hours=offset)
            chance = self._chances.get(start.hour, 5)
            hours.append(Hour(start, chance, 0.8 if chance >= 50 else 0.0))
        return hours


# Eastbound along High Street West, Glossop, to Henry Street Stop A (NaPTAN positions).
DEMO_STRIP = RouteStrip(
    "341",
    "A",
    (
        RouteStop("The Junction", 53.444467, -1.965966),
        RouteStop("Grapes Inn", 53.444223, -1.962774),
        RouteStop("Sunlaws Street", 53.443494, -1.959026),
        RouteStop("St Marys Road", 53.443511, -1.955759),
        RouteStop("Arundel Street", 53.443519, -1.95326),
        RouteStop("Henry Street", 53.44384, -1.948999),
    ),
)


class FakeVehicleSource:
    """Each 341 drives along the demo strip and reaches its last stop when it is due."""

    name = "vehicles"

    def __init__(self, clock, strip: RouteStrip = DEMO_STRIP, minutes_per_stop: float = 2.0) -> None:
        self._clock = clock
        self._strip = strip
        self._minutes_per_stop = minutes_per_stop

    def fetch(self) -> list[Vehicle]:
        now = self._clock()
        stops = self._strip.stops
        line, destination, _, headway, offset = _BUS_ROUTES[0]
        vehicles = []
        for due in _upcoming(now, headway, offset, count=3):
            position = (len(stops) - 1) - (due - now).total_seconds() / 60 / self._minutes_per_stop
            if position < 0:
                continue
            i = min(int(position), len(stops) - 2)
            t = position - i
            a, b = stops[i], stops[i + 1]
            bearing = math.degrees(
                math.atan2((b.lon - a.lon) * math.cos(math.radians(a.lat)), b.lat - a.lat)
            ) % 360
            vehicles.append(
                Vehicle(
                    line,
                    destination,
                    "inbound",
                    a.lat + (b.lat - a.lat) * t,
                    a.lon + (b.lon - a.lon) * t,
                    now.isoformat(),
                    bearing,
                    f"FAKE-{_hhmm(due)}",
                )
            )
        return vehicles
