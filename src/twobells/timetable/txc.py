"""Reading BODS TransXChange: the journeys in one document that call at your stops.

timetable/build.py downloads the documents and builds the timetable from
what this reads; setup/discover.py reads them too, to find the bus
companies. Each document is read in passes over its bytes, an element at a
time (read_document), and only the journeys calling at your stops are kept,
with the inbound-only ones build.py's notes describe.

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

Blocks. A VehicleJourney's Operational/Block/BlockNumber, where the
operator gives one, is kept (Journey.block): the journeys one bus works in
a day share it, so sources/livebus.py can follow a bus from the journey
that brings it to your stop to the one it leaves on. Many operators give
none (Bee Network's and High Peak's timetables, in October 2026); then
"" is kept.
"""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Set
from dataclasses import dataclass, field, replace
from datetime import date
from typing import NamedTuple
from xml.etree import ElementTree

from ..core import safexml
from . import routesetup
from .model import NEAR_M, Calendar, Call, DateRange, Journey, StopPosition, metres
from .routesetup import DocumentLimits

# Inbound-only journeys kept, at most, and no more than half the install's
# limit of those calling at your stops (inbound_limit); beyond it the rest are
# left out. Most are dropped once NaPTAN has placed their last stop, so this
# counts them before that, while they are all held at once (a Pi Zero builds
# this too).
MAX_INBOUND_JOURNEYS = 10_000
TEXT_LIMIT = 80  # line names, destinations, codes
_LATEST = 2 * 24 * 3600  # a journey still running two days after its service day is nonsense

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
    """A timetable that can't be read or built; build.py and trams.py raise it too."""


# Reading TransXChange.


def _tag(node) -> str:
    return node.tag.rpartition("}")[2]


def _text(node, path: str) -> str:
    return (node.findtext(path, "", _NS) or "").strip()[:TEXT_LIMIT]


def _seconds(text: str | None) -> int | None:
    """Seconds in an ISO 8601 duration such as PT1M30S; None if it isn't one."""
    match = _DURATION.fullmatch((text or "").strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _time(text: str) -> int | None:
    """Seconds after midnight in a time such as 06:20:00."""
    match = _TIME.fullmatch(text)
    if not match:
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return (hours * 60 + minutes) * 60 + seconds


def _date(text: str) -> date | None:
    if not _DATE.fullmatch(text):
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


class Shared(dict):
    """One of each equal value the journeys kept from a document hold. BODS
    gives each journey an operating profile of its own, mostly the same few,
    and runs the same times on other days as journeys of their own, so the
    journeys kept share one calendar, one tuple of calls, one of each minute
    and so on, rather than each holding a copy (build.JOURNEY_BYTES). Only what
    the journeys kept hold goes in, and the minutes of the others' calls (at
    most _LATEST // 60 of them), so it holds little more than they do. It
    holds strings, ints, tuples and calendars, never a bool: True would be
    taken for 1."""

    def __call__(self, value):
        return self.setdefault(value, value)

    def kept(self, version: Version, journey: Journey) -> tuple[Version, Journey]:
        """The journey, and the version it is from, as values already held."""
        return self(version), replace(
            journey, line=self(journey.line), operator=self(journey.operator),
            destination=self(journey.destination), calls=self(journey.calls), calendar=self(journey.calendar),
            block=self(journey.block),
        )


def _calls(links: list[_Link], departure: int, shared: Shared | None = None) -> tuple[Call, ...] | None:
    """Each stop with the minute the bus leaves it (reaches it, for the last),
    one of each minute from `shared`; None if the times are nonsense."""
    times = []
    time = departure
    arriving_wait = 0
    for index, link in enumerate(links):
        if index:
            time += arriving_wait if link.origin_wait is None else link.origin_wait
        times.append((link.origin, time))
        time += link.run
        arriving_wait = link.destination_wait
    times.append((links[-1].destination, time))
    if time >= _LATEST:  # the last is the latest: no wait or run time is negative
        return None
    minute = shared if shared is not None else Shared()
    return tuple(Call(atco, minute(seconds // 60)) for atco, seconds in times)


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


class Version(NamedTuple):
    """Which version of which service a journey came from (see build.py's _until_replaced)."""

    service: str  # ServiceCode; "" when the document gives none
    revision: int


def service_of(version: Version, journey: Journey) -> tuple[str, str, str]:
    """The service a journey is a version of: ServiceCode, line and operator."""
    return version.service, journey.line, journey.operator


# A version of a service as seen (see service_of): its start (date.min if it gives
# none), its end (None if open-ended) and its RevisionNumber.
Seen = tuple[date, date | None, int]


def sign(text) -> str:
    """A stop's sign letter or word as NaPTAN gives it ("Stop A", "Opp"):
    printable, short, "" for none."""
    cleaned = "".join(ch for ch in str(text) if ch.isprintable()).strip()
    return cleaned[:20]


def stop_position(name: str, lat: str, lon: str) -> StopPosition | None:
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
    position = stop_position(name, _text(node, "t:Location/t:Latitude"), _text(node, "t:Location/t:Longitude"))
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
    node, doc: _Document, atcos: Collection[str], noc: str, pattern_refs: dict, stands: Collection[str] = (),
    shared: Shared | None = None,
) -> tuple[Version, Journey] | None:
    """The journey a VehicleJourney describes, with the version of the service
    it is in, if it calls at one of `atcos`, or, marked inbound_only, if it
    doesn't but ends at one of `stands`. Its minutes are from `shared`."""
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
    calls = _calls(links, departure, shared)
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
    block = _text(node, "t:Operational/t:Block/t:BlockNumber")
    journey = Journey(line, operator, destination, code, calls, calendar, inbound_only=not calling, block=block)
    return Version(service.code, doc.revision), journey


def may_be_near(
    atco: str, positions: Mapping[str, StopPosition], atcos: Collection[str], yours: Mapping[str, StopPosition]
) -> bool:
    """Whether a stop may be one of your stops or within NEAR_M of one: by the
    timetables' positions (a document's `positions`; `yours` are those known
    from earlier documents), or, while that can't be told, if it is in the
    same ATCO area as one, to be measured once NaPTAN has placed it (see
    build.py's notes)."""
    if atco in atcos:
        return True
    placed = {**yours, **{stop: positions[stop] for stop in atcos if stop in positions}}
    here = positions.get(atco)
    near = here is not None and any(metres(here, there) <= NEAR_M for there in placed.values())
    unplaced = (here is None or len(placed) < len(atcos)) and atco[:3] in {stop[:3] for stop in atcos}
    return near or unplaced


def _stands(doc: _Document, atcos: Collection[str], yours: Mapping[str, StopPosition]) -> set[str]:
    """The stops in a document where a journey that doesn't call at your
    stops may end near one (may_be_near)."""
    return {atco for atco in doc.names if atco not in atcos and may_be_near(atco, doc.positions, atcos, yours)}


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
        yield from safexml.iter_grandchildren(chunks, sections, max_elements=routesetup.MAX_PART_ELEMENTS)
    except safexml.DoctypeError:
        raise TimetableError("Timetable file has a DOCTYPE; refusing to parse it") from None
    except safexml.TooManyElements:
        raise TimetableError(f"Timetable file has a part of more than {routesetup.MAX_PART_ELEMENTS:,} elements; "
                             "refusing to parse it") from None
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
class Room:
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


def line_and_operator(journey: Journey) -> tuple[str, str]:
    """A journey's line and operator, as inbound-only journeys are matched to those calling at your stops."""
    return journey.line.casefold(), journey.operator


def seen_version(version: Version, journey: Journey) -> tuple[tuple[str, str, str], Seen]:
    """The service a journey is a version of (service_of), and that version as seen."""
    return service_of(version, journey), (journey.calendar.start or date.min, journey.calendar.end, version.revision)


def _journeys_kept(
    vehicle_journeys: Iterable, doc: _Document, atcos: Collection[str], noc: str, pattern_refs: dict,
    stands: Collection[str], room: Room | None = None, *, today: date | None = None,
    versions: dict[tuple, set[Seen]] | None = None,
    calling_lines: Callable[[], Set[tuple[str, str]]] | None = None,
) -> list[tuple[Version, Journey]]:
    """The journeys calling at your stops, and the inbound-only ones of the
    lines among them (see build.py's notes), each counted against `room`
    as it is found (no limit without it). Those kept share their equal
    values (Shared).

    A journey whose timetable ended before `today` is let go as it is read,
    uncounted, its version noted in `versions` (an ended version still
    switches those of a lower revision off, build.py's _until_replaced). An inbound-only
    journey of a line with no journey calling at your stops is let go at
    the end. If the room for inbound-only ones fills first,
    `calling_lines()` (the document's lines calling at your stops, read
    again) says which to let go there and then, so they never take room
    from those kept; only those of the lines kept count as left out."""
    room = room or Room()
    found = []
    shared = Shared()
    calling, inbound = room.held, room.held_inbound
    lines: set[tuple[str, str]] = set()  # calling at your stops, ended or not
    ended: set[tuple[tuple[str, str, str], Seen, tuple[str, str] | None]] = set()  # one of each version
    leaving: Set[tuple[str, str]] | None = None  # calling_lines(), once asked
    left_out: dict[tuple[str, str], int] = {}  # by line
    for node in vehicle_journeys:
        read = _journey(node, doc, atcos, noc, pattern_refs, stands, shared)
        if read is None:
            continue
        version, journey = read
        line = line_and_operator(journey)
        if not journey.inbound_only:
            lines.add(line)
        if today is not None and journey.calendar.end and journey.calendar.end < today:
            ended.add((*seen_version(version, journey), line if journey.inbound_only else None))
            continue
        if journey.inbound_only:
            if leaving is not None and line not in leaving:
                continue
            if inbound >= room.inbound and leaving is None and calling_lines is not None:
                leaving = calling_lines()
                found = [(v, j) for v, j in found if not j.inbound_only or line_and_operator(j) in leaving]
                inbound = room.held_inbound + sum(j.inbound_only for _, j in found)
                if line not in leaving:
                    continue
            if inbound >= room.inbound:
                left_out[line] = left_out.get(line, 0) + 1
                continue
            inbound += 1
        elif any(call.atco in atcos for call in journey.calls):
            calling += 1
            if calling > room.journeys:
                raise TimetableError(f"More than {room.journeys} journeys call at your stops; refusing")
        else:
            continue
        found.append(shared.kept(version, journey))
    room.left_out += sum(count for line, count in left_out.items() if line in lines)
    if versions is not None:
        for group, seen, line in ended:
            if line is None or line in lines:
                versions.setdefault(group, set()).add(seen)
    return [(v, j) for v, j in found if not j.inbound_only or line_and_operator(j) in lines]


def read_document(
    xml: bytes, atcos: Collection[str], noc: str, yours: Mapping[str, StopPosition] | None = None,
    room: Room | None = None, *, today: date | None = None, versions: dict[tuple, set[Seen]] | None = None,
) -> tuple[list[tuple[Version, Journey]], dict[str, StopPosition]]:
    """The journeys in a document calling at your stops, each with the version
    of the service it is in, and the document's stop positions. With `yours`
    (your stops' positions known so far), also its inbound-only journeys of
    the lines calling there (see build.py's notes). As many as `room`
    allows (Room; no limit without it). With `today`, those that ended
    before it are left out, their versions noted in `versions`
    (_journeys_kept).

    It is read in two passes over the bytes, each element built and dropped
    in turn (safexml.iter_grandchildren), never the whole tree: first all but
    the journeys, as a journey refers to patterns, services and other
    journeys anywhere in it; then the journeys, keeping only those found. A
    journey taking its pattern from another adds a pass between them
    (_pattern_refs). Memory follows what is kept, not the document
    (routesetup.DocumentLimits), and what is kept is counted as it is found.
    Should the room for inbound-only journeys fill, the journeys are read
    once more for the lines calling at your stops (_journeys_kept)."""
    doc, referenced = _read_first_pass(xml)
    pattern_refs = _pattern_refs(xml, referenced)
    stands = _stands(doc, atcos, yours) if yours is not None else set()

    def calling_lines() -> set[tuple[str, str]]:
        read = (_journey(node, doc, atcos, noc, pattern_refs) for node in _vehicle_journeys(xml))
        return {line_and_operator(journey) for _, journey in filter(None, read) if not journey.inbound_only}

    found = _journeys_kept(_vehicle_journeys(xml), doc, atcos, noc, pattern_refs, stands, room, today=today,
                           versions=versions, calling_lines=calling_lines)
    return found, doc.positions


def parse_document(
    xml: bytes, atcos: Collection[str], noc: str, yours: Mapping[str, StopPosition] | None = None,
    room: Room | None = None, *, today: date | None = None,
) -> tuple[list[Journey], dict[str, StopPosition]]:
    """read_document without the versions: with `today`, those that ended
    before it are let go uncounted, as a build lets them go."""
    found, positions = read_document(xml, atcos, noc, yours, room, today=today)
    return [journey for _, journey in found], positions


def parse_journeys(xml: bytes, atcos: Collection[str], noc: str = "") -> list[Journey]:
    """Every journey in one TransXChange document that calls at one of `atcos`,
    whether it starts, passes or ends there. `noc` is the operator code to use
    when the document doesn't give one."""
    return parse_document(xml, set(atcos), noc)[0]
