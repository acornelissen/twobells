"""The bus timetable for your stops, as the board keeps it between weekly refreshes.

Built from BODS TransXChange by timetable.py and read by livebus.py, which
adjusts it with live BODS positions. Times are minutes after midnight of the
day a journey starts (its service day), so a bus at 00:20 on a journey that
started the evening before is 1460.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

DateRange = tuple[date, date]  # first and last day, inclusive


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

    @property
    def origin(self) -> Call:
        return self.calls[0]


@dataclass(frozen=True)
class StopPosition:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Schedule:
    built: datetime  # when it was made, board-local time
    valid_until: date | None  # the earliest OperatingPeriod end among its journeys, if any
    stops: dict[str, str]  # your stop labels to ATCO codes, as configured when built
    journeys: tuple[Journey, ...]  # only journeys calling at one of your stops
    # Positions of the stops journeys call at on the way to yours, for placing buses.
    positions: dict[str, StopPosition] = field(default_factory=dict)
    # The national operator codes whose timetables it was built from, sorted;
    # empty in a file saved before they were recorded.
    operators: tuple[str, ...] = ()

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
    )
