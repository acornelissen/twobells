"""A rain warning from Open-Meteo's hourly forecast (free, no key)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlencode

from .http import get_bytes

DEFAULT_BASE_URL = "https://api.open-meteo.com/v1/forecast"
# Two days of two hourly series is about 2 KB; anything near this is not a forecast.
MAX_BYTES = 64_000
_MAX_HOURS = 72

HttpGetBytes = Callable[[str, int], bytes]


@dataclass(frozen=True)
class Hour:
    start: datetime  # local time, in the board's timezone
    probability: int | None  # chance of rain, percent
    precipitation: float | None  # mm in the hour


def _number(value, low: float, high: float | None, what: str) -> float | None:
    if value is None:
        return None  # Open-Meteo leaves gaps as null
    wrong = isinstance(value, bool) or not isinstance(value, (int, float))
    if wrong or value < low or (high is not None and value > high):
        raise ValueError(f"Unexpected {what} in the forecast: {value!r}")
    return value


def parse_forecast(payload) -> list[Hour]:
    """The hourly series from an Open-Meteo response. Anything not shaped like
    one is refused with a ValueError, rather than shown on the board."""
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        raise ValueError("Forecast has no hourly data")
    times = hourly.get("time")
    chances = hourly.get("precipitation_probability")
    amounts = hourly.get("precipitation")
    if not all(isinstance(series, list) for series in (times, chances, amounts)):
        raise ValueError("Forecast hourly data is not a set of lists")
    if not len(times) == len(chances) == len(amounts):
        raise ValueError("Forecast hourly series differ in length")
    hours = []
    for time, chance, amount in list(zip(times, chances, amounts))[:_MAX_HOURS]:
        if not isinstance(time, str) or len(time) > 32:
            raise ValueError(f"Unexpected time in the forecast: {time!r}")
        try:
            start = datetime.fromisoformat(time)
        except ValueError:
            raise ValueError(f"Unexpected time in the forecast: {time!r}") from None
        if start.tzinfo is not None:
            raise ValueError(f"Forecast times should be local, not {time!r}")
        probability = _number(chance, 0, 100, "chance of rain")
        rainfall = _number(amount, 0, None, "rainfall")
        hours.append(Hour(start, None if probability is None else round(probability), rainfall))
    return hours


def hours_to_dicts(hours: list[Hour]) -> list[dict]:
    return [{"start": h.start.isoformat(), "probability": h.probability, "precipitation": h.precipitation} for h in hours]


def hours_from_dicts(items) -> list[Hour]:
    """Saved hours, skipping any that don't read back."""
    hours = []
    for item in items if isinstance(items, list) else []:
        try:
            probability = _number(item["probability"], 0, 100, "chance of rain")
            hours.append(Hour(
                datetime.fromisoformat(item["start"]),
                None if probability is None else round(probability),
                _number(item["precipitation"], 0, None, "rainfall"),
            ))
        except (TypeError, ValueError, KeyError):
            continue
    return hours


def rain_line(hours: list[Hour], now: datetime, until: datetime, threshold: int, live: bool = True) -> str | None:
    """"Rain from 07:10 (70%)" for the first hour between now and `until` with
    at least a `threshold` per cent chance of rain; None on a dry spell.

    The time is that hour's start, or now if it is the hour already under way.
    E-paper is not live: it keeps the hour's start, as a time that moved on
    every minute would redraw the panel every few minutes all through the rain."""
    for hour in hours:
        if hour.start + timedelta(hours=1) <= now or hour.start > until:
            continue
        if hour.probability is not None and hour.probability >= threshold:
            start = max(hour.start, now) if live else hour.start
            return f"Rain from {start:%H:%M} ({hour.probability}%)"
    return None


class OpenMeteoSource:
    name = "weather"

    def __init__(
        self,
        lat: float,
        lon: float,
        timezone: str = "Europe/London",
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGetBytes | None = None,
    ) -> None:
        self._lat = lat
        self._lon = lon
        self._timezone = timezone
        self._base_url = base_url
        self._http_get = http_get or get_bytes

    def url(self) -> str:
        # Two decimal places is about a kilometre: plenty for rain, and no
        # need to send anyone the house's exact position.
        params = {
            "latitude": f"{self._lat:.2f}",
            "longitude": f"{self._lon:.2f}",
            "hourly": "precipitation_probability,precipitation",
            "timezone": self._timezone,
            "forecast_days": 2,
        }
        return f"{self._base_url}?{urlencode(params)}"

    def fetch(self) -> list[Hour]:
        body = self._http_get(self.url(), MAX_BYTES)
        try:
            payload = json.loads(body)
        except ValueError:
            raise ValueError("Forecast is not JSON") from None
        return parse_forecast(payload)
