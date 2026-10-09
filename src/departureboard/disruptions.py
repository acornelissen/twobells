"""Disruption notices for the dashboard: bus situations from BODS SIRI-SX and
rail incidents (engineering works and the like) from the National Rail
Knowledgebase.

Both feeds cover far more than our corner of the map (SIRI-SX is all of
England, about 5 MB), so each is read as it arrives, one situation or
incident at a time, and only what touches us is kept:

Buses. A situation is ours if one of its consequences names
  - one of our lines run by the operator our timetable has for it (line and
    operator must both match: "61" is a line in a dozen towns);
  - one of our own stops (the ones in [bus] stops);
  - a stop our timetabled journeys call at near our stops (the timetable's
    positioned stops), but only when that consequence lists no lines of its
    own, as with "all lines" or a plain stop closure;
  - or "all lines" of an operator that runs one of our lines, but only when
    one of its affected stops lies within our area (the box around the
    positioned stops, about a kilometre wider each side). A network-wide
    notice with no stops in our area is left out, to keep the noise down.

Trains. An incident is ours if it names one of the operators running our
trains and mentions one of our stations: our own, the filter station, or
any station our trains call at, by name or by its three-letter code.

Either way it must be valid now or at some point in the next seven days.

All feed text is untrusted: it is cut to plain text (any HTML in it is
dropped, never interpreted), and links are kept only if they are https and
on a known operator's site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from html.parser import HTMLParser
from typing import Callable, Iterable, Iterator, Mapping
from urllib.parse import urlencode, urlsplit
from urllib.request import Request
from xml.etree import ElementTree

from . import http, safexml
from .schedule_model import Schedule, StopPosition
from .snapshot import iso, row_id
from .status import safe_message

SIRI_SX_URL = "https://data.bus-data.dft.gov.uk/api/v1/siri-sx/"
# A placeholder: the Rail Data Marketplace gives the real address on the
# Knowledgebase Incidents product page; [dashboard] kb_incidents_url must be
# set to it. The .invalid domain can never resolve, so nothing is ever sent here.
KB_URL_PLACEHOLDER = "https://set-kb-incidents-url-from-the-rdm-product-page.invalid/incidents"

MAX_SX_BYTES = 12_000_000  # all of England is about 5.5 MB
MAX_KB_BYTES = 8_000_000
READ_TIMEOUT = 60.0
HORIZON = timedelta(days=7)
TEXT_LIMIT = 500
TITLE_LIMIT = 160
NAME_LIMIT = 80
CODE_LIMIT = 40
MAX_ITEMS = 30  # per mode
MAX_PERIODS = 14
MAX_PLACES = 10
AREA_MARGIN = 0.01  # degrees, about a kilometre
SEVERITIES = ("slight", "normal", "severe", "unknown")

# Links are kept only to these sites (exact host names): the operators and
# authorities that run or publish our services. Anything else is dropped.
LINK_HOSTS = frozenset({
    "tfgm.com", "www.tfgm.com",
    "beenetwork.com", "www.beenetwork.com",
    "highpeakbuses.com", "www.highpeakbuses.com",
    "stagecoachbus.com", "www.stagecoachbus.com",
    "derbysbus.info", "www.derbysbus.info",
    "nationalrail.co.uk", "www.nationalrail.co.uk",
    "northernrailway.co.uk", "www.northernrailway.co.uk",
})
_LINK_LIMIT = 300

# SIRI severities, from slightest; the page gets slight, normal, severe or unknown.
_SEVERITY = {"noImpact": "slight", "verySlight": "slight", "slight": "slight", "normal": "normal",
             "severe": "severe", "verySevere": "severe"}
_RANK = {"slight": 1, "normal": 2, "severe": 3}
_REASONS = ("MiscellaneousReason", "EquipmentReason", "PersonnelReason", "EnvironmentReason")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CRS = re.compile(r"[A-Z]{3}")

Stream = Callable[[str, Mapping[str, str], int], Iterable[bytes]]


# Plain text from feed text.


class _Text(HTMLParser):
    """Collects the text of a fragment that may hold HTML, as words only."""

    _BREAKS = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4", "ul", "ol"}

    _HIDDEN = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._BREAKS:
            self.parts.append(" ")
        if tag in self._HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag in self._BREAKS:
            self.parts.append(" ")
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def plain_text(value: str | None, limit: int = TEXT_LIMIT) -> str:
    """Feed text as one line of plain words, cut at `limit` characters (with
    an ellipsis). Tags are dropped, entities read, control characters gone."""
    if not value:
        return ""
    parser = _Text()
    parser.feed(value)
    parser.close()
    text = "".join(c if c.isprintable() else " " for c in "".join(parser.parts))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def safe_link(value: str | None) -> str | None:
    """A link worth showing: https, on a known operator's site, no password or
    port, and not too long. None otherwise."""
    if not value:
        return None
    text = value.strip()
    if len(text) > _LINK_LIMIT or any(not c.isprintable() or c.isspace() for c in text):
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        return None
    return text if host in LINK_HOSTS else None


def _aware(text: str | None) -> datetime | None:
    """An ISO 8601 time with its offset; None if missing, unreadable or bare."""
    try:
        when = datetime.fromisoformat((text or "").strip())
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _local(element: ElementTree.Element) -> str:
    return element.tag.rpartition("}")[2]


def _children(element: ElementTree.Element | None, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local(child) == name] if element is not None else []


def _child(element: ElementTree.Element | None, *path: str) -> ElementTree.Element | None:
    """The first element down `path` of local names, in any namespace."""
    for name in path:
        found = _children(element, name)
        element = found[0] if found else None
    return element


def _text(element: ElementTree.Element | None, *path: str, limit: int = NAME_LIMIT) -> str:
    found = _child(element, *path)
    return plain_text(found.text if found is not None else None, limit)


def _descendants(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [node for node in element.iter() if _local(node) == name]


def _long_text(element: ElementTree.Element | None, name: str) -> str:
    """A child's text that may be long or hold HTML, as plain text."""
    found = _child(element, name)
    return plain_text(found.text if found is not None else None)


def _flag(element: ElementTree.Element | None, *path: str) -> bool:
    return _text(element, *path).lower() == "true"


def _periods(element: ElementTree.Element) -> tuple[tuple[datetime, datetime | None], ...]:
    """Each ValidityPeriod's start and end (None: open-ended); unreadable ones skipped."""
    found = []
    for period in _children(element, "ValidityPeriod"):
        start = _aware(_text(period, "StartTime"))
        end_text = _text(period, "EndTime")
        end = _aware(end_text)
        if start is None or (end_text and end is None) or (end is not None and end < start):
            continue
        found.append((start, end))
    return tuple(found)


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


# Bus situations (SIRI-SX).


@dataclass(frozen=True)
class AffectedStop:
    atco: str
    name: str
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class Consequence:
    """One consequence of a situation: what it affects."""

    severity: str  # slight, normal, severe or unknown
    lines: frozenset[tuple[str, str]]  # (line name, operator code)
    operators: frozenset[str]  # operator codes named for the whole consequence
    all_lines: bool
    stops: tuple[AffectedStop, ...]


@dataclass(frozen=True)
class Situation:
    number: str
    summary: str
    description: str
    reason: str | None
    planned: bool
    periods: tuple[tuple[datetime, datetime | None], ...]
    consequences: tuple[Consequence, ...]
    link: str | None


def _reason(element: ElementTree.Element) -> str | None:
    for name in _REASONS:
        text = _text(element, name, limit=CODE_LIMIT)
        if text and text != "unknown" and text.isalpha():
            words = _CAMEL.sub(" ", text).lower()
            return words[:1].upper() + words[1:]
    return None


def _consequence(element: ElementTree.Element) -> Consequence:
    affects = _child(element, "Affects")
    lines, operators, stops = set(), set(), []
    all_lines = False
    for operator in _children(_child(affects, "Operators"), "AffectedOperator"):
        code = _text(operator, "OperatorRef", limit=CODE_LIMIT)
        if code:
            operators.add(code)
    for network in _children(_child(affects, "Networks"), "AffectedNetwork"):
        if _child(network, "AllLines") is not None:
            all_lines = True
        for operator in _children(network, "AffectedOperator"):
            code = _text(operator, "OperatorRef", limit=CODE_LIMIT)
            if code:
                operators.add(code)
        for line in _children(network, "AffectedLine"):
            name = _text(line, "PublishedLineName", limit=CODE_LIMIT) or _text(line, "LineRef", limit=CODE_LIMIT)
            for operator in _children(line, "AffectedOperator") or [None]:
                code = _text(operator, "OperatorRef", limit=CODE_LIMIT) if operator is not None else ""
                if name:
                    lines.add((name, code))
    for stop in _children(_child(affects, "StopPoints"), "AffectedStopPoint"):
        atco = _text(stop, "StopPointRef", limit=CODE_LIMIT)
        if atco:
            stops.append(AffectedStop(atco, _text(stop, "StopPointName"),
                                      _number(_text(stop, "Location", "Latitude")),
                                      _number(_text(stop, "Location", "Longitude"))))
    severity = _SEVERITY.get(_text(element, "Severity", limit=CODE_LIMIT), "unknown")
    return Consequence(severity, frozenset(lines), frozenset(operators), all_lines, tuple(stops))


def _link(element: ElementTree.Element) -> str | None:
    """The first link worth showing (see safe_link), if any."""
    links = _child(element, "InfoLinks")
    for uri in _descendants(links, "Uri") if links is not None else []:
        link = safe_link(uri.text)
        if link:
            return link
    return None


def _situation(element: ElementTree.Element) -> Situation | None:
    """One PtSituationElement, or None if it is closed or says too little to show."""
    if _text(element, "Progress", limit=CODE_LIMIT) == "closed":
        return None
    number = _text(element, "SituationNumber", limit=CODE_LIMIT)
    summary = _text(element, "Summary", limit=TITLE_LIMIT)
    description = _long_text(element, "Description")
    periods = _periods(element)
    if not number or not (summary or description) or not periods:
        return None
    consequences = tuple(_consequence(c) for c in _children(_child(element, "Consequences"), "Consequence"))
    return Situation(number, summary, description, _reason(element), _flag(element, "Planned"), periods,
                     consequences, _link(element))


def parse_siri_sx(chunks: Iterable[bytes]) -> list[Situation]:
    """Every open situation in a SIRI-SX document arriving in chunks (a
    single bytes object is one chunk). Refuses a DOCTYPE."""
    if isinstance(chunks, (bytes, bytearray)):
        chunks = [bytes(chunks)]
    found = []
    for element in safexml.iter_elements(chunks, "PtSituationElement"):
        situation = _situation(element)
        if situation is not None:
            found.append(situation)
    return found


# What counts as ours.


@dataclass(frozen=True)
class BusArea:
    """Our lines and stops, from the bus timetable. Its positioned stops
    include the way into a stand near one of ours (inbound-only journeys):
    trouble there holds up the bus that will work a departure from our stop."""

    lines: Mapping[tuple[str, str], str]  # (line, operator), case-folded -> the line as shown
    stops: Mapping[str, str]  # our stop labels -> ATCO codes
    positioned: Mapping[str, StopPosition]
    box: tuple[float, float, float, float] | None  # west, south, east, north

    @classmethod
    def from_schedule(cls, schedule: Schedule) -> "BusArea":
        lines = {(j.line.casefold(), j.operator.casefold()): j.line for j in schedule.journeys}
        places = list(schedule.positions.values())
        box = None
        if places:
            lats, lons = [p.lat for p in places], [p.lon for p in places]
            box = (min(lons) - AREA_MARGIN, min(lats) - AREA_MARGIN, max(lons) + AREA_MARGIN,
                   max(lats) + AREA_MARGIN)
        return cls(lines, dict(schedule.stops), dict(schedule.positions), box)

    @property
    def operators(self) -> frozenset[str]:
        return frozenset(operator for _, operator in self.lines)

    def inside(self, stop: AffectedStop) -> bool:
        if self.box is None or stop.lat is None or stop.lon is None:
            return False
        west, south, east, north = self.box
        return west <= stop.lon <= east and south <= stop.lat <= north


@dataclass(frozen=True)
class RailArea:
    """Our train operators and stations, from the train board."""

    operators: frozenset[str]  # Darwin operator codes, like "NT"
    stations: Mapping[str, str]  # CRS code -> name


@dataclass(frozen=True)
class Disruption:
    """One disruption that affects us, ready for the snapshot."""

    key: str  # the feed's own number, kept on the server
    mode: str  # "bus" or "train"
    title: str
    summary: str
    reason: str | None
    planned: bool
    periods: tuple[tuple[datetime, datetime | None], ...]  # those touching the next seven days
    lines: tuple[str, ...]
    stops: tuple[str, ...]  # our stop labels
    places: tuple[str, ...]  # stop or station names, in words
    severity: str
    link: str | None


def _soon(periods, now: datetime) -> tuple[tuple[datetime, datetime | None], ...]:
    """The periods that touch the time from now to seven days ahead, in order."""
    until = now + HORIZON
    found = [(start, end) for start, end in periods if start <= until and (end is None or end >= now)]
    return tuple(sorted(found, key=lambda p: p[0])[:MAX_PERIODS])


def _add(names: list[str], name: str) -> None:
    if name and name not in names and len(names) < MAX_PLACES:
        names.append(name)


def _bus_match(situation: Situation, area: BusArea) -> tuple[set[str], set[str], list[str]] | None:
    """(our lines, our stop labels, place names) a situation affects, or None."""
    lines, labels, places = set(), set(), []
    matched = False
    our_operators = area.operators
    for consequence in situation.consequences:
        for line, operator in consequence.lines:
            shown = area.lines.get((line.casefold(), operator.casefold()))
            if shown is not None:
                lines.add(shown)
        for stop in consequence.stops:
            ours = [label for label, atco in area.stops.items() if atco == stop.atco]
            position = area.positioned.get(stop.atco)
            if ours or (position is not None and not consequence.lines):
                matched = True
                labels.update(ours)
            if ours or position is not None:
                _add(places, stop.name or (position.name if position else ""))
        operators = {o.casefold() for o in consequence.operators} & our_operators
        if consequence.all_lines and operators and any(area.inside(s) for s in consequence.stops):
            lines.update(shown for (_, op), shown in area.lines.items() if op in operators)
            for stop in consequence.stops:
                if area.inside(stop):
                    _add(places, stop.name)
    if not (lines or matched):
        return None
    return lines, labels, places


def _worst(severities: Iterable[str]) -> str:
    known = [s for s in severities if s in _RANK]
    return max(known, key=_RANK.get) if known else "unknown"


def bus_disruptions(situations: Iterable[Situation], area: BusArea | None, now: datetime) -> list[Disruption]:
    """The situations that affect our lines or stops in the next seven days,
    soonest first. See the module's notes for what counts."""
    if area is None:
        return []
    found = []
    for situation in situations:
        periods = _soon(situation.periods, now)
        if not periods:
            continue
        hit = _bus_match(situation, area)
        if hit is None:
            continue
        lines, labels, places = hit
        found.append(Disruption(
            key=situation.number, mode="bus", title=situation.summary or "Bus disruption",
            summary=situation.description, reason=situation.reason, planned=situation.planned,
            periods=periods, lines=tuple(sorted(lines, key=_line_order)), stops=tuple(sorted(labels)),
            places=tuple(places), severity=_worst(c.severity for c in situation.consequences),
            link=situation.link,
        ))
    return _ordered(found)


def _line_order(line: str) -> tuple:
    digits = "".join(c for c in line if c.isdigit())
    return (int(digits) if digits else 10**6, line)


def _ordered(found: list[Disruption]) -> list[Disruption]:
    unique = {d.key: d for d in found}
    return sorted(unique.values(), key=lambda d: (d.periods[0][0], d.title))[:MAX_ITEMS]


# Rail incidents (National Rail Knowledgebase).
#
# Read by local names, in any namespace, from the Knowledgebase Incidents
# XML as National Rail documents it: PtIncident elements, each with
# IncidentNumber, ValidityPeriod (StartTime, EndTime), Planned, Summary,
# Description (HTML), InfoLinks/InfoLink/Uri, Affects (Operators/
# AffectedOperator/OperatorRef and RoutesAffected, HTML), ClearedIncident and
# IncidentPriority. Not yet checked against a live feed.


@dataclass(frozen=True)
class Incident:
    number: str
    summary: str
    description: str
    routes: str
    planned: bool
    periods: tuple[tuple[datetime, datetime | None], ...]
    operators: frozenset[str]
    severity: str
    link: str | None


# IncidentPriority: 1 is the most serious.
_PRIORITY = {"1": "severe", "2": "normal", "3": "slight", "4": "slight"}


def _incident(element: ElementTree.Element) -> Incident | None:
    if _flag(element, "ClearedIncident"):
        return None
    number = _text(element, "IncidentNumber", limit=CODE_LIMIT)
    summary = _text(element, "Summary", limit=TITLE_LIMIT)
    description = _long_text(element, "Description")
    periods = _periods(element)
    if not number or not (summary or description) or not periods:
        return None
    affects = _child(element, "Affects")
    operators = frozenset(
        code for operator in _children(_child(affects, "Operators"), "AffectedOperator")
        if (code := _text(operator, "OperatorRef", limit=CODE_LIMIT))
    )
    routes = _long_text(affects, "RoutesAffected")
    return Incident(number, summary, description, routes, _flag(element, "Planned"), periods, operators,
                    _PRIORITY.get(_text(element, "IncidentPriority", limit=CODE_LIMIT), "unknown"),
                    _link(element))


def parse_kb_incidents(chunks: Iterable[bytes]) -> list[Incident]:
    """Every incident not yet cleared in a Knowledgebase Incidents document
    arriving in chunks (a single bytes object is one chunk). Refuses a DOCTYPE."""
    if isinstance(chunks, (bytes, bytearray)):
        chunks = [bytes(chunks)]
    found = []
    for element in safexml.iter_elements(chunks, "PtIncident"):
        incident = _incident(element)
        if incident is not None:
            found.append(incident)
    return found


def _mentions(text: str, name: str, crs: str) -> bool:
    if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
        return True
    return bool(_CRS.fullmatch(crs)) and re.search(rf"(?<![A-Za-z]){crs}(?![A-Za-z])", text) is not None


def train_disruptions(incidents: Iterable[Incident], area: RailArea | None, now: datetime) -> list[Disruption]:
    """The incidents that name one of our operators and mention one of our
    stations, valid now or in the next seven days, soonest first."""
    if area is None or not area.operators:
        return []
    found = []
    for incident in incidents:
        periods = _soon(incident.periods, now)
        if not periods or not (incident.operators & area.operators):
            continue
        text = " ".join((incident.summary, incident.routes, incident.description))
        places: list[str] = []
        for crs, name in sorted(area.stations.items()):
            if _mentions(text, name, crs):
                _add(places, name or crs)
        if not places:
            continue
        found.append(Disruption(
            key=incident.number, mode="train", title=incident.summary or "Rail disruption",
            summary=incident.description or incident.routes, reason=None, planned=incident.planned,
            periods=periods, lines=(), stops=(), places=tuple(places), severity=incident.severity,
            link=incident.link,
        ))
    return _ordered(found)


# The snapshot's disruptions section (docs/dashboard-api.md).


def _item(disruption: Disruption, tz: tzinfo) -> dict:
    periods = [{"from_at": iso(start.astimezone(tz)), "to_at": iso(end.astimezone(tz)) if end else None}
               for start, end in disruption.periods]
    return {
        "id": row_id("disruption", f"{disruption.mode}|{disruption.key}"),
        "mode": disruption.mode,
        "title": disruption.title,
        "summary": disruption.summary,
        "reason": disruption.reason,
        "from_at": periods[0]["from_at"],
        "to_at": periods[0]["to_at"],
        "periods": periods,
        "planned": disruption.planned,
        "lines": list(disruption.lines),
        "stops": list(disruption.stops),
        "places": list(disruption.places),
        "severity": disruption.severity if disruption.severity in SEVERITIES else "unknown",
        "link": disruption.link,
    }


def disruptions_part(bus: list[Disruption], bus_status: dict | None, train: list[Disruption],
                     train_status: dict | None, tz: tzinfo) -> dict:
    """The disruptions section: each source's status (None when it isn't set
    up) and the items, buses then trains, each soonest first."""
    return {
        "status": {"bus": bus_status, "train": train_status},
        "items": [_item(d, tz) for d in [*bus, *train]],
    }


# Fetching.


def stream(url: str, headers: Mapping[str, str], max_bytes: int, timeout: float = READ_TIMEOUT) -> Iterator[bytes]:
    """A response body in chunks as it arrives, refusing anything but https
    and more than `max_bytes` (http.TooLarge)."""
    if urlsplit(url).scheme != "https":
        raise ValueError("Refusing a feed address that isn't https")
    request = Request(url, headers={"User-Agent": http.USER_AGENT, **headers})
    total = 0
    with http.urlopen(request, timeout=timeout) as response:
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise http.TooLarge(f"Response larger than {max_bytes} bytes")
            yield chunk


def _failure(error: BaseException, key: str) -> RuntimeError:
    """The error as one safe line, with the key taken out whatever its form."""
    if isinstance(error, safexml.DoctypeError):
        text = "The feed sent a DOCTYPE; refused"
    elif isinstance(error, ElementTree.ParseError):
        text = "The feed sent something that isn't XML"
    else:
        text = str(error) or type(error).__name__
    if key:
        text = text.replace(key, "REDACTED")
    return RuntimeError(safe_message(text))


class BusDisruptionSource:
    """Every open situation in England from BODS SIRI-SX (same key as the buses)."""

    def __init__(self, api_key: str, url: str = SIRI_SX_URL, open_stream: Stream = stream) -> None:
        self._api_key = api_key
        self._url = url
        self._open = open_stream

    def fetch(self) -> list[Situation]:
        url = f"{self._url}?{urlencode({'api_key': self._api_key})}"
        try:
            return parse_siri_sx(self._open(url, {}, MAX_SX_BYTES))
        except Exception as error:  # noqa: BLE001 - re-raised without the key
            raise _failure(error, self._api_key) from None


class TrainIncidentSource:
    """Incidents from the National Rail Knowledgebase, through the Rail Data
    Marketplace (its own key, RDM_KB_API_KEY, in an x-apikey header)."""

    def __init__(self, api_key: str, url: str, open_stream: Stream = stream) -> None:
        self._api_key = api_key
        self._url = url
        self._open = open_stream

    def fetch(self) -> list[Incident]:
        try:
            return parse_kb_incidents(self._open(self._url, {"x-apikey": self._api_key}, MAX_KB_BYTES))
        except Exception as error:  # noqa: BLE001 - re-raised without the key
            raise _failure(error, self._api_key) from None


def kb_url_is_placeholder(url: str) -> bool:
    return (urlsplit(url).hostname or "").endswith(".invalid")
