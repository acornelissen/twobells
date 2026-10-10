"""The bus timetable for your stops, as the board keeps it between weekly refreshes.

Built from BODS TransXChange by timetable.py and read by livebus.py, which
adjusts it with live BODS positions. Times are minutes after midnight of the
day a journey starts (its service day), so a bus at 00:20 on a journey that
started the evening before is 1460.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

DateRange = tuple[date, date]  # first and last day, inclusive

# A journey ending this close to one of your stops counts as ending at it
# (stands and stops across the road): timetable.py keeps such journeys and
# enrich.py pairs them with departures from your stop.
NEAR_M = 150
_EARTH_M_PER_DEG = 111_320


@dataclass(frozen=True)
class Calendar:
    """When a journey runs, from its TransXChange operating profile."""

    days: frozenset[int]  # weekdays it runs, Monday = 0
    start: date | None = None  # OperatingPeriod; None is open-ended
    end: date | None = None
    # Bank holidays by TransXChange name ("ChristmasDay", "MayDay", ...).
    bank_holidays_on: frozenset[str] = frozenset()
    bank_holidays_off: frozenset[str] = frozenset()
    dates_on: tuple[DateRange, ...] = ()  # SpecialDaysOperation: extra days it runs
    dates_off: tuple[DateRange, ...] = ()  # SpecialDaysOperation: days it doesn't
    # Serviced organisations (schools): runs only on these days if any are given,
    # and never on these.
    school_days_only: tuple[DateRange, ...] = ()
    not_on_school_days: tuple[DateRange, ...] = ()


@dataclass(frozen=True)
class Call:
    """A stop the journey calls at, with its timetabled departure time."""

    atco: str
    minute: int  # minutes after midnight of the service day


@dataclass(frozen=True)
class Journey:
    line: str  # as on the bus, e.g. "341"
    operator: str  # national operator code, e.g. "BNML"
    destination: str
    journey_code: str  # TicketMachine JourneyCode; BODS sends it as DatedVehicleJourneyRef
    calls: tuple[Call, ...]  # every stop, in order; the first is the origin
    calendar: Calendar
    # Kept only to find the bus that will work a departure from your stop: it
    # ends at a stand near one of your stops without calling at any of them,
    # so it never gives a departure of its own. False in older files.
    inbound_only: bool = False

    @property
    def origin(self) -> Call:
        return self.calls[0]


@dataclass(frozen=True)
class StopPosition:
    name: str
    lat: float
    lon: float
    # The letter or word on the stop's sign ("Stop A"); "" for none, None when
    # not known (a file from before it was kept, or a timetable's own position).
    indicator: str | None = None


def metres(a: StopPosition, b: StopPosition) -> float:
    """Distance between two stops on a flat local grid; fine over a few hundred metres."""
    x = (b.lon - a.lon) * _EARTH_M_PER_DEG * math.cos(math.radians(a.lat))
    y = (b.lat - a.lat) * _EARTH_M_PER_DEG
    return math.hypot(x, y)


@dataclass(frozen=True)
class Schedule:
    built: datetime  # when it was made, board-local time
    valid_until: date | None  # the earliest OperatingPeriod end among its journeys, if any
    stops: dict[str, str]  # your stop labels to ATCO codes, as configured when built
    # Journeys calling at one of your stops, and inbound-only ones (see Journey).
    journeys: tuple[Journey, ...]
    # Positions of the stops journeys call at on the way to yours, for placing buses.
    positions: dict[str, StopPosition] = field(default_factory=dict)
    # The national operator codes whose timetables it was built from, sorted;
    # empty in a file saved before they were recorded.
    operators: tuple[str, ...] = ()
    # Which version of the builder made it; files from before this was kept are 1.
    format: int = 1

    @property
    def departure_journeys(self) -> tuple[Journey, ...]:
        """The journeys calling at your stops: those departures come from."""
        return tuple(journey for journey in self.journeys if not journey.inbound_only)

    def to_json(self) -> str:
        return json.dumps(_plain(asdict(self)), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Schedule":
        data = json.loads(text)
        return cls(
            built=datetime.fromisoformat(data["built"]),
            valid_until=date.fromisoformat(data["valid_until"]) if data.get("valid_until") else None,
            stops=dict(data["stops"]),
            journeys=tuple(_journey(j) for j in data["journeys"]),
            positions={k: StopPosition(**v) for k, v in data.get("positions", {}).items()},
            operators=tuple(data.get("operators", ())),
            format=int(data.get("format", 1)),
        )


def _plain(value):
    """asdict output with dates as ISO text and sets as sorted lists, for JSON."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _range(pair) -> DateRange:
    return date.fromisoformat(pair[0]), date.fromisoformat(pair[1])


def _calendar(data: dict) -> Calendar:
    return Calendar(
        days=frozenset(data["days"]),
        start=date.fromisoformat(data["start"]) if data.get("start") else None,
        end=date.fromisoformat(data["end"]) if data.get("end") else None,
        bank_holidays_on=frozenset(data.get("bank_holidays_on", ())),
        bank_holidays_off=frozenset(data.get("bank_holidays_off", ())),
        dates_on=tuple(_range(r) for r in data.get("dates_on", ())),
        dates_off=tuple(_range(r) for r in data.get("dates_off", ())),
        school_days_only=tuple(_range(r) for r in data.get("school_days_only", ())),
        not_on_school_days=tuple(_range(r) for r in data.get("not_on_school_days", ())),
    )


def _journey(data: dict) -> Journey:
    return Journey(
        line=data["line"],
        operator=data["operator"],
        destination=data["destination"],
        journey_code=data["journey_code"],
        calls=tuple(Call(c["atco"], c["minute"]) for c in data["calls"]),
        calendar=_calendar(data["calendar"]),
        inbound_only=_flag(data.get("inbound_only", False)),
    )


def _flag(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"Expected true or false, not {value!r}")
    return value
