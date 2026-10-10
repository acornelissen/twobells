import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from departureboard.sources.weather import (
    MAX_BYTES,
    Hour,
    OpenMeteoSource,
    hours_from_dicts,
    hours_to_dicts,
    parse_forecast,
    rain_line,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_glossop.json"


def payload():
    return json.loads(FIXTURE.read_text())


def at(hhmm, day=9):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute)


def test_parses_the_hourly_forecast():
    hours = parse_forecast(payload())
    assert len(hours) == 48
    assert hours[8] == Hour(at("08:00"), 70, 0.6)
    assert hours[0].start == at("00:00") and hours[-1].start == at("23:00", day=10)


def test_nulls_are_gaps_not_errors():
    data = payload()
    data["hourly"]["precipitation_probability"][3] = None
    data["hourly"]["precipitation"][3] = None
    assert parse_forecast(data)[3] == Hour(at("03:00"), None, None)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda d: d.pop("hourly"),
        lambda d: d.__setitem__("hourly", []),
        lambda d: d["hourly"].__setitem__("time", "2026-10-09T00:00"),
        lambda d: d["hourly"]["precipitation"].pop(),
        lambda d: d["hourly"]["time"].__setitem__(0, "nine o'clock"),
        lambda d: d["hourly"]["time"].__setitem__(0, 900),
        lambda d: d["hourly"]["time"].__setitem__(0, "2026-10-09T00:00+01:00"),
        lambda d: d["hourly"]["precipitation_probability"].__setitem__(0, 120),
        lambda d: d["hourly"]["precipitation_probability"].__setitem__(0, "70"),
        lambda d: d["hourly"]["precipitation_probability"].__setitem__(0, True),
        lambda d: d["hourly"]["precipitation"].__setitem__(0, -1),
    ],
)
def test_anything_not_shaped_like_a_forecast_is_refused(spoil):
    data = payload()
    spoil(data)
    with pytest.raises(ValueError):
        parse_forecast(data)


def test_a_forecast_that_is_not_an_object_is_refused():
    for odd in ([], "rain", None):
        with pytest.raises(ValueError):
            parse_forecast(odd)


def test_only_a_few_days_are_kept():
    data = payload()
    for key in ("time", "precipitation_probability", "precipitation"):
        data["hourly"][key] = data["hourly"][key] * 5
    data["hourly"]["time"] = [f"2026-10-09T{h % 24:02d}:00" for h in range(240)]
    assert len(parse_forecast(data)) == 72


def test_saved_hours_read_back_and_bad_ones_are_skipped():
    hours = parse_forecast(payload())[:3]
    saved = hours_to_dicts(hours)
    assert hours_from_dicts(saved) == hours
    assert hours_from_dicts([saved[0], {"start": "x"}, {"start": "2026-10-09T01:00", "probability": 500, "precipitation": 0}]) == hours[:1]
    assert hours_from_dicts("not a list") == []


def test_source_asks_for_the_hourly_rain_forecast_in_the_boards_timezone():
    calls = []

    def http_get(url, limit):
        calls.append((url, limit))
        return FIXTURE.read_bytes()

    source = OpenMeteoSource(53.44447, -1.94907, "Europe/London", http_get=http_get)
    assert len(source.fetch()) == 48
    [(url, limit)] = calls
    assert limit == MAX_BYTES  # the download is size-capped
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://api.open-meteo.com/v1/forecast"
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert query == {
        "latitude": "53.44",  # rounded: an exact position (here Glossop station) isn't needed
        "longitude": "-1.95",
        "hourly": "precipitation_probability,precipitation",
        "timezone": "Europe/London",
        "forecast_days": "2",
    }


def test_a_response_that_is_not_json_is_refused():
    source = OpenMeteoSource(53.44, -1.95, http_get=lambda url, limit: b"<html>busy</html>")
    with pytest.raises(ValueError):
        source.fetch()


def test_a_forecast_without_hourly_values_is_refused_by_the_source():
    source = OpenMeteoSource(53.44, -1.95, http_get=lambda url, limit: b'{"error": true, "reason": "x"}')
    with pytest.raises(ValueError):
        source.fetch()


HOURS = parse_forecast(payload())


def test_dry_until_you_reach_the_train_says_nothing():
    # 07:30 chance is 30%: under the 50% threshold.
    assert rain_line(HOURS, at("06:40"), at("07:42"), 50) is None


def test_rain_before_you_get_there_gives_the_hour_it_starts():
    assert rain_line(HOURS, at("07:40"), at("08:12"), 50) == "Rain from 08:00 (70%)"


def test_rain_after_you_get_there_is_not_mentioned():
    assert rain_line(HOURS, at("07:00"), at("07:59"), 50) is None


def test_already_raining_says_from_now():
    assert rain_line(HOURS, at("08:10"), at("08:42"), 50) == "Rain from 08:10 (70%)"


def test_already_raining_on_epaper_keeps_the_hours_start_so_it_need_not_redraw():
    assert rain_line(HOURS, at("08:10"), at("08:42"), 50, live=False) == "Rain from 08:00 (70%)"


def test_threshold_decides_what_counts_as_likely():
    assert rain_line(HOURS, at("06:40"), at("07:42"), 30) == "Rain from 07:00 (30%)"
    assert rain_line(HOURS, at("07:40"), at("09:12"), 80) == "Rain from 09:00 (85%)"
    assert rain_line(HOURS, at("07:40"), at("09:12"), 90) is None


def test_past_hours_are_ignored():
    # From 13:00 on the 9th it is dry; the morning's showers are over.
    assert rain_line(HOURS, at("13:00"), at("14:00"), 50) is None


def test_gaps_in_the_forecast_are_not_rain():
    gappy = [Hour(at("08:00"), None, None)]
    assert rain_line(gappy, at("07:40"), at("08:30"), 50) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_reading_that_is_not_a_real_number_is_refused(bad):
    # json.loads accepts NaN and Infinity, and NaN slips past range checks.
    data = payload()
    data["hourly"]["precipitation_probability"][0] = bad
    with pytest.raises(ValueError, match="chance of rain"):
        parse_forecast(data)
    data = payload()
    data["hourly"]["precipitation"][0] = bad
    with pytest.raises(ValueError, match="rainfall"):
        parse_forecast(data)
