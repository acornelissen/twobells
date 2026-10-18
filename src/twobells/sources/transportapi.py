"""TransportAPI bus stop departures (free plan: 30 hits per day)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from urllib.parse import urlencode

from ..core.model import Departure, Status, is_clock, minutes_between
from ..core.net import get_json_capped

DEFAULT_BASE_URL = "https://transportapi.com/v3"
# A departure is a KB or so, and a stop is asked for a few dozen at most; this
# leaves plenty of room and still stops a runaway response.
MAX_BYTES = 512 * 1024

HttpGet = Callable[[str, dict], dict]


def _get_capped(url: str, headers: dict) -> dict:
    # get_json_capped also gives up on the whole request after its deadline.
    return get_json_capped(url, headers, MAX_BYTES)


# Each entry comes from outside, so every field is checked as it is read: an
# entry without a clock time to leave at is skipped, and any other field of
# the wrong type is left blank, never raised.


def _text(value) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) else ""


def _parse_bus(entry: dict, stop_label: str | None) -> Departure:
    aimed = entry["aimed_departure_time"]
    expected = entry.get("expected_departure_time")
    if not is_clock(expected):
        status, expected = Status.UNKNOWN, None
    elif minutes_between(aimed, expected) > 0:
        status = Status.LATE
    else:
        status, expected = Status.ON_TIME, None
    return Departure(
        mode="bus",
        service=_text(entry.get("line_name")) or _text(entry.get("line")),
        destination=_text(entry.get("direction")),
        scheduled=aimed,
        expected=expected,
        status=status,
        stop=stop_label,
    )


def _sort_key(entry: dict) -> tuple[str, str]:
    best = entry.get("best_departure_estimate")
    return (_text(entry.get("date")), best if is_clock(best) else entry["aimed_departure_time"])


def _matches(entry: dict, directions: Iterable[str] | None) -> bool:
    if not directions:
        return True
    direction = _text(entry.get("direction")).lower()
    return any(d.lower() in direction for d in directions)


def _entries(payload: dict, directions: Iterable[str] | None) -> list[dict]:
    """The entries with a clock time to leave at, from every group."""
    groups = payload.get("departures") if isinstance(payload, dict) else None
    if not isinstance(groups, dict):
        return []
    return [
        e for group in groups.values() if isinstance(group, list)
        for e in group
        if isinstance(e, dict) and is_clock(e.get("aimed_departure_time")) and _matches(e, directions)
    ]


def _dated(
    payload: dict, directions: Iterable[str] | None, stop_label: str | None
) -> list[tuple[tuple[str, str], Departure]]:
    """Departures in order, each with its (date, time), which orders them across midnight."""
    entries = sorted(_entries(payload, directions), key=_sort_key)
    return [(_sort_key(e), _parse_bus(e, stop_label)) for e in entries]


def parse_stop(
    payload: dict,
    directions: Iterable[str] | None = None,
    stop_label: str | None = None,
) -> list[Departure]:
    return [dep for _, dep in _dated(payload, directions, stop_label)]


class TransportApiBusSource:
    """Polls one or more stops. Each stop is one API hit, reported as `cost`."""

    name = "bus"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        stops: dict[str, str],
        rows: int = 2,
        directions: list[str] | None = None,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGet = _get_capped,
    ) -> None:
        if not stops:
            raise ValueError("At least one stop is required")
        self._app_id = app_id
        self._app_key = app_key
        self._stops = dict(stops)
        self._rows = rows
        self._directions = directions
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get

    @property
    def cost(self) -> int:
        return len(self._stops)

    @property
    def stops(self) -> dict[str, str]:
        return dict(self._stops)

    def _url(self, atco: str) -> str:
        # Over-fetch when filtering so the board still fills up.
        limit = self._rows * 4 if self._directions else self._rows
        params = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "live": "true",
            "group": "no",
            "limit": limit,
        }
        return f"{self._base_url}/uk/bus/stop_timetables/{atco}.json?{urlencode(params)}"

    def fetch_raw(self, atco: str) -> dict:
        return self._http_get(self._url(atco), {})

    def parse(self, payload: dict, label: str) -> list[Departure]:
        return parse_stop(payload, self._directions, label)

    def fetch(self) -> list[Departure]:
        # Merged on date and time: by time alone, 00:05 at one stop would beat 23:55 at another.
        merged = [
            pair
            for label, atco in self._stops.items()
            for pair in _dated(self.fetch_raw(atco), self._directions, label)
        ]
        merged.sort(key=lambda pair: pair[0])
        return [dep for _, dep in merged[: self._rows]]
