import dataclasses

import pytest

from departureboard.core.model import Departure, Status, minutes_between


def make(scheduled="07:12", expected=None, status=Status.ON_TIME):
    return Departure(
        mode="train",
        service="Northern",
        destination="Manchester Piccadilly",
        scheduled=scheduled,
        expected=expected,
        status=status,
    )


def test_minutes_between_same_day():
    assert minutes_between("07:12", "07:19") == 7


def test_minutes_between_wraps_past_midnight():
    assert minutes_between("23:58", "00:03") == 5


def test_minutes_between_negative_when_earlier():
    assert minutes_between("07:19", "07:12") == -7


def test_on_time_departure_has_no_lateness():
    assert make().minutes_late == 0


def test_late_departure_reports_minutes_late():
    assert make(expected="07:19", status=Status.LATE).minutes_late == 7


def test_lateness_unknown_without_expected_time():
    assert make(status=Status.LATE).minutes_late is None


def test_departs_at_prefers_expected_time():
    assert make(expected="07:19", status=Status.LATE).departs_at == "07:19"


def test_departs_at_falls_back_to_scheduled():
    assert make().departs_at == "07:12"


def test_departure_is_gone_once_its_time_has_passed():
    assert make().is_gone(now="07:13")
    assert not make().is_gone(now="07:12")


def test_late_departure_is_not_gone_before_expected_time():
    assert not make(expected="07:19", status=Status.LATE).is_gone(now="07:15")


def test_cancelled_departure_stays_listed_until_scheduled_time():
    cancelled = make(status=Status.CANCELLED)
    assert not cancelled.is_gone(now="07:10")
    assert cancelled.is_gone(now="07:13")


def test_has_left_reads_the_time_on_the_day_it_was_fetched():
    from datetime import datetime

    fetched = datetime(2026, 10, 8, 8, 30)
    dep = make("08:40")
    assert not dep.has_left(datetime(2026, 10, 8, 8, 40, 59), fetched)
    assert dep.has_left(datetime(2026, 10, 8, 8, 41), fetched)
    assert dep.has_left(datetime(2026, 10, 8, 20, 41), fetched)


def test_has_left_carries_over_midnight():
    from datetime import datetime

    fetched = datetime(2026, 10, 8, 23, 50)
    dep = make("00:20")
    assert not dep.has_left(datetime(2026, 10, 9, 0, 10), fetched)
    assert dep.has_left(datetime(2026, 10, 9, 12, 30), fetched)


@pytest.mark.parametrize("field, value", [
    ("scheduled", "soon"), ("scheduled", "7:12"), ("scheduled", "24:00"), ("scheduled", None),
    ("expected", "Delayed"), ("expected", "07:60"), ("expected", ""),
])
def test_a_departure_needs_clock_times(field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(make(expected="07:19", status=Status.LATE), **{field: value})
