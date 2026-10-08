from departureboard.model import Departure, Status
from departureboard.state import StateStore, departure_from_dict, departure_to_dict


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
