"""TransportAPI bus stop departures (free plan: 30 hits per day)."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlencode

from ..core.model import Departure, Status, minutes_between
from ..core.net import get_json

DEFAULT_BASE_URL = "https://transportapi.com/v3"

HttpGet = Callable[[str, dict], dict]

_CREDENTIAL = re.compile(r"\b(app_id|app_key)=[^&\s\"']*")


def redact_credentials(value: Any) -> Any:
    """Copy of a response with app_id/app_key values masked in every string."""
    if isinstance(value, dict):
        return {k: redact_credentials(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_credentials(v) for v in value]
    if isinstance(value, str):
        return _CREDENTIAL.sub(r"\1=REDACTED", value)
    return value


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
