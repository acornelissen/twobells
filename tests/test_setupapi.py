"""The setup wizard's JSON handlers, with setup's lookups and the search job faked."""

import json
import tomllib
from pathlib import Path
from typing import ClassVar

import pytest

from departureboard.settings.config import load_config, needs_setup, read_base, read_config
from departureboard.settings.installs import INSTALLS, Install
from departureboard.settings.overlay import Target, board_target
from departureboard.settings.schema import keys as schema_keys
from departureboard.settings.store import Store
from departureboard.setup import api as setupapi
from departureboard.setup import discover
from departureboard.setup.api import Invalid, RateLimit, SetupApi, decode, suggested_labels
from departureboard.setup.keytest import KeyResult
from departureboard.setup.lookups import Place, SetupError, Station, Stop
from departureboard.web.groupform import check_views

# Home, near Hadfield station, to more places than anything written may show.
HOME = {"lat": 53.461964, "lon": -1.965633}
HOME_PLACE = Place(HOME["lat"], HOME["lon"], "SK13 1AH", "Scotland", "High Peak")
BODS_KEY = "bods-secret-key-123"
RDM_KEY = "rdm-secret-key-456"
STATIONS = (("Glossop", "GLO", 53.4445, -1.9491), ("Manchester Piccadilly", "MAN", 53.4774, -2.2309))
STOPS = [
    Stop("1800SB12341", "Station Road", "Stop A", "N", 53.4615, -1.9662, 64),
    Stop("1800SB12342", "Station Road", "Stop B", "S", 53.4614, -1.9665, 85),
    Stop("1800SB12343", "Woolley Bridge", "opp", "", 53.4625, -1.9690, 231),
]
CONFIG = """
state_file = "{state}"
display = "hdmi-1280x400"
"""
SAVED = """
theme = "plain"
rotate = 180
text_size = 13
title = "Our house"

[bus]
rows = 4
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


# Setup offers the general keys; a host may offer more. These tests offer the
# board's too, so TransportAPI's paired key test can be tried.
WITH_TAPI = schema_keys("general", "board")


class FakeJob:
    made: ClassVar[list["FakeJob"]] = []
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
        if self.state.get("roads", {}).get("state") == "running":
            self.state["roads"]["state"] = "cancelled"

    def finish(self, routes=True, roads=None):
        """Done; with `roads` (done, total), still finding the roads for the maps."""
        result = json.loads(json.dumps(RESULT))
        if not routes:
            del result["fragment"]["bus"]["routes"]
        self.state.update(state="done", step=discover.STEP_TIMETABLE, done=1, total=1, message="Found 1", result=result)
        if roads is not None:
            self.state["roads"] = {"state": "running", "done": roads[0], "total": roads[1]}


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

    def test_key(name, value, config_data, env):
        calls.append(("testkey", name, value, config_data, dict(env)))
        return KeyResult("works", "It works.")

    clock = [1000.0]

    api = SetupApi(
        board_target(config), lambda: dict(secrets), state,
        links=[("The board", "/"), ("Dashboard", "http://pi.local:8090/")],
        lookup_postcode=lambda postcode: calls.append(("postcode", postcode)) or HOME_PLACE,
        search_address=lambda q: calls.append(("address", q)) or [HOME_PLACE],
        nearby_stations=lambda lat, lon, **kw: [Station(n, c, a, o, 100) for n, c, a, o in STATIONS],
        nearby_stops=lambda lat, lon, **kw: list(STOPS),
        walk_minutes=walk,
        stations=lambda: STATIONS,
        make_job=FakeJob,
        test_key=test_key,
        key_tests=RateLimit(10.0, clock=lambda: clock[0]),
        keys=WITH_TAPI,
    )
    api.calls, api.secrets, api.state, api.config, api.clock = calls, secrets, state, config, clock
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
    ("testkey", {}, "name"),
    ("testkey", {"name": "PATH"}, "name"),
    ("testkey", {"name": ["RDM_API_KEY"]}, "name"),
    ("testkey", {"name": "RDM_API_KEY", "value": 12345}, "RDM_API_KEY"),
    ("testkey", {"name": "RDM_API_KEY", "value": "has spaces 12345"}, "RDM_API_KEY"),
    ("testkey", {"name": "RDM_API_KEY", "value": "x" * 201}, "RDM_API_KEY"),
    ("testkey", {"name": "RDM_API_KEY", "app_id": "an-app-id-1"}, "app_id"),
    ("testkey", {"name": "TAPI_APP_KEY", "app_id": "bad\nid"}, "TAPI_APP_ID"),
    ("testkey", {"name": "RDM_API_KEY", "keys": {}}, "keys"),
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
    # Nested 3000 deep fits in the size cap and parses, but is deeper than
    # Python can recurse: refused all the same, not a crash.
    for body in (b"x" * 20_000, b"{", b'{"lat": NaN}', b"\xff", b'{"a":' * 10 + b"1" + b"}" * 10,
                 b"[" * 3000 + b"]" * 3000):
        with pytest.raises(Invalid):
            decode(body)


# Finding things.


def test_finding_home_by_postcode_or_address(board):
    assert ok(board, "postcode", {"postcode": " sk13 1ah "})["place"]["label"] == "SK13 1AH"
    assert board.calls[-1] == ("postcode", "sk13 1ah")
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


def test_the_roads_found_after_the_search_are_reported_and_saving_need_not_wait(board):
    ok(board, "stops", HOME)
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": True})
    job = FakeJob.made[-1]
    job.finish(roads=(12, 140))
    status = ok(board, "job")["job"]
    assert status["state"] == "done" and status["found"]["operators"] == ["BNML"]
    assert status["roads"] == {"state": "running", "done": 12, "total": 140}
    saved = ok(board, "finish", {"home": HOME, "stops": {"A": "1800SB12341"}, "walk": {}})
    assert saved["saved"] and job.state["roads"]["state"] == "running"  # they finish on their own
    job.state["roads"].update(state="done", done=140)
    assert ok(board, "job")["job"]["roads"] == {"state": "done", "done": 140, "total": 140}


def test_a_job_s_roads_report_only_what_fits(board):
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    job = FakeJob.made[-1]
    job.finish(routes=False)
    assert "roads" not in ok(board, "job")["job"]  # from before the roads were reported
    job.state["roads"] = {"state": "lost", "done": "12", "total": None, "extra": "<b>"}
    assert ok(board, "job")["job"]["roads"] == {"state": "done", "done": 0, "total": 0}


def test_a_new_search_stops_the_last_one_s_roads(board):
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    first = FakeJob.made[-1]
    first.finish(routes=False, roads=(3, 10))
    started = ok(board, "start", {"stops": {"B": "1800SB12342"}, "routes": False})["job"]
    assert started["state"] == "running" and started["stops"] == {"B": "1800SB12342"}
    assert first.state["roads"]["state"] == "cancelled"  # its timetable is about to be replaced


def test_cancelling_the_job(board):
    ok(board, "cancel")  # nothing running: nothing to do
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert ok(board, "cancel")["job"]["state"] == "failed"
    assert ok(board, "job")["job"]["message"] == "Cancelled"


def test_another_search_on_the_same_folder_is_a_conflict(board):
    FakeJob.busy = True
    status, body = board.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert status == 409 and ok(board, "job")["job"] == {"state": "idle"}
    assert body["error"] == "A setup search is already running for this board"


def test_the_dashboard_says_dashboard_when_a_search_is_already_running(dashboard):
    FakeJob.busy = True
    status, body = dashboard.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})
    assert status == 409 and body["error"] == "A setup search is already running for this dashboard"


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
    assert saved["train"] == {"crs": "GLO", "walk_minutes": 6, "filter_crs": "MAN", "enabled": True}
    assert saved["bus"]["stops"] == {"A": "1800SB12341"} and saved["bus"]["walk_minutes"] == {"A": 3}
    assert saved["bus"]["source"] == "bods" and saved["bus"]["operators"] == ["BNML"]
    assert saved["bus"]["routes"]["341"]["stop"] == "A"
    assert saved["weather"] == {"lat": 53.46, "lon": -1.97}
    assert saved["bank_holiday_division"] == "scotland"
    assert read_config(board.config)["bus"]["routes"]["341"]["stops"][0]["name"] == "Hadfield"


def test_finishing_keeps_the_settings_setup_does_not_manage(board):
    saved_with_a_search(board)
    saved = tomllib.loads((board.state / "settings.toml").read_text())
    assert (saved["theme"], saved["rotate"], saved["text_size"]) == ("plain", 180, 13)
    assert saved["title"] == "Our house" and saved["bus"]["rows"] == 4


def test_no_exact_home_position_is_written_anywhere(board):
    saved_with_a_search(board, keys={"BODS_API_KEY": "new-bods-key-000"})
    written = "".join(p.read_text(errors="replace") for p in board.state.rglob("*") if p.is_file())
    for exact in ("53.461964", "-1.965633", "53.4620", "-1.9656", "53.462", "-1.966"):
        assert exact not in written
    assert "SK13" not in written
    assert ok(board, "walk", {"home": HOME, "stop_atcos": ["1800SB12341"]})["walk"] == {}  # found stops forgotten


def test_a_host_can_save_into_its_own_overlay(board, tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    api = SetupApi(Target(lambda: read_base(board.config), lambda base: own), lambda: dict(board.secrets), own,
                   stations=lambda: STATIONS, make_job=FakeJob,
                   saved_message="Saved. The dashboard restarts to use them.")
    status, body = api.handle("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}})
    assert status == 200 and body["message"] == "Saved. The dashboard restarts to use them."
    assert tomllib.loads((own / "settings.toml").read_text())["train"]["crs"] == "GLO"
    assert tomllib.loads((board.state / "settings.toml").read_text()) == tomllib.loads(SAVED)


def test_trains_only_needs_no_search(board):
    (board.state / "settings.toml").write_text('theme = "plain"\n')
    status, body = board.handle("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}})
    assert status == 200, body
    saved = tomllib.loads((board.state / "settings.toml").read_text())
    assert saved["train"] == {"crs": "GLO", "walk_minutes": "", "filter_crs": "", "enabled": True}
    assert saved["bus"] == {"enabled": False}  # switched off, not removed: an overlay can't remove config.toml's
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


TEMPLATE = Path(__file__).parent.parent / "config.example.toml"


@pytest.fixture
def template(board):
    """The board's fixture on config.example.toml as installed: CHANGE_ME
    placeholders for the station and stops, and nothing saved yet."""
    text = TEMPLATE.read_text().replace("/var/lib/departureboard/state.json", str(board.state / "state.json"))
    board.config.write_text(text)
    (board.state / "settings.toml").unlink()
    return board


def test_the_template_with_only_a_station_saves_with_buses_off(template):
    status, body = template.handle("finish", {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}})
    assert status == 200, body
    saved = tomllib.loads((template.state / "settings.toml").read_text())
    assert saved["train"]["enabled"] is True and saved["bus"] == {"enabled": False}
    assert not needs_setup(template.config)
    sources = load_config(template.config, template.secrets).board.sources
    assert [config.source.name for config in sources] == ["train"]


def test_the_template_with_only_stops_saves_with_trains_off(template):
    ok(template, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    FakeJob.made[-1].finish(False)
    status, body = template.handle("finish", {"home": HOME, "stops": {"A": "1800SB12341"}, "walk": {}})
    assert status == 200, body
    saved = tomllib.loads((template.state / "settings.toml").read_text())
    assert saved["train"] == {"enabled": False} and saved["bus"]["enabled"] is True
    assert not needs_setup(template.config)
    sources = load_config(template.config, template.secrets).board.sources
    assert [config.source.name for config in sources] == ["bus"]


def test_choosing_again_switches_a_section_back_on(template):
    ok(template, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    FakeJob.made[-1].finish(False)
    ok(template, "finish", {"home": HOME, "stops": {"A": "1800SB12341"}, "walk": {}})
    ok(template, "finish", {"home": HOME, "station_crs": "GLO", "stops": {"A": "1800SB12341"}, "walk": {}})
    saved = tomllib.loads((template.state / "settings.toml").read_text())
    assert saved["train"]["enabled"] is True and saved["bus"]["enabled"] is True
    sources = load_config(template.config, template.secrets).board.sources
    assert [config.source.name for config in sources] == ["train", "bus"]


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


def test_begin_names_the_page_and_offers_only_the_general_keys_by_default(board):
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state)
    body = ok(api, "begin")
    assert body["title"] == "Set up your departures" and body["noun"] == "board"
    assert [k["name"] for k in body["keys"]] == ["RDM_API_KEY", "BODS_API_KEY"]
    assert [name for name, _, _ in setupapi.GENERAL_KEYS] == [name for name, _, _ in schema_keys("general")]


# A host with keys of its own, as the dashboard has.

DETAILS_KEY = "details-secret-key-789"
KB_KEY = "kb-secret-key-012"


@pytest.fixture
def dashboard(board):
    """The board's fixture, set up as the dashboard sets its wizard up."""
    board.secrets["RDM_DETAILS_API_KEY"] = DETAILS_KEY
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, stations=lambda: STATIONS,
                   make_job=FakeJob, test_key=board._test_key, key_tests=RateLimit(10.0, clock=lambda: 0.0),
                   title="Set up your dashboard", noun="dashboard", keys=schema_keys("general", "dashboard"))
    api.calls, api.secrets, api.state = board.calls, board.secrets, board.state
    return api


def test_begin_names_the_dashboards_page_and_offers_only_its_keys(dashboard):
    body = ok(dashboard, "begin")
    assert body["title"] == "Set up your dashboard" and body["noun"] == "dashboard"
    keys = {k["name"]: k for k in body["keys"]}
    assert list(keys) == ["RDM_API_KEY", "BODS_API_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"]
    assert keys["RDM_DETAILS_API_KEY"]["set"] and not keys["RDM_KB_API_KEY"]["set"]
    assert keys["RDM_DETAILS_API_KEY"]["doc"].endswith("docs/KEYS.md#rail-data-marketplace-service-details")
    assert keys["RDM_KB_API_KEY"]["doc"].endswith("docs/KEYS.md#rail-data-marketplace-knowledgebase-incidents")
    assert keys["RDM_KB_API_KEY"]["label"] == "Rail Data Marketplace Knowledgebase Incidents key"
    assert DETAILS_KEY not in json.dumps(body)


@pytest.mark.parametrize("name", ["RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"])
def test_the_dashboard_tests_its_own_keys(dashboard, name):
    assert ok(dashboard, "testkey", {"name": name, "value": "pasted-key-345"})["state"] == "works"
    _, tested, value, _, env = dashboard.calls[-1]
    assert (tested, value, env["RDM_DETAILS_API_KEY"]) == (name, "pasted-key-345", DETAILS_KEY)


@pytest.mark.parametrize("payload", [
    {"name": "TAPI_APP_KEY"},
    {"name": "TAPI_APP_ID"},
    {"name": "RDM_API_KEY", "app_id": "an-app-id-1"},
])
def test_the_dashboard_never_tests_transportapi(dashboard, payload):
    status, body = dashboard.handle("testkey", payload)
    assert status == 400 and not [c for c in dashboard.calls if c[0] == "testkey"]


def test_the_pi_does_not_test_keys_it_does_not_use(board):
    status, body = board.handle("testkey", {"name": "RDM_KB_API_KEY"})
    assert status == 400 and body["field"] == "name"


def test_the_dashboard_saves_its_own_keys_and_no_others(dashboard):
    (dashboard.state / "settings.toml").write_text('theme = "plain"\n')
    finish = {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}}
    status, body = dashboard.handle("finish", {**finish, "keys": {"TAPI_APP_KEY": "pasted-tapi-key"}})
    assert status == 400 and body["field"] == "keys"
    assert ok(dashboard, "finish", {**finish, "keys": {"RDM_KB_API_KEY": KB_KEY}})["saved"]
    assert f"RDM_KB_API_KEY={KB_KEY}" in (dashboard.state / "secrets.env").read_text()


def test_the_dashboards_saved_keys_never_leave_in_an_answer(dashboard):
    leaky = f"upstream said {DETAILS_KEY}"
    dashboard._lookup_postcode = lambda postcode: (_ for _ in ()).throw(SetupError(leaky))
    status, body = dashboard.handle("postcode", {"postcode": "SK13 1AH"})
    assert status == 422 and DETAILS_KEY not in json.dumps(body)


@pytest.mark.parametrize("title, keys", [
    ("", schema_keys("general", "board")),
    ("x" * 61, schema_keys("general", "board")),
    ("Set up\nyour board", schema_keys("general", "board")),
    ("Set up", ()),
    ("Set up", (("lower_case", "Key", "Hint."),)),
])
def test_a_host_names_its_page_and_keys_plainly(board, title, keys):
    with pytest.raises(ValueError):
        SetupApi(board_target(board.config), dict, board.state, title=title, keys=keys)


@pytest.mark.parametrize("noun", ["", "Board", "the board", "board\n", "<b>", "x" * 21])
def test_a_host_calls_itself_one_plain_word(board, noun):
    with pytest.raises(ValueError):
        SetupApi(board_target(board.config), dict, board.state, noun=noun)


# Testing a key.


def test_testing_a_saved_key(board):
    assert ok(board, "testkey", {"name": "RDM_API_KEY"}) == {"state": "works", "message": "It works."}
    _, name, value, config_data, env = board.calls[-1]
    assert (name, value) == ("RDM_API_KEY", None)  # None: the saved one
    assert env["RDM_API_KEY"] == RDM_KEY
    assert config_data["title"] == "Our house"  # the settings, with the page's saved over them


def test_testing_a_pasted_key_does_not_save_it(board):
    before = sorted(p.name for p in board.state.iterdir())
    ok(board, "testkey", {"name": "BODS_API_KEY", "value": " pasted-bods-555 "})
    assert board.calls[-1][1:3] == ("BODS_API_KEY", "pasted-bods-555")
    ok(board, "testkey", {"name": "RDM_API_KEY", "value": ""})  # empty: the saved one
    assert board.calls[-1][2] is None
    assert sorted(p.name for p in board.state.iterdir()) == before and "secrets.env" not in before


def test_testing_transportapi_with_a_pasted_app_id(board):
    ok(board, "testkey", {"name": "TAPI_APP_KEY", "value": "pasted-tapi-key", "app_id": "pasted-tapi-id"})
    _, name, value, _, env = board.calls[-1]
    assert (name, value, env["TAPI_APP_ID"]) == ("TAPI_APP_KEY", "pasted-tapi-key", "pasted-tapi-id")


def test_each_key_is_tested_at_most_once_every_ten_seconds(board):
    ok(board, "testkey", {"name": "RDM_API_KEY"})
    status, body = board.handle("testkey", {"name": "RDM_API_KEY", "value": "another-key-1"})
    assert status == 429 and body == {"error": "Wait 10 s before testing this key again"}
    ok(board, "testkey", {"name": "BODS_API_KEY"})  # another key has its own limit
    board.clock[0] += 9.5
    status, body = board.handle("testkey", {"name": "RDM_API_KEY"})
    assert status == 429 and "Wait 1 s" in body["error"]
    board.clock[0] += 0.5
    ok(board, "testkey", {"name": "RDM_API_KEY"})
    assert [c[1] for c in board.calls if c[0] == "testkey"] == ["RDM_API_KEY", "BODS_API_KEY", "RDM_API_KEY"]


def test_transportapis_two_keys_share_one_limit(board):
    ok(board, "testkey", {"name": "TAPI_APP_KEY"})
    assert board.handle("testkey", {"name": "TAPI_APP_ID"})[0] == 429


def test_a_refused_request_does_not_use_up_the_limit(board):
    assert board.handle("testkey", {"name": "RDM_API_KEY", "value": "has spaces"})[0] == 400
    ok(board, "testkey", {"name": "RDM_API_KEY"})


def test_the_limit_is_shared_by_every_host_in_the_process(board):
    first = SetupApi(board_target(board.config), dict, board.state)
    second = SetupApi(board_target(board.config), dict, board.state)
    assert first._key_tests is second._key_tests is setupapi.KEY_TESTS
    now = [0.0]
    limit = RateLimit(10.0, clock=lambda: now[0])
    assert limit.take("A") == 0 and limit.take("A") == 10.0 and limit.take("B") == 0
    now[0] = 10.0
    assert limit.take("A") == 0


def test_a_key_test_never_answers_with_the_key(board):
    board._test_key = lambda name, value, config_data, env: KeyResult(
        "wrong_key", f"refused {value} and {env['RDM_API_KEY']} and {env['TAPI_APP_ID']}")
    status, body = board.handle("testkey", {"name": "TAPI_APP_KEY", "value": "pasted-tapi-key",
                                            "app_id": "pasted-tapi-id"})
    text = json.dumps(body)
    assert status == 200 and body["state"] == "wrong_key"
    for secret in ("pasted-tapi-key", "pasted-tapi-id", RDM_KEY):
        assert secret not in text


# Per install: the page's words, and which views it can turn on.


@pytest.mark.parametrize("name, noun", [("pi", "board"), ("docker", "dashboard"), ("desktop", "dashboard")])
def test_each_install_names_the_page_the_same_and_what_shows_the_departures_its_own_way(board, name, noun):
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, install=INSTALLS[name])
    body = ok(api, "begin")
    assert body["title"] == "Set up your departures" and body["noun"] == noun
    assert [k["name"] for k in body["keys"]] == ["RDM_API_KEY", "BODS_API_KEY"]  # the same on every install
    assert [view["name"] for view in body["views"]["offered"]] == list(INSTALLS[name].offered)


# An install offering both views, as the Pi will once it runs the dashboard too.
BOTH = Install("both", ("board", "dashboard"), ("board",), None, noun="board", fetch="files")
ONE = Install("one", ("dashboard",), ("dashboard",), None, fetch="process")


@pytest.fixture
def both(board):
    """The board's fixture on an install offering both views, its settings
    checked as each view on loads them, as serve checks them."""
    store = Store.open(board.config)
    api = SetupApi(store.target(lambda data, env: check_views(data, env, store.folder, BOTH)),
                   lambda: dict(board.secrets), board.state, install=BOTH, stations=lambda: STATIONS,
                   make_job=FakeJob, links=[("Settings", "/settings")],
                   view_links={"dashboard": ("The dashboard", "/")})
    api.secrets, api.state, api.config = board.secrets, board.state, board.config
    return api


TRAINS_ONLY = {"home": HOME, "station_crs": "GLO", "stops": {}, "walk": {}}


def test_begin_offers_the_installs_views_and_says_which_are_on(both):
    views = ok(both, "begin")["views"]
    assert [view["name"] for view in views["offered"]] == ["board", "dashboard"]
    assert [view["label"] for view in views["offered"]] == ["The board", "The dashboard"]
    assert all(view["hint"] for view in views["offered"])  # what each is, for the page to explain
    assert views["on"] == ["board"]  # none saved yet: the install's default
    (both.state / "settings.toml").write_text('[app]\nviews = ["dashboard"]\n')
    assert ok(both, "begin")["views"]["on"] == ["dashboard"]


def test_begin_says_the_install_s_default_views_when_the_settings_dont_read(both):
    (both.state / "settings.toml").write_text("[app]\nviews = \"board\"\n")
    assert ok(both, "begin")["views"]["on"] == ["board"]


@pytest.mark.parametrize("views, saved", [
    (["board"], ["board"]),
    (["dashboard"], ["dashboard"]),
    (["dashboard", "board"], ["board", "dashboard"]),  # in the order offered
])
def test_finishing_turns_on_the_views_chosen(both, views, saved):
    assert ok(both, "finish", {**TRAINS_ONLY, "views": views})["saved"]
    assert tomllib.loads((both.state / "settings.toml").read_text())["app"] == {"views": saved}


def test_finishing_without_views_leaves_them_as_they_are(both):
    (both.state / "settings.toml").write_text('[app]\nviews = ["dashboard"]\n')
    assert ok(both, "finish", TRAINS_ONLY)["saved"]
    assert tomllib.loads((both.state / "settings.toml").read_text())["app"] == {"views": ["dashboard"]}


@pytest.mark.parametrize("views", [
    [],  # at least one
    ["board", "board"],
    ["board", "board", "dashboard"],
    ["screen"],
    ["Board"],
    [1],
    [["board"]],
    "board",
    {"board": True},
    None,
])
def test_finishing_takes_only_views_offered_each_once(both, views):
    before = (both.state / "settings.toml").read_text()
    status, body = both.handle("finish", {**TRAINS_ONLY, "views": views})
    assert status == 400 and body["field"] == "views"
    assert (both.state / "settings.toml").read_text() == before


def test_a_view_the_install_doesnt_offer_is_refused(board):
    # Docker offers only the dashboard: it has no board to drive.
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, install=INSTALLS["docker"],
                   stations=lambda: STATIONS)
    status, body = api.handle("finish", {**TRAINS_ONLY, "views": ["board"]})
    assert status == 400 and body == {"error": "That isn't one of the views offered here", "field": "views"}
    assert "app" not in tomllib.loads((board.state / "settings.toml").read_text())


def test_a_key_outside_the_general_group_is_refused(board):
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, stations=lambda: STATIONS)
    for name in ("TAPI_APP_ID", "TAPI_APP_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"):
        status, body = api.handle("finish", {**TRAINS_ONLY, "keys": {name: "pasted-key-345"}})
        assert status == 400 and body["field"] == "keys", name
        status, body = api.handle("testkey", {"name": name, "value": "pasted-key-345"})
        assert status == 400 and body["field"] == "name", name
    assert not (board.state / "secrets.env").exists()  # nothing saved
    assert ok(api, "finish", {**TRAINS_ONLY, "keys": {"BODS_API_KEY": "new-bods-key-000"}})["saved"]
    assert "BODS_API_KEY=new-bods-key-000" in (board.state / "secrets.env").read_text()


def test_the_settings_are_checked_as_each_view_turned_on_loads_them(both):
    # The board ignores [dashboard]; the dashboard can't start with it wrong.
    both.config.write_text(both.config.read_text() + "\n[dashboard]\ntrain_rows = 50\n")
    assert ok(both, "finish", {**TRAINS_ONLY, "views": ["board"]})["saved"]
    before = (both.state / "settings.toml").read_text()
    status, body = both.handle("finish", {**TRAINS_ONLY, "views": ["board", "dashboard"]})
    assert status == 422 and "train_rows" in body["error"]
    assert (both.state / "settings.toml").read_text() == before


@pytest.mark.parametrize("views, message, links", [
    (["board"], "Saved. The board shows the new settings within a few minutes.", ["Settings"]),
    (["dashboard"], "Saved. The dashboard shows them within a few minutes.", ["The dashboard", "Settings"]),
    (["board", "dashboard"],
     "Saved. The board shows the new settings within a few minutes. The dashboard shows them within a few minutes.",
     ["The dashboard", "Settings"]),
])
def test_the_finished_page_says_what_happens_next_and_links_to_each_view_on(both, views, message, links):
    body = ok(both, "finish", {**TRAINS_ONLY, "views": views})
    assert body["message"] == message
    assert [link["label"] for link in body["links"]] == links
    assert {"label": "The dashboard", "href": "/"} in body["links"] or "dashboard" not in views


def test_where_serve_starts_again_the_finished_page_says_so(board):
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, install=ONE,
                   stations=lambda: STATIONS, links=[("Settings", "/settings")],
                   view_links={"dashboard": ("The dashboard", "/")})
    body = ok(api, "finish", {**TRAINS_ONLY, "views": ["dashboard"]})
    assert body["message"] == "Saved. The dashboard starts again in a few seconds to use them."
    assert body["links"] == [{"label": "The dashboard", "href": "/"}, {"label": "Settings", "href": "/settings"}]


@pytest.mark.parametrize("options", [
    {"view_links": {"screen": ("Screen", "/")}},
    {"view_links": {"dashboard": ("The dashboard", "javascript:alert(1)")}},
    {"install": Install("none", (), (), None)},
    {"install": Install("odd", ("screen",), (), None)},
])
def test_an_install_offers_views_and_links_to_them_plainly(board, options):
    with pytest.raises(ValueError):
        SetupApi(board_target(board.config), dict, board.state, **options)


def test_links_must_be_plain(board):
    for href in ("javascript:alert(1)", "//evil.example/", "/a b"):
        with pytest.raises(ValueError):
            SetupApi(board_target(board.config), dict, board.state, links=[("Board", href)])


# A runner: the internet work done elsewhere (the Pi's setup service).


class FakeRunner:
    def __init__(self):
        self.calls = []

    def lookup_postcode(self, postcode):
        self.calls.append(("postcode", postcode))
        return HOME_PLACE

    def search_address(self, query):
        self.calls.append(("address", query))
        return [HOME_PLACE]

    def nearby_stops(self, lat, lon, *, within_m, limit):
        self.calls.append(("stops", lat, lon, within_m, limit))
        return list(STOPS)

    def walk_minutes(self, home, places):
        self.calls.append(("walk", home, dict(places)))
        return dict.fromkeys(places, (4, "route"))

    def make_job(self, stops, pasted_key, *, routes, walk_minutes):
        self.calls.append(("job", dict(stops), pasted_key, routes, dict(walk_minutes)))
        return FakeJob("unused", stops, pasted_key, routes=routes, walk_minutes=walk_minutes)

    def test_key(self, name, value, app_id):
        self.calls.append(("testkey", name, value, app_id))
        return KeyResult("wrong_key", f"refused {value} and {app_id}")


@pytest.fixture
def ran(board):
    runner = FakeRunner()
    api = SetupApi(board_target(board.config), lambda: dict(board.secrets), board.state, runner=runner,
                   stations=lambda: STATIONS, nearby_stations=board._nearby_stations,
                   key_tests=RateLimit(10.0, clock=lambda: board.clock[0]), keys=WITH_TAPI)
    api.runner, api.board = runner, board
    return api


def test_a_runner_does_the_lookups(ran):
    assert ok(ran, "postcode", {"postcode": "SK13 1AH"})["place"]["label"] == "SK13 1AH"
    assert ok(ran, "address", {"q": "1 High Street"})["places"][0]["district"] == "High Peak"
    assert [s["label"] for s in ok(ran, "stops", HOME)["stops"]] == ["A", "B", "C"]
    walk = ok(ran, "walk", {"home": HOME, "station_crs": "GLO", "stop_atcos": ["1800SB12341"]})["walk"]
    assert walk == {"GLO": {"minutes": 4, "source": "route"}, "1800SB12341": {"minutes": 4, "source": "route"}}
    assert [c[0] for c in ran.runner.calls] == ["postcode", "address", "stops", "walk"]
    assert ran.runner.calls[2][3:] == (setupapi.STOPS_WITHIN_M, setupapi.STOPS_SHOWN)
    assert ran.board.calls == []  # nothing in this process


def test_a_runner_is_never_given_a_saved_key(ran):
    ok(ran, "start", {"stops": {"A": "1800SB12341"}, "routes": True, "walk": {"A": 3}})
    assert ran.runner.calls[-1] == ("job", {"A": "1800SB12341"}, "", True, {"A": 3})  # "": the saved one
    FakeJob.made[-1].finish()
    ok(ran, "start", {"stops": {"A": "1800SB12341"}, "routes": False, "keys": {"BODS_API_KEY": "pasted-key-789"}})
    assert ran.runner.calls[-1][2] == "pasted-key-789"
    ok(ran, "testkey", {"name": "RDM_API_KEY"})
    assert ran.runner.calls[-1] == ("testkey", "RDM_API_KEY", None, None)
    assert RDM_KEY not in repr(ran.runner.calls) and BODS_KEY not in repr(ran.runner.calls)


def test_a_runner_still_needs_a_bus_key_saved_or_pasted(ran):
    del ran.board.secrets["BODS_API_KEY"]
    assert ran.handle("start", {"stops": {"A": "1800SB12341"}, "routes": False})[0] == 400
    assert ran.runner.calls == []


def test_a_runners_key_test_is_limited_and_scrubbed(ran):
    status, body = ran.handle("testkey", {"name": "TAPI_APP_KEY", "value": "pasted-tapi-key",
                                          "app_id": "pasted-tapi-id"})
    assert status == 200 and ran.runner.calls[-1] == ("testkey", "TAPI_APP_KEY", "pasted-tapi-key", "pasted-tapi-id")
    assert "pasted-tapi" not in json.dumps(body)
    assert ran.handle("testkey", {"name": "TAPI_APP_ID"})[0] == 429
    assert len(ran.runner.calls) == 1


# The settings tabs test their own keys through test_key: any key keytest knows,
# under the wizard's limit, and with its runner.


def test_test_key_tries_any_key_keytest_knows_typed_or_saved(board):
    assert board.test_key("RDM_KB_API_KEY") == KeyResult("works", "It works.")
    assert board.test_key("RDM_DETAILS_API_KEY", "typed-details-1") == KeyResult("works", "It works.")
    assert [c[1:3] for c in board.calls] == [("RDM_KB_API_KEY", None), ("RDM_DETAILS_API_KEY", "typed-details-1")]
    with pytest.raises(Invalid):
        board.test_key("PASSWORD")
    # The wizard's own API still tests only the keys it offers.
    assert board.handle("testkey", {"name": "RDM_KB_API_KEY"})[0] == 400


def test_test_key_shares_the_wizards_limit(board):
    board.test_key("RDM_API_KEY")
    assert board.handle("testkey", {"name": "RDM_API_KEY"})[0] == 429
    ok(board, "testkey", {"name": "TAPI_APP_ID"})
    with pytest.raises(setupapi.TooSoon):
        board.test_key("TAPI_APP_KEY", "typed-tapi-key", "typed-tapi-id")


def test_test_key_goes_to_the_runner_when_there_is_one(ran):
    assert ran.test_key("TAPI_APP_KEY", "typed-tapi-key", "typed-tapi-id").state == "wrong_key"
    ran.test_key("RDM_KB_API_KEY")
    assert ran.runner.calls == [("testkey", "TAPI_APP_KEY", "typed-tapi-key", "typed-tapi-id"),
                                ("testkey", "RDM_KB_API_KEY", None, None)]
    assert ran.board.calls == []  # nothing in this process
