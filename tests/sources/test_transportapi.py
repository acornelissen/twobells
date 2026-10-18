import io

import pytest

from tests.paths import FIXTURES
from twobells.core import net
from twobells.core.model import Status
from twobells.sources import transportapi
from twobells.sources.transportapi import TransportApiBusSource, parse_stop


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


def test_merged_stops_keep_their_order_across_midnight():
    def at(time, date):
        return bus(aimed_departure_time=time, best_departure_estimate=time, date=date)

    # At 23:50: stop A has the 23:55 and 00:20, stop B the 00:05.
    payloads = {
        "a": stop(at("23:55", "2026-10-09"), at("00:20", "2026-10-10")),
        "b": stop(at("00:05", "2026-10-10")),
    }
    deps = source({"A": "a", "B": "b"}, payloads, rows=2).fetch()
    assert [(d.scheduled, d.stop) for d in deps] == [("23:55", "A"), ("00:05", "B")]


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


def test_fetch_raw_returns_payload_for_one_stop():
    payload = stop(bus())
    assert source({"A": "a"}, {"a": payload}).fetch_raw("a") is payload


def test_parses_a_late_evening_response():
    import json

    fixture = FIXTURES / "tapi_henry_street_a_late_evening.json"
    [dep] = parse_stop(json.loads(fixture.read_text()), stop_label="A")
    assert (dep.service, dep.destination, dep.scheduled) == ("237", "Ashton", "23:41")
    assert dep.status is Status.UNKNOWN  # timetable only (source "tnds"), no live estimate
    assert dep.stop == "A"


def test_a_bus_without_a_booked_time_is_skipped():
    payload = stop(
        bus(aimed_departure_time=None, best_departure_estimate=None),
        bus(aimed_departure_time="07:40", best_departure_estimate="07:40"),
    )
    assert [d.scheduled for d in parse_stop(payload)] == ["07:40"]


@pytest.mark.parametrize("aimed", ["soon", "7:20", "24:00", "07:60", "07:20:00", 720, ["07:20"]])
def test_a_bus_whose_booked_time_is_not_a_clock_time_is_skipped(aimed):
    payload = stop(bus(aimed_departure_time=aimed), bus(aimed_departure_time="07:40"))
    assert [d.scheduled for d in parse_stop(payload)] == ["07:40"]


@pytest.mark.parametrize("expected", ["soon", "7:24", 724, {"time": "07:24"}])
def test_a_live_time_that_is_not_a_clock_time_reads_as_unknown(expected):
    [dep] = parse_stop(stop(bus(expected_departure_time=expected, best_departure_estimate=expected)))
    assert dep.status is Status.UNKNOWN
    assert dep.expected is None


def test_entries_of_the_wrong_shape_are_skipped():
    good = bus(aimed_departure_time="07:40")
    assert [d.scheduled for d in parse_stop(stop("07:20", None, [good], good))] == ["07:40"]
    assert parse_stop({"departures": ["07:20"]}) == []
    assert parse_stop({"departures": {"all": "07:20"}}) == []
    assert parse_stop({"departures": "07:20"}) == []
    assert parse_stop(["07:20"]) == []


def test_odd_text_fields_are_left_blank_not_raised():
    entry = bus(line_name={"x": 1}, line=None, direction=["Hyde"], date=20261009)
    [dep] = parse_stop(stop(entry))
    assert (dep.service, dep.destination) == ("", "")


# Fetching: capped, and given up after a deadline, so a runaway or trickled
# answer can't use up a Pi Zero's memory or hang `run --loop`.


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def serve(monkeypatch, body):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return Response(body)

    monkeypatch.setattr(net, "urlopen", fake_urlopen)
    return seen


def test_an_oversized_answer_is_refused_and_the_request_has_a_deadline(monkeypatch):
    seen = serve(monkeypatch, b" " * (transportapi.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="larger than"):
        TransportApiBusSource("id", "key", {"A": "1000DGHS0900"}).fetch()
    assert seen[0].deadline is not None


def test_deeply_nested_json_is_a_bad_answer_like_any_other(monkeypatch):
    serve(monkeypatch, b"[" * 100_000 + b"]" * 100_000)
    with pytest.raises(ValueError, match="not valid JSON"):
        TransportApiBusSource("id", "key", {"A": "1000DGHS0900"}).fetch()


def test_a_real_sized_answer_is_read_through_the_capped_request(monkeypatch):
    serve(monkeypatch, (FIXTURES / "tapi_henry_street_a_late_evening.json").read_bytes())
    [dep] = TransportApiBusSource("id", "key", {"A": "1000DGHS0900"}).fetch()
    assert (dep.service, dep.scheduled) == ("237", "23:41")
