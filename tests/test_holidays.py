import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from departureboard import holidays
from departureboard.holidays import CACHE_NAME, URL, bank_holidays, parse, read_cache

BODY = (Path(__file__).parent / "fixtures" / "govuk_bank_holidays.json").read_bytes()
ENGLAND_AND_WALES = frozenset(date.fromisoformat(d) for d in (
    "2026-01-01", "2026-04-03", "2026-04-06", "2026-05-04",
    "2026-05-25", "2026-08-31", "2026-12-25", "2026-12-28",
))
NOW = datetime(2026, 10, 9, 7, 0)


class FakeFetch:
    def __init__(self, body=BODY):
        self.body = body
        self.error = None
        self.calls = []

    def __call__(self, url, max_bytes):
        self.calls.append((url, max_bytes))
        if self.error:
            raise self.error
        return self.body


def test_parse_reads_england_and_wales_only():
    assert parse(BODY) == ENGLAND_AND_WALES  # not Scotland's 3 August or Northern Ireland's 17 March


def test_parse_skips_bad_entries():
    events = [
        {"title": "Good", "date": "2026-12-25"},
        {"title": "No date"},
        {"title": "Not a date", "date": "soon"},
        {"title": "Compact", "date": "20261226"},
        {"title": "Impossible", "date": "2026-02-30"},
        {"title": "Number", "date": 20261225},
        "not an event",
    ]
    body = json.dumps({"england-and-wales": {"events": events}}).encode()
    assert parse(body) == {date(2026, 12, 25)}


@pytest.mark.parametrize("body", [
    b"not json",
    b"\xff\xfe",
    b"[]",
    b'{"scotland": {"events": []}}',
    b'{"england-and-wales": {"events": "none"}}',
    b'{"england-and-wales": []}',
    b'{"england-and-wales": {"events": [{"date": "bad"}]}}',
])
def test_parse_refuses_anything_but_a_list_of_holidays(body):
    with pytest.raises(ValueError):
        parse(body)


def test_first_use_fetches_and_caches(tmp_path):
    fetch = FakeFetch()
    assert bank_holidays(tmp_path, NOW, fetch) == ENGLAND_AND_WALES
    assert fetch.calls == [(URL, holidays.MAX_BYTES)]
    assert read_cache(tmp_path) == ENGLAND_AND_WALES


def test_a_fresh_cache_is_used_without_fetching(tmp_path):
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW, fetch)
    # The board runs every minute; only the first run of the week fetches.
    for minutes in range(1, 60 * 24 * 7, 97):
        assert bank_holidays(tmp_path, NOW + timedelta(minutes=minutes), fetch) == ENGLAND_AND_WALES
    assert len(fetch.calls) == 1


def test_a_week_old_cache_is_refreshed(tmp_path):
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW, fetch)
    fetch.body = json.dumps({"england-and-wales": {"events": [{"date": "2027-01-01"}]}}).encode()
    assert bank_holidays(tmp_path, NOW + timedelta(days=7), fetch) == {date(2027, 1, 1)}
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("error", [OSError("network down"), ValueError("Response larger than 1000000 bytes")])
def test_a_failed_refresh_keeps_the_cache(tmp_path, error):
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW, fetch)
    fetch.error = error
    assert bank_holidays(tmp_path, NOW + timedelta(days=8), fetch) == ENGLAND_AND_WALES
    assert read_cache(tmp_path) == ENGLAND_AND_WALES


def test_a_bad_response_keeps_the_cache(tmp_path):
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW, fetch)
    fetch.body = b'{"england-and-wales": {"events": []}}'
    assert bank_holidays(tmp_path, NOW + timedelta(days=8), fetch) == ENGLAND_AND_WALES


def test_without_a_cache_or_network_there_are_no_holidays(tmp_path):
    fetch = FakeFetch()
    fetch.error = OSError("network down")
    assert bank_holidays(tmp_path, NOW, fetch) == frozenset()


def test_a_failure_is_not_retried_every_minute(tmp_path):
    fetch = FakeFetch()
    fetch.error = OSError("network down")
    for minutes in range(0, 60 * 6, 1):
        bank_holidays(tmp_path, NOW + timedelta(minutes=minutes), fetch)
    assert len(fetch.calls) == 1
    fetch.error = None
    assert bank_holidays(tmp_path, NOW + holidays.RETRY, fetch) == ENGLAND_AND_WALES
    assert len(fetch.calls) == 2


def test_a_corrupt_cache_is_replaced(tmp_path):
    (tmp_path / CACHE_NAME).write_text("{ not json")
    fetch = FakeFetch()
    assert bank_holidays(tmp_path, NOW, fetch) == ENGLAND_AND_WALES
    assert len(fetch.calls) == 1


def test_bad_dates_in_the_cache_are_ignored(tmp_path):
    (tmp_path / CACHE_NAME).write_text(json.dumps(
        {"fetched": NOW.isoformat(), "checked": NOW.isoformat(), "dates": ["2026-12-25", "nonsense", 7]}
    ))
    assert read_cache(tmp_path) == {date(2026, 12, 25)}


def test_a_cache_dated_in_the_future_is_refreshed(tmp_path):
    # A Pi without a clock battery can boot with the wrong date and fetch then.
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW + timedelta(days=400), fetch)
    bank_holidays(tmp_path, NOW, fetch)
    assert len(fetch.calls) == 2


def test_reading_the_cache_never_fetches_or_writes(tmp_path):
    assert read_cache(tmp_path) == frozenset()
    assert not (tmp_path / CACHE_NAME).exists()


def test_the_cache_folder_is_made_if_missing(tmp_path):
    folder = tmp_path / "state"
    assert bank_holidays(folder, NOW, FakeFetch()) == ENGLAND_AND_WALES
    assert (folder / CACHE_NAME).exists()


# TransXChange names for each day, for the bus timetable's calendars.

def _events(*rows):
    return {date.fromisoformat(day): holidays.Event(title, notes) for day, title, notes in rows}


def test_parse_events_keeps_each_title_and_note():
    events = holidays.parse_events(BODY)
    assert set(events) == ENGLAND_AND_WALES
    assert events[date(2026, 12, 28)] == holidays.Event("Boxing Day", "Substitute day")
    assert events[date(2026, 5, 4)].title == "Early May bank holiday"


@pytest.mark.parametrize("day, names", [
    ("2026-01-01", {"NewYearsDay"}),
    ("2026-04-03", {"GoodFriday"}),
    ("2026-04-06", {"EasterMonday"}),
    ("2026-05-04", {"MayDay"}),
    ("2026-05-25", {"SpringBank"}),
    ("2026-08-31", {"LateSummerBankHolidayNotScotland"}),
    ("2026-12-24", {"ChristmasEve"}),
    ("2026-12-25", {"ChristmasDay"}),
    # Boxing Day is a Saturday in 2026: the day itself keeps its name, and
    # the Monday is the substitute bank holiday.
    ("2026-12-26", {"BoxingDay"}),
    ("2026-12-28", {"BoxingDayHoliday"}),
    ("2026-12-31", {"NewYearsEve"}),
    ("2026-10-09", set()),
])
def test_holiday_names_from_govuk_titles_and_fixed_dates(day, names):
    events = holidays.parse_events(BODY)
    assert holidays.holiday_names(date.fromisoformat(day), events) == names


def test_substitute_days_for_christmas_and_new_year():
    # 2027: Christmas Day and Boxing Day fall on a weekend; 2028 New Year's Day does too.
    events = _events(
        ("2027-12-27", "Christmas Day", "Substitute day"),
        ("2027-12-28", "Boxing Day", "Substitute day"),
        ("2028-01-03", "New Year’s Day", "Substitute day"),
    )
    assert holidays.holiday_names(date(2027, 12, 27), events) == {"ChristmasDayHoliday"}
    assert holidays.holiday_names(date(2027, 12, 28), events) == {"BoxingDayHoliday"}
    assert holidays.holiday_names(date(2028, 1, 3), events) == {"NewYearsDayHoliday"}
    assert holidays.holiday_names(date(2027, 12, 25), events) == {"ChristmasDay"}


def test_renamed_and_one_off_holidays():
    events = _events(
        ("2020-05-08", "Early May bank holiday (VE day)", ""),
        ("2023-05-08", "Bank holiday for the coronation of King Charles III", ""),
    )
    assert holidays.holiday_names(date(2020, 5, 8), events) == {"MayDay"}
    # TransXChange has no name for a one-off holiday; timetables give those as dates.
    assert holidays.holiday_names(date(2023, 5, 8), events) == set()


def test_fixed_dates_need_no_list():
    assert holidays.holiday_names(date(2030, 12, 24), {}) == {"ChristmasEve"}
    assert holidays.holiday_names(date(2030, 1, 1), {}) == {"NewYearsDay"}


def test_the_cache_keeps_titles(tmp_path):
    bank_holidays(tmp_path, NOW, FakeFetch())
    assert holidays.read_events(tmp_path) == holidays.parse_events(BODY)
    assert read_cache(tmp_path) == ENGLAND_AND_WALES


def test_a_cache_from_before_titles_still_works_and_gains_them_next_refresh(tmp_path):
    # Older versions saved dates only. Those still count; the weekly refresh adds titles.
    (tmp_path / CACHE_NAME).write_text(json.dumps(
        {"fetched": NOW.isoformat(), "checked": NOW.isoformat(), "dates": ["2026-12-25"]}
    ))
    fetch = FakeFetch()
    assert bank_holidays(tmp_path, NOW + timedelta(minutes=1), fetch) == {date(2026, 12, 25)}
    assert fetch.calls == []
    assert holidays.read_events(tmp_path) == {}
    assert bank_holidays(tmp_path, NOW + timedelta(days=7), fetch) == ENGLAND_AND_WALES
    assert holidays.read_events(tmp_path)[date(2026, 12, 28)].notes == "Substitute day"


def test_bad_events_in_the_cache_are_ignored(tmp_path):
    (tmp_path / CACHE_NAME).write_text(json.dumps({
        "fetched": NOW.isoformat(), "checked": NOW.isoformat(), "dates": ["2026-12-25"],
        "events": [
            {"date": "2026-12-25", "title": "Christmas Day", "notes": ""},
            {"date": "nonsense", "title": "Bad"},
            {"date": "2026-12-28", "title": 7},
            "not an event",
        ],
    }))
    assert holidays.read_events(tmp_path) == {
        date(2026, 12, 25): holidays.Event("Christmas Day", ""),
        date(2026, 12, 28): holidays.Event("", ""),  # still a holiday, with no name
    }


# Divisions: England and Wales, Scotland, Northern Ireland.

SCOTLAND = frozenset(date.fromisoformat(d) for d in ("2026-01-01", "2026-01-02", "2026-08-03", "2026-11-30"))
NORTHERN_IRELAND = frozenset({date(2026, 3, 17), date(2026, 7, 13)})


@pytest.mark.parametrize("division, dates", [
    ("england-and-wales", ENGLAND_AND_WALES),
    ("scotland", SCOTLAND),
    ("northern-ireland", NORTHERN_IRELAND),
])
def test_parse_picks_the_division(division, dates):
    assert parse(BODY, division) == dates


def test_parse_refuses_an_unknown_division():
    with pytest.raises(ValueError, match="wales"):
        parse(BODY, "wales")


@pytest.mark.parametrize("division, dates", [("scotland", SCOTLAND), ("northern-ireland", NORTHERN_IRELAND)])
def test_one_download_caches_every_division(tmp_path, division, dates):
    fetch = FakeFetch()
    assert bank_holidays(tmp_path, NOW, fetch, division=division) == dates
    assert read_cache(tmp_path, division) == dates
    assert bank_holidays(tmp_path, NOW + timedelta(minutes=1), fetch) == ENGLAND_AND_WALES
    assert len(fetch.calls) == 1


def test_bus_timetable_names_stay_england_and_wales_whatever_the_division(tmp_path):
    bank_holidays(tmp_path, NOW, FakeFetch(), division="scotland")
    assert holidays.read_events(tmp_path) == holidays.parse_events(BODY)


def test_a_cache_from_before_divisions_still_works_and_fetches_another_division(tmp_path):
    # Older versions saved England and Wales only, under "dates".
    (tmp_path / CACHE_NAME).write_text(json.dumps(
        {"fetched": NOW.isoformat(), "checked": NOW.isoformat(), "dates": ["2026-12-25"]}
    ))
    fetch = FakeFetch()
    assert bank_holidays(tmp_path, NOW + timedelta(minutes=1), fetch) == {date(2026, 12, 25)}
    assert fetch.calls == []
    assert read_cache(tmp_path, "scotland") == frozenset()
    assert bank_holidays(tmp_path, NOW + timedelta(minutes=2), fetch, division="scotland") == SCOTLAND
    assert len(fetch.calls) == 1
    assert read_cache(tmp_path) == ENGLAND_AND_WALES


def test_the_cache_keeps_england_and_wales_where_older_versions_read_it(tmp_path):
    bank_holidays(tmp_path, NOW, FakeFetch(), division="northern-ireland")
    saved = json.loads((tmp_path / CACHE_NAME).read_text())
    assert saved["dates"] == sorted(day.isoformat() for day in ENGLAND_AND_WALES)


def test_a_download_without_the_division_keeps_the_cache_and_waits_to_retry(tmp_path):
    fetch = FakeFetch()
    bank_holidays(tmp_path, NOW, fetch, division="scotland")
    fetch.body = json.dumps({"england-and-wales": {"events": [{"date": "2027-01-01"}]}}).encode()
    later = NOW + timedelta(days=8)
    assert bank_holidays(tmp_path, later, fetch, division="scotland") == SCOTLAND
    assert bank_holidays(tmp_path, later + timedelta(minutes=1), fetch, division="scotland") == SCOTLAND
    assert len(fetch.calls) == 2


def test_an_unknown_division_is_refused(tmp_path):
    with pytest.raises(ValueError):
        bank_holidays(tmp_path, NOW, FakeFetch(), division="wales")
    with pytest.raises(ValueError):
        read_cache(tmp_path, "wales")
