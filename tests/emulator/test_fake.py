from datetime import datetime, timedelta

import pytest

from twobells.core.model import Status, minutes_between
from twobells.emulator.fake import FakeBusSource, FakeTrainSource, SimClock

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


def test_fake_trains_call_where_their_calling_points_say_unless_cancelled():
    sources = [FakeTrainSource(SimClock(START), seed=seed, rows=6) for seed in range(20)]
    found = [pair for source in sources for pair in zip(source.fetch(), source.services, strict=True)]
    assert {dep.status for dep, _ in found} >= {Status.CANCELLED, Status.ON_TIME}
    for dep, service in found:
        if dep.status is Status.CANCELLED:
            assert dep.calling_at is None and all(p.skipped for p in service.calling_points)
        else:
            names = [p.name for p in service.calling_points]
            assert dep.calling_at == f"Calling at: {', '.join(names[:-1])} and {names[-1]}. " \
                                     "This train is formed of 3 coaches."


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

    from twobells.emulator.fake import DEMO_STRIP, FakeVehicleSource, SimClock
    from twobells.timetable.route import position_on

    # 341s are due at :05, :25, :45; at 07:21 the 07:25 is 4 min (2 stops) out.
    [bus] = FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 21))).fetch()
    assert bus.line == "341"
    assert position_on(DEMO_STRIP, bus) == pytest.approx(3, abs=0.01)
    [at_stop] = FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 25))).fetch()
    assert position_on(DEMO_STRIP, at_stop) == pytest.approx(5, abs=0.01)


def test_fake_bus_is_off_the_strip_when_far_away():
    from datetime import datetime

    from twobells.emulator.fake import FakeVehicleSource, SimClock

    # At 07:06 the next 341 is 19 min away, beyond the 10 min strip.
    assert FakeVehicleSource(SimClock(datetime(2026, 10, 8, 7, 6))).fetch() == []


def test_fake_trains_carry_a_disruption_message_while_told_to():
    from twobells.emulator.fake import DEMO_NOTICE

    clock = SimClock(START)
    source = FakeTrainSource(clock, notice_when=lambda now: now.minute >= 59)
    source.fetch()
    assert source.messages == []
    clock.advance()
    source.fetch()
    assert source.messages == [DEMO_NOTICE]


def test_fake_trains_have_no_messages_by_default():
    source = FakeTrainSource(SimClock(START))
    source.fetch()
    assert source.messages == []


def test_fake_forecast_covers_two_days_with_a_wet_morning_hour():
    from twobells.emulator.fake import FakeWeatherSource

    hours = FakeWeatherSource(SimClock(START)).fetch()
    assert len(hours) == 48
    assert hours[0].start == START.replace(hour=0, minute=0)
    assert [h.start.hour for h in hours if h.probability >= 50] == [7, 7]
    assert hours == FakeWeatherSource(SimClock(START + timedelta(hours=3))).fetch()


def test_fake_buses_say_where_each_tracked_bus_is():
    from twobells.core.model import strip_key

    source = FakeBusSource(SimClock(datetime(2026, 10, 8, 7, 6)), rows=5)
    departures = {strip_key(d): d for d in source.fetch()}
    strips = source.live_strips()
    assert set(strips) < set(departures)
    assert all(departures[key].status is not Status.UNKNOWN for key in strips)  # no tracker, no position
    next_bus = strips[("bus", "393", "B", "07:08")]  # two minutes away, two minutes a stop
    assert (next_bus.where, next_bus.at, next_bus.yours) == ("1 stop away", 4.0, 5)


def test_fake_buses_name_both_stops_henry_street_as_glossop_s_are():
    assert FakeBusSource(SimClock(START)).stop_names() == {"A": "Henry Street", "B": "Henry Street"}


def test_fake_bus_positions_fail_with_the_bus_feed():
    source = FakeBusSource(SimClock(START), failing_when=lambda now: True)
    with pytest.raises(OSError):
        source.live_strips()


def test_fake_trains_say_where_they_are():
    from twobells.emulator.fake import FakeServiceDetails
    from twobells.sources.darwin import ServiceGone, train_position

    clock = SimClock(datetime(2026, 10, 8, 7, 9))
    trains = FakeTrainSource(clock)
    trains.fetch()
    assert [s.service_id for s in trains.services] == ["FAKE0712GLO", "FAKE0742GLO", "FAKE0812GLO"]
    details = FakeServiceDetails(trains, clock)
    waiting = train_position(details.details("FAKE0712GLO"), clock())
    assert waiting.where == "Not started yet: leaves Hadfield at 07:11"  # 07:12, seven minutes late
    assert waiting.strip_stops == ("Hadfield", "Dinting", "Glossop", "Dinting") and waiting.train_at is None
    for _ in range(4):
        clock.advance()
    on_its_way = train_position(details.details("FAKE0712GLO"), clock())
    assert on_its_way.where.startswith("Left Hadfield") and 0 < on_its_way.train_at < 1
    with pytest.raises(ServiceGone):
        details.details("FAKE0000GLO")
