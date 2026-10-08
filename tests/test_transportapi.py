import pytest

from departureboard.model import Status
from departureboard.transportapi import TransportApiBusSource, parse_stop


def bus(**overrides):
    base = {
        "line_name": "341",
        "direction": "Hyde",
        "date": "2026-10-09",
        "aimed_departure_time": "07:20",
        "expected_departure_time": None,
        "best_departure_estimate": "07:20",
    }
    base.update(overrides)
    return base


def stop(*buses, key="all"):
    return {"atcocode": "1600GLX0001", "departures": {key: list(buses)}}


def test_live_on_time_bus():
    [dep] = parse_stop(stop(bus(expected_departure_time="07:20")))
    assert dep.mode == "bus"
    assert dep.service == "341"
    assert dep.destination == "Hyde"
    assert dep.scheduled == "07:20"
    assert dep.status is Status.ON_TIME


def test_live_late_bus():
    [dep] = parse_stop(stop(bus(expected_departure_time="07:24", best_departure_estimate="07:24")))
    assert dep.status is Status.LATE
    assert dep.expected == "07:24"


def test_timetable_only_bus_is_unknown_status():
    [dep] = parse_stop(stop(bus()))
    assert dep.status is Status.UNKNOWN
    assert dep.expected is None


def test_falls_back_to_line_when_line_name_missing():
    entry = bus()
    del entry["line_name"]
    entry["line"] = "61"
    [dep] = parse_stop(stop(entry))
    assert dep.service == "61"


def test_grouped_routes_are_flattened_and_sorted():
    payload = {
        "departures": {
            "341": [bus(aimed_departure_time="07:40", best_departure_estimate="07:40")],
            "61": [bus(line_name="61", aimed_departure_time="07:25", best_departure_estimate="07:25")],
        }
    }
    deps = parse_stop(payload)
    assert [d.service for d in deps] == ["61", "341"]


def test_direction_filter_keeps_matching_buses_only():
    payload = stop(bus(direction="Hyde"), bus(direction="Hayfield"))
    deps = parse_stop(payload, directions=["hyde"])
    assert [d.destination for d in deps] == ["Hyde"]


def test_missing_departures_is_empty():
    assert parse_stop({}) == []


def test_parse_stop_tags_departures_with_stop_label():
    [dep] = parse_stop(stop(bus()), stop_label="A")
    assert dep.stop == "A"


def source(stops, payloads, rows=2, directions=None, calls=None):
    def fake_get(url, headers):
        if calls is not None:
            calls.append((url, headers))
        atco = url.split("/stop_timetables/")[1].split(".json")[0]
        return payloads[atco]

    return TransportApiBusSource(
        app_id="id",
        app_key="key",
        stops=stops,
        rows=rows,
        directions=directions,
        base_url="https://example.test/v3",
        http_get=fake_get,
    )


def test_source_builds_live_request():
    calls = []
    deps = source({"A": "1000DGHS0900"}, {"1000DGHS0900": stop(bus())}, calls=calls).fetch()

    [(url, headers)] = calls
    assert url == (
        "https://example.test/v3/uk/bus/stop_timetables/1000DGHS0900.json"
        "?app_id=id&app_key=key&live=true&group=no&limit=2"
    )
    assert headers == {}
    assert len(deps) == 1


def test_cost_is_one_hit_per_stop():
    assert source({"A": "a", "B": "b"}, {}).cost == 2


def test_departures_from_all_stops_are_merged_in_time_order():
    payloads = {
        "a": stop(bus(aimed_departure_time="07:40", best_departure_estimate="07:40")),
        "b": stop(bus(line_name="61", aimed_departure_time="07:25", best_departure_estimate="07:25")),
    }
    deps = source({"A": "a", "B": "b"}, payloads, rows=3).fetch()
    assert [(d.service, d.stop) for d in deps] == [("61", "B"), ("341", "A")]


def test_source_truncates_to_requested_rows_after_filtering():
    payload = stop(
        bus(direction="Hayfield"),
        bus(direction="Hyde", aimed_departure_time="07:30", best_departure_estimate="07:30"),
        bus(direction="Hyde", aimed_departure_time="07:50", best_departure_estimate="07:50"),
        bus(direction="Hyde", aimed_departure_time="08:10", best_departure_estimate="08:10"),
    )
    deps = source({"A": "x"}, {"x": payload}, rows=2, directions=["Hyde"]).fetch()
    assert [d.scheduled for d in deps] == ["07:30", "07:50"]


def test_source_requires_at_least_one_stop():
    with pytest.raises(ValueError):
        source({}, {})
