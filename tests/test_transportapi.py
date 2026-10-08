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


def test_source_builds_live_request():
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return stop(bus())

    source = TransportApiBusSource(
        app_id="id",
        app_key="key",
        atco="1600GLX0001",
        rows=2,
        base_url="https://example.test/v3",
        http_get=fake_get,
    )
    deps = source.fetch()

    [(url, headers)] = calls
    assert url == (
        "https://example.test/v3/uk/bus/stop_timetables/1600GLX0001.json"
        "?app_id=id&app_key=key&live=true&group=no&limit=2"
    )
    assert headers == {}
    assert len(deps) == 1


def test_source_truncates_to_requested_rows_after_filtering():
    payload = stop(
        bus(direction="Hayfield"),
        bus(direction="Hyde", aimed_departure_time="07:30", best_departure_estimate="07:30"),
        bus(direction="Hyde", aimed_departure_time="07:50", best_departure_estimate="07:50"),
        bus(direction="Hyde", aimed_departure_time="08:10", best_departure_estimate="08:10"),
    )
    source = TransportApiBusSource(
        app_id="id",
        app_key="key",
        atco="x",
        rows=2,
        directions=["Hyde"],
        http_get=lambda url, headers: payload,
    )
    assert [d.scheduled for d in source.fetch()] == ["07:30", "07:50"]
