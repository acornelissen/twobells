"""The bus timetable for your stops, built from BODS TransXChange.

The way bustimes.org does it: every timetabled journey that calls at one of
your stops, with the time it leaves each stop on its way, and the days it
runs. livebus.py then adjusts it with live positions.

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

Built about once a week by a timer or the command line, never from the
minutely board run: an operator's timetables can run to hundreds of
megabytes. Each download waits on disk and its documents are parsed one at a
time, so a Pi Zero holds one document in memory, never the lot.

Roads. TransXChange gives no road between stops, so once a new timetable is
saved, refresh asks the OpenStreetMap car router for each pair of consecutive
positioned stops, once, and saves it again with the roads for the
dashboard's maps (add_roads, roadshape.py). A pair it can't find is drawn
straight; the router never costs a timetable.

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

import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import AbstractSet, BinaryIO, Callable, Collection, Iterable, Iterator, Mapping, NamedTuple
from urllib.parse import urlsplit
from urllib.request import Request

from . import http, roadshape, routesetup, safexml
from .schedule_model import NEAR_M, Calendar, Call, DateRange, Journey, Schedule, StopPosition, metres
from .status import safe_message

log = logging.getLogger(__name__)

FILE_NAME = "bus-timetable.json"  # beside state.json
# The builder's version, saved in the file: an older one is rebuilt (see due).
# 2: stops keep the letter on their sign.
# 3: the roads between stops, for the dashboard's maps (shapes).
FORMAT = 3
MAX_AGE = timedelta(days=7)
# Positions are kept for this many calls before each of your stops, and the one
# after: livebus places buses along that stretch and sees them pass your stop.
CALLS_BEFORE = 20
CALLS_AFTER = 1
MAX_JOURNEYS = 20_000  # far more than any town's stops see
# Inbound-only journeys kept, at most; beyond it the rest are left out. Most
# are dropped once NaPTAN has placed their last stop, so this counts them
# before that, while they are all held at once (a Pi Zero builds this too).
MAX_INBOUND_JOURNEYS = 10_000
MAX_FILE_BYTES = 50_000_000
DOWNLOAD_TIMEOUT = 60.0  # seconds without data before a download is given up
_TEXT_LIMIT = 80  # line names, destinations, codes
_LATEST = 2 * 24 * 3600  # a journey still running two days after its service day is nonsense
# A clock this far behind a timetable's build time was wrong when it was built
# (a Pi has no clock battery), so its age can't be trusted: build again.
_SKEW = timedelta(days=1)

FindDatasets = Callable[[str, str, "str | None"], list]  # (api_key, noc, line) -> links, as routesetup.dataset_urls
Download = Callable[[str, BinaryIO], None]  # (link, file to write it to)
# atcos -> {atco: (name, lat, lon[, sign letter])}, as routesetup.naptan_stop_details
Naptan = Callable[[Iterable[str]], dict]
# {(atco, atco): ((lat, lon), (lat, lon))} -> {(atco, atco): [(lat, lon), ...]}, as roadshape.segment_shapes
Route = Callable[[Mapping], Mapping]

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


def _read_stops(root, doc: _Document) -> None:
    for node in root.iterfind("t:StopPoints/t:AnnotatedStopPointRef", _NS):
        atco, name = _text(node, "t:StopPointRef"), _text(node, "t:CommonName")
        doc.names[atco] = name
        position = _position(name, _text(node, "t:Location/t:Latitude"), _text(node, "t:Location/t:Longitude"))
        if position is not None:
            doc.positions[atco] = position
    for node in root.iterfind("t:StopPoints/t:StopPoint", _NS):
        doc.names[_text(node, "t:AtcoCode")] = _text(node, "t:Descriptor/t:CommonName")


def _read_operators(root, doc: _Document) -> None:
    for node in root.iterfind("t:Operators/*", _NS):
        code = _text(node, "t:NationalOperatorCode") or _text(node, "t:OperatorCode")
        if code:
            doc.operators[node.get("id", "")] = code


def _read_schools(root, doc: _Document) -> None:
    for node in root.iterfind("t:ServicedOrganisations/t:ServicedOrganisation", _NS):
        doc.schools[_text(node, "t:OrganisationCode")] = (
            _ranges(node.find("t:WorkingDays", _NS)),
            _ranges(node.find("t:Holidays", _NS)),
        )


def _read_services(root, doc: _Document) -> None:
    for section in root.iterfind("t:JourneyPatternSections/t:JourneyPatternSection", _NS):
        doc.sections[section.get("id", "")] = [
            _link(node) for node in section.iterfind("t:JourneyPatternTimingLink", _NS)
        ]
    for node in root.iterfind("t:Services/t:Service", _NS):
        service = _Service(
            lines={line.get("id", ""): _text(line, "t:LineName") for line in node.iterfind("t:Lines/t:Line", _NS)},
            start=_date(_text(node, "t:OperatingPeriod/t:StartDate")),
            end=_date(_text(node, "t:OperatingPeriod/t:EndDate")),
            profile=node.find("t:OperatingProfile", _NS),
            operator_ref=_text(node, "t:RegisteredOperatorRef"),
        )
        doc.services[_text(node, "t:ServiceCode")] = service
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
) -> Journey | None:
    """The journey a VehicleJourney describes, if it calls at one of `atcos`,
    or, marked inbound_only, if it doesn't but ends at one of `stands`."""
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
    return Journey(line, operator, destination, code, calls, calendar, inbound_only=not calling)


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
        if here is not None and any(metres(here, there) <= NEAR_M for there in placed.values()):
            found.add(atco)
        elif (here is None or len(placed) < len(atcos)) and atco[:3] in areas:
            found.add(atco)
    return found


def _parse_document(
    xml: bytes, atcos: Collection[str], noc: str, yours: Mapping[str, StopPosition] | None = None
) -> tuple[list[Journey], dict[str, StopPosition]]:
    """The journeys in a document calling at your stops, and its stop
    positions. With `yours` (your stops' positions known so far), also its
    inbound-only journeys of the lines calling there (see the module's notes)."""
    try:
        root = safexml.fromstring(xml)
    except safexml.DoctypeError:
        raise TimetableError("Timetable file has a DOCTYPE; refusing to parse it") from None
    except SyntaxError as error:  # ElementTree.ParseError
        raise TimetableError(f"Timetable file is not XML: {error}") from None
    doc = _Document()
    _read_stops(root, doc)
    _read_operators(root, doc)
    _read_schools(root, doc)
    _read_services(root, doc)
    vehicle_journeys = root.findall("t:VehicleJourneys/t:VehicleJourney", _NS)
    pattern_refs = {_text(node, "t:VehicleJourneyCode"): _text(node, "t:JourneyPatternRef") for node in vehicle_journeys}
    stands = _stands(doc, atcos, yours) if yours is not None else set()
    journeys = []
    for node in vehicle_journeys:
        journey = _journey(node, doc, atcos, noc, pattern_refs, stands)
        if journey is not None and (journey.inbound_only or any(call.atco in atcos for call in journey.calls)):
            journeys.append(journey)
    lines = {(journey.line.casefold(), journey.operator) for journey in journeys if not journey.inbound_only}
    return [j for j in journeys if not j.inbound_only or (j.line.casefold(), j.operator) in lines], doc.positions


def parse_journeys(xml: bytes, atcos: Collection[str], noc: str = "") -> list[Journey]:
    """Every journey in one TransXChange document that calls at one of `atcos`,
    whether it starts, passes or ends there. `noc` is the operator code to use
    when the document doesn't give one."""
    return _parse_document(xml, set(atcos), noc)[0]


# When journeys run.


def _within(day: date, ranges: Iterable[DateRange]) -> bool:
    return any(start <= day <= end for start, end in ranges)


def runs_on(calendar: Calendar, day: date, holiday_names: Mapping[date, AbstractSet[str]] = {}) -> bool:
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


def journeys_on(schedule: Schedule, day: date, holiday_names: Mapping[date, AbstractSet[str]] = {}) -> list[Journey]:
    """The schedule's journeys that run on `day`, their service day."""
    return [journey for journey in schedule.journeys if runs_on(journey.calendar, day, holiday_names)]


# Building the schedule.


def download(url: str, file: BinaryIO, max_bytes: int = routesetup.MAX_DOWNLOAD_BYTES) -> None:
    """Write a timetable download into `file` as it arrives, refusing anything
    but https, and more than `max_bytes`."""
    if urlsplit(url).scheme != "https":
        raise TimetableError("Refusing a timetable link that isn't https")
    request = Request(url, headers={"User-Agent": http.USER_AGENT})
    total = 0
    with http.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise TimetableError(f"Timetable download larger than {max_bytes} bytes; refusing")
            file.write(chunk)


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


def _documents(links: list[tuple[str, str]], fetch: Download, scratch) -> Iterator[tuple[str, bytes]]:
    """(operator code, document) for every TransXChange document behind the
    links. Each download waits in a temporary file, gone once its documents
    have been used; each document is unpacked only when wanted."""
    for noc, url in links:
        with tempfile.TemporaryFile(dir=scratch) as file:
            fetch(url, file)
            file.seek(0)
            for document in routesetup.timetable_documents(file):
                yield noc, document


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
        if position is not None and len(row) > 3:
            position = replace(position, indicator=_sign(row[3]))
        position = position or known.get(atco)
        if position is not None:
            positions[atco] = position
    return positions


def add_roads(schedule: Schedule, route: Route = roadshape.segment_shapes) -> Schedule:
    """The schedule with the road between each pair of consecutive positioned
    stops, by roadshape.shape_key, for the maps; none where the router can't
    say (the maps draw those straight). Never fails for the router's sake."""
    pairs = roadshape.segment_pairs(schedule.journeys, schedule.positions)
    try:
        found = route(pairs)
        new = {roadshape.shape_key(pair): tuple((float(lat), float(lon)) for lat, lon in found[pair])
               for pair in pairs if found.get(pair)}
    except Exception as error:  # noqa: BLE001 - straight lines will do
        log.warning("Bus map roads not found, drawing straight lines: %s", safe_message(error))
        new = {}
    return replace(schedule, shapes=new)


def build_schedule(
    stops: Mapping[str, str],
    operators: Iterable[str],
    api_key: str,
    now: datetime,
    *,
    lines: Iterable[str] | None = None,
    scratch: Path | str | None = None,
    find_datasets: FindDatasets = routesetup.dataset_urls,
    fetch: Download = download,
    naptan: Naptan = routesetup.naptan_stop_details,
) -> Schedule:
    """A schedule of every timetabled journey calling at your `stops` (labels
    to ATCO codes), from the BODS timetables of `operators` (national operator
    codes), and the inbound-only journeys of their lines (see the module's
    notes). Downloads wait in `scratch` (the state folder: /tmp may be memory).
    It has no roads yet: see add_roads.

    Journeys whose timetable ended before today are left out (BODS keeps an
    old timetable beside its replacement for a while), and so are duplicates
    (same line, operator, calls and calendar), which live times couldn't tell
    apart. valid_until is the earliest end among the rest that call at your
    stops. TimetableError or RouteSetupError if anything fails, or no journey
    calls at your stops."""
    atcos = set(stops.values())
    wanted = [atco.encode() for atco in atcos]
    today = now.date()
    journeys: dict[tuple, Journey] = {}
    inbound: dict[tuple, Journey] = {}  # inbound-only, not yet measured
    nearby: set[str] = set()
    approaches: set[str] = set()
    known: dict[str, StopPosition] = {}
    yours: dict[str, StopPosition] = {}
    left_out = 0
    links = _dataset_links(operators, api_key, lines, find_datasets)
    for noc, document in _documents(links, fetch, scratch):
        # Skip, unparsed, the many documents of a big operator that never mention your stops.
        if not any(code in document for code in wanted):
            continue
        found, positions = _parse_document(document, atcos, noc, yours)
        yours.update((atco, positions[atco]) for atco in atcos if atco in positions)
        for journey in found:
            if journey.calendar.end and journey.calendar.end < today:
                continue
            key = (journey.line, journey.operator, journey.calls, journey.calendar)
            if not journey.inbound_only:
                journeys.setdefault(key, journey)
                nearby |= _nearby(journey, atcos)
            elif key in inbound or len(inbound) < MAX_INBOUND_JOURNEYS:
                inbound.setdefault(key, journey)
                approaches |= _approach(journey)
            else:
                left_out += 1
        known.update((atco, position) for atco, position in positions.items() if atco in nearby or atco in approaches)
        if len(journeys) > MAX_JOURNEYS:
            raise TimetableError(f"More than {MAX_JOURNEYS} journeys call at your stops; refusing")
    if not journeys:
        raise TimetableError("No timetabled journeys call at your stops")
    if left_out:
        log.warning("Left out %d journeys ending near your stops: more than %d", left_out, MAX_INBOUND_JOURNEYS)
    placed = _positions(nearby | approaches, known, naptan)
    near = tuple(journey for journey in inbound.values() if _ends_near(journey, atcos, placed))
    needed = nearby.union(*(_approach(journey) for journey in near))
    kept = tuple(journeys.values())
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
    by its owner and group only."""
    path = Path(folder) / FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(tmp, flags, 0o640), "w", encoding="utf-8") as file:
        os.fchmod(file.fileno(), 0o640)  # whatever the umask, or a leftover file's mode
        file.write(schedule.to_json())
        file.flush()
        # On disk before it replaces the old file, so a power cut can't leave it empty.
        os.fsync(file.fileno())
    os.replace(tmp, path)


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
) -> Schedule | None:
    """The schedule to use, from `folder` (the state folder). It is built
    again first if it is due (see due: weekly, or when the stops or operators
    change) or `force`d. A failed build keeps the saved one. With nothing due
    this only reads the saved file.

    A new timetable is saved before its roads are asked for (add_roads, with
    `route`), then saved again with them. However long the router takes, or if this is stopped meanwhile,
    the timetable is kept, at worst with straight lines on the maps."""
    current = load(folder)
    operators = list(operators)
    reason = "asked to" if force else due(current, stops, now, operators)
    if reason is None:
        return current
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        fresh = build(stops, operators, api_key, now, lines=lines, scratch=folder)
        save(folder, fresh)
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good schedule
        log.warning("Bus timetable not rebuilt (%s), keeping the saved one: %s", reason, safe_message(error))
        return current
    calling = len(fresh.departure_journeys)
    log.info("Bus timetable rebuilt (%s): %d journeys at your stops, %d ending near them", reason, calling,
             len(fresh.journeys) - calling)
    try:
        routed = add_roads(fresh, route)
        if routed.shapes:
            save(folder, routed)
    except Exception as error:  # noqa: BLE001 - the timetable is saved; the maps draw straight lines
        log.warning("Bus map roads not saved, drawing straight lines: %s", safe_message(error))
        return fresh
    return routed
