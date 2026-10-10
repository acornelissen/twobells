import json
from datetime import date, datetime

import pytest

from departureboard.schedule_model import Calendar, Call, Journey, Schedule, StopPosition


def test_schedule_round_trips_through_json():
    calendar = Calendar(
        days=frozenset({0, 1, 2, 3, 4}),
        start=date(2026, 9, 27),
        bank_holidays_off=frozenset({"ChristmasDay", "MayDay"}),
        school_days_only=((date(2026, 9, 1), date(2026, 9, 4)),),
    )
    journey = Journey("341", "BNML", "Hyde", "10620", (Call("1000DGHS0900", 380), Call("1800ANIN0K1", 1460)), calendar)
    schedule = Schedule(
        built=datetime(2026, 10, 9, 3, 0),
        valid_until=None,
        stops={"A": "1000DGHS0900"},
        journeys=(journey,),
        positions={"1000DGHS0900": StopPosition("Henry Street", 53.44384, -1.948999)},
        operators=("BNML", "HIPK"),
    )
    back = Schedule.from_json(schedule.to_json())
    assert back == schedule
    assert back.journeys[0].origin == Call("1000DGHS0900", 380)


def test_a_schedule_saved_before_operators_were_recorded_has_none():
    old = json.dumps({"built": "2026-10-09T03:00:00", "valid_until": None, "stops": {"A": "1000DGHS0900"},
                      "journeys": [], "positions": {}})
    assert Schedule.from_json(old).operators == ()


def test_an_inbound_only_journey_round_trips_and_older_files_have_none():
    calendar = Calendar(days=frozenset({5}))
    journey = Journey("341", "BNML", "Glossop", "1", (Call("1800ANIN0K1", 600), Call("1000DGHSW936", 640)),
                      calendar, inbound_only=True)
    schedule = Schedule(datetime(2026, 10, 9, 3, 0), None, {"A": "1000DGHS0900"}, (journey,))
    back = Schedule.from_json(schedule.to_json())
    assert back == schedule and back.journeys[0].inbound_only is True
    assert back.departure_journeys == ()
    old = json.loads(schedule.to_json())
    del old["journeys"][0]["inbound_only"]  # saved before inbound-only journeys were kept
    loaded = Schedule.from_json(json.dumps(old))
    assert loaded.journeys[0].inbound_only is False
    assert loaded.departure_journeys == loaded.journeys


def test_inbound_only_must_be_true_or_false():
    journey = Journey("341", "BNML", "Glossop", "1", (Call("x", 600),), Calendar(days=frozenset()))
    data = json.loads(Schedule(datetime(2026, 10, 9, 3, 0), None, {}, (journey,)).to_json())
    data["journeys"][0]["inbound_only"] = "false"
    with pytest.raises(TypeError):
        Schedule.from_json(json.dumps(data))


def test_an_older_file_has_format_one_and_no_sign_letters():
    old = json.dumps({"built": "2026-10-09T03:00:00", "valid_until": None, "stops": {"A": "1000DGHS0900"},
                      "journeys": [], "positions": {"1000DGHS0900": {"name": "Henry Street", "lat": 53.4, "lon": -1.9}}})
    schedule = Schedule.from_json(old)
    assert schedule.format == 1 and schedule.positions["1000DGHS0900"].indicator is None
