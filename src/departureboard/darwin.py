"""National Rail Darwin Live Departure Board (LDBWS) via the Rail Data Marketplace."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlencode

from .http import get_json
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


class DarwinSource:
    name = "train"

    def __init__(
        self,
        api_key: str,
        crs: str,
        filter_crs: str | None = None,
        rows: int = 3,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGet = get_json,
    ) -> None:
        self._api_key = api_key
        self._crs = crs
        self._filter_crs = filter_crs
        self._rows = rows
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get
        # From the last successful fetch; the app keeps them with the departures.
        self.messages: list[str] = []

    def _url(self) -> str:
        params = {"numRows": self._rows}
        if self._filter_crs:
            params.update(filterCrs=self._filter_crs, filterType="to")
        return f"{self._base_url}/GetDepartureBoard/{self._crs}?{urlencode(params)}"

    def fetch(self) -> list[Departure]:
        board = self._http_get(self._url(), {"x-apikey": self._api_key})
        departures = parse_board(board)
        self.messages = parse_messages(board)
        return departures
