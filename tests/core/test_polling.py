from datetime import date, datetime, time, timedelta

import pytest

from twobells.core.polling import PollPolicy, Window, parse_window

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


def test_short_intervals_are_not_polled_early():
    # A 30 s position feed would otherwise be polled every 15 s.
    p = PollPolicy((parse_window("Mon-Sun 00:00-23:59"),), timedelta(seconds=30))
    assert not p.is_due(THU_0700, last_fetch=THU_0700 - timedelta(seconds=20))
    assert p.is_due(THU_0700, last_fetch=THU_0700 - timedelta(seconds=28))


def test_window_can_run_past_midnight_into_the_next_morning():
    window = parse_window("Mon-Fri 22:00-01:00")
    friday = datetime(2026, 10, 9)
    assert window.contains(friday.replace(hour=22))
    assert window.contains(friday.replace(hour=23, minute=59))
    assert window.contains(friday + timedelta(days=1, minutes=30))  # Saturday 00:30, Friday's window
    assert not window.contains(friday + timedelta(days=1, hours=1, minutes=1))
    assert not window.contains(friday.replace(hour=21, minute=59))
    assert not window.contains(friday + timedelta(days=1, hours=22))  # Saturday night
    assert not window.contains(datetime(2026, 10, 12, 0, 30))  # Monday 00:30 follows Sunday
    assert window.contains(datetime(2026, 10, 13, 0, 30))  # Tuesday 00:30 follows Monday


def test_day_range_can_wrap_around_the_week():
    assert parse_window("Fri-Mon 07:00-09:00").days == frozenset({4, 5, 6, 0})
    assert parse_window("Sun-Sun 07:00-09:00").days == frozenset({6})


def test_due_when_the_clock_has_gone_back():
    # At the end of summer time 01:59 BST is followed by 01:00 GMT.
    assert policy().is_due(THU_0700, last_fetch=THU_0700 + timedelta(minutes=59))


# Monday 31 August 2026 is the summer bank holiday in England and Wales.
BANK_HOLIDAY = date(2026, 8, 31)
HOLIDAY_MONDAY_0700 = datetime(2026, 8, 31, 7, 0)


def test_a_bank_holiday_is_treated_like_a_sunday():
    weekdays = PollPolicy((parse_window("Mon-Fri 06:45-09:00"),), timedelta(minutes=2), frozenset({BANK_HOLIDAY}))
    sundays = PollPolicy((parse_window("Sun 06:45-09:00"),), timedelta(minutes=2), frozenset({BANK_HOLIDAY}))
    assert not weekdays.is_active(HOLIDAY_MONDAY_0700)
    assert not weekdays.is_due(HOLIDAY_MONDAY_0700, last_fetch=None)
    assert sundays.is_active(HOLIDAY_MONDAY_0700)
    # The day after is an ordinary Tuesday.
    assert weekdays.is_active(HOLIDAY_MONDAY_0700 + timedelta(days=1))
    assert not sundays.is_active(HOLIDAY_MONDAY_0700 + timedelta(days=1))


def test_without_bank_holidays_a_holiday_monday_is_a_monday():
    weekdays = PollPolicy((parse_window("Mon-Fri 06:45-09:00"),), timedelta(minutes=2))
    assert weekdays.is_active(HOLIDAY_MONDAY_0700)


def test_a_window_past_midnight_follows_the_bank_holiday_it_started_on():
    holidays = frozenset({BANK_HOLIDAY})
    weekdays = PollPolicy((parse_window("Mon-Fri 22:00-01:00"),), timedelta(minutes=2), holidays)
    sundays = PollPolicy((parse_window("Sun 22:00-01:00"),), timedelta(minutes=2), holidays)
    tuesday_0030 = datetime(2026, 9, 1, 0, 30)
    assert not weekdays.is_active(HOLIDAY_MONDAY_0700.replace(hour=23))
    assert not weekdays.is_active(tuesday_0030)
    assert sundays.is_active(HOLIDAY_MONDAY_0700.replace(hour=23))
    assert sundays.is_active(tuesday_0030)
    # Sunday night itself still runs into the holiday morning.
    assert sundays.is_active(datetime(2026, 8, 31, 0, 30))
