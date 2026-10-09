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
