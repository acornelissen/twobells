"""The bus timetable for your stops, built from BODS TransXChange.

The way bustimes.org does it: every timetabled journey that calls at one of
your stops, with the time it leaves each stop on its way, and the days it
runs. sources/livebus.py then adjusts it with live positions.

Inbound-only journeys. A bus often comes in to a stand near your stop
without calling at it, then works a departure from your stop. To find that
bus (enrich.inbound), journeys of the lines departing from your stops that
end within NEAR_M of one of them, without calling at any, are kept too,
marked inbound_only: they never give departures. They are looked for only in
documents that mention your stops (a document holds a whole service, both
ways), among the lines calling at your stops in that document. Distances
are by NaPTAN's positions, or the timetables' own where NaPTAN has none.
To hold fewer journeys while reading, a journey is dropped as it is read
when the timetables place its last stop further than NEAR_M from all of
your stops; when they can't tell, it is held for NaPTAN to measure only if
its last stop is in the same ATCO area (the first three characters) as one
of yours.

Built about once a week by a timer, the command line or the dashboard's
upkeep, and by the setup search, never from the minutely board run: an
operator's timetables can run to hundreds of megabytes. Each download waits
on disk and its documents are parsed one at a time, so a Pi Zero holds one
document in memory, never the lot. One too big for the install to read or
parse (routesetup.DocumentLimits, smaller on a Pi) is skipped with a warning
naming it, and the rest are read.

One build at a time. Every build goes through build(), which holds a lock
file in the store (LOCK_NAME) while it runs, let go by the system if the
process dies. A build that finds it held is skipped (TimetableBusy), except
the setup search's, which waits for it: the timetable it saves must be the
one for what it found.

Roads. TransXChange gives no road between stops, so once a new timetable is
saved, refresh asks the OpenStreetMap car router for each pair of consecutive
positioned stops, once, and saves it again with the roads for the
dashboard's maps (add_roads, timetable/roads.py). A road the last timetable has
between the same two stops, neither of them moved, is kept rather than asked
for again. A pair it can't find is drawn straight; the router never costs a
timetable. Until its roads are found the saved timetable says so
(roads_pending), so if they are stopped part way (the wizard's search
cancelled, the process stopped or started again) the next build finds the
rest, due or not. While they are being found, how far they have got is kept
in ROADS_FILE for the settings site (roads_progress).

Times. A journey's DepartureTime is when it leaves its first stop. Each
timing link's RunTime takes it to the next stop, where it may wait before
leaving: a WaitTime on the From of the link leaving a stop, or else one on
the To of the link arriving there (the From wins if both are given; they
describe the same wait, as bustimes.org reads them). A WaitTime on the From
of the first link is ignored, since DepartureTime is already the departure.
A call's minute is when the bus leaves that stop, or reaches it for the
last stop. Times are worked out in seconds and each call's seconds dropped
(rounded down), as printed timetables do, so a bus is never shown later
than timetabled and half-minutes don't add up along the route. Minutes run
past 1440 after midnight.

Versions. BODS keeps an old version of a service (its ServiceCode, line and
operator) beside the one replacing it, often open-ended, so both would run.
As bustimes.org does, a version ends the day before a later open-ended one
starts. A later one with an end (roadworks, say) only switches the earlier
one off while it runs, as days of non-operation, so the earlier one runs
again after it. Of two starting the same day, the higher RevisionNumber wins.

Calendars. runs_on() decides, in this order:
  1. outside the service's OperatingPeriod: no;
  2. a SpecialDaysOperation day of non-operation: no; a day of operation: yes;
  3. a bank holiday the journey doesn't run on: no; one it does run on: yes.
     A bank holiday listed in neither follows the regular days, as
     TransXChange says;
  4. school days (ServicedOrganisation working days or holidays): outside the
     days it runs only on, or inside the days it never runs on: no;
  5. otherwise the regular days of the week.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Set
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit
from xml.etree import ElementTree

from ..core import files, net, safexml
from ..core.errors import safe_message
from ..sources.bods import redact_key
from . import roads as roadshape
from . import routesetup
from .model import NEAR_M, Calendar, Call, DateRange, Journey, Schedule, StopPosition, metres
from .routesetup import DEFAULT_LIMITS, DocumentLimits

log = logging.getLogger(__name__)

FILE_NAME = "bus-timetable.json"  # beside state.json
LOCK_NAME = "timetable.lock"  # beside it; held while any build runs
LOCK_WAIT_S = 1.0  # between looks at a held lock, when waiting for it
ROADS_FILE = "bus-roads.json"  # beside it while its roads are being found: how far they have got
ROADS_EVERY_S = 5.0  # written at most this often (and the last), to spare an SD card
ROADS_STALE_S = 60.0  # unwritten this long, whatever was finding them has stopped
_ROADS_FILE_BYTES = 200
FAILED_FILE = "bus-timetable-failed.json"  # beside it after a build fails: when, and for what (see refresh)
RETRY_AFTER = timedelta(hours=6)  # a failed build waits this long before it is tried again
_FAILED_FILE_BYTES = 200
# The builder's version, saved in the file: an older one is rebuilt (see due).
# 2: stops keep the letter on their sign.
# 3: the roads between stops, for the dashboard's maps (shapes).
FORMAT = 3
MAX_AGE = timedelta(days=7)
# Positions are kept for this many calls before each of your stops, and the one
# after: livebus places buses along that stretch and sees them pass your stop.
CALLS_BEFORE = 20
CALLS_AFTER = 1
# Inbound-only journeys kept, at most, and no more than half the install's
# limit of those calling at your stops (inbound_limit); beyond it the rest are
# left out. Most are dropped once NaPTAN has placed their last stop, so this
# counts them before that, while they are all held at once (a Pi Zero builds
# this too).
MAX_INBOUND_JOURNEYS = 10_000
# The most memory each journey found takes, held until the timetable is
# saved, that the installs' limits are sized by (settings/installs.py):
# measured with tracemalloc on BODS data set 2100 (First South Yorkshire), its
# largest documents' journeys, of 80 calls on average, took 14.6 KB each on
# Python 3.12 and 15.6 KB on 3.11; of 24 calls, about 7 KB.
JOURNEY_BYTES = 16_000
MAX_FILE_BYTES = 50_000_000
DOWNLOAD_TIMEOUT = 60.0  # seconds without data before a download is given up
_TEXT_LIMIT = 80  # line names, destinations, codes
_LATEST = 2 * 24 * 3600  # a journey still running two days after its service day is nonsense
# A clock this far behind a timetable's build time was wrong when it was built
# (a Pi has no clock battery), so its age can't be trusted: build again.
_SKEW = timedelta(days=1)

FindDatasets = Callable[[str, str, "str | None"], list]  # (api_key, noc, line) -> links, as routesetup.dataset_urls


class Writable(Protocol):
    """Where a download is written: an open binary file, or one that meters it."""

    def write(self, data: bytes, /) -> int: ...


Download = Callable[[str, Writable], None]  # (link, file to write it to)
# atcos -> {atco: (name, lat, lon[, sign letter])}, as routesetup.naptan_stop_details
Naptan = Callable[[Iterable[str]], dict]
# ({(atco, atco): ((lat, lon), (lat, lon))}, progress=...) -> {(atco, atco): [(lat, lon), ...]},
# as roadshape.segment_shapes
Route = Callable[..., Mapping]
Progress = Callable[[int, int], None]  # (done, total)

_NS = {"t": "http://www.transxchange.org.uk/"}
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.\d+)?S)?)?$")
_TIME = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_EVERY_DAY = frozenset(range(7))
_DAYS = {
    "Monday": {0},
    "Tuesday": {1},
    "Wednesday": {2},
    "Thursday": {3},
    "Friday": {4},
    "Saturday": {5},
    "Sunday": {6},
    "MondayToThursday": {0, 1, 2, 3},
    "MondayToFriday": {0, 1, 2, 3, 4},
    "MondayToSaturday": {0, 1, 2, 3, 4, 5},
    "MondayToSunday": set(_EVERY_DAY),
    "Weekend": {5, 6},
    **{f"Not{name}": set(_EVERY_DAY) - {day} for day, name in enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))},
}

# TransXChange's groups of bank holidays, by the names holidays.holiday_names gives.
_HOLIDAY_MONDAYS = {"EasterMonday", "MayDay", "SpringBank", "LateSummerBankHolidayNotScotland",
                    "AugustBankHolidayScotland"}
_DISPLACEMENT = {"ChristmasDayHoliday", "BoxingDayHoliday", "NewYearsDayHoliday",
                 "Jan2ndScotlandHoliday", "StAndrewsDayHoliday"}
_ALL_BANK_HOLIDAYS = {"ChristmasDay", "BoxingDay", "GoodFriday", "NewYearsDay", "Jan2ndScotland",
                      "StAndrewsDay"} | _HOLIDAY_MONDAYS | _DISPLACEMENT
_HOLIDAY_GROUPS = {
    "AllBankHolidays": _ALL_BANK_HOLIDAYS,
    "AllHolidaysExceptChristmas": _ALL_BANK_HOLIDAYS
    - {"ChristmasDay", "BoxingDay", "ChristmasDayHoliday", "BoxingDayHoliday"},
    "Christmas": {"ChristmasDay", "BoxingDay"},
    "EarlyRunOff": {"ChristmasEve", "NewYearsEve"},
    "HolidayMondays": _HOLIDAY_MONDAYS,
    "DisplacementHolidays": _DISPLACEMENT,
}

class TimetableError(Exception):
    pass


class TimetableBusy(TimetableError):
    """Another build holds the lock, so this one didn't run."""


class TimetableTooBig(TimetableError):
    """A timetable larger than load() reads (MAX_FILE_BYTES), so not saved."""


# Reading TransXChange.


def _tag(node) -> str:
    return node.tag.rpartition("}")[2]


def _text(node, path: str) -> str:
    return (node.findtext(path, "", _NS) or "").strip()[:_TEXT_LIMIT]


def _seconds(text: str | None) -> int | None:
    """Seconds in an ISO 8601 duration such as PT1M30S; None if it isn't one."""
    match = _DURATION.match((text or "").strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _time(text: str) -> int | None:
    """Seconds after midnight in a time such as 06:20:00."""
    match = _TIME.match(text)
    if not match:
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return (hours * 60 + minutes) * 60 + seconds


def _date(text: str) -> date | None:
    if not _DATE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _ranges(parent) -> tuple[DateRange, ...]:
    """The DateRanges in an element; one without an end is a single day."""
    found = []
    for node in parent.iterfind("t:DateRange", _NS) if parent is not None else ():
        start = _date(_text(node, "t:StartDate"))
        end = _date(_text(node, "t:EndDate")) or start
        if start and end and start <= end:
            found.append((start, end))
    return tuple(found)


class _Link(NamedTuple):
    id: str
    origin: str
    destination: str
    run: int
    origin_wait: int | None  # None when the From gives no WaitTime
    destination_wait: int


def _link(node, base: _Link | None = None) -> _Link:
    """A timing link; a VehicleJourneyTimingLink overrides only what it gives."""
    run = _seconds(node.findtext("t:RunTime", None, _NS))
    origin_wait = _seconds(node.findtext("t:From/t:WaitTime", None, _NS))
    destination_wait = _seconds(node.findtext("t:To/t:WaitTime", None, _NS))
    if base is not None:
        return base._replace(
            run=base.run if run is None else run,
            origin_wait=base.origin_wait if origin_wait is None else origin_wait,
            destination_wait=base.destination_wait if destination_wait is None else destination_wait,
        )
    return _Link(
        node.get("id", ""),
        _text(node, "t:From/t:StopPointRef"),
        _text(node, "t:To/t:StopPointRef"),
        run or 0,
        origin_wait,
        destination_wait or 0,
    )


def _calls(links: list[_Link], departure: int) -> tuple[Call, ...] | None:
    """Each stop with the minute the bus leaves it (reaches it, for the last);
    None if the times are nonsense."""
    calls = []
    time = departure
    arriving_wait = 0
    for index, link in enumerate(links):
        if index:
            time += arriving_wait if link.origin_wait is None else link.origin_wait
        calls.append(Call(link.origin, time // 60))
        time += link.run
        arriving_wait = link.destination_wait
    calls.append(Call(links[-1].destination, time // 60))
    return tuple(calls) if time < _LATEST else None


@dataclass
class _Service:
    code: str  # ServiceCode
    lines: dict[str, str]  # Line id -> LineName
    start: date | None
    end: date | None
    profile: object  # its OperatingProfile element, or None
    operator_ref: str
    calendar: Calendar | None = None  # worked out when first needed


@dataclass
class _Pattern:
    service: _Service
    sections: list[str]
    destination: str
    operator_ref: str
    links: list[_Link] | None = None


@dataclass
class _Document:
    names: dict[str, str] = field(default_factory=dict)  # atco -> CommonName
    positions: dict[str, StopPosition] = field(default_factory=dict)
    operators: dict[str, str] = field(default_factory=dict)  # Operator id -> national operator code
    schools: dict[str, tuple[tuple[DateRange, ...], tuple[DateRange, ...]]] = field(default_factory=dict)
    sections: dict[str, list[_Link]] = field(default_factory=dict)
    patterns: dict[str, _Pattern] = field(default_factory=dict)
    services: dict[str, _Service] = field(default_factory=dict)
    revision: int = 0  # the document's RevisionNumber


class _Version(NamedTuple):
    """Which version of which service a journey came from (see _until_replaced)."""

    service: str  # ServiceCode; "" when the document gives none
    revision: int


def _sign(text) -> str:
    """A stop's sign letter or word as NaPTAN gives it ("Stop A", "Opp"):
    printable, short, "" for none."""
    cleaned = "".join(ch for ch in str(text) if ch.isprintable()).strip()
    return cleaned[:20]


def _position(name: str, lat: str, lon: str) -> StopPosition | None:
    try:
        lat_value, lon_value = float(lat), float(lon)
    except ValueError:
        return None
    if not (math.isfinite(lat_value) and math.isfinite(lon_value)):
        return None
    if not (-90 <= lat_value <= 90 and -180 <= lon_value <= 180):
        return None
    return StopPosition(name, lat_value, lon_value)


# Each reader below takes one element of a document: the TransXChange
# element named, inside the section of the root named (StopPoints and so on).

def _read_stop_ref(node, doc: _Document) -> None:
    """A StopPoints/AnnotatedStopPointRef: a stop's name and, if given, position."""
    atco, name = _text(node, "t:StopPointRef"), _text(node, "t:CommonName")
    doc.names[atco] = name
    position = _position(name, _text(node, "t:Location/t:Latitude"), _text(node, "t:Location/t:Longitude"))
    if position is not None:
        doc.positions[atco] = position


def _stop_point_name(node) -> tuple[str, str]:
    """(ATCO code, name) of a StopPoints/StopPoint, whose name wins over an
    AnnotatedStopPointRef's for the same stop."""
    return _text(node, "t:AtcoCode"), _text(node, "t:Descriptor/t:CommonName")


def _read_operator(node, doc: _Document) -> None:
    """Any element in Operators."""
    code = _text(node, "t:NationalOperatorCode") or _text(node, "t:OperatorCode")
    if code:
        doc.operators[node.get("id", "")] = code


def _read_school(node, doc: _Document) -> None:
    """A ServicedOrganisations/ServicedOrganisation."""
    doc.schools[_text(node, "t:OrganisationCode")] = (
        _ranges(node.find("t:WorkingDays", _NS)),
        _ranges(node.find("t:Holidays", _NS)),
    )


def _read_section(node, doc: _Document) -> None:
    """A JourneyPatternSections/JourneyPatternSection."""
    doc.sections[node.get("id", "")] = [_link(link) for link in node.iterfind("t:JourneyPatternTimingLink", _NS)]


def _read_service(node, doc: _Document) -> None:
    """A Services/Service, with its journey patterns."""
    service = _Service(
        code=_text(node, "t:ServiceCode"),
        lines={line.get("id", ""): _text(line, "t:LineName") for line in node.iterfind("t:Lines/t:Line", _NS)},
        start=_date(_text(node, "t:OperatingPeriod/t:StartDate")),
        end=_date(_text(node, "t:OperatingPeriod/t:EndDate")),
        profile=node.find("t:OperatingProfile", _NS),
        operator_ref=_text(node, "t:RegisteredOperatorRef"),
    )
    doc.services[service.code] = service
    for pattern in node.iterfind("t:StandardService/t:JourneyPattern", _NS):
        doc.patterns[pattern.get("id", "")] = _Pattern(
            service,
            [(ref.text or "").strip() for ref in pattern.iterfind("t:JourneyPatternSectionRefs", _NS)],
            _text(pattern, "t:DestinationDisplay"),
            _text(pattern, "t:OperatorRef"),
        )


def _school_rules(profile, doc: _Document) -> tuple[tuple[DateRange, ...], tuple[DateRange, ...], bool]:
    """(only on, never on, nothing known) from a ServicedOrganisationDayType.

    Each organisation gives its WorkingDays (school days) and may give its
    Holidays. Running on working days means only within them; not running on
    them means never within them; holidays the same way round. When only the
    other list is given, it is used inverted: not running in the holidays
    means only on the working days, and so on."""
    only, never, unknown = [], [], False
    node = profile.find("t:ServicedOrganisationDayType", _NS)
    if node is None:
        return (), (), False
    for kind, runs in (("DaysOfOperation", True), ("DaysOfNonOperation", False)):
        for group, working in (("WorkingDays", True), ("Holidays", False)):
            refs = node.findall(f"t:{kind}/t:{group}/t:ServicedOrganisationRef", _NS)
            for ref in refs:
                days, holidays = doc.schools.get((ref.text or "").strip(), ((), ()))
                same, other = (days, holidays) if working else (holidays, days)
                if same:
                    (only if runs else never).extend(same)
                elif other:
                    (never if runs else only).extend(other)
                elif runs:
                    unknown = True
    return tuple(only), tuple(never), unknown


def _holiday_names(parent) -> tuple[set[str], tuple[DateRange, ...]]:
    """Bank holiday names in a DaysOfOperation or DaysOfNonOperation, with
    groups expanded, and any OtherPublicHoliday dates."""
    names, dates = set(), []
    for node in parent if parent is not None else ():
        name = _tag(node)
        if name == "OtherPublicHoliday":
            day = _date(_text(node, "t:Date"))
            if day:
                dates.append((day, day))
        else:
            names |= _HOLIDAY_GROUPS.get(name, {name})
    return names, tuple(dates)


def _calendar(profile, service: _Service, doc: _Document) -> Calendar:
    """When a journey runs. With no OperatingProfile at all it runs every day;
    a profile without regular days runs only on the days it lists."""
    if profile is None:
        return Calendar(days=_EVERY_DAY, start=service.start, end=service.end)
    days = set()
    for node in profile.iterfind("t:RegularDayType/t:DaysOfWeek/*", _NS):
        days |= _DAYS.get(_tag(node), set())
    special = profile.find("t:SpecialDaysOperation", _NS)
    dates_on = _ranges(special.find("t:DaysOfOperation", _NS)) if special is not None else ()
    dates_off = _ranges(special.find("t:DaysOfNonOperation", _NS)) if special is not None else ()
    bank = profile.find("t:BankHolidayOperation", _NS)
    on, other_on = _holiday_names(bank.find("t:DaysOfOperation", _NS) if bank is not None else None)
    off, other_off = _holiday_names(bank.find("t:DaysOfNonOperation", _NS) if bank is not None else None)
    only, never, unknown = _school_rules(profile, doc)
    if unknown:
        days = set()  # school days only, but the timetable doesn't say which: none
    return Calendar(
        days=frozenset(days),
        start=service.start,
        end=service.end,
        bank_holidays_on=frozenset(on),
        bank_holidays_off=frozenset(off),
        dates_on=dates_on + other_on,
        dates_off=dates_off + other_off,
        school_days_only=only,
        not_on_school_days=never,
    )


def _pattern_links(pattern: _Pattern, doc: _Document) -> list[_Link]:
    if pattern.links is None:
        pattern.links = [link for ref in pattern.sections for link in doc.sections.get(ref, [])]
    return pattern.links


def _journey(
    node, doc: _Document, atcos: Collection[str], noc: str, pattern_refs: dict, stands: Collection[str] = ()
) -> tuple[_Version, Journey] | None:
    """The journey a VehicleJourney describes, with the version of the service
    it is in, if it calls at one of `atcos`, or, marked inbound_only, if it
    doesn't but ends at one of `stands`."""
    ref = _text(node, "t:JourneyPatternRef") or pattern_refs.get(_text(node, "t:VehicleJourneyRef"), "")
    pattern = doc.patterns.get(ref)
    if pattern is None:
        return None
    links = _pattern_links(pattern, doc)
    if not links:
        return None
    calling = any(link.origin in atcos or link.destination in atcos for link in links)
    if not calling and links[-1].destination not in stands:
        return None
    departure = _time(_text(node, "t:DepartureTime"))
    if departure is None:
        return None
    shift = _text(node, "t:DepartureDayShift")
    departure += 24 * 3600 * (int(shift) if shift.isdigit() else 0)
    overrides = {
        _text(timing, "t:JourneyPatternTimingLinkRef"): timing
        for timing in node.iterfind("t:VehicleJourneyTimingLink", _NS)
    }
    if overrides:
        links = [_link(overrides[link.id], link) if link.id in overrides else link for link in links]
    calls = _calls(links, departure)
    if calls is None:
        return None
    service = doc.services.get(_text(node, "t:ServiceRef"), pattern.service)
    profile = node.find("t:OperatingProfile", _NS)
    if profile is not None:
        calendar = _calendar(profile, service, doc)
    else:
        if service.calendar is None:
            service.calendar = _calendar(service.profile, service, doc)
        calendar = service.calendar
    line = service.lines.get(_text(node, "t:LineRef")) or next(iter(service.lines.values()), "")
    operator = (
        doc.operators.get(_text(node, "t:OperatorRef"))
        or doc.operators.get(pattern.operator_ref)
        or doc.operators.get(service.operator_ref)
        or noc
    )
    last = calls[-1].atco
    destination = _text(node, "t:DestinationDisplay") or pattern.destination or doc.names.get(last) or last
    code = _text(node, "t:Operational/t:TicketMachine/t:JourneyCode") or _text(node, "t:VehicleJourneyCode")
    journey = Journey(line, operator, destination, code, calls, calendar, inbound_only=not calling)
    return _Version(service.code, doc.revision), journey


def _stands(doc: _Document, atcos: Collection[str], yours: Mapping[str, StopPosition]) -> set[str]:
    """The stops in a document where a journey that doesn't call at your
    stops may end near one: within NEAR_M of one by the timetables' positions
    (`yours` are those known from earlier documents), or, while that can't be
    told, in the same ATCO area as one, to be measured once NaPTAN has placed
    it (see the module's notes)."""
    placed = {**yours, **{atco: doc.positions[atco] for atco in atcos if atco in doc.positions}}
    areas = {atco[:3] for atco in atcos}
    found = set()
    for atco in doc.names:
        if atco in atcos:
            continue
        here = doc.positions.get(atco)
        near = here is not None and any(metres(here, there) <= NEAR_M for there in placed.values())
        unplaced = (here is None or len(placed) < len(atcos)) and atco[:3] in areas
        if near or unplaced:
            found.add(atco)
    return found


_TXC = "{" + _NS["t"] + "}"  # as in an element's tag
# The sections of a document read first, then the one read again for its journeys.
_FIRST_PASS = frozenset(_TXC + name for name in (
    "StopPoints", "Operators", "ServicedOrganisations", "JourneyPatternSections", "Services", "VehicleJourneys"))
_SECOND_PASS = frozenset({_TXC + "VehicleJourneys"})
_CHUNK = 64 * 1024  # bytes of a document parsed at a time; the elements finished in them are held together


def _elements(xml: bytes, sections: Collection[str]) -> Iterator[tuple[str, ElementTree.Element]]:
    """safexml.iter_grandchildren over a document held in memory."""
    chunks = (xml[start : start + _CHUNK] for start in range(0, len(xml), _CHUNK))
    try:
        yield from safexml.iter_grandchildren(chunks, sections)
    except safexml.DoctypeError:
        raise TimetableError("Timetable file has a DOCTYPE; refusing to parse it") from None
    except SyntaxError as error:  # ElementTree.ParseError
        raise TimetableError(f"Timetable file is not XML: {error}") from None


def _read_first_pass(xml: bytes) -> tuple[_Document, set[str]]:
    """Everything in a document but its journeys, and the codes of the
    journeys whose pattern others take: a journey without a
    JourneyPatternRef takes that of the journey its VehicleJourneyRef names."""
    doc = _Document()
    point_names: dict[str, str] = {}
    referenced: set[str] = set()
    for section, node in _elements(xml, _FIRST_PASS):
        name = section.removeprefix(_TXC)
        tag = node.tag.removeprefix(_TXC)
        if not section:  # the root
            revision = node.get("RevisionNumber", "").strip()
            doc.revision = int(revision) if revision.isdigit() and len(revision) < 10 else 0
        elif name == "StopPoints" and tag == "AnnotatedStopPointRef":
            _read_stop_ref(node, doc)
        elif name == "StopPoints" and tag == "StopPoint":
            atco, common_name = _stop_point_name(node)
            point_names[atco] = common_name
        elif name == "Operators":
            _read_operator(node, doc)
        elif name == "ServicedOrganisations" and tag == "ServicedOrganisation":
            _read_school(node, doc)
        elif name == "JourneyPatternSections" and tag == "JourneyPatternSection":
            _read_section(node, doc)
        elif name == "Services" and tag == "Service":
            _read_service(node, doc)
        elif name == "VehicleJourneys" and tag == "VehicleJourney" and not _text(node, "t:JourneyPatternRef"):
            referenced.add(_text(node, "t:VehicleJourneyRef"))
    doc.names.update(point_names)
    return doc, referenced


def _pattern_refs(xml: bytes, codes: Set[str]) -> dict[str, str]:
    """The JourneyPatternRef of each journey whose VehicleJourneyCode is one
    of `codes`, read in a pass of its own only if there are any: most
    journeys give their own, and holding every journey's would take memory
    in step with the document."""
    refs: dict[str, str] = {}
    if codes:
        for node in _vehicle_journeys(xml):
            code = _text(node, "t:VehicleJourneyCode")
            if code in codes:
                refs[code] = _text(node, "t:JourneyPatternRef")
    return refs


def _vehicle_journeys(xml: bytes) -> Iterator[ElementTree.Element]:
    """Each VehicleJourney in a document, one at a time."""
    for section, node in _elements(xml, _SECOND_PASS):
        if section and node.tag == _TXC + "VehicleJourney":
            yield node


def inbound_limit(limits: DocumentLimits) -> int:
    """The most inbound-only journeys a build with these limits holds."""
    return min(MAX_INBOUND_JOURNEYS, limits.journeys // 2)


@dataclass
class _Room:
    """How many journeys a build may hold, counted as each is found, so a
    document with more than the memory there is fails cleanly: those calling
    at your stops (`journeys`; one more is a TimetableError) and inbound-only
    ones (`inbound`; the rest are left out, and counted). `held` and
    `held_inbound` are those kept from the documents before."""

    journeys: int = sys.maxsize
    inbound: int = sys.maxsize
    held: int = 0
    held_inbound: int = 0
    left_out: int = 0


def _journeys_kept(
    vehicle_journeys: Iterable, doc: _Document, atcos: Collection[str], noc: str, pattern_refs: dict,
    stands: Collection[str], room: _Room | None = None,
) -> list[tuple[_Version, Journey]]:
    """The journeys calling at your stops, and the inbound-only ones of the
    lines among them (see the module's notes), each counted against `room`
    as it is found (no limit without it)."""
    room = room or _Room()
    found = []
    calling, inbound = room.held, room.held_inbound
    for node in vehicle_journeys:
        read = _journey(node, doc, atcos, noc, pattern_refs, stands)
        if read is None:
            continue
        if read[1].inbound_only:
            if inbound >= room.inbound:
                room.left_out += 1
                continue
            inbound += 1
        elif any(call.atco in atcos for call in read[1].calls):
            calling += 1
            if calling > room.journeys:
                raise TimetableError(f"More than {room.journeys} journeys call at your stops; refusing")
        else:
            continue
        found.append(read)
    lines = {(journey.line.casefold(), journey.operator) for _, journey in found if not journey.inbound_only}
    return [(v, j) for v, j in found if not j.inbound_only or (j.line.casefold(), j.operator) in lines]


def _read_document(
    xml: bytes, atcos: Collection[str], noc: str, yours: Mapping[str, StopPosition] | None = None,
    room: _Room | None = None,
) -> tuple[list[tuple[_Version, Journey]], dict[str, StopPosition]]:
    """The journeys in a document calling at your stops, each with the version
    of the service it is in, and the document's stop positions. With `yours`
    (your stops' positions known so far), also its inbound-only journeys of
    the lines calling there (see the module's notes). As many as `room`
    allows (_Room; no limit without it).

    It is read in two passes over the bytes, each element built and dropped
    in turn (safexml.iter_grandchildren), never the whole tree: first all but
    the journeys, as a journey refers to patterns, services and other
    journeys anywhere in it; then the journeys, keeping only those found. A
    journey taking its pattern from another adds a pass between them
    (_pattern_refs). Memory follows what is kept, not the document
    (routesetup.DocumentLimits), and what is kept is counted as it is found."""
    doc, referenced = _read_first_pass(xml)
    pattern_refs = _pattern_refs(xml, referenced)
    stands = _stands(doc, atcos, yours) if yours is not None else set()
    found = _journeys_kept(_vehicle_journeys(xml), doc, atcos, noc, pattern_refs, stands, room)
    return found, doc.positions


def _parse_document(
    xml: bytes, atcos: Collection[str], noc: str, yours: Mapping[str, StopPosition] | None = None,
    room: _Room | None = None,
) -> tuple[list[Journey], dict[str, StopPosition]]:
    """_read_document without the versions."""
    found, positions = _read_document(xml, atcos, noc, yours, room)
    return [journey for _, journey in found], positions


def parse_journeys(xml: bytes, atcos: Collection[str], noc: str = "") -> list[Journey]:
    """Every journey in one TransXChange document that calls at one of `atcos`,
    whether it starts, passes or ends there. `noc` is the operator code to use
    when the document doesn't give one."""
    return _parse_document(xml, set(atcos), noc)[0]


# When journeys run.


def _within(day: date, ranges: Iterable[DateRange]) -> bool:
    return any(start <= day <= end for start, end in ranges)


def runs_on(calendar: Calendar, day: date, holiday_names: Mapping[date, Set[str]] = {}) -> bool:
    """Whether a journey with this calendar runs on `day`, its service day (the
    day it starts). `holiday_names` gives each day's TransXChange bank holiday
    names, from holidays.holiday_names. See the module's notes for the order."""
    if (calendar.start and day < calendar.start) or (calendar.end and day > calendar.end):
        return False
    if _within(day, calendar.dates_off):
        return False
    if _within(day, calendar.dates_on):
        return True
    names = holiday_names.get(day, ())
    if calendar.bank_holidays_off.intersection(names):
        return False
    if calendar.bank_holidays_on.intersection(names):
        return True
    if calendar.school_days_only and not _within(day, calendar.school_days_only):
        return False
    if _within(day, calendar.not_on_school_days):
        return False
    return day.weekday() in calendar.days


def journeys_on(schedule: Schedule, day: date, holiday_names: Mapping[date, Set[str]] = {}) -> list[Journey]:
    """The schedule's journeys that run on `day`, their service day."""
    return [journey for journey in schedule.journeys if runs_on(journey.calendar, day, holiday_names)]


# Building the schedule.


def download(url: str, file: Writable, max_bytes: int = routesetup.MAX_DOWNLOAD_BYTES) -> None:
    """Write a timetable download into `file` as it arrives, refusing anything
    but https, and more than `max_bytes`."""
    if urlsplit(url).scheme != "https":
        raise TimetableError("Refusing a timetable link that isn't https")
    try:
        for chunk in net.stream(url, {}, max_bytes, timeout=DOWNLOAD_TIMEOUT, deadline=net.DOWNLOAD_DEADLINE):
            file.write(chunk)
    except net.TooLarge:
        raise TimetableError(f"Timetable download larger than {max_bytes} bytes; refusing") from None


def _dataset_links(operators: Iterable[str], api_key: str, lines, find_datasets: FindDatasets) -> list[tuple[str, str]]:
    """(operator code, link) for each data set to fetch, each link once. With
    `lines`, only the data sets BODS says carry one of them."""
    links: dict[str, str] = {}
    wanted_lines = sorted(set(lines or ())) or [None]
    for noc in operators:
        for line in wanted_lines:
            for url in find_datasets(api_key, noc, line):
                links.setdefault(url, noc)
    return [(noc, url) for url, noc in links.items()]


def _documents(
    links: list[tuple[str, str]], fetch: Download, scratch, max_bytes: int = routesetup.MAX_XML_BYTES,
    skipped: Callable[[str], None] = routesetup.log_skipped,
) -> Generator[tuple[str, str, bytes], None, None]:
    """(operator code, name, document) for every TransXChange document behind
    the links: one over `max_bytes` is skipped and told to `skipped`
    (routesetup.named_documents). Each download waits in a temporary file,
    gone once its documents have been used; each document is unpacked only
    when wanted."""
    for noc, url in links:
        with tempfile.TemporaryFile(dir=scratch) as file:
            fetch(url, file)
            file.seek(0)
            for name, document in routesetup.named_documents(file, max_bytes, name=redact_key(url), skipped=skipped):
                yield noc, name, document


def _group(version: _Version, journey: Journey) -> tuple[str, str, str]:
    """The service a journey is a version of: ServiceCode, line and operator."""
    return version.service, journey.line, journey.operator


# A version of a service as seen (see _group): its start (date.min if it gives
# none), its end (None if open-ended) and its RevisionNumber.
_Seen = tuple[date, date | None, int]


def _until_replaced(version: _Version, journey: Journey, versions: Mapping[tuple, Set[_Seen]]) -> Journey | None:
    """The journey as a later version of its service leaves it, as bustimes.org
    does: BODS keeps an old version beside the one replacing it, often
    open-ended, and both would run. It ends the day before the first later
    open-ended version starts; a later version with an end only switches it
    off from that version's start to its end (calendar.dates_off, which
    runs_on puts first), within the journey's own period. None if a version
    starting the same day with a higher RevisionNumber replaces it. `versions`
    has every version of each service seen. A journey whose document gives no
    ServiceCode is kept as it is."""
    if not version.service:
        return journey
    calendar = journey.calendar
    start = calendar.start or date.min
    others = versions.get(_group(version, journey), ())
    if any(other == start and revision > version.revision for other, _, revision in others):
        return None
    end = calendar.end
    replaced = [other for other, other_end, _ in others if other > start and other_end is None]
    if replaced and (end is None or min(replaced) <= end):
        end = min(replaced) - timedelta(days=1)
    off = tuple(sorted((other, other_end if end is None else min(other_end, end))
                       for other, other_end, _ in others
                       if other > start and other_end is not None and (end is None or other <= end)))
    if end == calendar.end and not off:
        return journey
    return replace(journey, calendar=replace(calendar, end=end, dates_off=calendar.dates_off + off))


def _current(found: Iterable[tuple[_Version, Journey]], versions: Mapping[tuple, Set[_Seen]],
             today: date) -> tuple[Journey, ...]:
    """The journeys still running from `today`, each until a later version of
    its service replaces it (_until_replaced), and each once: duplicates (same
    line, operator, calls and calendar) can't be told apart by live times."""
    kept: dict[tuple, Journey] = {}
    for version, journey in found:
        current = _until_replaced(version, journey, versions)
        if current is None or (current.calendar.end and current.calendar.end < today):
            continue
        kept.setdefault((current.line, current.operator, current.calls, current.calendar), current)
    return tuple(kept.values())


def _nearby(journey: Journey, atcos: Collection[str]) -> set[str]:
    """The stops a journey calls at on its way to each of `atcos`, and the one after."""
    found = set()
    for index, call in enumerate(journey.calls):
        if call.atco in atcos:
            found.update(c.atco for c in journey.calls[max(0, index - CALLS_BEFORE) : index + CALLS_AFTER + 1])
    return found


def _approach(journey: Journey) -> set[str]:
    """The stop an inbound-only journey ends at and the ones it calls at on its way."""
    return {call.atco for call in journey.calls[-(CALLS_BEFORE + 1) :]}


def _ends_near(journey: Journey, atcos: Collection[str], positions: Mapping[str, StopPosition]) -> bool:
    """Whether a journey ends within NEAR_M of one of `atcos`, by `positions`."""
    end = positions.get(journey.calls[-1].atco)
    return end is not None and any(atco in positions and metres(end, positions[atco]) <= NEAR_M for atco in atcos)


def _positions(atcos: set[str], known: dict[str, StopPosition], naptan: Naptan) -> dict[str, StopPosition]:
    """NaPTAN's name and position for each stop, or the timetables' own where
    NaPTAN has none or can't be reached."""
    try:
        found = naptan(sorted(atcos))
    except Exception as error:  # noqa: BLE001 - the timetables' own positions will do
        log.warning("NaPTAN not reached, using the timetables' stop positions: %s", safe_message(error))
        found = {}
    positions = {}
    for atco in sorted(atcos):
        row = found.get(atco)
        position = _position(str(row[0]).strip()[:_TEXT_LIMIT], str(row[1]), str(row[2])) if row else None
        if position is not None and row and len(row) > 3:
            position = replace(position, indicator=_sign(row[3]))
        position = position or known.get(atco)
        if position is not None:
            positions[atco] = position
    return positions


def _kept_roads(pairs: Mapping, previous: Schedule | None) -> dict[str, tuple[tuple[float, float], ...]]:
    """The roads in `previous` between the same two stops, neither of them moved."""
    if previous is None:
        return {}
    kept = {}
    for (a, b), (start, end) in pairs.items():
        road = previous.shapes.get(roadshape.shape_key((a, b)))
        was_a, was_b = previous.positions.get(a), previous.positions.get(b)
        if road and was_a and was_b and (was_a.lat, was_a.lon) == start and (was_b.lat, was_b.lon) == end:
            kept[roadshape.shape_key((a, b))] = road
    return kept


def add_roads(schedule: Schedule, route: Route = roadshape.segment_shapes,
              previous: Schedule | None = None, progress: Progress = lambda done, total: None) -> Schedule:
    """The schedule with the road between each pair of consecutive positioned
    stops, by roadshape.shape_key, for the maps; none where the router can't
    say (the maps draw those straight). A road in `previous` (the timetable
    this one replaces) between the same two stops, neither of them moved, is
    kept rather than asked for again. `progress(done, total)` follows the
    router's requests. Never fails for the router's sake."""
    pairs = roadshape.segment_pairs(schedule.journeys, schedule.positions)
    kept = _kept_roads(pairs, previous)
    wanted = {pair: ends for pair, ends in pairs.items() if roadshape.shape_key(pair) not in kept}
    try:
        found = route(wanted, progress=progress)
        new = {roadshape.shape_key(pair): tuple((float(lat), float(lon)) for lat, lon in found[pair])
               for pair in wanted if found.get(pair)}
    except Exception as error:  # noqa: BLE001 - straight lines will do
        log.warning("Bus map roads not found, drawing straight lines: %s", safe_message(error))
        new = {}
    if kept:
        log.info("Bus map roads: kept %d from the last timetable", len(kept))
    return replace(schedule, shapes={**kept, **new})


def build_schedule(
    stops: Mapping[str, str],
    operators: Iterable[str],
    api_key: str,
    now: datetime,
    *,
    lines: Iterable[str] | None = None,
    scratch: Path | str | None = None,
    limits: DocumentLimits = DEFAULT_LIMITS,
    find_datasets: FindDatasets = routesetup.dataset_urls,
    fetch: Download = download,
    naptan: Naptan = routesetup.naptan_stop_details,
) -> Schedule:
    """A schedule of every timetabled journey calling at your `stops` (labels
    to ATCO codes), from the BODS timetables of `operators` (national operator
    codes), and the inbound-only journeys of their lines (see the module's
    notes). Downloads wait in `scratch` (the state folder: /tmp may be memory).
    A document bigger than `limits` allow (the install's) is skipped with a
    warning naming it. It has no roads yet: see add_roads.

    BODS keeps an old version of a service beside the one replacing it, so
    each version ends the day before a later open-ended one starts, and is
    switched off while a later one with an end runs (_until_replaced).
    Journeys whose timetable ended before today are left out, and so are
    duplicates (same line, operator, calls and calendar), which live times
    couldn't tell apart. valid_until is the earliest end among the rest that
    call at your stops: when due() builds it again. TimetableError or
    RouteSetupError if anything fails, or no journey calls at your stops."""
    atcos = set(stops.values())
    wanted = [atco.encode() for atco in atcos]
    today = now.date()
    # Journeys as read, by version too: which are current is known only once
    # every version of their service has been seen (_current).
    journeys: dict[tuple, tuple[_Version, Journey]] = {}
    inbound: dict[tuple, tuple[_Version, Journey]] = {}  # inbound-only, not yet measured
    versions: dict[tuple, set[_Seen]] = {}  # by _group
    nearby: set[str] = set()
    approaches: set[str] = set()
    known: dict[str, StopPosition] = {}
    yours: dict[str, StopPosition] = {}
    left_out = 0
    room = _Room(limits.journeys, inbound_limit(limits))
    links = _dataset_links(operators, api_key, lines, find_datasets)
    for noc, name, document in _documents(links, fetch, scratch, limits.read):
        # Skip, unparsed, the many documents of a big operator that never mention your stops.
        if not any(code in document for code in wanted):
            continue
        if len(document) > limits.parse:
            log.warning("%s", routesetup.too_big(name, limits.parse, len(document)))
            continue
        room.held, room.held_inbound = len(journeys), len(inbound)
        found, positions = _read_document(document, atcos, noc, yours, room)
        yours.update((atco, positions[atco]) for atco in atcos if atco in positions)
        for version, journey in found:
            # Even an ended version ends the ones before it, if it was open-ended.
            versions.setdefault(_group(version, journey), set()).add(
                (journey.calendar.start or date.min, journey.calendar.end, version.revision))
            if journey.calendar.end and journey.calendar.end < today:
                continue
            key = (version, journey.line, journey.operator, journey.calls, journey.calendar)
            if not journey.inbound_only:
                journeys.setdefault(key, (version, journey))
                nearby |= _nearby(journey, atcos)
            elif key in inbound or len(inbound) < room.inbound:
                inbound.setdefault(key, (version, journey))
                approaches |= _approach(journey)
            else:
                left_out += 1
        known.update((atco, position) for atco, position in positions.items() if atco in nearby or atco in approaches)
    kept = _current(journeys.values(), versions, today)
    if not kept:
        raise TimetableError("No timetabled journeys call at your stops")
    left_out += room.left_out
    if left_out:
        log.warning("Left out %d journeys ending near your stops: more than %d", left_out, room.inbound)
    placed = _positions(nearby | approaches, known, naptan)
    near = tuple(journey for journey in _current(inbound.values(), versions, today)
                 if _ends_near(journey, atcos, placed))
    needed = set().union(*(_nearby(journey, atcos) for journey in kept), *(_approach(journey) for journey in near))
    ends = [journey.calendar.end for journey in kept if journey.calendar.end]
    positions = {atco: position for atco, position in placed.items() if atco in needed}
    return Schedule(
        built=now,
        valid_until=min(ends, default=None),
        stops=dict(stops),
        journeys=kept + near,
        positions=positions,
        operators=tuple(sorted(set(operators))),
        format=FORMAT,
    )


# Keeping it.


def save(folder: Path | str, schedule: Schedule) -> None:
    """Save the schedule beside the state file, whole or not at all, readable
    by its owner and group only. TimetableTooBig, keeping the saved one, if
    it is larger than load() reads."""
    data = schedule.to_json().encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise TimetableTooBig(f"Bus timetable is larger than {MAX_FILE_BYTES} bytes ({len(data)}); not saving it")
    files.write_atomic(Path(folder) / FILE_NAME, data, 0o640)


def load(folder: Path | str) -> Schedule | None:
    """The saved schedule, or None if there is none or it can't be read."""
    path = Path(folder) / FILE_NAME
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            log.warning("Ignoring bus timetable %s: larger than %d bytes", path, MAX_FILE_BYTES)
            return None
        return Schedule.from_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        log.warning("Ignoring unreadable bus timetable %s: %s", path, safe_message(error))
        return None


class RoadsProgress:
    """How far finding the roads has got, kept in ROADS_FILE beside the
    timetable for the settings site (roads_progress), which may be another
    process's. Call it as roadshape.segment_shapes's `progress`; clear() once
    the roads are found. Written whole, at most every ROADS_EVERY_S seconds
    and the last; it never fails, as it only says how things are going."""

    def __init__(self, folder: Path | str, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(folder) / ROADS_FILE
        self._clock = clock
        self._written: float | None = None

    def __call__(self, done: int, total: int) -> None:
        now = self._clock()
        if self._written is not None and done < total and now - self._written < ROADS_EVERY_S:
            return
        self._written = now
        try:
            files.write_atomic(self._path, json.dumps({"done": done, "total": total, "at": now}), 0o640)
        except OSError as error:
            log.warning("Can't keep how far the bus map roads have got: %s", error.strerror or error)

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            log.warning("Can't remove %s: %s", self._path, error.strerror or error)


def _small_json(path: Path, limit: int) -> dict | None:
    """The JSON object in a small plain file of at most `limit` bytes; None if
    there is none, it is bigger, a link or not a plain file, or not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | files.NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        body = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(body) > limit:
        return None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def roads_progress(folder: Path | str, now: Callable[[], float] = time.time) -> tuple[int, int] | None:
    """(done, total) while the roads for the maps are being found, from
    ROADS_FILE. None when they aren't, when it was left by a process that
    stopped (unwritten for ROADS_STALE_S), or when it isn't a small plain
    file of two counts and a time."""
    data = _small_json(Path(folder) / ROADS_FILE, _ROADS_FILE_BYTES)
    if data is None:
        return None
    done, total, at = data.get("done"), data.get("total"), data.get("at")
    if (not isinstance(done, int) or isinstance(done, bool) or not isinstance(total, int) or isinstance(total, bool)
            or not 0 <= done <= total):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    if abs(now() - at) > ROADS_STALE_S:
        return None
    return done, total


def due(
    schedule: Schedule | None, stops: Mapping[str, str], now: datetime, operators: Iterable[str] | None = None
) -> str | None:
    """Why the schedule should be built again, or None if it shouldn't. With
    `operators`, a schedule built from others is due too."""
    if schedule is None:
        return "there is none"
    if dict(schedule.stops) != dict(stops):
        return "your stops changed"
    if schedule.format < FORMAT:
        return "it was built by an older version"
    if operators is not None and schedule.operators != tuple(sorted(set(operators))):
        return "your operators changed"
    if now - schedule.built >= MAX_AGE:
        return "it is a week old"
    if schedule.built - now > _SKEW:
        return "it was built in the future"
    if schedule.valid_until and now.date() > schedule.valid_until:
        return "a timetable in it has ended"
    return None


def _built_for(stops: Mapping[str, str], operators: Iterable[str], api_key: str) -> str:
    """A digest of the stops, operators and BODS key a build is for, so a
    failed build's record (FAILED_FILE) says what it was for without keeping
    them: a refused key fails every build, and saving the right one should
    build at once. The key goes in only as its own digest."""
    key = hashlib.sha256(api_key.encode()).hexdigest()
    plain = json.dumps([sorted(dict(stops).items()), sorted(set(operators)), key], separators=(",", ":"))
    return hashlib.sha256(plain.encode()).hexdigest()


def _note_failure(folder: Path | str, built_for: str, now: datetime) -> None:
    """Keep when a build failed and what for (_built_for), in FAILED_FILE (see refresh)."""
    try:
        files.write_atomic(Path(folder) / FAILED_FILE, json.dumps({"at": now.isoformat(), "for": built_for}), 0o640)
    except OSError as error:
        log.warning("Can't keep when the bus timetable build failed: %s", error.strerror or error)


def _failed_lately(folder: Path | str, built_for: str, now: datetime) -> datetime | None:
    """When the last build failed, if it was for the same (_built_for) less
    than RETRY_AFTER ago; None otherwise, or if FAILED_FILE can't be read. A
    failure the clock puts in the future doesn't count."""
    data = _small_json(Path(folder) / FAILED_FILE, _FAILED_FILE_BYTES)
    if data is None or data.get("for") != built_for or not isinstance(data.get("at"), str):
        return None
    try:
        at = datetime.fromisoformat(data["at"])
    except ValueError:
        return None
    if at.tzinfo is not None or not timedelta(0) <= now - at < RETRY_AFTER:
        return None
    return at


def _forget_failure(folder: Path | str) -> None:
    try:
        (Path(folder) / FAILED_FILE).unlink(missing_ok=True)
    except OSError as error:
        log.warning("Can't remove %s: %s", FAILED_FILE, error.strerror or error)


def refresh(
    folder: Path | str,
    stops: Mapping[str, str],
    operators: Iterable[str],
    now: datetime,
    api_key: str,
    *,
    lines: Iterable[str] | None = None,
    force: bool = False,
    build: Callable[..., Schedule] = build_schedule,
    route: Route = roadshape.segment_shapes,
    saved: Callable[[Schedule], None] = lambda schedule: None,
    cancelled: Callable[[], bool] = lambda: False,
    limits: DocumentLimits = DEFAULT_LIMITS,
) -> Schedule | None:
    """The schedule to use, from `folder` (the state folder). It is built
    again first if it is due (see due: weekly, or when the stops or operators
    change) or `force`d. A failed build keeps the saved one, and is kept in
    FAILED_FILE: it isn't tried again for RETRY_AFTER (each try downloads
    every timetable again), unless `force`d or the stops, operators or key
    change. A build that fails once `cancelled()` says so was stopped, not
    failed, and isn't kept. With nothing due this only reads the saved file,
    unless its roads are still to be found. It is built within `limits`,
    the install's (see build_schedule).

    A new timetable is saved before its roads are asked for, marked
    roads_pending, and `saved(schedule)` is told; then it is saved again
    with them (add_roads, with `route`, keeping the saved one's where it
    can). However long the router takes, or if this is stopped meanwhile,
    the timetable is kept, at worst with straight lines on the maps. If
    `cancelled()` says so once they have been asked for (they stopped part
    way), or this is stopped, they stay pending: the next refresh finds the
    rest.

    It takes no lock: build it with build(), which does."""
    current = load(folder)
    operators = list(operators)
    reason = "asked to" if force else due(current, stops, now, operators)
    built_for = _built_for(stops, operators, api_key)
    failed = _failed_lately(folder, built_for, now) if reason and not force else None
    if failed is not None:
        log.info("Bus timetable due (%s), but building it failed at %s: trying again after %s", reason,
                 f"{failed:%H:%M}", f"{failed + RETRY_AFTER:%H:%M}")
        reason = None
    if reason is None:
        if current is not None and current.roads_pending:
            log.info("Bus timetable saved before all its roads were found: finding the rest")
            return _with_roads(folder, current, route, current, cancelled)
        return current
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        fresh = replace(build(stops, operators, api_key, now, lines=lines, scratch=folder, limits=limits),
                        roads_pending=True)
        save(folder, fresh)
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good schedule
        log.warning("Bus timetable not rebuilt (%s), keeping the saved one: %s", reason, safe_message(error))
        if not cancelled():  # stopped, as the setup search's Cancel does: not to wait RETRY_AFTER for
            _note_failure(folder, built_for, now)
        return current
    _forget_failure(folder)
    calling = len(fresh.departure_journeys)
    log.info("Bus timetable rebuilt (%s): %d journeys at your stops, %d ending near them", reason, calling,
             len(fresh.journeys) - calling)
    saved(fresh)
    return _with_roads(folder, fresh, route, current, cancelled)


def _with_roads(folder: Path | str, schedule: Schedule, route: Route, previous: Schedule | None,
                cancelled: Callable[[], bool]) -> Schedule:
    """The saved `schedule` with its roads (add_roads), saved again: still
    pending if `cancelled()` by the end. As it was if they can't be saved;
    without them for good if they would make it too big to load, or they
    would be asked for again by every refresh."""
    progress = RoadsProgress(folder)
    try:
        routed = add_roads(schedule, route, previous, progress)
        routed = replace(routed, roads_pending=cancelled())
        save(folder, routed)
    except TimetableTooBig as error:
        log.warning("Bus map roads left out, drawing straight lines: %s", error)
        straight = replace(schedule, roads_pending=False)
        try:
            save(folder, straight)
        except Exception as again:  # noqa: BLE001 - the timetable is saved, its roads pending
            log.warning("Bus timetable not saved without its roads: %s", safe_message(again))
            return schedule
        return straight
    except Exception as error:  # noqa: BLE001 - the timetable is saved; the maps draw straight lines
        log.warning("Bus map roads not saved, drawing straight lines: %s", safe_message(error))
        return schedule
    finally:
        progress.clear()
    return routed


@contextmanager
def _lock(folder: Path, wait: bool, cancelled: Callable[[], bool], waiting: Callable[[], None]) -> Iterator[None]:
    """Hold the store's build lock. Without `wait`, TimetableBusy at once if
    another holds it; with it, look again every LOCK_WAIT_S seconds, calling
    `waiting()` once, until it is free or `cancelled()` (TimetableBusy too).
    The file is private, and a link put in its place is refused."""
    path = folder / LOCK_NAME
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = files.open_lock(path)
    except OSError as error:
        raise TimetableError(f"Can't open {path}: {error.strerror or error}") from None
    try:
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or cancelled():
                    raise TimetableBusy("Another bus timetable build is running") from None
                if not told:
                    told = True
                    waiting()
                time.sleep(LOCK_WAIT_S)
        yield
    finally:
        os.close(fd)  # lets go of the lock, if it was taken


def build(
    folder: Path | str,
    stops: Mapping[str, str],
    operators: Iterable[str],
    now: datetime,
    api_key: str,
    *,
    wait: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
    waiting: Callable[[], None] = lambda: None,
    **options,
) -> Schedule | None:
    """The one way to build the timetable: refresh (with `options`: lines,
    force, build, route, saved, limits) while holding the store's lock, so two
    builds never run at once, in this process or another, and its roads are
    found under it too. Without `wait`, TimetableBusy if another build is
    running; with it, wait for that one (see _lock). `cancelled()` stops the
    wait, and tells refresh whether finding the roads was stopped.
    TimetableError if the lock file can't be opened."""
    folder = Path(folder)
    with _lock(folder, wait, cancelled, waiting):
        return refresh(folder, stops, operators, now, api_key, cancelled=cancelled, **options)
