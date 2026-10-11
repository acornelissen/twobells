import copy
import dataclasses
import json
import pickle
from datetime import date, datetime

import pytest

from departureboard.timetable.model import Calendar, Call, Journey, Schedule, StopPosition, sign_letter


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


@pytest.mark.parametrize(
    "indicator, letter",
    [
        ("Stop A", "A"), ("stop D", "D"), ("Stand 12", "12"), ("Bay C3", "C3"), ("B", "B"), (" Stop E ", "E"),
        # Words saying where, not which stop.
        ("opp", None), ("Opp", None), ("adj", None), ("NR", None), ("OS", None), ("NE-bound", None),
        ("", None), (None, None),
    ],
)
def test_the_letter_on_a_stop_s_sign(indicator, letter):
    assert sign_letter(indicator) == letter


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


def _every_field():
    calendar = Calendar(
        days=frozenset({0, 4}), start=date(2026, 9, 27), end=date(2027, 1, 3),
        bank_holidays_on=frozenset({"MayDay"}), bank_holidays_off=frozenset({"ChristmasDay", "BoxingDay"}),
        dates_on=((date(2026, 12, 24), date(2026, 12, 24)),), dates_off=((date(2026, 12, 25), date(2026, 12, 26)),),
        school_days_only=((date(2026, 9, 1), date(2026, 10, 23)),),
        not_on_school_days=((date(2026, 10, 26), date(2026, 10, 30)),),
    )
    journey = Journey("341", "BNML", "Hyde", "10620", (Call("1000DGHS0900", 380), Call("1800ANIN0K1", 1460)),
                      calendar, inbound_only=True)
    return Schedule(datetime(2026, 10, 9, 3, 0), date(2027, 1, 3), {"A": "1000DGHS0900"}, (journey,),
                    {"1000DGHS0900": StopPosition("Henry Street", 53.44384, -1.948999, "A")}, ("BNML",), 3,
                    {"1000DGHS0900>1800ANIN0K1": ((53.44, -1.95),)}, True)


# A timetable file with every field, as saved before journeys took slots.
_SAVED = (
    '{"built":"2026-10-09T03:00:00","format":3,"journeys":[{"calendar":{"bank_holidays_off":["BoxingDay",'
    '"ChristmasDay"],"bank_holidays_on":["MayDay"],"dates_off":[["2026-12-25","2026-12-26"]],"dates_on":'
    '[["2026-12-24","2026-12-24"]],"days":[0,4],"end":"2027-01-03","not_on_school_days":[["2026-10-26",'
    '"2026-10-30"]],"school_days_only":[["2026-09-01","2026-10-23"]],"start":"2026-09-27"},"calls":[{"atco":'
    '"1000DGHS0900","minute":380},{"atco":"1800ANIN0K1","minute":1460}],"destination":"Hyde","inbound_only":true,'
    '"journey_code":"10620","line":"341","operator":"BNML"}],"operators":["BNML"],"positions":{"1000DGHS0900":'
    '{"indicator":"A","lat":53.44384,"lon":-1.948999,"name":"Henry Street"}},"roads_pending":true,"shapes":'
    '{"1000DGHS0900>1800ANIN0K1":[[53.44,-1.95]]},"stops":{"A":"1000DGHS0900"},"valid_until":"2027-01-03"}'
)


def test_a_timetable_is_saved_as_it_always_was_and_older_files_load():
    schedule = _every_field()
    assert schedule.to_json() == _SAVED
    assert Schedule.from_json(_SAVED) == schedule


def test_journeys_keep_no_dict_of_their_own_and_stay_frozen():
    # A build holds thousands of journeys of 80 calls each: slots, not a
    # dict per object, keep each small. They still compare, hash, copy and
    # pickle as before, and can't be changed.
    schedule = _every_field()
    journey = schedule.journeys[0]
    for value in (journey, journey.calls[0], journey.calendar, schedule.positions["1000DGHS0900"]):
        assert not hasattr(value, "__dict__"), type(value).__name__
        assert pickle.loads(pickle.dumps(value)) == value
        assert copy.deepcopy(value) == value and hash(copy.copy(value)) == hash(value)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, "changed")
        with pytest.raises((AttributeError, TypeError)):  # TypeError: dataclasses' slots, Python 3.12
            value.extra = 1
    assert dataclasses.replace(journey, line="342").line == "342"
    assert len({journey, dataclasses.replace(journey)}) == 1


def _as_it_was_saved(schedule):
    """The file as Schedule.to_json wrote it whole, before it was written in parts."""
    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(plain(v) for v in value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    data = dataclasses.asdict(dataclasses.replace(schedule, shapes={}))
    data["shapes"] = dict(schedule.shapes)
    return json.dumps(plain(data), separators=(",", ":"), sort_keys=True)


def _several():
    schedule = _every_field()
    first = schedule.journeys[0]
    second = dataclasses.replace(first, journey_code="10621", calls=(Call("1800ANIN0K1", 400),), inbound_only=False,
                                 calendar=Calendar(frozenset({6, 5}), bank_holidays_on=frozenset({"B", "A"})))
    return dataclasses.replace(
        schedule, stops={"B": "1800ANIN0K1", "A": "1000DGHS0900"}, journeys=(first, second, first),
        positions={"1800ANIN0K1": StopPosition("Market Hall", 53.4, -1.9), **schedule.positions},
        shapes={"Z>A": ((53.0, -1.0),), **schedule.shapes})


def test_a_timetable_written_in_parts_is_the_file_it_always_was():
    schedule = _several()
    assert "".join(schedule.json_parts()) == schedule.to_json() == _as_it_was_saved(schedule)
    empty = Schedule(datetime(2026, 10, 9, 3, 0), None, {}, ())
    assert empty.to_json() == _as_it_was_saved(empty)


@pytest.mark.parametrize("piece", [1, 2, 3, 7, 64])
def test_a_timetable_read_from_a_file_a_little_at_a_time_is_read_whole(monkeypatch, piece):
    import io

    from departureboard.timetable import model

    monkeypatch.setattr(model._Reader, "_PIECE", piece)
    schedule = _several()
    text = schedule.to_json()
    assert Schedule.from_file(io.StringIO(text)) == schedule
    assert Schedule.from_file(io.StringIO(json.dumps(json.loads(text), indent=2))) == schedule
    light = Schedule.from_file(io.StringIO(text), journeys=False)
    assert light == dataclasses.replace(schedule, journeys=(), shapes={})
    with pytest.raises(ValueError):
        Schedule.from_file(io.StringIO(text[:-1]))
    with pytest.raises(ValueError):
        Schedule.from_file(io.StringIO(text + "1"))


def test_a_timetable_read_shares_its_journeys_equal_values():
    back = Schedule.from_json(_several().to_json())
    first, second, third = back.journeys
    assert first.calendar is third.calendar and first.calls[0].atco is third.calls[0].atco
    assert first.calls[1].atco is second.calls[0].atco and first.line is second.line
