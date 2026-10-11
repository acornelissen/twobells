"""National Rail Darwin Live Departure Board (LDBWS) via the Rail Data Marketplace."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

from ..core.errors import safe_message
from ..core.model import Departure, Status, minutes_between
from ..core.net import NotJSON, get_json_capped

DEFAULT_BASE_URL = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/LDBWS/api/20220120"
# The Rail Data Marketplace's "Service Details" product: one train's whole journey.
DETAILS_BASE_URL = "https://api1.raildata.org.uk/1010-service-details1_2/LDBWS/api/20220120"

_HHMM = re.compile(r"^\d{2}:\d{2}$")
# Platform labels are short: "1", "10", "2A".
_PLATFORM = re.compile(r"^[0-9A-Za-z]{1,4}$")

HttpGet = Callable[[str, dict], dict]


def _status_and_expected(service: dict) -> tuple[Status, str | None]:
    etd = (service.get("etd") or "").strip()
    if service.get("isCancelled") or etd == "Cancelled":
        return Status.CANCELLED, None
    if etd == "On time" or etd == service.get("std"):
        return Status.ON_TIME, None
    if etd == "Delayed":
        return Status.LATE, None
    if _HHMM.match(etd):
        return Status.LATE, etd
    return Status.UNKNOWN, None


def _platform(value) -> str | None:
    """The platform if it looks like one; anything else is left off the board."""
    if isinstance(value, str) and _PLATFORM.match(value.strip()):
        return value.strip()
    return None


def _parse_service(service: dict) -> Departure:
    """A Departure from a service whose fields _rich_service has checked."""
    status, expected = _status_and_expected(service)
    destinations = service.get("destination") or []
    return Departure(
        mode="train",
        service=service.get("operator") or "",
        destination=" & ".join(d.get("locationName", "") for d in destinations),
        scheduled=service["std"],
        expected=expected,
        status=status,
        platform=_platform(service.get("platform")),
    )


def parse_board(board: dict) -> list[Departure]:
    """The departures alone, read with the rich board's checks (below): a
    train without a clock time to leave at is skipped."""
    return [s.departure for s in parse_board_rich(board).services]


# Incident messages (NRCC) for the station: a few, each short enough to read on a board.
MESSAGE_LIMIT = 300
_MAX_MESSAGES = 3
_MARKUP_LIMIT = 5000  # characters of HTML read per message; real ones are a few hundred
_MESSAGE_KEYS = ("Value", "value", "xhtmlMessage", "message")


_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextOnly(HTMLParser):
    """Collects the text of an HTML fragment, entities decoded. Block tags
    become spaces, so "<p>One</p><p>Two</p>" doesn't run the words together;
    inline ones like <a> vanish, so a link's text keeps its full stop."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append(" ")


def plain_text(markup: str, limit: int = MESSAGE_LIMIT) -> str:
    """An HTML message as one line of plain text, cut to `limit` characters."""
    parser = _TextOnly()
    parser.feed(markup[:_MARKUP_LIMIT])
    parser.close()
    text = " ".join("".join(parser.parts).split())
    text = "".join(c for c in text if c.isprintable())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # End on a whole word unless that throws most of it away.
    if " " in cut[limit // 2 :]:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


# LDBWS (2021 on) gives each message a category and a severity, as Darwin's
# station messages have them. Both are read if sent, in either spelling, and
# anything else is left as None.
_CATEGORIES = {"train": "train", "station": "station", "connections": "connections", "system": "system",
               "misc": "misc", "priortrains": "prior_trains", "priorother": "prior_other"}
_SEVERITIES = {"normal": "normal", "minor": "minor", "major": "major", "severe": "severe",
               "0": "normal", "1": "minor", "2": "major", "3": "severe"}
# Words that make a message about the trains, whatever its category: a
# station notice must never hide one.
_SERVICE_WORDS = re.compile(
    r"\b(disrupt\w*|cancel\w*|delay\w*|no trains?|trains? (will|may|are|is|do) not|"
    r"replacement (bus|coach)\w*|station (is )?closed)\b", re.IGNORECASE)
# The station's facilities, for a message that comes without a category.
_FACILITY_WORDS = re.compile(
    r"\b(ticket (offices?|machines?|vending|barriers?)|booking offices?|lifts?|escalators?|toilets?|"
    r"waiting (rooms?|areas?)|car parks?|help points?|step[- ]free|customer information (screens?|systems?)|"
    r"caf[eé]s?|shops?|cycle (hire|storage|parking)|wi-?fi|payphones?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Message:
    """One of the station's NRCC messages."""

    text: str  # plain text, cut to MESSAGE_LIMIT
    category: str | None  # train, station, connections, system, misc, prior_trains or prior_other
    severity: str | None  # normal, minor, major or severe
    kind: str  # "station" for a notice about the station's facilities, else "service"


def _markups(value, depth: int = 0, meta: dict | None = None):
    """(HTML, the object it came in or None) for each message in
    nrccMessages, whichever shape it comes in: a list of {"Value": html},
    a single one, plain strings, or the other key spellings."""
    if depth > 3:
        return
    if isinstance(value, str):
        yield value, meta
    elif isinstance(value, list):
        for item in value:
            yield from _markups(item, depth + 1, meta)
    elif isinstance(value, dict):
        key = next((k for k in _MESSAGE_KEYS if k in value), None)
        if key is not None:
            yield from _markups(value[key], depth + 1, value)


def _field(meta: dict | None, names: tuple[str, ...], known: dict[str, str]) -> str | None:
    """A category or severity from the message's object, if it is one we know."""
    for name in names:
        value = meta.get(name) if meta is not None else None
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str) and value.strip().lower() in known:
            return known[value.strip().lower()]
    return None


def message_kind(text: str, category: str | None) -> str:
    """Whether a message is a "station" notice about the station's facilities
    (ticket office, lifts and the like) or a "service" one. Darwin's
    category decides when it is sent; without one, the words do. Words about
    the trains (delays, cancellations, a closed station) always make it a
    service message."""
    if _SERVICE_WORDS.search(text):
        return "service"
    if category is not None:
        return "station" if category == "station" else "service"
    return "station" if _FACILITY_WORDS.search(text) else "service"


def parse_messages(board: dict) -> list[Message]:
    """The station's incident messages, at most a few, each once."""
    messages: list[Message] = []
    for markup, meta in _markups(board.get("nrccMessages")):
        text = plain_text(markup)
        if not text or any(m.text == text for m in messages):
            continue
        category = _field(meta, ("category", "Category"), _CATEGORIES)
        severity = _field(meta, ("severity", "Severity"), _SEVERITIES)
        messages.append(Message(text, category, severity, message_kind(text, category)))
        if len(messages) == _MAX_MESSAGES:
            break
    return messages


# The rich board, from GetDepBoardWithDetails: each train's coaches, origin,
# reasons and the stops it calls at after this station. All of it comes from
# outside, so every field is checked as it is read: a value of the wrong type
# or shape is left out (None, "" or skipped), never raised.

NAME_LIMIT = 80
REASON_LIMIT = 200
_MAX_CALLING_POINTS = 150
_MAX_CALLING_LISTS = 10  # one per part of a train that divides
_MAX_ENDS = 4  # origins or destinations of one train; two when it divides
_MAX_COACHES = 30

_CLOCK = re.compile(r"[01][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]")
_TIME_WORDS = {"On time", "Delayed", "Cancelled", "No report"}
_CRS = re.compile(r"[A-Z]{3}")
_SERVICE_ID = re.compile(r"[A-Za-z0-9_+/=-]{6,64}")
_OPERATOR_CODE = re.compile(r"[A-Z0-9]{2,3}")
# Like 2026-10-09T07:00:00+01:00
_GENERATED_AT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})?"
)


@dataclass(frozen=True)
class CallingPoint:
    """A stop the train calls at after this station."""

    name: str
    crs: str | None
    scheduled: str  # HH:MM
    expected: str | None  # HH:MM or one of Darwin's words, like "On time"
    actual: str | None  # the same, once the train has been there
    cancelled: bool
    length: int | None  # coaches from here, when Darwin knows


@dataclass(frozen=True)
class TrainService:
    departure: Departure
    service_id: str | None
    origin: str
    operator_code: str
    length: int | None  # coaches
    cancel_reason: str | None
    delay_reason: str | None
    calling_points: tuple[CallingPoint, ...]


@dataclass(frozen=True)
class TrainBoard:
    services: list[TrainService]
    messages: list[Message]
    generated_at: str | None  # Darwin's own ISO 8601 timestamp, as sent
    location: str | None = None  # the station's name, like "Glossop"


def _name(value) -> str | None:
    """A place or operator name: printable, on one line, cut to NAME_LIMIT."""
    if not isinstance(value, str):
        return None
    # Whitespace first, so a non-breaking space still parts two words.
    text = "".join(c for c in " ".join(value.split()) if c.isprintable())
    return text[:NAME_LIMIT].rstrip() or None


def _matching(pattern: re.Pattern, value) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return None


def _time(value) -> str | None:
    """HH:MM or one of the words Darwin uses in place of a time."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value in _TIME_WORDS:
        return value
    return _matching(_CLOCK, value)


def _coaches(value) -> int | None:
    # bool is an int in Python, and True coaches is not a train.
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_COACHES:
        return value
    return None


def _reason(value) -> str | None:
    """A cancellation or delay reason. LDBWS sends a string; a newer shape
    wraps it as {"value": "..."}, so both are read."""
    if isinstance(value, dict):
        value = value.get("value", value.get("Value"))
    if not isinstance(value, str):
        return None
    return plain_text(value, REASON_LIMIT) or None


def _locations(value) -> list[dict]:
    """[{"locationName": ..., "crs": ...}] with each name checked; others dropped."""
    if not isinstance(value, list):
        return []
    places = []
    for item in value[:_MAX_ENDS]:
        name = _name(item.get("locationName")) if isinstance(item, dict) else None
        if name:
            places.append({"locationName": name, "crs": _matching(_CRS, item.get("crs"))})
    return places


def _as_list(value) -> list:
    """Darwin's lists sometimes arrive as a single object; anything else is empty."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _calling_point(point) -> CallingPoint | None:
    if not isinstance(point, dict):
        return None
    name = _name(point.get("locationName"))
    scheduled = _matching(_CLOCK, point.get("st"))
    if name is None or scheduled is None:
        return None
    return CallingPoint(
        name=name,
        crs=_matching(_CRS, point.get("crs")),
        scheduled=scheduled,
        expected=_time(point.get("et")),
        actual=_time(point.get("at")),
        cancelled=point.get("isCancelled") is True,
        length=_coaches(point.get("length")),
    )


def _point_lists(value) -> list[tuple[CallingPoint, ...]]:
    """Darwin's calling point lists, one per part of a train that divides or
    joins, each checked and capped; empty lists are left out."""
    lists = []
    for part in _as_list(value)[:_MAX_CALLING_LISTS]:
        if not isinstance(part, dict):
            continue
        raw = _as_list(part.get("callingPoint"))[:_MAX_CALLING_POINTS]
        points = tuple(p for p in map(_calling_point, raw) if p is not None)
        if points:
            lists.append(points)
    return lists


def _calling_points(service: dict, destinations: list[dict]) -> tuple[CallingPoint, ...]:
    """The stops after this station. A train that divides has one list per
    part; we keep the one that ends at a destination on the board (the first
    such, by crs or else by name), and otherwise the first list, which is the
    main train in Darwin's order."""
    lists = _point_lists(service.get("subsequentCallingPoints"))
    if not lists:
        return ()
    crs_codes = {d["crs"] for d in destinations if d["crs"]}
    names = {d["locationName"] for d in destinations}
    for points in lists:
        last = points[-1]
        if (last.crs and last.crs in crs_codes) or last.name in names:
            return points
    return lists[0]


def _rich_service(service) -> TrainService | None:
    """One train from the rich board, or None if it lacks a departure time.
    The Departure is built by _parse_service from checked values only, so the
    plain board's rules (status, platform) apply unchanged."""
    if not isinstance(service, dict):
        return None
    std = _matching(_CLOCK, service.get("std"))
    if std is None:
        return None
    destinations = _locations(service.get("destination"))
    checked = {
        "std": std,
        # An estimate that is neither a time nor a known word reads as unknown.
        "etd": _time(service.get("etd")) or "",
        "isCancelled": service.get("isCancelled") is True,
        "operator": _name(service.get("operator")) or "",
        "destination": destinations,
        "platform": service.get("platform"),
    }
    return TrainService(
        departure=_parse_service(checked),
        service_id=_matching(_SERVICE_ID, service.get("serviceID")),
        origin=" & ".join(o["locationName"] for o in _locations(service.get("origin"))),
        operator_code=_matching(_OPERATOR_CODE, service.get("operatorCode")) or "",
        length=_coaches(service.get("length")),
        cancel_reason=_reason(service.get("cancelReason")),
        delay_reason=_reason(service.get("delayReason")),
        calling_points=_calling_points(service, destinations),
    )


def parse_board_rich(board) -> TrainBoard:
    """The board from GetDepBoardWithDetails (or GetDepartureBoard, which
    just has no calling points). Raises ValueError only if it isn't a board."""
    if not isinstance(board, dict):
        raise ValueError("Darwin sent something other than a departure board")
    services = []
    for raw in _as_list(board.get("trainServices")):
        service = _rich_service(raw)
        if service is not None:
            services.append(service)
    return TrainBoard(
        services=services,
        messages=parse_messages(board),
        generated_at=_matching(_GENERATED_AT, board.get("generatedAt")),
        location=_name(board.get("locationName")),
    )


# The detailed board is a few KB per train; this leaves plenty of room for a
# long board and still stops a runaway response. The plain board and one
# train's details are smaller, so they are read with the same cap.
# get_json_capped also gives up on the whole request after its deadline.
DETAILS_MAX_BYTES = 512 * 1024


def _get_details(url: str, headers: dict) -> dict:
    return get_json_capped(url, headers, DETAILS_MAX_BYTES)


class DarwinSource:
    name = "train"

    def __init__(
        self,
        api_key: str,
        crs: str,
        filter_crs: str | None = None,
        rows: int = 3,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGet | None = None,
        with_details: bool = False,
    ) -> None:
        self._api_key = api_key
        self._crs = crs
        self._filter_crs = filter_crs
        self._rows = rows
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get
        # Ask for GetDepBoardWithDetails, the board with calling points.
        self.with_details = with_details
        # The messages' text from the last successful fetch; the app keeps
        # them with the departures, for the e-paper board's notice.
        self.messages: list[str] = []
        # The trains of the last successful fetch, with their service IDs, so
        # a screen can ask where they are (TrainDetails). Kept in memory only.
        self.services: list[TrainService] = []

    def _url(self) -> str:
        params: dict[str, int | str] = {"numRows": self._rows}
        if self._filter_crs:
            params.update(filterCrs=self._filter_crs, filterType="to")
        method = "GetDepBoardWithDetails" if self.with_details else "GetDepartureBoard"
        return f"{self._base_url}/{method}/{self._crs}?{urlencode(params)}"

    def _get(self) -> dict:
        http_get = self._http_get or _get_details  # capped, either board
        return http_get(self._url(), {"x-apikey": self._api_key})

    def fetch_board(self) -> TrainBoard:
        """The rich board: calling points too when with_details is set."""
        board = parse_board_rich(self._get())
        self.messages = [m.text for m in board.messages]
        self.services = board.services
        return board

    def fetch(self) -> list[Departure]:
        # The plain board reads the same way (parse_board), just without calling points.
        return [s.departure for s in self.fetch_board().services]


# One train's whole journey, from GetServiceDetails (the Rail Data
# Marketplace's "Service Details" product, with its own key). It is read with
# the same checks as the rich board, and the service ID only ever goes into
# the URL's path, quoted.


class DetailsError(Exception):
    """Service details couldn't be had. The text never carries the key or a URL query."""


class DetailsUnavailable(DetailsError):
    """The key isn't allowed to ask for service details (HTTP 401 or 403)."""


class ServiceGone(DetailsError):
    """Darwin no longer knows the train (HTTP 404, or an empty answer)."""


@dataclass(frozen=True)
class StationCall(CallingPoint):
    """The train at this station: a calling point whose scheduled, expected
    and actual times are the departure's (the arrival's where it ends here),
    with Darwin's arrival and departure times too: HH:MM, a word, or None."""

    sta: str | None = None
    eta: str | None = None
    ata: str | None = None
    std: str | None = None
    etd: str | None = None
    atd: str | None = None


@dataclass(frozen=True)
class ServiceDetails:
    previous: tuple[CallingPoint, ...]  # the stops before this station, in order
    subsequent: tuple[CallingPoint, ...]  # and after it
    here: StationCall
    cancel_reason: str | None
    delay_reason: str | None
    length: int | None  # coaches
    generated_at: str | None  # Darwin's own ISO 8601 timestamp, as sent


def _station_call(raw: dict) -> StationCall | None:
    name = _name(raw.get("locationName"))
    sta, std = _matching(_CLOCK, raw.get("sta")), _matching(_CLOCK, raw.get("std"))
    scheduled = std or sta
    if name is None or scheduled is None:
        return None
    eta, ata, etd, atd = (_time(raw.get(key)) for key in ("eta", "ata", "etd", "atd"))
    return StationCall(
        name=name,
        crs=_matching(_CRS, raw.get("crs")),
        scheduled=scheduled,
        expected=etd if std else eta,
        actual=atd if std else ata,
        cancelled=raw.get("isCancelled") is True or "Cancelled" in (eta, etd),
        length=_coaches(raw.get("length")),
        sta=sta, eta=eta, ata=ata, std=std, etd=etd, atd=atd,
    )


def parse_service_details(raw) -> ServiceDetails:
    """The answer from GetServiceDetails. Raises ServiceGone if it isn't a
    service with a time at this station; anything else odd is left out."""
    if not isinstance(raw, dict):
        raise ServiceGone("Darwin sent no service")
    here = _station_call(raw)
    if here is None:
        raise ServiceGone("Darwin sent no times for this station")
    # A train that joins another has a list per part; the first is the main
    # train in Darwin's order.
    previous = _point_lists(raw.get("previousCallingPoints"))
    return ServiceDetails(
        previous=previous[0] if previous else (),
        subsequent=_calling_points(raw, []),
        here=here,
        cancel_reason=_reason(raw.get("cancelReason")),
        delay_reason=_reason(raw.get("delayReason")),
        length=_coaches(raw.get("length")),
        generated_at=_matching(_GENERATED_AT, raw.get("generatedAt")),
    )


class ServiceDetailsClient:
    """Asks GetServiceDetails about one train at a time."""

    def __init__(self, api_key: str, base_url: str = DETAILS_BASE_URL, http_get: HttpGet | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get or _get_details

    def _safe(self, error: BaseException) -> str:
        text = str(error) or type(error).__name__
        if self._api_key:
            text = text.replace(self._api_key, "REDACTED")
        return safe_message(text)

    def details(self, service_id: str) -> ServiceDetails:
        """The train's journey. Raises ValueError for an ID that isn't one,
        DetailsUnavailable, ServiceGone, or DetailsError for anything else."""
        if _matching(_SERVICE_ID, service_id) is None:
            raise ValueError("Not a Darwin service ID")
        url = f"{self._base_url}/GetServiceDetails/{quote(service_id, safe='')}"
        try:
            raw = self._http_get(url, {"x-apikey": self._api_key})
        except HTTPError as error:
            if error.code in (401, 403):
                raise DetailsUnavailable(f"Service details refused the key (HTTP {error.code})") from None
            if error.code == 404:
                raise ServiceGone("Darwin no longer knows this train") from None
            raise DetailsError(self._safe(error)) from None
        except NotJSON:
            raise ServiceGone("Darwin sent an empty or unreadable answer") from None
        except Exception as error:  # noqa: BLE001 - raised again without the key or the URL
            raise DetailsError(self._safe(error)) from None
        return parse_service_details(raw)


class DetailsSource(Protocol):
    def details(self, service_id: str) -> ServiceDetails: ...


DETAILS_TRAINS = 3  # trains placed on their route: the first ones not cancelled
DETAILS_TTL = 60.0  # seconds before a train's details are asked for again
DETAILS_OFF = 6 * 3600.0  # seconds without details after the key is refused


class TrainDetails:
    """Service details for the first few trains on a board that aren't
    cancelled, each asked for at most once a minute. Details of trains that
    have left the board, or been cancelled, are forgotten. A refused key turns
    details off for six hours; any other failure skips that train this round,
    keeping what was known. The dashboard and a screen's board keep one each."""

    def __init__(self, client: DetailsSource, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._monotonic = monotonic
        # By Darwin service ID, with when they came (monotonic).
        self._found: dict[str, tuple[float, ServiceDetails]] = {}
        self._off_until: float | None = None

    def refresh(self, services: list[TrainService]) -> list[tuple[str, Exception | None]]:
        """Asks for the details that are due. Says how each ask went, in
        order: None if it worked, else the error. DetailsUnavailable ends the
        round; ServiceGone forgets that train."""
        running = [s.service_id for s in services
                   if s.service_id and s.departure.status is not Status.CANCELLED]
        self._found = {sid: entry for sid, entry in self._found.items() if sid in running}
        if self._off_until is not None and self._monotonic() < self._off_until:
            return []
        results: list[tuple[str, Exception | None]] = []
        for service_id in running[:DETAILS_TRAINS]:
            entry = self._found.get(service_id)
            if entry is not None and self._monotonic() - entry[0] < DETAILS_TTL:
                continue
            try:
                found = self._client.details(service_id)
            except DetailsUnavailable as error:
                self._off_until = self._monotonic() + DETAILS_OFF
                self._found = {}
                results.append((service_id, error))
                return results
            except ServiceGone as error:
                self._found.pop(service_id, None)
                results.append((service_id, error))
            except Exception as error:  # noqa: BLE001 - one train's position matters less than the rest
                results.append((service_id, error))
            else:
                self._found[service_id] = (self._monotonic(), found)
                results.append((service_id, None))
        return results

    def known(self, max_age: float | None = None) -> dict[str, ServiceDetails]:
        """The details known, by service ID; with `max_age`, only those that
        came at most that many seconds ago."""
        now = self._monotonic()
        return {sid: found for sid, (came, found) in self._found.items()
                if max_age is None or 0 <= now - came <= max_age}


# Where the train is, for the dashboard's strip: worked out from the stops
# Darwin says it has been to, and the times between them.

STRIP_BEFORE = 5  # stops shown before this station
_MOST_OF_THE_WAY = 0.9  # never drawn at a stop before Darwin says it got there


@dataclass(frozen=True)
class TrainPosition:
    where: str  # a plain sentence, like "Left Hadfield 22:44, on time"
    strip_stops: tuple[str, ...]  # up to 5 stops before this station, it, and the next
    yours: int  # this station's index in strip_stops
    train_at: float | None  # the train in stop units along strip_stops; None if not on it
    # The same in a few words, for a screen's row: "Left Hadfield", "At Glossop".
    short: str = field(default="", compare=False)


def _clock(value: str | None, scheduled: str) -> str:
    """A time as HH:MM: Darwin's clock time if it gave one, else the booked
    time ("On time", "No report", "Delayed" and nothing all fall back to it)."""
    return value if value and _CLOCK.fullmatch(value) else scheduled


def _minutes_to(hhmm: str, now: datetime) -> float:
    """Minutes from now until hhmm, the nearest such time (within 12 hours)."""
    return minutes_between(f"{now:%H:%M}", hhmm) - now.second / 60 - now.microsecond / 60_000_000


def _along(left: str, due: str, now: datetime) -> float:
    """How far the train has come from a stop it left at `left` towards one
    it is due at `due`: a fraction from 0 to most of the way."""
    start, end = _minutes_to(left, now), _minutes_to(due, now)
    if end <= start:
        return 0.0
    return min(max(-start / (end - start), 0.0), _MOST_OF_THE_WAY)


def _passed(point: CallingPoint) -> bool:
    return point.actual is not None and point.actual != "Cancelled"


def _left(point: CallingPoint) -> str:
    """"Left Hadfield 22:44, on time", "Left Guide Bridge, 3 min late", or
    just "Left Flowery Field" when no time was reported."""
    actual = point.actual
    if actual == "On time":
        return f"Left {point.name} {point.scheduled}, on time"
    if actual and _CLOCK.fullmatch(actual):
        late = minutes_between(point.scheduled, actual)
        if late > 0:
            return f"Left {point.name}, {late} min late"
        return f"Left {point.name} {actual}, on time"
    return f"Left {point.name}"


def train_position(details: ServiceDetails | None, now: datetime) -> TrainPosition | None:
    """Where the train is now, in words and along the strip; None without
    details, or when it is cancelled here. `now` is local time."""
    if details is None or details.here.cancelled:
        return None
    here, previous, after = details.here, details.previous, details.subsequent
    before = previous[-STRIP_BEFORE:]
    stops = tuple(p.name for p in before) + (here.name,) + ((after[0].name,) if after else ())
    yours = len(before)
    # A call has a booked arrival or departure (see _station_call), and `scheduled` is one of them.
    arrives = _clock(here.eta, here.sta) if here.sta else _clock(here.etd, here.std or here.scheduled)

    if here.atd is not None:
        if not after:
            train_at = yours
        elif _passed(after[0]):
            train_at = yours + 1
        else:
            left_here = _clock(here.atd, here.std or here.scheduled)
            train_at = yours + _along(left_here, _clock(after[0].expected, after[0].scheduled), now)
        return TrainPosition(f"Departed {here.name}", stops, yours, round(train_at, 3), "Departed")
    if here.ata is not None:
        return TrainPosition(f"At {here.name} now", stops, yours, yours, f"At {here.name}")

    last = max((i for i, p in enumerate(previous) if _passed(p)), default=None)
    if last is None:
        if previous:
            first = previous[0]
            starts = _clock(first.expected, first.scheduled)
            return TrainPosition(f"Not started yet: leaves {first.name} at {starts}", stops, yours, None,
                                 "Not started")
        starts = _clock(here.etd, here.std or here.scheduled)
        return TrainPosition(f"Not started yet: leaves {here.name} at {starts}", stops, yours, None, "Not started")

    left = previous[last]
    if last + 1 < len(previous):
        due = _clock(previous[last + 1].expected, previous[last + 1].scheduled)
    else:
        due = arrives
    index = last - (len(previous) - len(before))  # its place on the strip
    if index < 0:
        return TrainPosition(_left(left), stops, yours, None, f"Left {left.name}")  # further back than the strip
    train_at = index + _along(_clock(left.actual, left.scheduled), due, now)
    return TrainPosition(_left(left), stops, yours, round(train_at, 3), f"Left {left.name}")
