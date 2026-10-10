from departureboard.core.model import Departure, Status, shift
from departureboard.core.walk import WalkTimes, leave_by, leave_time


def train(scheduled, expected=None, status=Status.ON_TIME):
    return Departure("train", "Northern", "Manchester Piccadilly", scheduled, expected, status)


def bus(line, scheduled, stop="A", expected=None, status=Status.ON_TIME):
    return Departure("bus", line, "Hyde", scheduled, expected, status, stop=stop)


WALK = WalkTimes(train=7, bus={"A": 6, "B": 5})


def test_shift_moves_a_clock_time():
    assert shift("07:12", -7) == "07:05"
    assert shift("00:03", -7) == "23:56"
    assert shift("23:58", 5) == "00:03"


def test_train_walk_time():
    assert WALK.minutes(train("07:12")) == 7


def test_bus_walk_time_is_per_stop():
    assert WALK.minutes(bus("341", "07:20", stop="A")) == 6
    assert WALK.minutes(bus("61", "07:20", stop="B")) == 5


def test_bus_walk_time_falls_back_to_default():
    walk = WalkTimes(bus={"*": 4})
    assert walk.minutes(bus("341", "07:20", stop="Z")) == 4
    assert walk.minutes(bus("341", "07:20", stop=None)) == 4


def test_unknown_walk_time_is_none():
    assert WalkTimes().minutes(train("07:12")) is None


def test_leave_by_first_departure_still_reachable():
    deps = [train("07:12"), train("07:42")]
    assert leave_by(deps, WALK, now="07:00") == ("07:05", deps[0])
    assert leave_by(deps, WALK, now="07:06") == ("07:35", deps[1])


def test_leaving_exactly_now_still_counts():
    deps = [train("07:12")]
    assert leave_by(deps, WALK, now="07:05") == ("07:05", deps[0])


def test_leave_by_uses_expected_time_when_late():
    late = train("07:12", "07:20", Status.LATE)
    assert leave_by([late], WALK, now="07:08") == ("07:13", late)


def test_leave_by_skips_cancelled():
    deps = [train("07:12", status=Status.CANCELLED), train("07:42")]
    assert leave_by(deps, WALK, now="07:00") == ("07:35", deps[1])


def test_leave_by_none_when_nothing_reachable():
    assert leave_by([train("07:12")], WALK, now="07:10") is None


def test_leave_by_none_without_walk_time():
    assert leave_by([train("07:12")], WalkTimes(), now="07:00") is None


def test_buses_use_their_own_stop_walk_time():
    deps = [bus("341", "07:10", stop="A"), bus("61", "07:10", stop="B")]
    assert leave_by(deps, WALK, now="07:05") == ("07:05", deps[1])


def test_leave_time_for_one_departure():
    assert leave_time(bus("341", "07:20", stop="A"), WALK) == "07:14"


def test_leave_time_uses_expected_time_when_late():
    assert leave_time(bus("341", "07:20", expected="07:24", status=Status.LATE), WALK) == "07:18"


def test_no_leave_time_for_cancelled_or_unknown_walk():
    assert leave_time(bus("341", "07:20", status=Status.CANCELLED), WALK) is None
    assert leave_time(bus("341", "07:20", stop="Z"), WALK) is None
