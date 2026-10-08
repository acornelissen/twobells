"""TransportAPI bus stop departures (free plan: 30 hits per day)."""

from __future__ import annotations

from typing import Callable, Iterable
from urllib.parse import urlencode

from .http import get_json
from .model import Departure, Status, minutes_between

DEFAULT_BASE_URL = "https://transportapi.com/v3"

HttpGet = Callable[[str, dict], dict]


def _parse_bus(entry: dict, stop_label: str | None) -> Departure:
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
        stop=stop_label,
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


def _entries(payload: dict, directions: Iterable[str] | None) -> list[dict]:
    groups = (payload.get("departures") or {}).values()
    return [e for group in groups for e in group if _matches(e, directions)]


def parse_stop(
    payload: dict,
    directions: Iterable[str] | None = None,
    stop_label: str | None = None,
) -> list[Departure]:
    entries = sorted(_entries(payload, directions), key=_sort_key)
    return [_parse_bus(e, stop_label) for e in entries]


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
        http_get: HttpGet = get_json,
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

    def fetch_stop(self, label: str, atco: str) -> list[Departure]:
        payload = self._http_get(self._url(atco), {})
        return parse_stop(payload, self._directions, label)

    def fetch(self) -> list[Departure]:
        merged = [
            dep for label, atco in self._stops.items() for dep in self.fetch_stop(label, atco)
        ]
        return sorted(merged, key=lambda d: d.departs_at)[: self._rows]
