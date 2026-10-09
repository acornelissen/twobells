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
    from departureboard.state import departures_from_dicts

    good = departure_to_dict(Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME))
    bad = [{**good, "colour": "red"}, {**good, "status": "lost"}, {"mode": "bus"}, "341", None]
    with caplog.at_level("WARNING"):
        assert departures_from_dicts([bad[0], good, *bad[1:]]) == [departure_from_dict(good)]
    assert len(caplog.records) == len(bad)


def test_save_flushes_to_disk_before_replacing(tmp_path, monkeypatch):
    import departureboard.state as state

    calls = []
    replace = state.os.replace

    def replacing(src, dst):
        calls.append("replace")
        replace(src, dst)

    monkeypatch.setattr(state.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(state.os, "replace", replacing)
    StateStore(tmp_path / "state.json").save({"a": 1})
    assert calls == ["fsync", "replace"]
    assert StateStore(tmp_path / "state.json").load() == {"a": 1}
