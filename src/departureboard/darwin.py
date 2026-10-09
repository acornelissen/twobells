"""National Rail Darwin Live Departure Board (LDBWS) via the Rail Data Marketplace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlencode

from .http import get_json, get_json_capped
from .model import Departure, Status

DEFAULT_BASE_URL = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/LDBWS/api/20220120"

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
    return [_parse_service(s) for s in board.get("trainServices") or []]


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


def _markups(value, depth: int = 0):
    """The HTML strings in nrccMessages, whichever shape it comes in: a list of
    {"Value": html}, a single one, plain strings, or the other key spellings."""
    if depth > 3:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _markups(item, depth + 1)
    elif isinstance(value, dict):
        key = next((k for k in _MESSAGE_KEYS if k in value), None)
        if key is not None:
            yield from _markups(value[key], depth + 1)


def parse_messages(board: dict) -> list[str]:
    """The station's incident messages as plain text, at most a few."""
    messages: list[str] = []
    for markup in _markups(board.get("nrccMessages")):
        text = plain_text(markup)
        if text and text not in messages:
            messages.append(text)
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
    messages: list[str]
    generated_at: str | None  # Darwin's own ISO 8601 timestamp, as sent


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


def _calling_points(service: dict, destinations: list[dict]) -> tuple[CallingPoint, ...]:
    """The stops after this station. A train that divides has one list per
    part; we keep the one that ends at a destination on the board (the first
    such, by crs or else by name), and otherwise the first list, which is the
    main train in Darwin's order."""
    lists = []
    for part in _as_list(service.get("subsequentCallingPoints"))[:_MAX_CALLING_LISTS]:
        if not isinstance(part, dict):
            continue
        raw = _as_list(part.get("callingPoint"))[:_MAX_CALLING_POINTS]
        points = tuple(p for p in map(_calling_point, raw) if p is not None)
        if points:
            lists.append(points)
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
    )


# The detailed board is a few KB per train; this leaves plenty of room for a
# long board and still stops a runaway response.
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
        # From the last successful fetch; the app keeps them with the departures.
        self.messages: list[str] = []

    def _url(self) -> str:
        params = {"numRows": self._rows}
        if self._filter_crs:
            params.update(filterCrs=self._filter_crs, filterType="to")
        method = "GetDepBoardWithDetails" if self.with_details else "GetDepartureBoard"
        return f"{self._base_url}/{method}/{self._crs}?{urlencode(params)}"

    def _get(self) -> dict:
        http_get = self._http_get or (_get_details if self.with_details else get_json)
        return http_get(self._url(), {"x-apikey": self._api_key})

    def fetch_board(self) -> TrainBoard:
        """The rich board: calling points too when with_details is set."""
        board = parse_board_rich(self._get())
        self.messages = board.messages
        return board

    def fetch(self) -> list[Departure]:
        if self.with_details:
            return [s.departure for s in self.fetch_board().services]
        board = self._get()
        departures = parse_board(board)
        self.messages = parse_messages(board)
        return departures
