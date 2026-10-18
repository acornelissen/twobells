import dataclasses
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from twobells.core.model import Departure, Status, line_order, minutes_between


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


def test_minutes_late_across_the_clocks_changing_are_the_minutes_that_pass():
    london = ZoneInfo("Europe/London")
    # 25 October 2026: booked 01:50 BST, expected 01:10 GMT, seen at 01:05 GMT: 20 minutes, not 40 early.
    back = make("01:50", "01:10", Status.LATE)
    assert back.minutes_late_at(datetime(2026, 10, 25, 1, 5, tzinfo=UTC).astimezone(london)) == 20
    # 29 March 2026: booked 00:50 GMT, expected 02:10 BST: 20 minutes, not 80.
    forward = make("00:50", "02:10", Status.LATE)
    assert forward.minutes_late_at(datetime(2026, 3, 29, 0, 55, tzinfo=london)) == 20
    # Any other day, as on the clock; none without an expected time, none for on time.
    assert make("07:12", "07:19", Status.LATE).minutes_late_at(datetime(2026, 10, 9, 7, tzinfo=london)) == 7
    assert make(status=Status.LATE).minutes_late_at(datetime(2026, 10, 9, 7, tzinfo=london)) is None
    assert make().minutes_late_at(datetime(2026, 10, 9, 7, tzinfo=london)) == 0


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


def test_a_delayed_train_without_an_estimate_is_not_gone_at_its_booked_time():
    from datetime import datetime

    for status in (Status.LATE, Status.UNKNOWN):
        dep = make("10:00", status=status)
        assert not dep.is_gone(now="10:01")
        assert not dep.has_left(datetime(2026, 10, 8, 10, 30), datetime(2026, 10, 8, 10, 1))


def test_a_delayed_train_goes_in_the_end_if_no_fetch_comes():
    from datetime import datetime

    from twobells.core.model import HOLD_UNTIMED

    fetched = datetime(2026, 10, 8, 10, 1)
    dep = make("10:00", status=Status.LATE)
    assert not dep.has_left(fetched + HOLD_UNTIMED, fetched)
    assert dep.has_left(fetched + HOLD_UNTIMED + timedelta(minutes=1), fetched)


def test_a_bus_without_a_live_time_still_goes_at_its_booked_time():
    from datetime import datetime

    bus = Departure("bus", "341", "Hyde", "10:00", None, Status.UNKNOWN)
    assert bus.is_gone(now="10:01")
    assert bus.has_left(datetime(2026, 10, 8, 10, 1), datetime(2026, 10, 8, 9, 50))


@pytest.mark.parametrize("field, value", [
    ("scheduled", "soon"), ("scheduled", "7:12"), ("scheduled", "24:00"), ("scheduled", None),
    ("expected", "Delayed"), ("expected", "07:60"), ("expected", ""),
])
def test_a_departure_needs_clock_times(field, value):
    with pytest.raises(ValueError):
        dataclasses.replace(make(expected="07:19", status=Status.LATE), **{field: value})


def test_a_strip_says_where_and_draws_a_short_line_with_your_stop_on_it():
    from twobells.core.model import Strip

    strip = Strip("At Dinting", stops=7, yours=5, at=3.4)
    assert (strip.where, strip.stops, strip.yours, strip.at) == ("At Dinting", 7, 5, 3.4)
    assert Strip("Not started yet", stops=2, yours=1).at is None


@pytest.mark.parametrize("fields", [
    {"where": "x" * 61, "stops": 3, "yours": 1}, {"where": 3, "stops": 3, "yours": 1},
    {"where": "a", "stops": 1, "yours": 0}, {"where": "a", "stops": 9, "yours": 0},
    {"where": "a", "stops": True, "yours": 0}, {"where": "a", "stops": 3, "yours": 3},
    {"where": "a", "stops": 3, "yours": -1}, {"where": "a", "stops": 3, "yours": 1, "at": 2.5},
    {"where": "a", "stops": 3, "yours": 1, "at": -0.1}, {"where": "a", "stops": 3, "yours": 1, "at": float("nan")},
])
def test_a_strip_that_cant_be_drawn_is_refused(fields):
    from twobells.core.model import Strip

    with pytest.raises(ValueError):
        Strip(**fields)


def test_a_strip_belongs_to_the_same_departure_whatever_its_live_time():
    from twobells.core.model import strip_key

    on_time = Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME, stop="A")
    late = Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A")
    other = Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME, stop="B")
    assert strip_key(on_time) == strip_key(late) != strip_key(other)


# --- Platform changes from one fetch to the next ---

from twobells.core.model import mark_platform_changes  # noqa: E402


def at(platform, scheduled="07:12", **fields):
    return dataclasses.replace(make(scheduled), platform=platform, **fields)


def test_a_platform_unlike_the_last_fetchs_is_changed():
    [dep] = mark_platform_changes([at("1")], [at("2")])
    assert dep.platform_changed


def test_the_same_platform_is_no_change():
    [dep] = mark_platform_changes([at("1")], [at("1")])
    assert not dep.platform_changed


def test_a_change_stays_marked_while_the_train_keeps_its_new_platform():
    first = mark_platform_changes([at("1")], [at("2")])
    [dep] = mark_platform_changes(first, [at("2")])
    assert dep.platform_changed
    # And a second change is a change too.
    [dep] = mark_platform_changes(first, [at("3")])
    assert dep.platform_changed


def test_a_platform_appearing_or_going_is_no_change():
    assert not mark_platform_changes([at(None)], [at("2")])[0].platform_changed
    assert not mark_platform_changes([at("1")], [at(None)])[0].platform_changed
    # Nor is a train the last fetch didn't have.
    assert not mark_platform_changes([], [at("2")])[0].platform_changed


def test_only_the_same_train_is_compared():
    before = [at("1", "07:12"), at("3", "07:42")]
    marked = mark_platform_changes(before, [at("1", "07:12"), at("2", "08:12")])
    assert [d.platform_changed for d in marked] == [False, False]
    elsewhere = dataclasses.replace(at("2"), destination="Hadfield")
    assert not mark_platform_changes([at("1")], [elsewhere])[0].platform_changed


def test_a_cancelled_train_is_never_a_platform_change():
    # It leaves from no platform, so a new one is nothing to tell anyone.
    cancelled = at("2", status=Status.CANCELLED)
    assert not mark_platform_changes([at("1")], [cancelled])[0].platform_changed
    # Nor does a change marked before it was cancelled stay.
    first = mark_platform_changes([at("1")], [at("2")])
    assert not mark_platform_changes(first, [cancelled])[0].platform_changed


def test_a_replacement_bus_is_never_a_platform_change():
    train = at("1")
    bus = dataclasses.replace(make(), replacement_bus=True)
    assert not mark_platform_changes([train], [bus])[0].platform_changed
    assert not mark_platform_changes([bus], [bus])[0].platform_changed


def test_a_ferry_is_never_a_platform_change_nor_the_train_at_its_time():
    ferry = dataclasses.replace(at("1"), ferry=True)
    assert not mark_platform_changes([at("2")], [ferry])[0].platform_changed
    assert not mark_platform_changes([ferry], [dataclasses.replace(ferry, platform="2")])[0].platform_changed


def test_a_departure_is_a_ferry_only_when_it_says_so_and_never_a_bus_too():
    assert at("1").ferry is False
    with pytest.raises(TypeError):
        at("1", ferry="yes")
    with pytest.raises(ValueError):
        at("1", ferry=True, replacement_bus=True)


def test_via_and_platform_changed_must_be_the_right_types():
    with pytest.raises(TypeError):
        at("1", via=3)
    with pytest.raises(TypeError):
        at("1", platform_changed="yes")
    assert at("1", via="via Guide Bridge").via == "via Guide Bridge"


def test_lines_sort_by_their_number_with_the_prefix_breaking_ties():
    lines = ["X57", "341", "Free", "61", "57", "6", "X1"]
    assert sorted(lines, key=line_order) == ["X1", "6", "57", "X57", "61", "341", "Free"]
