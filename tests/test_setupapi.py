"""The setup wizard's JSON handlers, with setup's lookups and the search job faked."""

import json
import tomllib

import pytest

from departureboard import discover
from departureboard.config import read_config
from departureboard.setup import Place, SetupError, Station, Stop
from departureboard.setupapi import Invalid, SetupApi, decode, suggested_labels

# Home, to more places than anything written may show.
HOME = {"lat": 53.461964, "lon": -1.965633}
HOME_PLACE = Place(HOME["lat"], HOME["lon"], "SK13 1AH", "Scotland", "High Peak")
BODS_KEY = "bods-secret-key-123"
RDM_KEY = "rdm-secret-key-456"
STATIONS = (("Glossop", "GLO", 53.4446, -1.9656), ("Manchester Piccadilly", "MAN", 53.4774, -2.2309))
STOPS = [
    Stop("1800SB12341", "Norfolk Street", "Stop A", "N", 53.4440, -1.9500, 60),
    Stop("1800SB12342", "Norfolk Street", "Stop B", "S", 53.4442, -1.9502, 75),
    Stop("1800SB12343", "High Street West", "opp", "", 53.4450, -1.9530, 140),
]
CONFIG = """
state_file = "{state}"
display = "hdmi-1280x400"
"""
SAVED = """
theme = "plain"
rotate = 180
text_size = 13

[bus]
rows = 4

[dashboard]
title = "Our house"
"""
RESULT = {
    "fragment": {"bus": {"source": "bods", "operators": ["BNML"], "routes": {"341": {"stop": "A", "stops": [
        {"name": "Hadfield", "lat": 53.46, "lon": -1.965}, {"name": "Norfolk Street", "lat": 53.4452, "lon": -1.9512},
    ]}}}},
    "lines": {"BNML": ["341"]},
    "document_operators": {},
    "stop_lines": {"A": ["341"]},
    "datasets_scanned": 3,
    "journeys_found": 40,
    "timetable_journeys": 40,
    "notes": [],
}


class FakeJob:
    made: list["FakeJob"] = []
    busy = False

    def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None):
        self.folder, self.stops, self.api_key, self.routes, self.walk = folder, stops, api_key, routes, walk_minutes
        self.state = {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}
        FakeJob.made.append(self)

    def start(self):
        if FakeJob.busy:
            raise discover.SetupBusy("A setup search is already running for this board")
        self.state.update(state="running", step=discover.STEP_DISCOVER, total=5)

    def status(self):
        return json.loads(json.dumps(self.state))

    def cancel(self):
        if self.state["state"] in ("queued", "running"):
            self.state.update(state="failed", message="Cancelled")

    def finish(self, routes=True):
        result = json.loads(json.dumps(RESULT))
        if not routes:
            del result["fragment"]["bus"]["routes"]
        self.state.update(state="done", step=discover.STEP_TIMETABLE, done=1, total=1, message="Found 1", result=result)


@pytest.fixture
def board(tmp_path):
    FakeJob.made, FakeJob.busy = [], False
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    (state / "settings.toml").write_text(SAVED)
    secrets = {"RDM_API_KEY": RDM_KEY, "BODS_API_KEY": BODS_KEY}
    calls = []

    def walk(home, places):
        calls.append(("walk", home, dict(places)))
        return {key: (5 if key == "GLO" else 2, "route" if key == "GLO" else "estimate") for key in places}

    api = SetupApi(
        config, lambda: dict(secrets), state,
        links=[("The board", "/"), ("Dashboard", "http://pi.local:8090/")],
        lookup_postcode=lambda postcode: calls.append(("postcode", postcode)) or HOME_PLACE,
        search_address=lambda q: calls.append(("address", q)) or [HOME_PLACE],
        nearby_stations=lambda lat, lon, **kw: [Station(n, c, a, o, 100) for n, c, a, o in STATIONS],
        nearby_stops=lambda lat, lon, **kw: list(STOPS),
        walk_minutes=walk,
        stations=lambda: STATIONS,
        make_job=FakeJob,
    )
    api.calls, api.secrets, api.state, api.config = calls, secrets, state, config
    return api


def ok(api, name, payload=None):
    status, body = api.handle(name, {} if payload is None else payload)
    assert status == 200, body
    return body


def saved_with_a_search(api, routes=True, **finish):
    ok(api, "stops", HOME)
    ok(api, "start", {"stops": {"A": "1800SB12341"}, "routes": routes})
    FakeJob.made[-1].finish(routes)
    payload = {"home": {**HOME, "country": "Scotland"}, "station_crs": "GLO", "filter_crs": "MAN",
               "stops": {"A": "1800SB12341"}, "walk": {"GLO": 6, "1800SB12341": 3}, **finish}
    return api.handle("finish", payload)


# Strict input.


@pytest.mark.parametrize("name, payload, field", [
    ("postcode", {"postcode": 12345}, "postcode"),
    ("postcode", {"postcode": "SK13 1AH", "extra": 1}, "extra"),
    ("postcode", {}, "postcode"),
    ("postcode", {"postcode": "x" * 17}, "postcode"),
    ("address", {"q": "ab"}, "q"),
    ("address", {"q": "a\x00bcd"}, "q"),
    ("stations", {"lat": 48.85, "lon": 2.35}, "home"),  # Paris
    ("stations", {"lat": True, "lon": -1.9}, "home"),
    ("stations", {"lat": "53.4", "lon": -1.9}, "home"),
    ("stops", {"lat": 53.4}, "lon"),
    ("station", {"crs": "GL0"}, "crs"),
    ("station", {"crs": "XYZ"}, "crs"),
    ("walk", {"home": HOME, "stop_atcos": ["../etc"]}, "stop_atcos"),
    ("walk", {"home": HOME, "stop_atcos": ["1800SB12341"] * 11}, "stop_atcos"),
    ("start", {"stops": {}, "routes": False}, "stops"),
    ("start", {"stops": {"A": "1800SB12341"}, "routes": "yes"}, "routes"),
    ("start", {"stops": {"ABCD": "1800SB12341"}, "routes": False}, "stops"),
    ("start", {"stops": {"A": "1800SB12341", "a": "1800SB12342"}, "routes": False}, "stops"),
    ("start", {"stops": {"A": "1800SB12341", "B": "1800SB12341"}, "routes": False}, "stops"),
    ("start", {"stops": {"A": "1800SB12341"}, "routes": False, "walk": {"Z": 3}}, "walk"),
    ("start", {"stops": {"A": "1800SB12341"}, "routes": False, "keys": {"PATH": "x"}}, "keys"),
    ("finish", {"home": HOME, "stops": {}, "walk": {}}, "stops"),
    ("finish", {"home": {**HOME, "country": "France"}, "station_crs": "GLO", "stops": {}, "walk": {}}, "home"),
    ("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {"GLO": 61}}, "walk"),
    ("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {"MAN": 5}}, "walk"),
    ("finish", {"home": HOME, "filter_crs": "MAN", "stops": {"A": "1800SB12341"}, "walk": {}}, "filter_crs"),
])
def test_input_is_checked_strictly(board, name, payload, field):
    status, body = board.handle(name, payload)
    assert status == 400 and body["field"] == field, body
    assert not board.calls and not FakeJob.made


def test_a_request_must_be_an_object(board):
    assert board.handle("job", [])[0] == 400
    assert board.handle("nope", {})[0] == 404


def test_decode_refuses_big_odd_or_deep_bodies():
    assert decode(b'{"q": "Glossop"}') == {"q": "Glossop"}
    for body in (b"x" * 20_000, b"{", b'{"lat": NaN}', b"\xff", b'{"a":' * 10 + b"1" + b"}" * 10):
        with pytest.raises(Invalid):
            decode(body)


# Finding things.


def test_finding_home_by_postcode_or_address(board):
    assert ok(board, "postcode", {"postcode": " SK13 1AH "})["place"]["label"] == "SK13 1AH"
    assert board.calls[-1] == ("postcode", "SK13 1AH")
    places = ok(board, "address", {"q": "1 High Street, Glossop"})["places"]
    assert places[0]["country"] == "Scotland" and places[0]["lat"] == HOME["lat"]


def test_a_lookup_that_fails_says_so(board):
    board._lookup_postcode = lambda postcode: (_ for _ in ()).throw(SetupError("postcodes.io doesn't know that"))
    assert board.handle("postcode", {"postcode": "SK13 1AH"}) == (422, {"error": "postcodes.io doesn't know that"})


def test_a_fault_is_said_plainly(board):
    board._search_address = lambda q: 1 / 0
    assert board.handle("address", {"q": "Glossop"}) == (500, {"error": "Something went wrong; try again"})


def test_stations_and_a_typed_code(board):
    assert [s["crs"] for s in ok(board, "stations", HOME)["stations"]] == ["GLO", "MAN"]
    assert ok(board, "station", {"crs": "man"})["station"]["name"] == "Manchester Piccadilly"


def test_stops_come_with_labels_from_their_signs_when_they_can():
    assert suggested_labels(STOPS[:2]) == ["A", "B"]
    assert suggested_labels(STOPS) == ["A", "B", "C"]  # "opp" has no letter: A, B, C in order
    assert suggested_labels([STOPS[1], STOPS[0]]) == ["B", "A"]


def test_walk_times_for_the_station_and_found_stops(board):
    assert [s["label"] for s in ok(board, "stops", HOME)["stops"]] == ["A", "B", "C"]
    walk = ok(board, "walk", {"home": HOME, "station_crs": "GLO", "stop_atcos": ["1800SB12341", "1800SB99999"]})
    assert walk["walk"] == {"GLO": {"minutes": 5, "source": "route"},
                            "1800SB12341": {"minutes": 2, "source": "estimate"}}
    _, home, places = board.calls[-1]
    assert (home.lat, home.lon) == (HOME["lat"], HOME["lon"]) and set(places) == {"GLO", "1800SB12341"}


# The search.


def test_the_job_runs_one_at_a_time_and_reports_what_it_found(board):
    assert ok(board, "job")["job"] == {"state": "idle"}
    started = ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": True, "walk": {"A": 3}})["job"]
    assert started["state"] == "running" and started["stops"] == {"A": "1800SB12341"}
    job = FakeJob.made[-1]
    assert (job.stops, job.api_key, job.routes, job.walk, job.folder) == (
        {"A": "1800SB12341"}, BODS_KEY, True, {"A": 3}, board.state)
    status, body = board.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert status == 409 and "already running" in body["error"]
    job.finish()
    found = ok(board, "job")["job"]["found"]
    assert found["operators"] == ["BNML"] and found["routes"] == ["341"] and found["stop_lines"] == {"A": ["341"]}
    ok(board, "start", {"stops": {"B": "1800SB12342"}, "routes": False})  # done: a new search may start
    assert len(FakeJob.made) == 2


def test_cancelling_the_job(board):
    ok(board, "cancel")  # nothing running: nothing to do
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert ok(board, "cancel")["job"]["state"] == "failed"
    assert ok(board, "job")["job"]["message"] == "Cancelled"


def test_another_search_on_the_same_folder_is_a_conflict(board):
    FakeJob.busy = True
    status, body = board.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert status == 409 and ok(board, "job")["job"] == {"state": "idle"}


def test_the_search_needs_a_bus_key_pasted_or_saved(board):
    del board.secrets["BODS_API_KEY"]
    status, body = board.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert status == 400 and body["field"] == "BODS_API_KEY"
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False, "keys": {"BODS_API_KEY": "pasted-key-789"}})
    assert FakeJob.made[-1].api_key == "pasted-key-789"


# Saving.


def test_finishing_writes_what_setup_found_into_the_overlay(board):
    status, body = saved_with_a_search(board)
    assert status == 200 and body["saved"] and body["links"][0] == {"label": "The board", "href": "/"}
    saved = tomllib.loads((board.state / "settings.toml").read_text())
    assert saved["train"] == {"crs": "GLO", "walk_minutes": 6, "filter_crs": "MAN"}
    assert saved["bus"]["stops"] == {"A": "1800SB12341"} and saved["bus"]["walk_minutes"] == {"A": 3}
    assert saved["bus"]["source"] == "bods" and saved["bus"]["operators"] == ["BNML"]
    assert saved["bus"]["routes"]["341"]["stop"] == "A"
    assert saved["weather"] == {"lat": 53.44, "lon": -1.95}
    assert saved["bank_holiday_division"] == "scotland"
    assert read_config(board.config)["bus"]["routes"]["341"]["stops"][0]["name"] == "Hadfield"


def test_finishing_keeps_the_settings_setup_does_not_manage(board):
    saved_with_a_search(board)
    saved = tomllib.loads((board.state / "settings.toml").read_text())
    assert (saved["theme"], saved["rotate"], saved["text_size"]) == ("plain", 180, 13)
    assert saved["dashboard"] == {"title": "Our house"} and saved["bus"]["rows"] == 4


def test_no_exact_home_position_is_written_anywhere(board):
    saved_with_a_search(board, keys={"BODS_API_KEY": "new-bods-key-000"})
    written = "".join(p.read_text(errors="replace") for p in board.state.rglob("*") if p.is_file())
    for exact in ("53.461964", "-1.965633", "53.4620", "-1.9656", "53.444", "-1.949"):
        assert exact not in written
    assert "SK13" not in written
    assert ok(board, "walk", {"home": HOME, "stop_atcos": ["1800SB12341"]})["walk"] == {}  # found stops forgotten


def test_trains_only_needs_no_search(board):
    (board.state / "settings.toml").write_text('theme = "plain"\n')
    status, body = board.handle("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}})
    assert status == 200, body
    saved = tomllib.loads((board.state / "settings.toml").read_text())
    assert saved["train"] == {"crs": "GLO", "walk_minutes": "", "filter_crs": ""} and "bus" not in saved
    assert "bank_holiday_division" not in saved  # a point on the map has no country


def test_finishing_stops_needs_their_search(board):
    status, body = board.handle("finish", {"home": HOME, "stops": {"A": "1800SB12341"}, "walk": {}})
    assert status == 409 and "Find the bus companies" in body["error"]
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    FakeJob.made[-1].finish(False)
    status, _ = board.handle("finish", {"home": HOME, "stops": {"B": "1800SB12341"}, "walk": {}})
    assert status == 409  # labelled differently from the search: its routes and lines don't match


def test_without_routes_old_strips_for_gone_stops_are_cleared(board):
    (board.state / "settings.toml").write_text(SAVED.replace("[bus]\n", '[bus.routes.6]\nstop = "Z"\nstops = []\n\n[bus]\n'))
    status, body = saved_with_a_search(board, routes=False)
    assert status == 200, body
    assert tomllib.loads((board.state / "settings.toml").read_text())["bus"]["routes"] == {}


def test_settings_that_would_not_load_are_not_saved(board):
    del board.secrets["RDM_API_KEY"]
    before = (board.state / "settings.toml").read_text()
    status, body = saved_with_a_search(board)
    assert status == 422 and "RDM_API_KEY" in body["error"]
    assert (board.state / "settings.toml").read_text() == before


def test_errors_never_carry_keys(board):
    leaky = f"upstream said key {BODS_KEY} and {RDM_KEY} and {'x' * 150}{BODS_KEY}"
    board._lookup_postcode = lambda postcode: (_ for _ in ()).throw(SetupError(leaky))
    status, body = board.handle("postcode", {"postcode": "SK13 1AH"})
    assert status == 422
    text = json.dumps(body)
    assert BODS_KEY not in text and RDM_KEY not in text and BODS_KEY[:8] not in text
    # A pasted key that is malformed, or that is fine while the settings fail.
    status, body = board.handle("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {},
                                           "keys": {"RDM_API_KEY": "has spaces 12345"}})
    assert status == 400 and "has spaces" not in json.dumps(body) and body["field"] == "RDM_API_KEY"
    del board.secrets["RDM_API_KEY"]
    board.secrets["BODS_API_KEY"] = "zz"
    status, body = saved_with_a_search(board, keys={"BODS_API_KEY": "pasted-bods-key-1"})
    assert status == 422 and "pasted-bods-key-1" not in json.dumps(body)
    # The job's own messages too, with the key pasted for it but never saved.
    ok(board, "start", {"stops": {"B": "1800SB12342"}, "routes": False, "keys": {"BODS_API_KEY": "only-pasted-key"}})
    FakeJob.made[-1].state.update(state="failed", message="BODS refused only-pasted-key")
    assert "only-pasted-key" not in json.dumps(board.handle("job", {}))


def test_begin_says_which_keys_are_set_never_their_values(board):
    body = ok(board, "begin")
    keys = {k["name"]: k for k in body["keys"]}
    assert keys["RDM_API_KEY"]["set"] and not keys["TAPI_APP_ID"]["set"]
    assert keys["BODS_API_KEY"]["doc"].endswith("docs/KEYS.md#bus-open-data-service")
    assert RDM_KEY not in json.dumps(body) and BODS_KEY not in json.dumps(body)
    assert body["job"] == {"state": "idle"}


def test_links_must_be_plain(board):
    for href in ("javascript:alert(1)", "//evil.example/", "/a b"):
        with pytest.raises(ValueError):
            SetupApi(board.config, dict, board.state, links=[("Board", href)])
