from datetime import datetime, time, timedelta

import pytest

from departureboard.schedule import PollPolicy, Window, parse_window

THU_0700 = datetime(2026, 10, 8, 7, 0)
SAT_0700 = datetime(2026, 10, 10, 7, 0)


def test_parse_weekday_range():
    window = parse_window("Mon-Fri 06:45-09:00")
    assert window.days == frozenset(range(5))
    assert window.start.strftime("%H:%M") == "06:45"
    assert window.end.strftime("%H:%M") == "09:00"


def test_parse_single_day_and_list():
    assert parse_window("Sat 10:00-11:00").days == frozenset({5})
    assert parse_window("Mon,Wed 10:00-11:00").days == frozenset({0, 2})


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_window("weekdays mornings")


def test_window_contains_time_on_matching_day():
    window = parse_window("Mon-Fri 06:45-09:00")
    assert window.contains(THU_0700)
    assert not window.contains(SAT_0700)
    assert not window.contains(THU_0700.replace(hour=9, minute=1))


def test_window_end_is_inclusive():
    window = parse_window("Mon-Fri 06:45-09:00")
    assert window.contains(THU_0700.replace(hour=9, minute=0))


def policy(interval_minutes=5):
    return PollPolicy(
        windows=(parse_window("Mon-Fri 06:45-09:00"),),
        interval=timedelta(minutes=interval_minutes),
    )


def test_due_when_never_fetched_inside_window():
    assert policy().is_due(THU_0700, last_fetch=None)


def test_not_due_outside_window():
    assert not policy().is_due(SAT_0700, last_fetch=None)


def test_not_due_before_interval_elapsed():
    assert not policy().is_due(THU_0700, last_fetch=THU_0700 - timedelta(minutes=4))


def test_due_once_interval_elapsed():
    assert policy().is_due(THU_0700, last_fetch=THU_0700 - timedelta(minutes=5))


def test_slack_tolerates_timer_jitter():
    # A minutely timer can fire a few seconds early relative to the last fetch.
    last = THU_0700 - timedelta(minutes=5) + timedelta(seconds=10)
    assert policy().is_due(THU_0700, last_fetch=last)


def test_is_active_reflects_any_window():
    p = PollPolicy(
        windows=(parse_window("Mon-Fri 06:45-09:00"), parse_window("Sat 06:00-08:00")),
        interval=timedelta(minutes=2),
    )
    assert p.is_active(THU_0700)
    assert p.is_active(SAT_0700)
    assert not p.is_active(SAT_0700.replace(hour=12))


def test_parsed_window_equals_constructed_window():
    assert parse_window("Mon 01:00-02:00") == Window(frozenset({0}), time(1, 0), time(2, 0))
