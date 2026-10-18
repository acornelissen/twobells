"""London's Underground, DLR, trams, cable car and river buses, live from
TfL's Unified API (api.tfl.gov.uk).

Your stops are TfL's codes for them (940GZZLUBNK, 930GWMR), from the
bundled list of London stops (setup/lookups.py), which also says which TfL
lines call at each. A fetch makes:

- one call to StopPoint/{code}/Arrivals per stop: TfL's predictions, each
  with when it is expected, where it goes, its line and platform, and
  where it is now ("Between Borough and London Bridge");
- one call to Line/{ids}/Status for the lines at your stops: those not
  running well are its messages, the board's notice and the dashboard's
  disruption line.

The dashboard also asks StopPoint/{codes}/Disruption every few minutes, for
lifts and escalators out of order, as station information.

TfL gives predictions only, no booked times, so a row is live or it isn't
there: it has nothing to be late against, and no punctuality is kept for
it. Overground and the Elizabeth line are trains, from Darwin, and London's
buses come from the bus data, so their predictions are left out here. The
cable car runs every few minutes with no times at all, so a cable car
station only ever has its line's status.

Every field TfL sends is hostile: each is checked for type, shape, length
and range as it is read, and a prediction with a field that is wrong is
left out rather than shown wrong. Without a key TfL allows 50 calls a
minute from one address; TFL_APP_KEY raises that, and goes in the app_key
header, never in a URL, so no error text or log can carry it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from urllib.parse import quote

from ..core.errors import safe_message
from ..core.model import Departure, Status
from ..core.net import get_json_capped
from .darwin import plain_text

BASE_URL = "https://api.tfl.gov.uk"
# The modes shown: not overground or elizabeth-line (Darwin's), nor bus (the bus data's).
MODES = frozenset({"tube", "dlr", "tram", "cable-car", "river-bus"})
# A stop TfL's live times are for (setup/lookups.py LONDON_STOP).
STOP = re.compile(r"^(?:940GZZ(?:LU|DL|CR|AL)[A-Z]{3}|930G[A-Z]{3,4})$")
LINE_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_KINDS = {"LU": "tube", "DL": "dlr", "CR": "tram", "AL": "cable-car"}
# The section's heading for stops all of one kind; a mix of kinds is "Metro".
LABELS = {"tube": "Underground", "dlr": "DLR", "tram": "Trams", "cable-car": "Cable car",
          "river-bus": "River buses"}
MIXED = "Metro"
EMPTY = {"tube": "No trains", "dlr": "No trains", "tram": "No trams", "cable-car": "No times for the cable car",
         "river-bus": "No river buses"}

MAX_BYTES = 2_000_000  # a busy station's arrivals are a few tens of kilobytes
MAX_PREDICTIONS = 200  # read from one answer, at most
TEXT_LIMIT = 80  # a destination, a line's name, where a train is
MESSAGE_LIMIT = 300
MAX_MESSAGES = 3
# A prediction further off than this, or this far gone, is a mistake.
HORIZON = timedelta(hours=3)
GONE = timedelta(minutes=2)
# Two predictions for one train: the same line and destination, this close
# (a DLR train is listed at both the platforms it opens on).
SAME_TRAIN = timedelta(seconds=60)
# Line status severities (TfL's Meta/Severity): running well, and how bad the rest are.
GOOD = frozenset({10, 18})
SEVERE = frozenset({1, 2, 6, 16, 20})  # closed, suspended, severe delays, not running, service closed
MAJOR = frozenset({3, 4, 5, 11})  # part suspended, planned closure, part closure, part closed

_PLATFORM = re.compile(r"(?:^|\s)Platform\s+([0-9A-Za-z]{1,4})$", re.IGNORECASE)
_BOUND = re.compile(r"^(Northbound|Southbound|Eastbound|Westbound|Inner Rail|Outer Rail)\b", re.IGNORECASE)
_SUFFIX = re.compile(r"\s*(?:-Underground| Underground Station| DLR Station| Rail Station| Tram Stop)$",
                     re.IGNORECASE)
_STATION_WORDS = re.compile(r"\b(lifts?|escalators?|step[- ]free)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

HttpGet = Callable[[str, dict], object]


def stop_kind(code: str) -> str:
    """tube, dlr, tram, cable-car or river-bus, from a stop's code."""
    return "river-bus" if code.startswith("930G") else _KINDS[code[6:8]]


def label_for(codes: Sequence[str]) -> str:
    """The section's heading for these stops: "Underground", "DLR" and the
    like when they are all of one kind, else "Metro"."""
    kinds = {stop_kind(code) for code in codes if STOP.fullmatch(code)}
    return LABELS[kinds.pop()] if len(kinds) == 1 else MIXED


def empty_for(codes: Sequence[str]) -> str:
    """What the section says with nothing to show."""
    kinds = {stop_kind(code) for code in codes if STOP.fullmatch(code)}
    return EMPTY[kinds.pop()] if len(kinds) == 1 else "Nothing due"


def _text(value, limit: int = TEXT_LIMIT) -> str:
    """One line of printable text, or "" for anything that isn't text or is too long."""
    if not isinstance(value, str) or len(value) > limit * 4:
        return ""
    text = " ".join(_CONTROL.sub(" ", value).split())
    text = "".join(c for c in text if c.isprintable())
    return text if len(text) <= limit else ""


def _when(value) -> datetime | None:
    """An ISO time TfL sends ("2026-10-17T18:44:53Z"), aware; None if it isn't one."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Arrival:
    """One prediction TfL gives for one of your stops."""

    stop: str  # your stop's label
    line_id: str  # "central"
    line: str  # "Central"
    mode: str  # one of MODES
    destination: str  # "Epping", or TfL's words when it has no destination, "Check Front of Train"
    expected: datetime  # aware
    via: str | None = None  # "via Bank"
    platform: str | None = None  # "6"
    bound: str | None = None  # "Eastbound"
    where: str | None = None  # "Between Borough and London Bridge"
    vehicle: str | None = None

    @property
    def line_words(self) -> str:
        """The line as the views name it: "Central line", "DLR", "RB1"."""
        if self.mode == "tube" and not self.line.lower().endswith("line"):
            return f"{self.line} line"
        return self.line


def _arrival(item, label: str, code: str, now: datetime) -> Arrival | None:
    """A prediction from one item of an Arrivals answer, or None for one
    that is wrong, for another mode, or ends here."""
    if not isinstance(item, dict):
        return None
    mode, line_id = item.get("modeName"), item.get("lineId")
    if mode not in MODES or not isinstance(line_id, str) or not LINE_ID.fullmatch(line_id):
        return None
    if item.get("naptanId") not in (None, code) or item.get("destinationNaptanId") == code:
        return None  # another stop's, or it terminates here
    expected = _when(item.get("expectedArrival"))
    seconds = item.get("timeToStation")
    if expected is None and isinstance(seconds, int) and not isinstance(seconds, bool) and 0 <= seconds <= 10_800:
        expected = now + timedelta(seconds=seconds)
    if expected is None or not now - GONE <= expected <= now + HORIZON:
        return None
    towards = _text(item.get("towards"))
    destination = _SUFFIX.sub("", _text(item.get("destinationName")))
    via = None
    if destination and " via " in towards:
        via = "via " + towards.split(" via ", 1)[1]
    destination = destination or towards
    line = _text(item.get("lineName"), 40) or line_id
    if not destination:
        return None
    platform_name = _text(item.get("platformName"), 60)
    platform = _PLATFORM.search(platform_name)
    bound = _BOUND.match(platform_name)
    vehicle = _text(item.get("vehicleId"), 20)
    return Arrival(stop=label, line_id=line_id, line=line, mode=mode, destination=destination, expected=expected,
                   via=via, platform=platform.group(1) if platform else None,
                   bound=bound.group(1).title() if bound else None,
                   where=_text(item.get("currentLocation")) or None, vehicle=vehicle or None)


def parse_arrivals(data, label: str, code: str, now: datetime) -> list[Arrival]:
    """Your stop's predictions from an Arrivals answer, soonest first, each
    train once. ValueError for an answer that isn't a list."""
    if not isinstance(data, list):
        raise ValueError("TfL's arrivals aren't a list")
    found: list[Arrival] = []
    for item in data[:MAX_PREDICTIONS]:
        arrival = _arrival(item, label, code, now)
        if arrival is None:
            continue
        twin = next((i for i, a in enumerate(found) if _same_train(a, arrival)), None)
        if twin is None:
            found.append(arrival)
        elif found[twin].platform != arrival.platform:
            found[twin] = replace(found[twin], platform=None)  # on either side: no one platform to say
    return sorted(found, key=lambda a: (a.expected, a.line_id, a.destination))


def _same_train(a: Arrival, b: Arrival) -> bool:
    return (a.line_id == b.line_id and a.destination == b.destination and abs(a.expected - b.expected) <= SAME_TRAIN
            and (a.vehicle is None or b.vehicle is None or a.vehicle == b.vehicle))


@dataclass(frozen=True)
class Message:
    """A line not running well, or a station's lifts or escalators: a
    sentence, in plain text, with the severity the dashboard shows it with
    (normal, minor, major or severe) and its kind: "service" for a line,
    "station" for a station's facilities."""

    text: str
    severity: str
    kind: str


def _severity(value) -> str:
    if value in SEVERE:
        return "severe"
    if value in MAJOR:
        return "major"
    return "minor"


def parse_statuses(data, lines: Sequence[str]) -> list[Message]:
    """The lines in `lines` not running well, from a Line Status answer, each
    once, in `lines`' order. ValueError for an answer that isn't a list."""
    if not isinstance(data, list):
        raise ValueError("TfL's line statuses aren't a list")
    found: dict[str, list[Message]] = {}
    for line in data[:50]:
        if not isinstance(line, dict) or line.get("id") not in lines or line.get("modeName") not in MODES:
            continue
        name = _text(line.get("name"), 40) or str(line["id"])
        statuses = line.get("lineStatuses")
        for status in statuses[:5] if isinstance(statuses, list) else ():
            if not isinstance(status, dict):
                continue
            severity = status.get("statusSeverity")
            if not isinstance(severity, int) or isinstance(severity, bool) or severity in GOOD:
                continue
            words = _text(status.get("statusSeverityDescription"), 40) or "Disruption"
            reason = status.get("reason")
            text = plain_text(reason, MESSAGE_LIMIT) if isinstance(reason, str) and reason.strip() else ""
            text = text or f"{name}: {words}."
            messages = found.setdefault(line["id"], [])
            if all(m.text != text for m in messages):
                messages.append(Message(text, _severity(severity), "service"))
    return [m for line_id in lines for m in found.get(line_id, [])][:MAX_MESSAGES * 2]


def parse_station_notes(data, codes: Sequence[str], now: datetime) -> list[Message]:
    """Your stations' lifts, escalators and step-free access, from a
    StopPoint Disruption answer: those in force now, each once.
    ValueError for an answer that isn't a list."""
    if not isinstance(data, list):
        raise ValueError("TfL's station notices aren't a list")
    found: list[Message] = []
    for item in data[:50]:
        if not isinstance(item, dict) or (item.get("stationAtcoCode") or item.get("atcoCode")) not in codes:
            continue
        start, end = _when(item.get("fromDate")), _when(item.get("toDate"))
        if (start is not None and start > now) or (end is not None and end < now):
            continue
        description = item.get("description")
        if not isinstance(description, str) or not _STATION_WORDS.search(description[:2000]):
            continue
        text = plain_text(description, MESSAGE_LIMIT)
        if text and all(m.text != text for m in found):
            found.append(Message(text, "normal", "station"))
        if len(found) == MAX_MESSAGES:
            break
    return found


def departure(arrival: Arrival, zone: tzinfo) -> Departure:
    """A prediction as the board's departure: its time is when it is
    expected, and it is live (Status.ON_TIME: there is no booked time to be
    late against)."""
    return Departure(mode="metro", service=arrival.line, destination=arrival.destination,
                     scheduled=arrival.expected.astimezone(zone).strftime("%H:%M"), expected=None,
                     status=Status.ON_TIME, platform=arrival.platform, stop=arrival.stop, via=arrival.via)


def _get(url: str, headers: dict):
    return get_json_capped(url, headers, MAX_BYTES)


class TflSource:
    """The next departures from your London stops, live from TfL."""

    name = "metro"
    # What the settings site says of the last fetch beside its time; nothing here.
    note: str | None = None

    def __init__(self, stops: Mapping[str, str], zone: tzinfo, app_key: str = "", rows: int = 3,
                 directions: list[str] | None = None, lines: Mapping[str, Sequence[str]] | None = None,
                 base_url: str = BASE_URL, http_get: HttpGet | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._stops = dict(stops)
        self._zone = zone
        self._app_key = app_key
        self._rows = rows
        self._directions = [d.lower() for d in directions or []]
        # The TfL lines at each stop, by its code, for their status; from the bundled stop list.
        self._lines = {code: tuple(found) for code, found in (lines or {}).items()}
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get or _get
        self._clock = clock or (lambda: datetime.now(UTC))
        # The lines' messages from the last fetch, for the board's notice;
        # None when their status couldn't be had, and the board keeps the last.
        self.messages: list[str] | None = []

    @property
    def stops(self) -> dict[str, str]:
        return dict(self._stops)

    @property
    def label(self) -> str:
        return label_for(list(self._stops.values()))

    @property
    def empty(self) -> str:
        return empty_for(list(self._stops.values()))

    @property
    def lines(self) -> list[str]:
        """TfL's ids of the lines at your stops, each once, in the order of your stops."""
        found: list[str] = []
        for code in self._stops.values():
            found.extend(line for line in self._lines.get(code, ()) if line not in found)
        return found

    def _ask(self, path: str):
        headers = {"app_key": self._app_key} if self._app_key else {}
        try:
            return self._http_get(f"{self._base_url}/{path}", headers)
        except Exception as error:  # noqa: BLE001 - said again without the key
            text = safe_message(error)
            if self._app_key:
                text = text.replace(self._app_key, "REDACTED")
            raise RuntimeError(f"TfL: {text}") from None

    def arrivals(self, rows: int | None = None) -> list[Arrival]:
        """The next `rows` (the board's, if not given) predictions across your
        stops, soonest first, those heading your `directions` only."""
        now = self._clock()
        found: list[Arrival] = []
        for label, code in self._stops.items():
            data = self._ask(f"StopPoint/{quote(code, safe='')}/Arrivals")
            try:
                found.extend(parse_arrivals(data, label, code, now))
            except ValueError as error:
                raise RuntimeError(f"TfL: {error}") from None
        if self._directions:
            found = [a for a in found if any(d in a.destination.lower() for d in self._directions)]
        found.sort(key=lambda a: (a.expected, a.stop, a.line_id))
        return found[: self._rows if rows is None else rows]

    def line_statuses(self) -> list[Message]:
        """The lines at your stops not running well; none to ask about, none."""
        lines = self.lines
        if not lines:
            return []
        data = self._ask(f"Line/{quote(','.join(lines), safe=',')}/Status")
        try:
            return parse_statuses(data, lines)
        except ValueError as error:
            raise RuntimeError(f"TfL: {error}") from None

    def station_notes(self) -> list[Message]:
        """Your stations' lifts, escalators and step-free access, in force now."""
        codes = list(self._stops.values())
        if not codes:
            return []
        data = self._ask(f"StopPoint/{quote(','.join(codes), safe=',')}/Disruption"
                         "?getFamily=false&includeRouteBlockedStops=false")
        try:
            return parse_station_notes(data, codes, self._clock())
        except ValueError as error:
            raise RuntimeError(f"TfL: {error}") from None

    def fetch(self) -> list[Departure]:
        departures = [departure(a, self._zone) for a in self.arrivals()]
        # The lines' status is worth less than the times: a failure leaves the notice as it was.
        try:
            self.messages = [m.text for m in self.line_statuses()][:MAX_MESSAGES]
        except RuntimeError:
            self.messages = None
        return departures
