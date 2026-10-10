import json
from datetime import date, datetime

import pytest

from departureboard.timetable.model import Calendar, Call, Journey, Schedule, StopPosition


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


def test_road_shapes_round_trip_and_older_files_have_none():
    schedule = Schedule(datetime(2026, 10, 9, 3, 0), None, {"A": "1000DGHS0900"}, (),
                        shapes={"1000DGHS3408>1000DGHS3406": ((53.4436, -1.9563), (53.44352, -1.9558))})
    text = schedule.to_json()
    assert '"shapes":{"1000DGHS3408>1000DGHS3406":[[53.4436,-1.9563],[53.44352,-1.9558]]}' in text
    assert Schedule.from_json(text) == schedule
    old = json.loads(text)
    del old["shapes"]
    assert Schedule.from_json(json.dumps(old)).shapes == {}


def test_roads_still_to_find_round_trip_and_older_files_have_none_to_find():
    schedule = Schedule(datetime(2026, 10, 9, 3, 0), None, {"A": "1000DGHS0900"}, (), roads_pending=True)
    assert Schedule.from_json(schedule.to_json()).roads_pending is True
    old = json.loads(schedule.to_json())
    del old["roads_pending"]
    assert Schedule.from_json(json.dumps(old)).roads_pending is False
    old["roads_pending"] = "yes"  # only true itself says so; the timetable still loads
    assert Schedule.from_json(json.dumps(old)).roads_pending is False


GOOD_ROAD = [[53.4436, -1.9563]]


def _with_roads(shapes, journeys="[]"):
    return ('{"built": "2026-10-09T03:00:00", "valid_until": null, "stops": {}, "journeys": ' + journeys
            + ', "shapes": ' + shapes + '}')


@pytest.mark.parametrize("points", [[[53.4, "x"]], [[53.4]], [[53.4, -1.9, 0]], [[53.4, None]], [53.4, -1.9],
                                    "53.4", {"lat": 53.4}])
def test_a_road_shape_that_isnt_points_leaves_every_road_straight(points, caplog):
    # Strict: one bad road and none are used. The timetable itself still loads.
    schedule = Schedule.from_json(_with_roads(json.dumps({"A>B": GOOD_ROAD, "B>C": points})))
    assert schedule.shapes == {} and len(schedule.shapes) == 0 and schedule.shapes.get("A>B") is None
    assert "roads" in caplog.text


def test_a_road_shape_that_isnt_finite_leaves_every_road_straight(caplog):
    schedule = Schedule.from_json(_with_roads('{"A>B": [[53.4436, -1.9563]], "B>C": [[NaN, -1.9]]}'))
    assert schedule.shapes == {}
    assert "finite" in caplog.text


def test_roads_that_arent_a_mapping_are_none():
    assert Schedule.from_json(_with_roads("[[53.4, -1.9]]")).shapes == {}


def test_the_roads_are_read_only_when_first_asked_for(monkeypatch):
    # The e-paper board reads the timetable every minute and never draws a road.
    from departureboard.timetable import model

    read = []
    real = model._point
    monkeypatch.setattr(model, "_point", lambda pair: read.append(pair) or real(pair))
    schedule = Schedule.from_json(_with_roads(json.dumps({"A>B": GOOD_ROAD * 3})))
    assert read == []
    assert schedule.shapes["A>B"] == ((53.4436, -1.9563),) * 3
    assert len(read) == 3
    assert dict(schedule.shapes) == {"A>B": ((53.4436, -1.9563),) * 3} and len(read) == 3  # read once


def test_roads_read_lazily_round_trip_and_compare_equal():
    schedule = Schedule(datetime(2026, 10, 9, 3, 0), None, {}, (), shapes={"A>B": ((53.4436, -1.9563),)})
    back = Schedule.from_json(schedule.to_json())
    assert back == schedule and schedule == back
    assert back.to_json() == schedule.to_json()
    assert Schedule.from_json(back.to_json()) == back
