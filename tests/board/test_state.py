import json

import pytest

from twobells.board.state import MEMORY_ONLY, StateStore, departure_from_dict, departure_to_dict
from twobells.core.model import Departure, Status


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
