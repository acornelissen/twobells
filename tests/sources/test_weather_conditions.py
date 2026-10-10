import io
import json
import math
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from departureboard.core import net
from departureboard.sources.weather import (
    MAX_BYTES,
    WMO_TEXT,
    Current,
    HourDetail,
    OpenMeteoSource,
    parse_conditions,
)
from tests.paths import FIXTURES

FIXTURE = FIXTURES / "openmeteo_conditions.json"


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def at(hhmm, day=9):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute)


def source_returning(body: bytes, calls=None):
    def http_get(url, limit):
        if calls is not None:
            calls.append((url, limit))
        return body

    return OpenMeteoSource(53.44447, -1.94907, "Europe/London", http_get=http_get)


def test_source_asks_for_current_hourly_and_daily_weather_in_mph():
    calls = []
    source_returning(FIXTURE.read_bytes(), calls).fetch_conditions()
    [(url, limit)] = calls
    assert limit == MAX_BYTES  # the download is size-capped
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.open-meteo.com/v1/forecast"
    query = parse_qs(parts.query)
    assert all(len(values) == 1 for values in query.values())  # no parameter twice
    assert {k: v[0] for k, v in query.items()} == {
        "latitude": "53.44",  # rounded: an exact position (here Glossop station) isn't needed
        "longitude": "-1.95",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m,wind_direction_10m,is_day",
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m,is_day",
        "daily": "sunrise,sunset",
        "wind_speed_unit": "mph",
        "timezone": "Europe/London",
        "forecast_days": "2",
    }


def test_the_rain_warning_url_is_unchanged():
    query = parse_qs(urlsplit(source_returning(b"").url()).query)
    assert query["hourly"] == ["precipitation_probability,precipitation"]
    assert "current" not in query and "daily" not in query and "wind_speed_unit" not in query


def test_parses_now():
    now = parse_conditions(payload()).now
    assert now == Current(
        at=at("08:15"),
        temperature=9.7,
        feels_like=6.4,
        code=61,
        text="Light rain",
        wind=14.6,
        gust=31.3,
        direction=236,
        is_day=True,
    )


def test_parses_the_hours_ahead():
    hours = parse_conditions(payload()).hours
    assert len(hours) == 48
    assert hours[0].start == at("00:00") and hours[-1].start == at("23:00", day=10)
    assert hours[8] == HourDetail(at("08:00"), 9.6, 70, 0.6, 61, "Light rain", 14.1, True)
    assert hours[29] == HourDetail(at("05:00", day=10), 7.6, 3, 0.0, 45, "Fog", 3.6, False)


def test_parses_sunrise_and_sunset_for_today_and_tomorrow():
    assert parse_conditions(payload()).sun == [
        (date(2026, 10, 9), at("07:22"), at("18:27")),
        (date(2026, 10, 10), at("07:24", day=10), at("18:25", day=10)),
    ]


def test_fetch_conditions_parses_the_response():
    conditions = source_returning(FIXTURE.read_bytes()).fetch_conditions()
    assert conditions == parse_conditions(payload())


@pytest.mark.parametrize(
    "code, text",
    [(0, "Clear sky"), (3, "Overcast"), (48, "Freezing fog"), (55, "Heavy drizzle"), (82, "Heavy showers"), (99, "Thunderstorm with heavy hail")],
)
def test_codes_read_in_plain_english(code, text):
    assert WMO_TEXT[code] == text


def test_every_code_open_meteo_uses_has_words():
    used = {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99}
    assert set(WMO_TEXT) == used
    assert all(isinstance(text, str) and text for text in WMO_TEXT.values())


def test_an_unknown_code_reads_unknown():
    data = payload()
    data["current"]["weather_code"] = 42
    data["hourly"]["weather_code"][0] = 7
    conditions = parse_conditions(data)
    assert (conditions.now.code, conditions.now.text) == (42, "Unknown")
    assert (conditions.hours[0].code, conditions.hours[0].text) == (7, "Unknown")


def test_nulls_blank_the_field_but_keep_the_hour():
    data = payload()
    for key in ("temperature_2m", "precipitation_probability", "precipitation", "weather_code", "wind_speed_10m", "is_day"):
        data["hourly"][key][3] = None
    for key in ("temperature_2m", "apparent_temperature", "weather_code", "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "is_day"):
        data["current"][key] = None
    conditions = parse_conditions(data)
    assert len(conditions.hours) == 48
    assert conditions.hours[3] == HourDetail(at("03:00"), None, None, None, None, "Unknown", None, None)
    assert conditions.now == Current(at("08:15"), None, None, None, "Unknown", None, None, None, None)


def test_an_hour_without_a_time_is_left_out():
    data = payload()
    data["hourly"]["time"][3] = None
    data["hourly"]["time"][4] = "four o'clock"
    data["hourly"]["time"][5] = "2026-10-09T05:00+01:00"  # we asked for local times
    hours = parse_conditions(data).hours
    assert len(hours) == 45
    assert [h.start.hour for h in hours[:4]] == [0, 1, 2, 6]


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("hourly", "temperature_2m", 61),
        ("hourly", "temperature_2m", -61),
        ("hourly", "precipitation_probability", 101),
        ("hourly", "precipitation_probability", -1),
        ("hourly", "precipitation", 501),
        ("hourly", "precipitation", -0.1),
        ("hourly", "wind_speed_10m", 251),
        ("hourly", "wind_speed_10m", -1),
        ("hourly", "weather_code", 100),
        ("hourly", "weather_code", 61.5),
        ("hourly", "is_day", 2),
        ("hourly", "temperature_2m", "9.6"),
        ("hourly", "temperature_2m", True),
        ("hourly", "temperature_2m", math.nan),
        ("hourly", "temperature_2m", math.inf),
        ("hourly", "temperature_2m", [9.6]),
        ("current", "temperature_2m", 75),
        ("current", "apparent_temperature", -70),
        ("current", "wind_speed_10m", 300),
        ("current", "wind_gusts_10m", -5),
        ("current", "wind_direction_10m", 361),
        ("current", "wind_direction_10m", -1),
        ("current", "weather_code", -1),
        ("current", "is_day", True),
        ("current", "is_day", "1"),
    ],
)
def test_out_of_range_or_odd_values_become_none(section, key, value):
    data = payload()
    if section == "hourly":
        data["hourly"][key][8] = value
        hour = parse_conditions(data).hours[8]
        field = {"temperature_2m": "temperature", "precipitation_probability": "probability",
                 "precipitation": "precipitation", "wind_speed_10m": "wind", "weather_code": "code", "is_day": "is_day"}[key]
        assert getattr(hour, field) is None
        assert hour.start == at("08:00")
    else:
        data["current"][key] = value
        now = parse_conditions(data).now
        field = {"temperature_2m": "temperature", "apparent_temperature": "feels_like", "wind_speed_10m": "wind",
                 "wind_gusts_10m": "gust", "wind_direction_10m": "direction", "weather_code": "code", "is_day": "is_day"}[key]
        assert getattr(now, field) is None


def test_values_at_the_edge_of_their_range_are_kept():
    data = payload()
    data["current"].update(temperature_2m=-60, apparent_temperature=60, wind_speed_10m=0, wind_gusts_10m=250, wind_direction_10m=360)
    data["hourly"]["precipitation_probability"][0] = 100
    data["hourly"]["precipitation"][0] = 500
    conditions = parse_conditions(data)
    now = conditions.now
    assert (now.temperature, now.feels_like, now.wind, now.gust, now.direction) == (-60, 60, 0, 250, 360)
    assert (conditions.hours[0].probability, conditions.hours[0].precipitation) == (100, 500)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda d: d["hourly"]["temperature_2m"].pop(),
        lambda d: d["hourly"]["is_day"].append(1),
        lambda d: d["hourly"]["time"].pop(),
        lambda d: d["daily"]["sunset"].pop(),
    ],
)
def test_series_of_different_lengths_are_refused(spoil):
    data = payload()
    spoil(data)
    with pytest.raises(ValueError, match="differ in length"):
        parse_conditions(data)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda d: d.pop("hourly"),
        lambda d: d.__setitem__("hourly", []),
        lambda d: d["hourly"].pop("weather_code"),
        lambda d: d["hourly"].__setitem__("wind_speed_10m", "breezy"),
        lambda d: d.__setitem__("daily", "sunny"),
    ],
)
def test_a_response_not_shaped_like_a_forecast_is_refused(spoil):
    data = payload()
    spoil(data)
    with pytest.raises(ValueError):
        parse_conditions(data)


def test_a_forecast_that_is_not_an_object_is_refused():
    for odd in ([], "rain", None, 3):
        with pytest.raises(ValueError):
            parse_conditions(odd)


def test_no_usable_current_reading_means_no_now():
    for spoil in (lambda d: d.pop("current"), lambda d: d.__setitem__("current", [1]), lambda d: d["current"].pop("time"),
                  lambda d: d["current"].__setitem__("time", "soon")):
        data = payload()
        spoil(data)
        conditions = parse_conditions(data)
        assert conditions.now is None
        assert len(conditions.hours) == 48


def test_no_daily_section_means_no_sun_times():
    data = payload()
    data.pop("daily")
    assert parse_conditions(data).sun == []


@pytest.mark.parametrize(
    "spoil",
    [
        lambda d: d["daily"]["sunrise"].__setitem__(0, None),
        lambda d: d["daily"]["sunset"].__setitem__(0, "dusk"),
        lambda d: d["daily"]["time"].__setitem__(0, "2026-10-09T00:00"),
        lambda d: d["daily"]["sunrise"].__setitem__(0, "2026-10-08T07:20"),  # another day's
        lambda d: d["daily"]["sunrise"].__setitem__(0, "2026-10-09T19:00"),  # after sunset
    ],
)
def test_a_day_without_a_sensible_sunrise_and_sunset_is_left_out(spoil):
    data = payload()
    spoil(data)
    assert [day for day, _, _ in parse_conditions(data).sun] == [date(2026, 10, 10)]


def test_only_two_days_of_hours_are_kept():
    data = payload()
    for key, series in data["hourly"].items():
        data["hourly"][key] = series * 5
    data["hourly"]["time"] = [f"2026-10-{9 + h // 24:02d}T{h % 24:02d}:00" for h in range(240)]
    assert len(parse_conditions(data).hours) == 48


def test_the_response_fits_well_inside_the_size_cap():
    compact = json.dumps(payload(), separators=(",", ":")).encode()  # as Open-Meteo sends it
    assert len(compact) < MAX_BYTES // 8


def test_an_oversized_response_is_refused_before_parsing(monkeypatch):
    class Response(io.BytesIO):
        pass

    monkeypatch.setattr(net, "urlopen", lambda request, timeout: Response(b" " * (MAX_BYTES + 10)))
    with pytest.raises(ValueError, match="larger than"):
        OpenMeteoSource(53.44, -1.95).fetch_conditions()


def test_a_response_that_is_not_json_is_refused():
    with pytest.raises(ValueError):
        source_returning(b"<html>busy</html>").fetch_conditions()
