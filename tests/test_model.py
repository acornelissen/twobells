from departureboard.model import Departure, Status, minutes_between


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
