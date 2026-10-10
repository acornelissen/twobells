"""The setup service and the page talking through the spool, end to end in
threads (no systemd), with setup's lookups faked: no network."""

import json
import logging
import os
import threading
import time

import pytest

from departureboard import discover, setupspool
from departureboard.keytest import KeyResult
from departureboard.setup import Place, SetupError, Stop
from departureboard.overlay import board_target
from departureboard.setupapi import RateLimit, SetupApi
from departureboard.setupspool import Refused, Spool, SpoolRunner, Worker

HOME = {"lat": 53.461964, "lon": -1.965633}
HOME_PLACE = Place(HOME["lat"], HOME["lon"], "SK13 1AH", "England", "High Peak")
STOPS = [Stop("1800SB12341", "Station Road", "Stop A", "N", 53.4615, -1.9662, 64),
         Stop("1800SB12342", "Station Road", "Stop B", "S", 53.4614, -1.9665, 85)]
STATIONS = (("Glossop", "GLO", 53.4445, -1.9491),)
SAVED_BODS = "saved-bods-key-111"
SAVED_RDM = "saved-rdm-key-222"
PASTED = "pasted-key-333333"
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
    """A discover.SetupJob that finishes when told to."""

    made: list["FakeJob"] = []
    busy = False

    def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None, nice=0):
        self.folder, self.stops, self.api_key, self.routes, self.walk = folder, stops, api_key, routes, walk_minutes
        self.nice = nice
        self.state = {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}
        self.cancelled = False
        FakeJob.made.append(self)

    def start(self):
        if FakeJob.busy:
            raise discover.SetupBusy("busy")
        self.state.update(state="running", step=discover.STEP_DISCOVER, total=3)

    def status(self):
        return json.loads(json.dumps(self.state))

    def cancel(self):
        self.cancelled = True
        if self.state["state"] in ("queued", "running"):
            self.state.update(state="failed", message="Cancelled")

    def finish(self, roads=None):
        """Done; with `roads` (done, total), still finding the roads for the maps."""
        self.state.update(state="done", step=discover.STEP_TIMETABLE, done=1, total=1,
                          message=f"Found 1 with {self.api_key}", result=json.loads(json.dumps(RESULT)))
        if roads is not None:
            self.state["roads"] = {"state": "running", "done": roads[0], "total": roads[1]}


@pytest.fixture
def board(tmp_path):
    FakeJob.made, FakeJob.busy = [], False
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\ndisplay = "hdmi-1280x400"\n')
    calls = []
    secrets = {"BODS_API_KEY": SAVED_BODS, "RDM_API_KEY": SAVED_RDM}

    def test_key(name, value, settings, env):
        calls.append(("testkey", name, value, dict(env)))
        logging.getLogger("departureboard.keytest").info("Tested %s: %s", name, "wrong_key")
        return KeyResult("wrong_key", f"It refused {value or env.get(name)}")

    worker = Worker(
        state / setupspool.SPOOL, job_folder=state, load_secrets=lambda: dict(secrets),
        settings=lambda: {"train": {"crs": "GLO"}},
        lookup_postcode=lambda postcode: calls.append(("postcode", postcode)) or HOME_PLACE,
        search_address=lambda q: calls.append(("address", q)) or [HOME_PLACE],
        nearby_stops=lambda lat, lon, within_m, limit: calls.append(("stops", lat, lon, within_m, limit)) or STOPS,
        walk_minutes=lambda home, places: calls.append(("walk", home, places)) or {k: (4, "route") for k in places},
        make_job=FakeJob, test_key=test_key, idle=60, poll=0.01, stop=threading.Event(),
    )
    runner = SpoolRunner(state / setupspool.SPOOL, poll=0.01, waits=dict.fromkeys(setupspool.WAITS, 5.0),
                         start_wait=5.0)
    api = SetupApi(board_target(config), lambda: dict(secrets), state, runner=runner, stations=lambda: STATIONS,
                   key_tests=RateLimit(10.0))
    api.worker, api.runner, api.calls, api.state, api.secrets = worker, runner, calls, state, secrets
    return api


@pytest.fixture
def serving(board):
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    yield board
    board.worker._stop.set()
    thread.join(5)
    assert not thread.is_alive()


def ok(api, name, payload=None):
    status, body = api.handle(name, {} if payload is None else payload)
    assert status == 200, body
    return body


def spool_text(state) -> str:
    """Everything left in the spool, as one string."""
    out = []
    for folder, _, names in os.walk(state / setupspool.SPOOL):
        for name in names:
            if name != setupspool.WORKER_LOCK:
                out.append(open(os.path.join(folder, name), encoding="utf-8", errors="replace").read())
    return "\n".join(out)


def wait_for(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


# Round trips.


def test_the_wizard_round_trips_through_the_setup_service(serving, caplog):
    caplog.set_level(logging.DEBUG)
    assert ok(serving, "postcode", {"postcode": "SK13 1AH"})["place"]["district"] == "High Peak"
    assert ok(serving, "address", {"q": "1 High Street, Glossop"})["places"][0]["label"] == "SK13 1AH"
    stops = ok(serving, "stops", HOME)["stops"]
    assert [s["label"] for s in stops] == ["A", "B"] and stops[0]["bearing"] == "N"
    walk = ok(serving, "walk", {"home": HOME, "station_crs": "GLO", "stop_atcos": ["1800SB12341"]})["walk"]
    assert walk == {"GLO": {"minutes": 4, "source": "route"}, "1800SB12341": {"minutes": 4, "source": "route"}}
    tested = ok(serving, "testkey", {"name": "BODS_API_KEY", "value": PASTED})
    assert tested["state"] == "wrong_key" and PASTED not in json.dumps(tested)
    assert [c[0] for c in serving.calls] == ["postcode", "address", "stops", "walk", "testkey"]
    assert serving.calls[2][3:] == (1000, 15)
    assert serving.calls[4][2] == PASTED and serving.calls[4][3]["RDM_API_KEY"] == SAVED_RDM  # saved: read there

    # Nothing in the spool afterwards, and nothing private in the log.
    assert spool_text(serving.state) == ""
    for private in (PASTED, SAVED_BODS, SAVED_RDM, "SK13", "High Street", "53.46", "-1.96"):
        assert private not in caplog.text, private
    assert "Setup request: postcode" in caplog.text


def test_the_search_round_trips_with_its_progress(serving, caplog):
    caplog.set_level(logging.DEBUG)
    ok(serving, "stops", HOME)
    started = ok(serving, "start", {"stops": {"A": "1800SB12341"}, "routes": True, "walk": {"A": 3},
                                    "keys": {"BODS_API_KEY": PASTED}})["job"]
    assert started["state"] in ("queued", "running")
    wait_for(lambda: FakeJob.made)
    job = FakeJob.made[-1]
    assert (job.stops, job.api_key, job.routes, job.walk, job.folder) == (
        {"A": "1800SB12341"}, PASTED, True, {"A": 3}, serving.state)
    assert job.nice == setupspool.SEARCH_NICE == 10  # it gives way to the lookups
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "running")
    assert ok(serving, "job")["job"]["total"] == 3
    job.finish()
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "done")
    found = ok(serving, "job")["job"]
    assert found["found"]["operators"] == ["BNML"] and found["found"]["routes"] == ["341"]
    assert PASTED not in json.dumps(found)  # the service scrubbed its own message

    saved = ok(serving, "finish", {"home": HOME, "station_crs": "GLO", "stops": {"A": "1800SB12341"},
                                   "walk": {"1800SB12341": 3}})
    assert saved["saved"]
    assert '"BNML"' in (serving.state / "settings.toml").read_text()
    assert PASTED not in spool_text(serving.state) and PASTED not in caplog.text


def test_the_search_uses_the_saved_key_there_without_sending_it(board):
    ok(board, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    [name] = os.listdir(board.state / setupspool.SPOOL / "requests")
    request = board.state / setupspool.SPOOL / "requests" / name
    assert oct(request.stat().st_mode & 0o777) == "0o600"
    text = request.read_text()
    assert SAVED_BODS not in text and json.loads(text)["args"]["key"] == ""
    board.worker.step()
    assert FakeJob.made[-1].api_key == SAVED_BODS
    assert not request.exists()


def test_cancelling_the_search(serving):
    ok(serving, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    wait_for(lambda: FakeJob.made)
    ok(serving, "cancel")
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "failed")
    assert FakeJob.made[-1].cancelled and ok(serving, "job")["job"]["message"] == "Cancelled"


def test_a_search_already_running_elsewhere_is_said(serving):
    FakeJob.busy = True
    ok(serving, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "failed")
    assert ok(serving, "job")["job"]["message"] == "A setup search is already running for this board"


def test_a_lookup_that_fails_there_says_so_here(serving):
    def fail(postcode):
        raise SetupError("postcodes.io doesn't know that postcode")

    serving.worker._lookup_postcode = fail
    assert serving.handle("postcode", {"postcode": "ZZ1 1ZZ"}) == (
        422, {"error": "postcodes.io doesn't know that postcode"})
    serving.worker._lookup_postcode = lambda postcode: 1 / 0
    assert serving.handle("postcode", {"postcode": "ZZ1 1ZZ"}) == (500, {"error": "Something went wrong; try again"})


def test_the_service_limits_key_tests_too(board):
    board.runner.post("a" * 32, "testkey", {"name": "RDM_API_KEY", "value": "", "app_id": ""})
    board.runner.post("b" * 32, "testkey", {"name": "RDM_API_KEY", "value": "", "app_id": ""})
    board.worker.step()
    assert board.runner.reply("a" * 32, delete=True)["ok"]
    second = board.runner.reply("b" * 32, delete=True)
    assert not second["ok"] and "Wait" in second["error"]
    assert len(board.calls) == 1


# No answer.


def test_no_answer_is_said_plainly_and_the_request_withdrawn(board):
    board.runner._waits = dict.fromkeys(setupspool.WAITS, 0.05)
    status, body = board.handle("testkey", {"name": "BODS_API_KEY", "value": PASTED})
    assert status == 422 and body["error"] == setupspool.NO_ANSWER
    assert spool_text(board.state) == ""  # the pasted key didn't stay waiting


def test_a_search_never_started_fails(board):
    board.runner._start_wait = 0.0
    job = board.runner.make_job({"A": "1800SB12341"}, PASTED, routes=False, walk_minutes={})
    job.start()
    time.sleep(0.01)
    assert job.status()["message"] == setupspool.NO_ANSWER
    assert spool_text(board.state) == ""


def test_a_search_that_goes_quiet_has_stopped(board):
    now = [0.0]
    board.runner.monotonic = lambda: now[0]
    job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
    job.start()
    board.worker.step()
    assert job.status()["state"] == "running"
    now[0] = setupspool.SILENT_S + 1
    status = job.status()
    assert status["state"] == "failed" and status["message"] == setupspool.STOPPED


def test_a_search_stays_alive_while_a_long_lookup_runs(board):
    # A stops lookup may take up to its wait (longer than SILENT_S) in the
    # service's main thread; the search's heartbeat must keep going meanwhile.
    assert setupspool.WAITS["stops"] > setupspool.SILENT_S
    now = [0.0]
    board.runner.monotonic = board.worker._monotonic = lambda: now[0]
    entered, release = threading.Event(), threading.Event()

    def slow_stops(lat, lon, within_m, limit):
        entered.set()
        release.wait(10)
        return STOPS

    board.worker._nearby_stops = slow_stops
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    try:
        job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
        job.start()
        wait_for(lambda: job.status()["state"] == "running")
        board.runner.post(setupspool.new_id(), "stops", {"home": HOME, "within_m": 1000, "limit": 15})
        assert entered.wait(5)
        status = board.runner.spool.status / f"{job.id}.json"
        while now[0] <= setupspool.SILENT_S + setupspool.HEARTBEAT_S:
            beat = json.loads(status.read_text())["beat"]
            now[0] += setupspool.HEARTBEAT_S + 1
            wait_for(lambda before=beat: json.loads(status.read_text())["beat"] > before)
            assert job.status()["state"] == "running"
    finally:
        release.set()
        board.worker._stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_a_service_told_to_stop_says_the_search_was_cancelled_at_once(board):
    # Even while a lookup still holds the main thread, as systemd's stop doesn't wait for it.
    entered, release = threading.Event(), threading.Event()

    def slow_stops(lat, lon, within_m, limit):
        entered.set()
        release.wait(10)
        return STOPS

    board.worker._nearby_stops = slow_stops
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    try:
        job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
        job.start()
        wait_for(lambda: job.status()["state"] == "running")
        board.runner.post(setupspool.new_id(), "stops", {"home": HOME, "within_m": 1000, "limit": 15})
        assert entered.wait(5)
        board.worker._stop.set()
        wait_for(lambda: job.status()["state"] == "failed")
        assert job.status()["message"] == "Cancelled" and FakeJob.made[-1].cancelled
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


# The roads for the bus maps, found after the search is done.


def test_the_roads_after_the_search_are_reported_and_keep_the_service_up(board):
    board.worker._idle = 0.05
    job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
    job.start()
    board.worker.step()
    fake = FakeJob.made[-1]
    fake.finish(roads=(3, 10))
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    try:
        wait_for(lambda: job.status().get("roads") == {"state": "running", "done": 3, "total": 10})
        assert job.status()["state"] == "done"  # the settings can be saved now
        time.sleep(0.3)
        assert thread.is_alive()  # long past idle: it is still finding them
        fake.state["roads"].update(done=7)
        wait_for(lambda: job.status()["roads"]["done"] == 7)
        fake.state["roads"].update(state="done", done=10)
        thread.join(5)
        assert not thread.is_alive()  # idle once they are found
    finally:
        board.worker._stop.set()
        thread.join(5)
    assert job.status()["roads"] == {"state": "done", "done": 10, "total": 10}
    assert os.listdir(board.runner.spool.status) == []  # collected


def test_a_service_told_to_stop_while_the_roads_are_found_keeps_the_search_done(board):
    # As systemd's stop: the timetable is saved without its roads, and the next build finds them.
    job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
    job.start()
    board.worker.step()
    fake = FakeJob.made[-1]
    fake.finish(roads=(3, 10))
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    wait_for(lambda: job.status().get("roads", {}).get("state") == "running")
    board.worker._stop.set()
    thread.join(5)
    assert not thread.is_alive() and fake.cancelled
    status = job.status()
    assert status["state"] == "done" and status["result"]["fragment"]["bus"]["operators"] == ["BNML"]
    assert status["roads"] == {"state": "cancelled", "done": 3, "total": 10}


def test_a_new_search_stops_the_last_one_s_roads_there(serving):
    ok(serving, "start", {"stops": {"A": "1800SB12341"}, "routes": False})
    wait_for(lambda: FakeJob.made)
    first = FakeJob.made[-1]
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "running")
    first.finish(roads=(3, 10))
    wait_for(lambda: ok(serving, "job")["job"].get("roads", {}).get("state") == "running")
    ok(serving, "start", {"stops": {"B": "1800SB12342"}, "routes": False})
    wait_for(lambda: len(FakeJob.made) == 2)
    assert first.cancelled and FakeJob.made[-1].stops == {"B": "1800SB12342"}
    wait_for(lambda: ok(serving, "job")["job"]["state"] == "running")


def test_check_status_takes_the_roads_of_a_search_that_is_done():
    status = {"state": "done", "step": "", "done": 0, "total": 0, "message": "Found 1", "result": RESULT,
              "roads": {"state": "running", "done": 12, "total": 140}}
    assert setupspool.check_status(status)["roads"] == {"state": "running", "done": 12, "total": 140}
    assert "roads" not in setupspool.check_status({**status, "roads": None})
    del status["roads"]
    assert "roads" not in setupspool.check_status(status)


@pytest.mark.parametrize("roads", [
    {"state": "lost", "done": 1, "total": 2},
    {"state": "running", "done": "1", "total": 2},
    {"state": "running", "done": 3, "total": 2},
    {"state": "running", "done": 1},
    {"state": "running", "done": 1, "total": 2, "more": 1},
    [1, 2],
])
def test_check_status_refuses_roads_that_dont_fit(roads):
    status = {"state": "done", "step": "", "done": 0, "total": 0, "message": "", "result": RESULT, "roads": roads}
    with pytest.raises(Refused):
        setupspool.check_status(status)


def test_check_status_refuses_roads_before_the_search_is_done():
    with pytest.raises(Refused):
        setupspool.check_status({"state": "running", "step": "", "done": 0, "total": 0, "message": "",
                                 "result": None, "roads": {"state": "running", "done": 0, "total": 2}})


# Refused files, both ways.


def request(board, name="a" * 32, made_offset=0.0, **change) -> dict:
    return {"v": 1, "id": name, "made": time.time() + made_offset, "kind": "postcode",
            "args": {"postcode": "SK13 1AH"}, **change}


def put(board, data, name=None, raw=None):
    path = board.state / setupspool.SPOOL / "requests" / (name or f"{data['id']}.json")
    path.write_bytes(raw if raw is not None else json.dumps(data).encode())
    os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize("change", [
    {"made_offset": -setupspool.REQUEST_MAX_AGE_S - 10},  # stale
    {"made_offset": 60},  # from the future
    {"made": "now"},
    {"v": 2},
    {"id": "b" * 32},  # not its file's
    {"kind": "shell"},
    {"args": {"postcode": "SK13 1AH", "extra": 1}},
    {"args": {"postcode": "x" * 17}},
    {"kind": "stops", "args": {"home": {"lat": 48.85, "lon": 2.35}, "within_m": 1000, "limit": 15}},  # Paris
    {"kind": "stops", "args": {"home": HOME, "within_m": 10**9, "limit": 15}},
    {"kind": "walk", "args": {"home": HOME, "places": {"../x": HOME}}},
    {"kind": "testkey", "args": {"name": "PATH", "value": "", "app_id": ""}},
    {"kind": "testkey", "args": {"name": "RDM_API_KEY", "value": "has spaces", "app_id": ""}},
    {"kind": "testkey", "args": {"name": "RDM_API_KEY", "value": "", "app_id": "an-app-id"}},
    {"kind": "search", "args": {"stops": {}, "routes": False, "walk": {}, "key": ""}},
    {"kind": "search", "args": {"stops": {"A": "1800SB12341"}, "routes": "yes", "walk": {}, "key": ""}},
    {"kind": "search", "args": {"stops": {"A": "1800SB12341"}, "routes": False, "walk": {"A": 61}, "key": ""}},
    {"kind": "cancel", "args": {"job": "../x"}},
    {"kind": "active", "args": {"browser": "x"}},  # active takes nothing
    {"kind": "active", "args": []},
])
def test_the_service_deletes_requests_that_dont_fit_unanswered(board, change, caplog):
    path = put(board, request(board, **change), name="a" * 32 + ".json")
    board.worker.step()
    assert not path.exists() and os.listdir(board.state / setupspool.SPOOL / "replies") == []
    assert board.calls == [] and FakeJob.made == []
    assert "Refused a setup request" in caplog.text


@pytest.mark.parametrize("raw", [
    b"x" * (setupspool.MAX_REQUEST_BYTES + 1),  # oversized
    b'{"v": 1, "id": "' + b"a" * 32 + b'", "made": NaN, "kind": "postcode", "args": {}}',
    b"\xff\xfe not utf-8",
    b"[" * 50 + b"]" * 50,
    b"[" * 3000 + b"]" * 3000,  # deeper than Python will recurse, though json reads it
])
def test_the_service_deletes_oversized_or_odd_files_unread(board, raw):
    path = put(board, None, name="a" * 32 + ".json", raw=raw)
    board.worker.step()
    assert not path.exists() and os.listdir(board.state / setupspool.SPOOL / "replies") == []


def test_the_service_never_follows_a_link_or_keeps_a_stray(board, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(request(board)))
    requests = board.state / setupspool.SPOOL / "requests"
    (requests / ("a" * 32 + ".json")).symlink_to(target)
    (requests / "stray.txt").write_text("x")
    (requests / "folder").mkdir()
    board.worker.step()
    assert os.listdir(requests) == [] and target.exists() and board.calls == []


def test_the_page_refuses_replies_that_dont_fit(board):
    replies = board.state / setupspool.SPOOL / "replies"
    rid = "c" * 32
    for reply in (
        {"v": 1, "id": "d" * 32, "ok": True, "value": {}},  # someone else's
        {"v": 1, "id": rid, "ok": True},
        {"v": 1, "id": rid, "ok": False, "error": "x", "why": "other"},
        {"v": 1, "id": rid, "ok": True, "value": 1, "extra": 1},
    ):
        (replies / f"{rid}.json").write_text(json.dumps(reply))
        with pytest.raises(SetupError, match="doesn't fit"):
            board.runner.reply(rid, delete=True)
    (replies / f"{rid}.json").write_bytes(b" " * (setupspool.MAX_REPLY_BYTES + 1))
    with pytest.raises(SetupError, match="doesn't fit"):
        board.runner.reply(rid, delete=True)
    assert os.listdir(replies) == []


@pytest.mark.parametrize("kind, value", [
    ("postcode", {"lat": 48.85, "lon": 2.35, "label": "Paris", "country": "", "district": ""}),
    ("postcode", {"lat": 53.4, "lon": -1.9, "label": "a\nb", "country": "", "district": ""}),
    ("stops", [{"atco": "../etc", "name": "x", "indicator": "", "bearing": "", "lat": 53.4, "lon": -1.9,
                "distance_m": 5}]),
    ("walk", {"NOTASKED": [3, "route"]}),
    ("walk", {"GLO": [3, "teleport"]}),
    ("testkey", {"state": "works!", "message": "x"}),
])
def test_the_page_checks_each_answers_shape(board, kind, value):
    def answer():
        wait_for(lambda: os.listdir(board.state / setupspool.SPOOL / "requests"))
        [name] = os.listdir(board.state / setupspool.SPOOL / "requests")
        rid = name.removesuffix(".json")
        board.runner.spool.write(board.runner.spool.replies, rid, {"v": 1, "id": rid, "ok": True, "value": value},
                                 setupspool.MAX_REPLY_BYTES)

    thread = threading.Thread(target=answer)
    thread.start()
    with pytest.raises(SetupError, match="doesn't fit"):
        if kind == "postcode":
            board.runner.lookup_postcode("SK13 1AH")
        elif kind == "stops":
            board.runner.nearby_stops(53.4, -1.9, within_m=1000, limit=15)
        elif kind == "walk":
            board.runner.walk_minutes(HOME_PLACE, {"GLO": HOME_PLACE})
        else:
            board.runner.test_key("RDM_API_KEY", None, None)
    thread.join(5)


def test_a_search_status_that_doesnt_fit_ends_it(board):
    job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
    job.start()
    bad = {**json.loads(json.dumps(RESULT))}
    bad["fragment"]["bus"]["operators"] = ["x; rm"]
    board.runner.spool.write(board.runner.spool.status, job.id, {"v": 1, "id": job.id, "ok": True, "beat": 1,
                             "value": {"state": "done", "step": "", "done": 1, "total": 1, "message": "",
                                       "result": bad}}, setupspool.MAX_REPLY_BYTES)
    assert job.status()["message"] == setupspool.UNFIT


def test_check_status_takes_a_real_jobs_result():
    status = {"state": "done", "step": discover.STEP_TIMETABLE, "done": 1, "total": 1, "message": "Found 1",
              "result": RESULT}
    assert setupspool.check_status(status)["result"] == RESULT
    with pytest.raises(Refused):
        setupspool.check_status({**status, "state": "running"})  # a result before it's done


# Priority.


def stop_searching(api_key, area, at_most):
    raise discover.DiscoveryError("stop here")


@pytest.mark.parametrize("per_thread, nice, lowered", [(True, 10, [10]), (True, 0, []), (False, 10, [])])
def test_the_search_runs_nicer_in_its_own_thread_only(tmp_path, monkeypatch, per_thread, nice, lowered):
    calls = []
    monkeypatch.setattr(discover, "_NICE_PER_THREAD", per_thread)  # Linux: a thread's nice value is its own
    monkeypatch.setattr(os, "nice", lambda by: calls.append((by, threading.current_thread().name)) or 0)
    job = discover.SetupJob(tmp_path, {"A": "1800SB12341"}, "key", find=stop_searching, nice=nice)
    job.start()
    assert job.wait(5) and job.status()["message"] == "stop here"
    assert calls == [(by, "setup-job") for by in lowered]  # never the caller's thread


def test_a_search_that_cant_be_made_nicer_still_runs(tmp_path, monkeypatch, caplog):
    def refused(by):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(discover, "_NICE_PER_THREAD", True)
    monkeypatch.setattr(os, "nice", refused)
    job = discover.SetupJob(tmp_path, {"A": "1800SB12341"}, "key", find=stop_searching, nice=10)
    job.start()
    assert job.wait(5) and job.status()["message"] == "stop here"
    assert "Can't lower the setup search's priority" in caplog.text


# Housekeeping.


def test_old_files_are_swept(board):
    spool = board.runner.spool
    old = time.time() - 3600
    for folder in (spool.tmp, spool.requests, spool.replies, spool.status):
        path = folder / ("e" * 32 + ".json")
        path.write_text("{}")
        os.utime(path, (old, old))
    fresh = spool.replies / ("f" * 32 + ".json")
    fresh.write_text("{}")
    spool.sweep(time.time())
    assert spool_text(board.state) == "{}"  # only the fresh one


def test_a_lookups_answer_the_page_gave_up_on_goes_once_its_wait_is_over(board):
    # A postcode's answer holds home's exact position: it mustn't sit in the spool until the Pi restarts.
    rid = "a" * 32
    board.runner.post(rid, "postcode", {"postcode": "SK13 1AH"})
    board.worker.step()
    reply = board.runner.spool.replies / f"{rid}.json"
    assert reply.exists()  # and the page, having given up, never reads it
    late = time.time() - max(setupspool.WAITS.values()) - 1
    os.utime(reply, (late, late))
    board.worker.step()  # the service's next look round, with nothing new asked
    assert not reply.exists()


def test_a_finished_search_waits_longer_for_the_page(board):
    # The search's result took minutes to find; a phone asleep for a while can still collect it.
    job = board.runner.make_job({"A": "1800SB12341"}, "", routes=False, walk_minutes={})
    job.start()
    board.worker.step()
    FakeJob.made[-1].finish()
    board.worker.step()
    [status] = os.listdir(board.runner.spool.status)
    late = time.time() - max(setupspool.WAITS.values()) - 1
    os.utime(board.runner.spool.status / status, (late, late))
    board.worker.step()
    board.runner.post("b" * 32, "cancel", {"job": "c" * 32})  # the page's sweep too
    assert job.status()["state"] == "done"
    assert os.listdir(board.runner.spool.status) == []  # collected


def test_the_spool_is_private_and_never_a_link(tmp_path):
    spool = Spool(tmp_path / "spool")
    spool.make()
    assert sorted(os.listdir(spool.folder)) == ["replies", "requests", "snapshots", "status", "tmp"]
    for path in (spool.folder, spool.tmp, spool.requests, spool.replies, spool.status, spool.snapshots):
        assert oct(path.stat().st_mode & 0o777) == "0o700"
    (tmp_path / "linked").symlink_to(tmp_path / "spool")
    with pytest.raises(setupspool.SpoolError):
        Spool(tmp_path / "linked").make()


def test_the_spool_is_systemds_folder_in_memory_and_never_on_disk(tmp_path, monkeypatch):
    run = tmp_path / "run"
    monkeypatch.setattr(setupspool, "RUNTIME", tmp_path / "no-run")
    assert setupspool.spool_folder({"RUNTIME_DIRECTORY": str(run)}) == run
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(run))
    assert setupspool.spool_folder() == run
    for odd in ("relative/run", f"{run}:{tmp_path / 'other'}"):
        with pytest.raises(setupspool.SpoolError):
            setupspool.spool_folder({"RUNTIME_DIRECTORY": odd})
    # Run by hand: the units' folder if it's there, and never one on the SD card instead.
    with pytest.raises(setupspool.SpoolError, match="RUNTIME_DIRECTORY"):
        setupspool.spool_folder({})
    monkeypatch.setattr(setupspool, "RUNTIME", run)
    run.mkdir()
    assert setupspool.spool_folder({}) == run


def test_the_spool_is_the_fetch_services_and_the_messages_name_it():
    from pathlib import Path

    assert setupspool.RUNTIME == Path("/run/departureboard-fetch")
    assert "departureboard-fetch.path" in setupspool.NO_ANSWER
    assert "journalctl -u departureboard-fetch " in setupspool.NO_SETTINGS


def test_the_service_stops_when_idle_and_runs_once(board):
    board.worker._idle = 0.05
    started = time.monotonic()
    assert board.worker.run() == 0
    assert time.monotonic() - started < 2


def test_the_service_stays_up_while_told_to_and_never_leaves_a_request_waiting(board):
    board.worker._idle = 0  # idle the moment it isn't busy
    asked = []

    def tend():
        asked.append(time.monotonic())
        if len(asked) == 20:  # one comes in after the step, just as it would have stopped
            board.runner.post("e" * 32, "postcode", {"postcode": "SK13 1AH"})
        return len(asked) < 20  # busy (the dashboard is on) until then

    real_step = board.worker.step
    board.worker.step = lambda: False if len(asked) == 20 else real_step()
    assert board.worker.run(tend) == 0
    assert len(asked) > 20  # kept going past the idle time while busy, and once more for the late request
    assert os.listdir(board.runner.spool.requests) == []
    assert board.runner.reply("e" * 32, delete=True)["ok"] is True


def test_an_active_request_is_handed_on_and_gets_no_reply(board, caplog):
    seen = []
    board.worker._on_active = lambda: seen.append(True)
    poster = setupspool.Poster(board.runner.spool.folder)
    assert poster.active() is True
    [name] = os.listdir(board.runner.spool.requests)
    sent = json.loads((board.runner.spool.requests / name).read_text())
    assert (sent["kind"], sent["args"]) == ("active", {})
    assert oct((board.runner.spool.requests / name).stat().st_mode & 0o777) == "0o600"
    with caplog.at_level(logging.INFO):
        assert board.worker.step() is True  # a request, so not idle
    assert seen == [True] and os.listdir(board.runner.spool.requests) == []
    assert os.listdir(board.runner.spool.replies) == [] and os.listdir(board.runner.spool.status) == []
    assert "Setup request" not in caplog.text  # each minute a browser is open: not in the log


def test_an_active_request_needs_no_settings_either(board):
    worker = Worker(board.runner.spool.folder, job_folder=None, load_secrets=dict, idle=0, poll=0.01,
                    unavailable=setupspool.NO_SETTINGS)
    setupspool.Poster(board.runner.spool.folder).active()
    assert worker.run() == 0
    assert os.listdir(board.runner.spool.requests) == [] and os.listdir(board.runner.spool.replies) == []


def test_the_poster_posts_at_most_one_active_a_minute(tmp_path):
    now = [1000.0]
    poster = setupspool.Poster(tmp_path / "spool", monotonic=lambda: now[0])
    assert [poster.active(), poster.active()] == [True, False]
    now[0] += setupspool.ACTIVE_EVERY_S - 1
    assert poster.active() is False
    now[0] += 1
    assert poster.active() is True
    assert len(os.listdir(poster.spool.requests)) == 2


def test_the_poster_never_fails_the_browsers_request(tmp_path, caplog):
    poster = setupspool.Poster(tmp_path / "spool")
    os.chmod(poster.spool.requests, 0o500)  # can't write there
    try:
        assert poster.active() is False
    finally:
        os.chmod(poster.spool.requests, 0o700)
    assert "Can't tell the fetch service" in caplog.text


def test_only_one_service_answers_at_a_time(board):
    import fcntl

    board.runner.spool.make()
    with open(board.runner.spool.folder / setupspool.WORKER_LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        path = put(board, request(board))
        old = time.time() - 3600  # one a sweep would delete
        os.utime(path, (old, old))
        body = path.read_bytes()
        board.worker._idle = 60  # not waited: it stops at once
        started = time.monotonic()
        assert board.worker.run() == 0  # a clean stop, so systemd counts no failure
        assert time.monotonic() - started < 2
    # Untouched, for the service holding the lock: not read, swept or answered.
    assert os.listdir(board.runner.spool.requests) == ["a" * 32 + ".json"] and path.read_bytes() == body
    assert path.stat().st_mtime == old and os.listdir(board.runner.spool.replies) == [] and board.calls == []


def test_a_service_that_cant_read_its_config_says_so_to_each_request(board, tmp_path):
    worker = Worker(board.runner.spool.folder, job_folder=None, load_secrets=dict, idle=0, poll=0.01,
                    unavailable=setupspool.NO_SETTINGS)
    board.runner.post("a" * 32, "postcode", {"postcode": "SK13 1AH"})
    board.runner.post("b" * 32, "testkey", {"name": "RDM_API_KEY", "value": PASTED, "app_id": ""})
    job = board.runner.make_job({"A": "1800SB12341"}, PASTED, routes=False, walk_minutes={})
    job.start()
    board.runner.post("c" * 32, "cancel", {"job": "d" * 32})
    assert worker.run() == 0
    assert os.listdir(board.runner.spool.requests) == []  # none left to start it again
    for rid in ("a" * 32, "b" * 32):
        assert board.runner.reply(rid, delete=True) == {
            "v": 1, "id": rid, "ok": False, "why": "setup", "error": setupspool.NO_SETTINGS}
    assert job.status()["state"] == "failed" and job.status()["message"] == setupspool.NO_SETTINGS
    assert board.runner.reply("c" * 32, delete=True) is None  # a cancel needs no answer
    assert board.calls == [] and FakeJob.made == [] and PASTED not in spool_text(board.state)


def test_the_page_shows_why_the_service_cant_help(board):
    board.worker = Worker(board.runner.spool.folder, job_folder=None, load_secrets=dict, poll=0.01,
                          unavailable=setupspool.NO_SETTINGS, stop=threading.Event())
    thread = threading.Thread(target=board.worker.run, daemon=True)
    thread.start()
    try:
        assert board.handle("postcode", {"postcode": "SK13 1AH"}) == (422, {"error": setupspool.NO_SETTINGS})
    finally:
        board.worker._stop.set()
        thread.join(5)
