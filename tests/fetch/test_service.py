"""The fetch service on a Pi: the dashboard's pollers while its view is on,
the wizard's requests, and stopping when there is nothing to do. In threads,
with the pollers faked: no network."""

import fcntl
import json
import os
import signal
import threading
import time
from typing import ClassVar

import pytest

from tests.setup.test_spool import read_on_the_way_out
from twobells.core import files
from twobells.core.walk import WalkTimes
from twobells.fetch import handoff
from twobells.fetch import service as fetch
from twobells.fetch.service import FetchService, make_collector
from twobells.settings.installs import install_named
from twobells.settings.store import Store
from twobells.setup.lookups import Place
from twobells.setup.spool.folder import Spool
from twobells.setup.spool.runner import Poster, SpoolRunner
from twobells.setup.spool.worker import WORKER_LOCK

PI = install_named("pi")
ON = '[app]\nviews = ["board", "dashboard"]\n'
OFF = '[app]\nviews = ["board"]\n'
ROW = "4b7e000000000001"
HISTORY = {"mode": "train", "route": "GLO|Manchester", "scheduled": "07:12", "label": "07:12 to Manchester",
           "day_type": "weekday", "phrase": None}


class FakeCollector:
    """A collector.Collector that publishes one row and fetches nothing."""

    made: ClassVar[list["FakeCollector"]] = []

    def __init__(self, dash, hub, history):
        self.dash, self.hub, self.history = dash, hub, history
        self.started = self.stopped = False
        self.aside = []
        FakeCollector.made.append(self)

    def start(self):
        self.started = True
        self.hub.publish("trains", {"rows": [{"id": ROW}]}, {ROW: {"history": HISTORY}})

    def stop(self, timeout=5.0):
        self.stopped = True
        return True

    def healthy(self):
        return True

    def set_timetable_aside(self, aside):
        self.aside.append(aside)


@pytest.fixture
def pi(tmp_path, monkeypatch):
    FakeCollector.made = []
    monkeypatch.setenv("RDM_API_KEY", "from-the-environment")
    config = tmp_path / "config.toml"
    config.write_text('title = "Glossop"\n[train]\ncrs = "GLO"\n')
    folder = tmp_path / "state"
    folder.mkdir()
    store = Store.open(config, tmp_path / "none.env", state_dir=folder, install="pi")
    spool = Spool(tmp_path / "run")
    spool.make()
    return store, spool


def service(pi, idle=60.0, **options) -> FetchService:
    store, spool = pi
    options.setdefault("collector", FakeCollector)
    return FetchService(store, PI, spool.folder, idle=idle, poll=0.01, **options)


def save(pi, text: str) -> None:
    """Save settings as the settings pages do: whole, never half a file for the service to read."""
    store, _ = pi
    files.write_atomic(store.settings_file, text, 0o600)


def wait_for(check, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


class Running:
    """A fetch service run on a thread, stopped at the end."""

    def __init__(self, found: FetchService):
        self.service = found
        self.code = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.code = self.service.run()

    def stop(self):
        self.service.stop.set()
        self.thread.join(5)
        assert not self.thread.is_alive()
        return self.code


def lock_free(store) -> bool:
    with open(store.folder / "dashboard.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


# The dashboard


def test_the_pollers_run_while_the_dashboard_is_on_and_stop_when_it_goes_off(pi):
    store, spool = pi
    save(pi, ON)
    running = Running(service(pi))
    try:
        wait_for(lambda: FakeCollector.made and FakeCollector.made[0].started)
        path = handoff.snapshot_path(spool.folder)
        wait_for(path.exists)
        handed = json.loads(path.read_bytes())
        assert handed["snapshot"]["title"] == "Glossop" and ROW in handed["lookups"] and handed["healthy"]
        assert not lock_free(store)  # one dashboard per store
        save(pi, OFF)
        wait_for(lambda: FakeCollector.made[0].stopped)
        wait_for(lambda: not path.exists())  # none kept while it is off
        assert lock_free(store) and running.service.hub is None
        assert running.thread.is_alive()  # the wizard may still want it, until it is idle
        assert len(FakeCollector.made) == 1
    finally:
        assert running.stop() == 0


def test_the_web_servers_mirror_reads_what_the_fetch_service_hands_over(pi):
    store, spool = pi
    save(pi, ON)
    running = Running(service(pi))
    mirror = handoff.SnapshotMirror(spool.folder, "Placeholder", WalkTimes(), None)
    try:
        wait_for(lambda: mirror.check() or mirror.lookup(ROW) is not None)
        assert json.loads(mirror.current()[1])["title"] == "Glossop" and mirror.healthy()
    finally:
        running.stop()


def test_the_pollers_never_build_the_timetable_here(pi, monkeypatch):
    made = []
    monkeypatch.setattr(fetch, "Collector", lambda *args, **kwargs: made.append(kwargs) or FakeCollector(*args))
    save(pi, ON)
    running = Running(service(pi, collector=fetch.make_collector))
    try:
        wait_for(lambda: made)
        assert made == [{"build_timetable": False}]
        assert FakeCollector.made[0].history is not None  # the punctuality history is kept here
    finally:
        running.stop()


def test_a_snapshot_left_from_before_goes_when_the_dashboard_is_off(pi):
    _, spool = pi
    save(pi, OFF)
    path = handoff.snapshot_path(spool.folder)
    path.write_text("{}")
    assert service(pi, idle=0).run() == 0
    assert not path.exists() and FakeCollector.made == []


def test_not_set_up_runs_no_dashboard(pi):
    store, _ = pi
    store.config_path.write_text('title = "Glossop"\n[train]\ncrs = "CHANGE_ME"\n')
    save(pi, ON)
    assert service(pi, idle=0).run() == 0
    assert FakeCollector.made == []


def test_new_settings_start_the_pollers_again_with_them(pi):
    store, _ = pi
    save(pi, ON)
    running = Running(service(pi))
    try:
        wait_for(lambda: FakeCollector.made)
        first = FakeCollector.made[0]
        save(pi, 'title = "Our house"\n' + ON)
        wait_for(lambda: len(FakeCollector.made) == 2 and FakeCollector.made[1].started)
        second = FakeCollector.made[1]
        assert first.stopped and second.started and not second.stopped
        assert second.dash.title == "Our house" and running.service.hub is second.hub
        assert not lock_free(store)  # the lock went from the old to the new
    finally:
        running.stop()
    assert second.stopped and lock_free(store)


def test_a_saved_position_strips_setting_reaches_the_snapshot(pi):
    _, spool = pi
    save(pi, ON)
    running = Running(service(pi))
    path = handoff.snapshot_path(spool.folder)
    handed = lambda: json.loads(path.read_bytes())["snapshot"]  # noqa: E731
    try:
        wait_for(path.exists)
        assert handed()["row_strips"] is True
        save(pi, ON + "[dashboard]\nrow_strips = false\n")
        wait_for(lambda: len(FakeCollector.made) == 2)
        wait_for(lambda: path.exists() and handed()["row_strips"] is False)
    finally:
        running.stop()


def test_a_saved_scrolling_setting_reaches_the_snapshot(pi):
    _, spool = pi
    save(pi, ON)
    running = Running(service(pi))
    path = handoff.snapshot_path(spool.folder)
    handed = lambda: json.loads(path.read_bytes())["snapshot"]  # noqa: E731
    try:
        wait_for(path.exists)
        assert handed()["scrolling"] is True
        save(pi, "scrolling = false\n" + ON)
        wait_for(lambda: len(FakeCollector.made) == 2)
        wait_for(lambda: path.exists() and handed()["scrolling"] is False)
    finally:
        running.stop()


class PollersOnly(FakeCollector):
    """The real collector's pollers for the settings, never started."""

    def __init__(self, dash, hub, history):
        # Before super(), which adds it to `made` for the test to read.
        self.pollers = set(make_collector(dash, hub, history).pollers())
        super().__init__(dash, hub, history)


def test_a_saved_show_list_stops_fetching_what_it_leaves_out_without_a_restart(pi):
    weather = '[weather]\nlat = 53.44\nlon = -1.95\n'
    save(pi, ON + weather)
    running = Running(service(pi, collector=PollersOnly))
    try:
        wait_for(lambda: FakeCollector.made)
        assert "weather" in FakeCollector.made[0].pollers
        save(pi, ON + weather + '[dashboard]\nshow = ["next_to_catch"]\n')
        wait_for(lambda: len(FakeCollector.made) == 2)
        assert FakeCollector.made[1].pollers == {"trains", "upkeep", "heartbeat"}
    finally:
        running.stop()


@pytest.mark.parametrize("broken", [
    ON + "[train]\ncrs = 12\n",  # the dashboard's settings don't load
    "[app]\nviews = 3\n",  # nor do the views
    "not toml [",
])
def test_settings_that_dont_load_keep_the_pollers_running(pi, caplog, broken):
    save(pi, ON)
    running = Running(service(pi))
    try:
        wait_for(lambda: FakeCollector.made)
        save(pi, broken)
        wait_for(lambda: "left as it was" in caplog.text)
        time.sleep(0.05)
        assert len(FakeCollector.made) == 1 and not FakeCollector.made[0].stopped
        assert running.service.hub is FakeCollector.made[0].hub
    finally:
        running.stop()


def test_an_active_request_keeps_the_pollers_going_through_the_quiet_hours(pi):
    _, spool = pi
    save(pi, ON)
    running = Running(service(pi))
    try:
        wait_for(lambda: running.service.hub is not None)
        assert not running.service.hub.active(600)
        assert Poster(spool.folder).active()
        wait_for(lambda: running.service.hub.active(600))
        assert os.listdir(spool.requests) == [] and os.listdir(spool.replies) == []  # taken, no reply
    finally:
        running.stop()


def test_an_active_request_with_the_dashboard_off_is_taken_and_forgotten(pi):
    _, spool = pi
    save(pi, OFF)
    Poster(spool.folder).active()
    assert service(pi, idle=0).run() == 0
    assert os.listdir(spool.requests) == [] and os.listdir(spool.replies) == []


# Stopping


def test_with_the_dashboard_off_it_stops_when_idle(pi):
    save(pi, OFF)
    started = time.monotonic()
    assert service(pi, idle=0.05).run() == 0
    assert time.monotonic() - started < 2


def test_with_the_dashboard_on_it_never_stops_idle(pi):
    save(pi, ON)
    running = Running(service(pi, idle=0.05))
    wait_for(lambda: FakeCollector.made)
    time.sleep(0.3)
    assert running.thread.is_alive()
    save(pi, OFF)  # then idle, it stops of its own accord
    running.thread.join(5)
    assert not running.thread.is_alive() and running.code == 0 and FakeCollector.made[0].stopped


def test_it_answers_a_waiting_request_before_stopping(pi):
    _, spool = pi
    save(pi, OFF)
    home = Place(53.461964, -1.965633, "SK13 1AH", "England", "High Peak")
    runner = SpoolRunner(spool.folder)
    runner.post("a" * 32, "postcode", {"postcode": "SK13 1AH"})
    found = service(pi, idle=0, lookup_postcode=lambda postcode: home)
    replies = read_on_the_way_out(found.worker, runner, ["a" * 32])
    assert found.run() == 0
    assert replies["a" * 32]["value"]["label"] == "SK13 1AH"
    assert os.listdir(spool.replies) == []  # then gone with the service


def test_a_second_service_stops_at_once_and_runs_no_dashboard(pi):
    _, spool = pi
    save(pi, ON)
    with open(spool.folder / WORKER_LOCK, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        started = time.monotonic()
        assert service(pi).run() == 0  # a clean stop, so systemd counts no failure
        assert time.monotonic() - started < 2
    assert FakeCollector.made == [] and not handoff.snapshot_path(spool.folder).exists()


def test_stopped_by_systemd_it_stops_the_pollers_and_lets_go(tmp_path, monkeypatch, pi):
    # systemctl stop or update.sh: SIGTERM. Through the command, as the unit runs it.
    from twobells.cli.main import main

    store, spool = pi
    save(pi, ON)
    monkeypatch.setattr(fetch, "Collector", lambda dash, hub, history, **kwargs: FakeCollector(dash, hub, history))
    monkeypatch.setenv("RUNTIME_DIRECTORY", str(spool.folder))

    class Unhandled(Exception):
        pass

    def unhandled(signum, frame):
        raise Unhandled  # SIGTERM reached the test's own handler: the service didn't take it

    def systemd():
        wait_for(lambda: FakeCollector.made and FakeCollector.made[0].started)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, unhandled)
    thread = threading.Thread(target=systemd)
    try:
        thread.start()
        try:
            assert main(["fetch", "--config", str(store.config_path), "--secrets", str(tmp_path / "none.env"),
                         "--state-dir", str(store.folder)]) == 0
        except Unhandled:
            pytest.fail("the fetch service didn't handle SIGTERM")
        thread.join(5)
        assert signal.getsignal(signal.SIGTERM) is unhandled  # put back as it was
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert FakeCollector.made[0].stopped and lock_free(store)
    assert handoff.snapshot_path(spool.folder).exists()  # kept, going stale: the view is still on


def test_the_bus_company_search_reads_as_much_of_each_timetable_file_as_a_pi_can(pi, monkeypatch):
    from twobells.setup import discover
    from twobells.timetable import routesetup

    made = []
    monkeypatch.setattr(discover, "SetupJob", lambda *args, **options: made.append(options))
    service(pi).worker._make_job("folder", {"A": "1000DGHS0900"}, "k", routes=False, walk_minutes={}, nice=5)
    assert made[0]["limits"] == routesetup.document_limits(PI)


def test_a_setup_search_sets_the_dashboard_s_timetable_aside_while_it_runs(pi, monkeypatch):
    # It builds a timetable here, beside the dashboard's pollers: the one they
    # read isn't held beside it (tests/test_deploy.py counts on it), even by
    # pollers started again meanwhile.
    from twobells.setup import discover

    made = []
    monkeypatch.setattr(discover, "SetupJob", lambda *args, **options: made.append(options))
    save(pi, ON)
    running = Running(service(pi))
    try:
        wait_for(lambda: FakeCollector.made and FakeCollector.made[0].started)
        first = FakeCollector.made[0]
        assert first.aside == []
        running.service.worker._make_job("folder", {"A": "1000DGHS0900"}, "k", routes=False, walk_minutes={},
                                         nice=5)
        with made[0]["while_running"]():
            assert first.aside == [True]
            save(pi, ON + "# changed\n")
            wait_for(lambda: len(FakeCollector.made) == 2 and FakeCollector.made[1].started)
            assert FakeCollector.made[1].aside == [True]
        assert FakeCollector.made[1].aside == [True, False]
    finally:
        running.stop()
