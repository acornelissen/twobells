"""TransportAPI bus stop departures (free plan: 30 hits per day)."""

from __future__ import annotations

from typing import Callable, Iterable
from urllib.parse import urlencode

from .http import get_json
from .model import Departure, Status, minutes_between

DEFAULT_BASE_URL = "https://transportapi.com/v3"

HttpGet = Callable[[str, dict], dict]


def _parse_bus(entry: dict) -> Departure:
    aimed = entry["aimed_departure_time"]
    expected = entry.get("expected_departure_time")
    if not expected:
        status, expected = Status.UNKNOWN, None
    elif minutes_between(aimed, expected) > 0:
        status = Status.LATE
    else:
        status, expected = Status.ON_TIME, None
    return Departure(
        mode="bus",
        service=str(entry.get("line_name") or entry.get("line") or ""),
        destination=entry.get("direction") or "",
        scheduled=aimed,
        expected=expected,
        status=status,
    )


def _sort_key(entry: dict) -> tuple[str, str]:
    return (
        entry.get("date") or "",
        entry.get("best_departure_estimate") or entry["aimed_departure_time"],
    )


def _matches(entry: dict, directions: Iterable[str] | None) -> bool:
    if not directions:
        return True
    direction = (entry.get("direction") or "").lower()
    return any(d.lower() in direction for d in directions)


def parse_stop(payload: dict, directions: Iterable[str] | None = None) -> list[Departure]:
    groups = (payload.get("departures") or {}).values()
    entries = [e for group in groups for e in group if _matches(e, directions)]
    return [_parse_bus(e) for e in sorted(entries, key=_sort_key)]


class TransportApiBusSource:
    name = "bus"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        atco: str,
        rows: int = 2,
        directions: list[str] | None = None,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGet = get_json,
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._atco = atco
        self._rows = rows
        self._directions = directions
        self._base_url = base_url.rstrip("/")
        self._http_get = http_get

    def _url(self) -> str:
        # Over-fetch when filtering so the board still fills up.
        limit = self._rows * 4 if self._directions else self._rows
        params = {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "live": "true",
            "group": "no",
            "limit": limit,
        }
        return f"{self._base_url}/uk/bus/stop_timetables/{self._atco}.json?{urlencode(params)}"

    def fetch(self) -> list[Departure]:
        payload = self._http_get(self._url(), {})
        return parse_stop(payload, self._directions)[: self._rows]
