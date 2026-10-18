import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from twobells.board.state import (
    MEMORY_ONLY,
    StateStore,
    departure_from_dict,
    departure_to_dict,
    vehicle_to_dict,
    vehicles_from_dicts,
)
from twobells.core.model import Departure, Status
from twobells.sources.bods import Vehicle


def test_missing_state_file_loads_empty(tmp_path):
    assert StateStore(tmp_path / "nope.json").load() == {}


def test_corrupt_state_file_loads_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateStore(path).load() == {}


def test_save_then_load(tmp_path):
    store = StateStore(tmp_path / "sub" / "state.json")
    store.save({"a": 1})
    assert store.load() == {"a": 1}


def test_departure_round_trips_through_dict():
    dep = Departure("train", "Northern", "Manchester", "07:12", "07:19", Status.LATE, "1")
    assert departure_from_dict(departure_to_dict(dep)) == dep


def test_a_replacement_bus_round_trips_and_older_saved_trains_are_not_one():
    dep = Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, replacement_bus=True)
    assert departure_from_dict(departure_to_dict(dep)) == dep
    older = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME))
    del older["replacement_bus"]
    assert departure_from_dict(older).replacement_bus is False


def test_a_ferry_round_trips_and_older_saved_trains_are_not_one():
    dep = Departure("train", "Wightlink", "Ryde Pier Head", "07:12", None, Status.ON_TIME, ferry=True)
    assert departure_from_dict(departure_to_dict(dep)) == dep
    older = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME))
    del older["ferry"]
    assert departure_from_dict(older).ferry is False


def test_via_and_a_changed_platform_round_trip_and_older_saved_trains_have_neither():
    dep = Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, "2",
                    via="via Guide Bridge", platform_changed=True)
    assert departure_from_dict(departure_to_dict(dep)) == dep
    older = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, "2"))
    del older["via"], older["platform_changed"]
    assert (departure_from_dict(older).via, departure_from_dict(older).platform_changed) == (None, False)


def test_the_calling_at_line_round_trips_and_older_saved_trains_have_none():
    dep = Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, "1",
                    calling_at="Calling at: Manchester only.")
    assert departure_from_dict(departure_to_dict(dep)) == dep
    older = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, "1"))
    del older["calling_at"]
    assert departure_from_dict(older).calling_at is None
    with pytest.raises(TypeError):
        departure_from_dict({**older, "calling_at": ["Manchester"]})


# What a departure was saved with before a screen's speaker had its own fields.
OLDER_KEYS = {"mode", "service", "destination", "scheduled", "expected", "status", "platform", "stop",
              "replacement_bus", "ferry", "via", "platform_changed", "calling_at"}


def test_what_a_speaker_says_round_trips_through_the_state_file(tmp_path):
    dep = Departure("train", "Northern", "Manchester", "07:12", "07:20", Status.LATE, "1",
                    calling_stops=("Dinting", "Manchester Piccadilly"), skipped_stops=("Hattersley",), coaches=3,
                    reason="This train has been delayed by a broken down train")
    store = StateStore(tmp_path / "state.json")
    store.save({"sources": {"train": {"departures": [departure_to_dict(dep)]}}})
    [saved] = store.load()["sources"]["train"]["departures"]
    assert departure_from_dict(saved) == dep


def test_an_older_saved_train_loads_with_nothing_more_to_say():
    older = {"mode": "train", "service": "Northern", "destination": "Manchester", "scheduled": "07:12",
             "expected": None, "status": "on_time", "platform": "1", "stop": None, "replacement_bus": False,
             "ferry": False, "via": None, "platform_changed": False, "calling_at": None}
    assert set(older) == OLDER_KEYS
    dep = departure_from_dict(older)
    assert (dep.calling_stops, dep.skipped_stops, dep.coaches, dep.reason) == ((), (), None, None)


def test_a_train_with_nothing_more_to_say_is_saved_as_before_for_an_older_version_to_read():
    # E-paper's trains never carry what a speaker says, so its state file is unchanged.
    dep = Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME, "1",
                    calling_at="Calling at: Manchester only.")
    assert set(departure_to_dict(dep)) == OLDER_KEYS


@pytest.mark.parametrize("field, value", [
    ("calling_stops", "Dinting"), ("calling_stops", [1]), ("calling_stops", {"a": 1}), ("skipped_stops", [None]),
    ("coaches", True), ("coaches", "3"), ("coaches", 0), ("coaches", 2.5), ("reason", ["late"]),
])
def test_a_saved_train_with_a_speaker_field_of_the_wrong_type_is_rejected(field, value):
    good = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", None, Status.ON_TIME))
    with pytest.raises((TypeError, ValueError)):
        departure_from_dict({**good, field: value})


def test_undecodable_state_file_loads_empty(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"sources": "\xff\xfe"}')
    with caplog.at_level("WARNING"):
        assert StateStore(path).load() == {}
    assert "state.json" in caplog.text


def test_state_file_that_is_not_an_object_loads_empty(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("[1, 2]")
    with caplog.at_level("WARNING"):
        assert StateStore(path).load() == {}
    assert "state.json" in caplog.text


def test_malformed_departures_are_skipped_with_a_warning(caplog):
    from twobells.board.state import departures_from_dicts

    good = departure_to_dict(Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME))
    bad = [{**good, "colour": "red"}, {**good, "status": "lost"}, {"mode": "bus"}, "341", None]
    with caplog.at_level("WARNING"):
        assert departures_from_dicts([bad[0], good, *bad[1:]]) == [departure_from_dict(good)]
    assert len(caplog.records) == len(bad)


def test_save_flushes_to_disk_before_replacing(tmp_path, monkeypatch):
    import twobells.core.files as files  # state.json is written through files.write_atomic

    calls = []
    replace = files.os.replace

    def replacing(src, dst):
        calls.append("replace")
        replace(src, dst)

    monkeypatch.setattr(files.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(files.os, "replace", replacing)
    StateStore(tmp_path / "state.json").save({"a": 1})
    assert calls == ["fsync", "replace"]
    assert StateStore(tmp_path / "state.json").load() == {"a": 1}


def test_state_with_a_malformed_table_is_cleaned_not_fatal(tmp_path):
    import json

    from twobells.board.state import StateStore

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sources": [1, 2], "budgets": "x", "last_render": {"at": None}, "sources_ok": 1}))
    state = StateStore(path).load()
    assert "sources" not in state and "budgets" not in state
    assert state["last_render"] == {"at": None}


@pytest.mark.parametrize("field, value", [
    ("scheduled", "soon"), ("scheduled", None), ("scheduled", "7:20"), ("scheduled", "24:00"), ("scheduled", 720),
    ("expected", "Delayed"), ("expected", "07:60"), ("expected", 724),
])
def test_a_saved_departure_whose_time_is_not_a_clock_time_is_rejected(field, value):
    good = departure_to_dict(Departure("train", "Northern", "Manchester", "07:12", "07:19", Status.LATE))
    with pytest.raises(ValueError):
        departure_from_dict({**good, field: value})


@pytest.mark.parametrize("field, value", [
    ("mode", None), ("service", 341), ("destination", ["Hyde"]), ("platform", 1), ("stop", {"a": 1}),
    ("replacement_bus", "yes"), ("replacement_bus", 1),
    ("ferry", "yes"), ("ferry", 1),
])
def test_a_saved_departure_with_a_field_of_the_wrong_type_is_rejected(field, value):
    good = departure_to_dict(Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME, stop="A"))
    with pytest.raises((TypeError, ValueError)):
        departure_from_dict({**good, field: value})


def test_an_old_state_file_with_a_bad_time_still_loads_the_good_ones(tmp_path):
    from twobells.board.state import departures_from_dicts

    good = departure_to_dict(Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME))
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sources": {"bus": {"departures": [{**good, "scheduled": "soon"}, good]}}}))
    items = StateStore(path).load()["sources"]["bus"]["departures"]
    assert departures_from_dicts(items) == [departure_from_dict(good)]


def test_a_saved_source_that_is_not_an_object_is_dropped(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"sources": {"train": "07:12", "bus": {"departures": []}}}))
    with caplog.at_level("WARNING"):
        assert StateStore(path).load()["sources"] == {"bus": {"departures": []}}
    assert "train" in caplog.text


@pytest.mark.parametrize("items", [5, None, "07:20", {"scheduled": "07:20"}])
def test_saved_departures_that_are_not_a_list_read_as_none(items):
    from twobells.board.state import departures_from_dicts

    assert departures_from_dicts(items) == []


def test_row_strips_are_never_saved_nor_read_back(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = {"sources": {}, MEMORY_ONLY: {"bus": {"items": {}}}}
    store.save(state)
    assert MEMORY_ONLY not in json.loads((tmp_path / "state.json").read_text())
    assert MEMORY_ONLY in state  # still in memory
    (tmp_path / "state.json").write_text(json.dumps({"sources": {}, MEMORY_ONLY: {"bus": {}}}))
    assert store.load() == {"sources": {}}


def _vehicle(**changes):
    aimed = datetime(2026, 10, 8, 6, 40, tzinfo=timezone(timedelta(hours=1)))
    vehicle = Vehicle("341", "Hyde", "inbound", 53.44, -2.0, "2026-10-08T07:00:00+01:00", 90.0,
                      operator="BNML", origin_ref="1800SB12345", origin_aimed=aimed)
    return dataclasses.replace(vehicle, **changes)


def test_a_saved_bus_position_round_trips():
    vehicle = _vehicle()
    assert vehicles_from_dicts([vehicle_to_dict(vehicle)]) == [vehicle]
    unknown = _vehicle(bearing=None, origin_aimed=None)
    assert vehicles_from_dicts([vehicle_to_dict(unknown)]) == [unknown]
    whole = {**vehicle_to_dict(vehicle), "lat": 53, "lon": -2}  # as a hand-edited file may have them
    assert vehicles_from_dicts([whole]) == [_vehicle(lat=53.0, lon=-2.0)]


def _bad_vehicle_rows():
    good = vehicle_to_dict(_vehicle())
    without_lat = {key: value for key, value in good.items() if key != "lat"}
    without_line = {key: value for key, value in good.items() if key != "line"}
    return [
        {**good, "lat": 10**400}, {**good, "lon": -(10**400)}, {**good, "bearing": 10**400},
        {**good, "lat": "53.44"}, {**good, "lon": None}, {**good, "lat": True}, {**good, "bearing": "east"},
        {**good, "lat": 91.0}, {**good, "lon": -180.5},
        {**good, "line": 341}, {**good, "operator": None}, {**good, "recorded_at": ["07:00"]},
        {**good, "origin_aimed": 5}, {**good, "origin_aimed": "not a time"}, {**good, "colour": "red"},
        without_lat, without_line, "341", None, [1, 2], 53.44,
    ]


def test_saved_bus_positions_that_dont_read_back_are_skipped_with_a_warning(caplog):
    bad = _bad_vehicle_rows()
    with caplog.at_level("WARNING"):
        assert vehicles_from_dicts([bad[0], vehicle_to_dict(_vehicle()), *bad[1:]]) == [_vehicle()]
    assert len(caplog.records) == len(bad)


def test_a_state_file_with_bad_bus_positions_still_reads_the_good_ones(tmp_path):
    path = tmp_path / "state.json"
    rows = [*_bad_vehicle_rows(), vehicle_to_dict(_vehicle())]
    path.write_text(json.dumps({"vehicles": {"items": rows}}))  # 10**400 is written as its 401 digits
    assert vehicles_from_dicts(StateStore(path).load()["vehicles"]["items"]) == [_vehicle()]


@pytest.mark.parametrize("items", [5, None, "341", {"line": "341"}])
def test_saved_bus_positions_that_are_not_a_list_read_as_none(items):
    assert vehicles_from_dicts(items) == []
