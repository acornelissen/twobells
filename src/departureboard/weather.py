"""Weather from Open-Meteo (free, no key): the board's rain warning, and the
fuller conditions (now, the next day or so, sunrise and sunset) for the web
dashboard."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from .http import get_bytes

DEFAULT_BASE_URL = "https://api.open-meteo.com/v1/forecast"
# Two days of two hourly series is about 2 KB, and the fuller conditions about
# 4 KB; anything near this is not a forecast.
MAX_BYTES = 64_000
_MAX_HOURS = 72
_MAX_DETAIL_HOURS = 48
_MAX_SUN_DAYS = 7

# WMO weather interpretation codes, as Open-Meteo uses them.
WMO_TEXT: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Light freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with light hail",
    99: "Thunderstorm with heavy hail",
}
UNKNOWN_WEATHER = "Unknown"

_CONDITIONS_CURRENT = (
    "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m,is_day"
)
_CONDITIONS_HOURLY = "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m,is_day"
_CONDITIONS_DAILY = "sunrise,sunset"

HttpGetBytes = Callable[[str, int], bytes]


@dataclass(frozen=True)
class Hour:
    start: datetime  # local time, in the board's timezone
    probability: int | None  # chance of rain, percent
    precipitation: float | None  # mm in the hour


def _number(value, low: float, high: float | None, what: str) -> float | None:
    if value is None:
        return None  # Open-Meteo leaves gaps as null
    # NaN and Infinity parse as JSON and NaN passes any range check, so finite first.
    wrong = isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
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
    for time, chance, amount in list(zip(times, chances, amounts, strict=True))[:_MAX_HOURS]:
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
    return [{"start": h.start.isoformat(), "probability": h.probability, "precipitation": h.precipitation}
            for h in hours]


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


@dataclass(frozen=True)
class Current:
    at: datetime  # local time of the reading, in the board's timezone
    temperature: float | None  # °C
    feels_like: float | None  # °C
    code: int | None  # WMO weather code
    text: str  # the code in words, "Unknown" if it has none
    wind: float | None  # mph
    gust: float | None  # mph
    direction: float | None  # degrees the wind comes from, 0 to 360
    is_day: bool | None


@dataclass(frozen=True)
class HourDetail:
    start: datetime  # local time, in the board's timezone
    temperature: float | None  # °C
    probability: int | None  # chance of rain, percent
    precipitation: float | None  # mm in the hour
    code: int | None  # WMO weather code
    text: str  # the code in words, "Unknown" if it has none
    wind: float | None  # mph
    is_day: bool | None


@dataclass(frozen=True)
class Conditions:
    now: Current | None
    hours: list[HourDetail]
    sun: list[tuple[date, datetime, datetime]]  # (day, sunrise, sunset), local


def _reading(value, low: float, high: float) -> float | None:
    """A number within its range, or None. Unlike the rain warning, one odd
    reading only blanks that field: the rest of the dashboard still shows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high:  # also turns away NaN, which json accepts
        return None
    return value


def _code(value) -> int | None:
    code = _reading(value, 0, 99)
    return code if isinstance(code, int) else None


def _code_text(code: int | None) -> str:
    return WMO_TEXT.get(code, UNKNOWN_WEATHER) if code is not None else UNKNOWN_WEATHER


def _is_day(value) -> bool | None:
    if isinstance(value, bool) or value not in (0, 1):
        return None
    return value == 1


def _local_time(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 32:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is None else None  # we asked for local times


def _local_date(value) -> date | None:
    if not isinstance(value, str) or len(value) > 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _series(section, keys: tuple[str, ...], what: str) -> list[tuple]:
    """The rows of a set of parallel series. Series of different lengths are
    refused, not cut to the shortest: values could no longer be trusted to
    belong to the time they sit beside."""
    if not isinstance(section, dict):
        raise ValueError(f"Forecast has no {what} data")
    columns = [section.get(key) for key in keys]
    if not all(isinstance(column, list) for column in columns):
        raise ValueError(f"Forecast {what} data is not a set of lists")
    if len({len(column) for column in columns}) > 1:
        raise ValueError(f"Forecast {what} series differ in length")
    return list(zip(*columns, strict=True))


def _current(section) -> Current | None:
    if not isinstance(section, dict):
        return None
    at = _local_time(section.get("time"))
    if at is None:
        return None  # a reading without a time can't be shown as "now"
    code = _code(section.get("weather_code"))
    return Current(
        at=at,
        temperature=_reading(section.get("temperature_2m"), -60, 60),
        feels_like=_reading(section.get("apparent_temperature"), -60, 60),
        code=code,
        text=_code_text(code),
        wind=_reading(section.get("wind_speed_10m"), 0, 250),
        gust=_reading(section.get("wind_gusts_10m"), 0, 250),
        direction=_reading(section.get("wind_direction_10m"), 0, 360),
        is_day=_is_day(section.get("is_day")),
    )


def _hours(section) -> list[HourDetail]:
    keys = ("time", "temperature_2m", "precipitation_probability", "precipitation", "weather_code", "wind_speed_10m",
            "is_day")
    hours = []
    for time, temperature, chance, amount, code, wind, is_day in _series(section, keys, "hourly"):
        start = _local_time(time)
        if start is None:
            continue  # an hour with no time can't be placed
        probability = _reading(chance, 0, 100)
        code = _code(code)
        hours.append(HourDetail(
            start=start,
            temperature=_reading(temperature, -60, 60),
            probability=None if probability is None else round(probability),
            precipitation=_reading(amount, 0, 500),
            code=code,
            text=_code_text(code),
            wind=_reading(wind, 0, 250),
            is_day=_is_day(is_day),
        ))
        if len(hours) == _MAX_DETAIL_HOURS:
            break
    return hours


def _sun(section) -> list[tuple[date, datetime, datetime]]:
    if section is None:
        return []
    days = []
    for when, rise, set_ in _series(section, ("time", "sunrise", "sunset"), "daily"):
        day, rise, set_ = _local_date(when), _local_time(rise), _local_time(set_)
        if day is None or rise is None or set_ is None:
            continue
        if not (rise.date() == day == set_.date() and rise < set_):
            continue  # not a sunrise and sunset for that day
        days.append((day, rise, set_))
        if len(days) == _MAX_SUN_DAYS:
            break
    return days


def parse_conditions(payload) -> Conditions:
    """Now, the hours ahead, and sunrise and sunset from an Open-Meteo
    response. A response without the hourly series, or with series that
    don't line up, is refused with a ValueError; a missing or odd single value
    is None, and an hour or day without a usable time is left out."""
    if not isinstance(payload, dict):
        raise ValueError("Forecast is not an object")
    return Conditions(_current(payload.get("current")), _hours(payload.get("hourly")), _sun(payload.get("daily")))


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

    def conditions_url(self) -> str:
        params = {
            "latitude": f"{self._lat:.2f}",
            "longitude": f"{self._lon:.2f}",
            "current": _CONDITIONS_CURRENT,
            "hourly": _CONDITIONS_HOURLY,
            "daily": _CONDITIONS_DAILY,
            "wind_speed_unit": "mph",
            "timezone": self._timezone,
            "forecast_days": 2,
        }
        return f"{self._base_url}?{urlencode(params)}"

    def fetch_conditions(self) -> Conditions:
        body = self._http_get(self.conditions_url(), MAX_BYTES)
        try:
            payload = json.loads(body)
        except ValueError:
            raise ValueError("Forecast is not JSON") from None
        return parse_conditions(payload)
