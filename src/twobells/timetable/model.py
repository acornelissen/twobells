"""The bus timetable for your stops, as the board keeps it between weekly refreshes.

Built from BODS TransXChange by timetable/build.py and read by sources/livebus.py, which
adjusts it with live BODS positions. Times are minutes after midnight of the
day a journey starts (its service day), so a bus at 00:20 on a journey that
started the evening before is 1460.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from typing import TextIO

log = logging.getLogger(__name__)

DateRange = tuple[date, date]  # first and last day, inclusive

# A journey ending this close to one of your stops counts as ending at it
# (stands and stops across the road): timetable/build.py keeps such journeys and
# dashboard/enrich.py pairs them with departures from your stop.
NEAR_M = 150
_EARTH_M_PER_DEG = 111_320
# "Stop A", "Stand 12", "Bay C3" or a bare "B": a letter the street sign shows.
_SIGN_LETTER = re.compile(r"^(?:(?i:stop|stand|stance|bay)\s+)?([A-Z]{1,2}[0-9]?|[0-9]{1,2})$")
# Short words NaPTAN gives as an indicator that say where, not which stop.
_NOT_LETTERS = frozenset({"AT", "BY", "IN", "NR", "ON", "OP", "OS"})

# The journeys and what they hold have slots, not a dict each: a build holds
# thousands of journeys of up to a hundred calls (timetable/build.py's
# JOURNEY_BYTES). The saved file is the same either way.


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Call:
    """A stop the journey calls at, with its timetabled departure time."""

    atco: str
    minute: int  # minutes after midnight of the service day


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class StopPosition:
    name: str
    lat: float
    lon: float
    # The letter or word on the stop's sign ("Stop A"); "" for none, None when
    # not known (a file from before it was kept, or a timetable's own position).
    indicator: str | None = None


def sign_letter(indicator: str | None) -> str | None:
    """The letter or number on a stop's sign from NaPTAN's indicator ("Stop A"
    is A, "Stand 12" is 12), or None for a word like "opp" or none at all."""
    match = _SIGN_LETTER.match((indicator or "").strip())
    if match is None or match.group(1) in _NOT_LETTERS:
        return None
    return match.group(1)


def metres(a: StopPosition, b: StopPosition) -> float:
    """Distance between two stops on a flat local grid; fine over a few hundred metres."""
    x = (b.lon - a.lon) * _EARTH_M_PER_DEG * math.cos(math.radians(a.lat))
    y = (b.lat - a.lat) * _EARTH_M_PER_DEG
    return math.hypot(x, y)


@dataclass(frozen=True)
class Schedule:
    built: datetime  # when it was made, board-local time
    # The earliest OperatingPeriod end among its journeys, if any: when it is
    # built again (timetable.due). Only that service ends then, not the rest.
    valid_until: date | None
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
    # The road between consecutive positioned stops, for the dashboard's maps:
    # (lat, lon) points between the two stops, not including them, keyed
    # "ATCO_A>ATCO_B" in the order the bus calls (roadshape.shape_key). A pair
    # left out is drawn straight; empty in a file saved before they were kept.
    # From a file, a SavedRoads: read when first used.
    shapes: Mapping[str, tuple[tuple[float, float], ...]] = field(default_factory=dict)
    # Saved before its roads were found, or while they were stopped part way:
    # the next build finds the rest (timetable.refresh). False in older files.
    roads_pending: bool = False

    @property
    def departure_journeys(self) -> tuple[Journey, ...]:
        """The journeys calling at your stops: those departures come from."""
        return tuple(journey for journey in self.journeys if not journey.inbound_only)

    def to_json(self) -> str:
        return "".join(self.json_parts())

    def json_parts(self) -> Iterator[str]:
        """to_json in parts, a journey, stop or road at a time, so saving
        holds neither the whole text nor a copy of every journey: compact,
        with every object's keys sorted, as the file has always been."""
        yield "{"
        for index, name in enumerate(sorted(item.name for item in fields(self))):
            yield f"{',' if index else ''}{json.dumps(name)}:"
            value = getattr(self, name)
            if name == "journeys":
                yield "["
                for number, journey in enumerate(value):
                    yield f"{',' if number else ''}{_dumps(journey)}"
                yield "]"
            elif name in ("positions", "shapes"):
                yield "{"
                for number, key in enumerate(sorted(value)):
                    yield f"{',' if number else ''}{json.dumps(key)}:{_dumps(value[key])}"
                yield "}"
            else:
                yield _dumps(value)
        yield "}"

    @classmethod
    def from_json(cls, text: str) -> Schedule:
        return cls._read(_Reader(text))

    @classmethod
    def from_file(cls, file: TextIO, journeys: bool = True) -> Schedule:
        """from_json, read from an open file a piece at a time: what is held
        is the journeys made so far, sharing their equal values as a build's
        do, never the file's text or every journey's JSON. Without
        `journeys`, those and the roads are read and let go one at a time,
        each journey still checked: the rest is all the settings pages need."""
        return cls._read(_Reader(file=file), journeys)

    @classmethod
    def _read(cls, reader: _Reader, journeys: bool = True) -> Schedule:
        data = {}
        for key in reader.members():
            if key == "journeys":
                data[key] = _journeys(reader, journeys)
            elif key == "shapes" and not journeys:
                reader.skip()
            else:
                data[key] = reader.value()
        reader.end()
        return cls(
            built=datetime.fromisoformat(data["built"]),
            valid_until=date.fromisoformat(data["valid_until"]) if data.get("valid_until") else None,
            stops=dict(data["stops"]),
            journeys=data["journeys"],
            positions={k: StopPosition(**v) for k, v in data.get("positions", {}).items()},
            operators=tuple(data.get("operators", ())),
            format=int(data.get("format", 1)),
            shapes=SavedRoads(data.get("shapes", {})) if journeys else {},
            roads_pending=data.get("roads_pending") is True,
        )


class SavedRoads(Mapping):
    """A timetable file's roads (Schedule.shapes), read only when first used:
    a file can hold thousands of points, and the e-paper board, reading it
    every minute on a Pi Zero, never draws them. Read strictly: if any road
    isn't a list of finite (lat, lon) pairs, none is kept, and the maps draw
    straight lines; the timetable is still used."""

    def __init__(self, saved):
        self._saved = saved  # as json.loads gave it
        self._roads: dict[str, tuple[tuple[float, float], ...]] | None = None
        self._lock = threading.Lock()  # the dashboard's threads share a timetable

    def _read(self) -> dict[str, tuple[tuple[float, float], ...]]:
        with self._lock:
            if self._roads is None:
                try:
                    self._roads = _roads(self._saved)
                except (ValueError, TypeError) as error:
                    log.warning("Ignoring the bus timetable's roads, drawing straight lines: %s", error)
                    self._roads = {}
                self._saved = None
            return self._roads

    def __getitem__(self, key: str) -> tuple[tuple[float, float], ...]:
        return self._read()[key]

    def __iter__(self):
        return iter(self._read())

    def __len__(self) -> int:
        return len(self._read())

    def __repr__(self) -> str:
        return f"SavedRoads({self._read()!r})"


_SPACE = re.compile(r"[ \t\n\r]*")  # JSON's white space
_DECODER = json.JSONDecoder()


class _Reader:
    """A JSON document read a value at a time, from text or from a text file:
    json's own raw_decode, over what has been read of the file, topped up as
    needed. Only the value being read and a piece of the file are held.
    members() and items() go through an object or array: each value they
    stop at is to be read (value or skip) before the next."""

    _PIECE = 16 * 1024  # characters read from the file at a time, at least

    def __init__(self, text: str = "", file: TextIO | None = None) -> None:
        self._text = text
        self._at = 0  # what has been read of _text
        self._file = file  # None once it has all been read

    def _more(self) -> bool:
        """Read more of the file, letting go of what has been read: as much
        again as is held, so a long value is read in a few goes, not one
        per piece. False at its end."""
        if self._file is None:
            return False
        piece = self._file.read(max(self._PIECE, len(self._text) - self._at))
        if not piece:
            self._file = None
            return False
        self._text = self._text[self._at :] + piece
        self._at = 0
        return True

    def next(self) -> str:
        """The next character but white space, not read yet; "" at the end."""
        while True:
            space = _SPACE.match(self._text, self._at)  # always matches, if only nothing
            self._at = space.end() if space else self._at
            if self._at < len(self._text) or not self._more():
                return self._text[self._at : self._at + 1]

    def value(self):
        """The next value, as json.loads makes it."""
        self.next()
        while True:
            try:
                value, end = _DECODER.raw_decode(self._text, self._at)
            except json.JSONDecodeError:
                if self._more():  # cut off where the piece read ends
                    continue
                raise
            if end < len(self._text) or not self._more():  # a number ending there may go on
                self._at = end
                return value

    def skip(self) -> None:
        """Read the next value and let it go, a member or item at a time."""
        if self.next() == "{":
            for _ in self.members():
                self.value()
        elif self.next() == "[":
            for _ in self.items():
                self.value()
        else:
            self.value()

    def _take(self, char: str) -> bool:
        if self.next() == char:
            self._at += 1
            return True
        return False

    def _expect(self, char: str) -> None:
        if not self._take(char):
            raise ValueError(f"Expected {char!r} in the bus timetable file")

    def members(self) -> Iterator[str]:
        """Each key of the object next, at its value."""
        self._expect("{")
        if self._take("}"):
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("A key in the bus timetable file isn't text")
            self._expect(":")
            yield key
            if self._take("}"):
                return
            self._expect(",")

    def items(self) -> Iterator[None]:
        """Stop at each item of the array next."""
        self._expect("[")
        if self._take("]"):
            return
        while True:
            yield
            if self._take("]"):
                return
            self._expect(",")

    def end(self) -> None:
        """Nothing but white space is left."""
        if self.next():
            raise ValueError("More than a timetable in the bus timetable file")


def _dumps(value) -> str:
    return json.dumps(_plain(value), separators=(",", ":"), sort_keys=True)


def _plain(value):
    """Dataclasses as dicts, dates as ISO text and sets as sorted lists, for JSON."""
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
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


def _journeys(reader: _Reader, keep: bool) -> tuple[Journey, ...]:
    """The journeys in the array next, made one at a time; each still made,
    and let go, without `keep`."""
    if reader.next() != "[":
        raise TypeError("The journeys must be a list")
    shared: dict = {}

    def one(value):
        """One of each equal value, as a build shares them (build._Shared)."""
        return shared.setdefault(value, value)

    found = []
    for _ in reader.items():
        journey = _journey(reader.value(), one)
        if keep:
            found.append(journey)
    return tuple(found)


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


def _journey(data: dict, one: Callable = lambda value: value) -> Journey:
    """A journey as saved, holding `one` of each of its stops, minutes,
    calendar and names (never of inbound_only: True would be taken for 1)."""
    return Journey(
        line=one(data["line"]),
        operator=one(data["operator"]),
        destination=one(data["destination"]),
        journey_code=data["journey_code"],
        calls=tuple(Call(one(c["atco"]), one(c["minute"])) for c in data["calls"]),
        calendar=one(_calendar(data["calendar"])),
        inbound_only=_flag(data.get("inbound_only", False)),
    )


def _roads(saved) -> dict[str, tuple[tuple[float, float], ...]]:
    if not isinstance(saved, dict):
        raise TypeError("The roads must be a mapping")
    return {str(key): tuple(_point(p) for p in points) for key, points in saved.items()}


def _point(pair) -> tuple[float, float]:
    lat, lon = (float(v) for v in pair)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("A road point must be two finite numbers")
    return lat, lon


def _flag(value) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"Expected true or false, not {value!r}")
    return value
