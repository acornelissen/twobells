"""Static checks on the dashboard's browser files and its contract fixtures.

The page runs under `script-src 'self'; style-src 'self'`, sets feed text only
as text, and ships a vendored Leaflet pinned by hash. These tests keep it that
way without a browser.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "departureboard" / "dashboard_static"
VENDOR = STATIC / "vendor"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- Vendored Leaflet ---


def test_vendor_hashes_match():
    leaflet = VENDOR / "leaflet-1.9.4"
    listed = {}
    for line in (leaflet / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name.lstrip("*")] = digest
    assert set(listed) == {"LICENSE", "leaflet-src.esm.js", "leaflet.css"}
    for name, digest in listed.items():
        assert hashlib.sha256((leaflet / name).read_bytes()).hexdigest() == digest, name
    shipped = {p.name for p in leaflet.iterdir()}
    assert shipped == set(listed) | {"SHA256SUMS"}, "a vendored file is not pinned"


# --- The contract fixtures (docs/dashboard-api.md), for both sides to share ---


class Opt:
    """A value that may also be null."""

    def __init__(self, spec):
        self.spec = spec


class MapOf:
    """An object with free-form keys, each value matching spec."""

    def __init__(self, spec):
        self.spec = spec


ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?[+-]\d\d:\d\d$")
HHMM = re.compile(r"^\d\d:\d\d$")
ID = re.compile(r"^[0-9a-f]{16}$")
DAY = re.compile(r"^\d{4}-\d\d-\d\d$")


def at(value):
    return isinstance(value, str) and bool(ISO.match(value))


def hhmm(value):
    return isinstance(value, str) and bool(HHMM.match(value))


def row_id(value):
    return isinstance(value, str) and bool(ID.match(value))


def one_of(*choices):
    return lambda value: value in choices


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check(value, spec, path="$"):
    """Asserts value matches spec: a dict of keys, a one-item list, Opt, MapOf,
    a type, or a predicate."""
    if isinstance(spec, Opt):
        if value is not None:
            check(value, spec.spec, path)
    elif isinstance(spec, MapOf):
        assert isinstance(value, dict), f"{path} is not an object"
        for key, item in value.items():
            check(item, spec.spec, f"{path}.{key}")
    elif isinstance(spec, dict):
        assert isinstance(value, dict), f"{path} is not an object"
        assert set(value) == set(spec), f"{path} keys differ: {sorted(set(value) ^ set(spec))}"
        for key, item_spec in spec.items():
            check(value[key], item_spec, f"{path}.{key}")
    elif isinstance(spec, list):
        assert isinstance(value, list), f"{path} is not a list"
        for i, item in enumerate(value):
            check(item, spec[0], f"{path}[{i}]")
    elif isinstance(spec, type):
        assert isinstance(value, spec) and not (spec is int and isinstance(value, bool)), f"{path} is not {spec.__name__}"
    else:
        assert spec(value), f"{path} = {value!r} is not valid"


STATUS = {"last_success": Opt(at), "stale": bool, "error": Opt(str), "note": Opt(str)}
ROW_STATUS = one_of("on_time", "late", "cancelled", "unknown")
HISTORY = {"phrase": Opt(str)}

TRAIN_ROW = {
    "id": row_id, "service": str, "destination": str, "origin": Opt(str),
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "minutes_late": Opt(integer),
    "platform": Opt(str), "length": Opt(integer),
    "cancel_reason": Opt(str), "delay_reason": Opt(str), "leave_at": Opt(at),
    "calling_points": [{
        "name": str, "crs": Opt(str), "scheduled": Opt(hhmm), "expected": Opt(hhmm),
        "actual": Opt(hhmm), "cancelled": bool,
    }],
    "history": HISTORY,
}
BUS_ROW = {
    "id": row_id, "line": str, "destination": str, "stop": str,
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "minutes_late": Opt(integer),
    "live": bool, "leave_at": Opt(at),
    "vehicle": Opt({"age_s": integer, "recorded_at": at, "bearing": Opt(number)}),
    "strip": Opt({"stops": [str], "yours": integer, "bus_at": Opt(number), "where": str}),
    "history": HISTORY,
}
WEATHER = {
    "status": STATUS,
    "rain": Opt(str),
    "now": Opt({
        "temperature": number, "feels_like": Opt(number), "code": Opt(integer), "text": str,
        "wind": Opt(number), "gust": Opt(number), "direction": Opt(number), "is_day": bool,
    }),
    "hours": [{
        "start": at, "temperature": number, "probability": Opt(number), "precipitation": Opt(number),
        "code": Opt(integer), "text": str, "wind": Opt(number), "is_day": bool,
    }],
    "sun": Opt({"sunrise": at, "sunset": at}),
}
SNAPSHOT = {
    "version": integer,
    "generated_at": at,
    "title": str,
    "walk": {"train": Opt(integer), "bus": MapOf(integer)},
    "trains": Opt({
        "station": str, "filter": Opt(str), "status": STATUS, "messages": [str], "rows": [TRAIN_ROW],
    }),
    "buses": Opt({
        "status": STATUS,
        "timetable": {
            "state": one_of("ready", "building", "missing"), "built": Opt(at),
            "valid_until": Opt(lambda v: isinstance(v, str) and bool(DAY.match(v))), "journeys": Opt(integer),
        },
        "stops": MapOf(str),
        "rows": [BUS_ROW],
    }),
    "weather": Opt(WEATHER),
}
BUS_DETAIL = {
    "id": row_id, "line": str, "destination": str, "stop": str,
    "stops": [{"name": str, "lat": number, "lon": number, "time": Opt(hhmm), "yours": bool}],
    "vehicle": Opt({"lat": number, "lon": number, "bearing": Opt(number), "recorded_at": at, "age_s": integer}),
    "where": Opt(str),
}
HISTORY_DETAIL = {
    "id": row_id, "mode": one_of("train", "bus"), "label": str,
    "day_type": one_of("weekday", "saturday", "sunday"), "phrase": Opt(str),
    "days": [{
        "day": lambda v: isinstance(v, str) and bool(DAY.match(v)), "status": ROW_STATUS,
        "delay_min": Opt(integer), "live": bool, "reason": Opt(str),
    }],
}


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_snapshot_fixture_follows_the_contract():
    snapshot = _fixture("dashboard_snapshot.json")
    check(snapshot, SNAPSHOT)
    trains, buses = snapshot["trains"]["rows"], snapshot["buses"]["rows"]
    assert len(trains) == 10 and len(buses) == 10
    # The cases the page has to draw are all here.
    assert any(r["status"] == "cancelled" and r["cancel_reason"] and r["leave_at"] is None for r in trains)
    assert any(r["status"] == "late" and r["delay_reason"] for r in trains)
    assert snapshot["trains"]["messages"]
    assert any(r["live"] and r["strip"] and r["strip"]["bus_at"] is not None for r in buses)
    assert any(not r["live"] and r["vehicle"] is None and r["status_text"] == "Timetable only" for r in buses)
    assert len({r["id"] for r in trains + buses}) == 20
    for row in trains + buses:
        if row["minutes_late"] is not None:
            assert row["status"] == "late"
    for row in buses:
        if row["strip"]:
            assert 0 <= row["strip"]["yours"] < len(row["strip"]["stops"])


def test_bus_fixture_follows_the_contract():
    bus = _fixture("dashboard_bus.json")
    check(bus, BUS_DETAIL)
    assert sum(stop["yours"] for stop in bus["stops"]) == 1
    assert bus["id"] in {r["id"] for r in _fixture("dashboard_snapshot.json")["buses"]["rows"]}


def test_history_fixture_follows_the_contract():
    history = _fixture("dashboard_history.json")
    check(history, HISTORY_DETAIL)
    days = [d["day"] for d in history["days"]]
    assert days == sorted(days, reverse=True) and len(days) <= 90


def test_shape_checker_rejects_wrong_shapes():
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["trains"]["rows"][0]["id"] = "../../etc"
    with pytest.raises(AssertionError, match="id"):
        check(snapshot, SNAPSHOT)
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["buses"]["rows"][0]["extra"] = 1
    with pytest.raises(AssertionError, match="keys differ"):
        check(snapshot, SNAPSHOT)
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["version"] = True
    with pytest.raises(AssertionError, match="version"):
        check(snapshot, SNAPSHOT)
