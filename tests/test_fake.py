from datetime import datetime, timedelta

import pytest

from departureboard.fake import FakeBusSource, FakeTrainSource, SimClock
from departureboard.model import Status, minutes_between

START = datetime(2026, 10, 8, 6, 58)


def test_sim_clock_starts_at_given_time_and_advances_by_step():
    clock = SimClock(START, step=timedelta(minutes=1))
    assert clock() == START
    clock.advance()
    assert clock() == START + timedelta(minutes=1)


def test_fake_trains_are_upcoming_and_in_order():
    clock = SimClock(START)
    deps = FakeTrainSource(clock, seed=1).fetch()
    assert len(deps) == 3
    assert all(d.mode == "train" for d in deps)
    assert all(minutes_between("06:58", d.scheduled) >= 0 for d in deps)
    assert [d.scheduled for d in deps] == sorted(d.scheduled for d in deps)


def test_fake_trains_run_half_hourly_to_manchester():
    deps = FakeTrainSource(SimClock(START), seed=1).fetch()
    assert [d.scheduled for d in deps] == ["07:12", "07:42", "08:12"]
    assert {d.destination for d in deps} == {"Manchester Piccadilly"}


def test_fake_data_is_deterministic_for_a_seed():
    a = FakeTrainSource(SimClock(START), seed=7).fetch()
    b = FakeTrainSource(SimClock(START), seed=7).fetch()
    assert a == b


def test_fake_train_statuses_change_over_time():
    clock = SimClock(START)
    source = FakeTrainSource(clock, seed=3)
    seen = set()
    for _ in range(120):
        seen.update(d.status for d in source.fetch())
        clock.advance()
    assert {Status.ON_TIME, Status.LATE, Status.CANCELLED} <= seen


def test_late_fake_trains_carry_an_expected_time_after_schedule():
    clock = SimClock(START)
    source = FakeTrainSource(clock, seed=3)
    for _ in range(120):
        for dep in source.fetch():
            if dep.status is Status.LATE and dep.expected:
                assert minutes_between(dep.scheduled, dep.expected) > 0
        clock.advance()


def test_fake_buses_are_labelled_with_stops_and_upcoming():
    deps = FakeBusSource(SimClock(START), seed=1, rows=2).fetch()
    assert len(deps) == 2
    assert all(d.mode == "bus" and d.stop in {"A", "B"} for d in deps)
    assert all(minutes_between("06:58", d.scheduled) >= 0 for d in deps)


def test_fake_source_can_be_told_to_fail():
    source = FakeTrainSource(SimClock(START), seed=1)
    source.failing = True
    with pytest.raises(OSError):
        source.fetch()


def test_fake_source_fails_while_its_outage_predicate_holds():
    clock = SimClock(START)
    source = FakeBusSource(clock, failing_when=lambda now: now.minute == 59)
    source.fetch()
    clock.advance()
    with pytest.raises(OSError):
        source.fetch()


def test_fake_bus_reaches_your_stop_when_it_is_due():
    from datetime import datetime

    import pytest

    from departureboard.fake import DEMO_STRIP, FakeVehicleSource, SimClock
    from departureboard.route import position_on

    # 341s are due at :05, :25, :45; at 07:21 the 07:25 is 4 min (2 stops) out.
    [bus] = FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 21))).fetch()
    assert bus.line == "341"
    assert position_on(DEMO_STRIP, bus) == pytest.approx(3, abs=0.01)
    [at_stop] = FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 25))).fetch()
    assert position_on(DEMO_STRIP, at_stop) == pytest.approx(5, abs=0.01)


def test_fake_bus_is_off_the_strip_when_far_away():
    from datetime import datetime

    from departureboard.fake import FakeVehicleSource, SimClock

    # At 07:06 the next 341 is 19 min away, beyond the 10 min strip.
    assert FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 6))).fetch() == []
