"""The dashboard's snapshot handed from the fetch service to the web server
on a Pi, through a file in the spool: written whole, read as untrusted."""

import json
import os
import threading
import time

import pytest

from twobells.core.walk import WalkTimes
from twobells.dashboard.snapshot import Hub
from twobells.fetch import handoff
from twobells.fetch.handoff import MAX_SNAPSHOT_BYTES, SnapshotMirror, SnapshotWriter
from twobells.settings import show
from twobells.setup.spool.checks import check_request
from twobells.setup.spool.folder import Spool
from twobells.setup.spool.runner import Poster

BUS_ID = "4b7e000000000001"
OLD_ID = "4b7e000000000002"
HISTORY = {"mode": "bus", "route": "A|341|Hyde", "scheduled": "07:20", "label": "07:20 341 to Hyde",
           "day_type": "weekday", "phrase": "Late 4 of the last 10 weekdays (avg 6 min)"}
AREA = {"stops": [], "vehicles": [{"lat": 53.44, "lon": -1.95, "line": "341"}]}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def spool(tmp_path):
    found = Spool(tmp_path / "run")
    found.make()
    return found


def published_hub(bus="Hyde"):
    hub = Hub("Glossop", WalkTimes(train=10, bus={"A": 8}))
    hub.publish("buses", {"rows": [{"id": OLD_ID}]}, {OLD_ID: {"bus": {"id": OLD_ID}}})
    hub.publish("buses", {"rows": [{"id": BUS_ID, "destination": bus}]},
                {BUS_ID: {"bus": {"id": BUS_ID, "destination": bus}, "history": HISTORY}})
    hub.put("area", AREA)
    return hub


def mirror_of(spool, clock=None, poster=None, **options):
    return SnapshotMirror(spool.folder, "Glossop", WalkTimes(train=10, bus={"A": 8}), poster,
                          clock=clock or time.time, **options)


def without_version(body: bytes) -> dict:
    found = json.loads(body)
    found.pop("version")
    return found


def good_file(spool, clock=time.time) -> dict:
    SnapshotWriter(published_hub(), spool, lambda: True, clock=clock).write()
    return json.loads(handoff.snapshot_path(spool.folder).read_bytes())


def put(spool, data=None, raw=None) -> None:
    """Replace the snapshot file as a writer would: whole, 0600."""
    path = handoff.snapshot_path(spool.folder)
    tmp = spool.tmp / "put.tmp"
    tmp.write_bytes(raw if raw is not None else json.dumps(data).encode())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# Writing


def test_the_snapshot_is_written_whole_through_tmp_and_private(spool, monkeypatch):
    moved = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: moved.append((src, dst)) or real_replace(src, dst))
    hub = published_hub()
    writer = SnapshotWriter(hub, spool, lambda: True)
    assert writer.write() == hub.version
    path = handoff.snapshot_path(spool.folder)
    [(src, dst)] = moved
    assert src.parent == spool.tmp and dst == path and os.listdir(spool.tmp) == []
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_bytes())
    assert list(data) == ["v", "run", "made", "version", "snapshot", "lookups", "previous", "area", "healthy"]
    assert (data["v"], data["run"], data["version"], data["healthy"]) == (1, writer.run_id, hub.version, True)


def test_an_oversized_snapshot_is_skipped_and_the_last_one_kept(spool, caplog):
    hub = published_hub()
    SnapshotWriter(hub, spool, lambda: True).write()
    kept = handoff.snapshot_path(spool.folder).read_bytes()
    hub.publish("weather", {"text": "x" * 1000})
    writer = SnapshotWriter(hub, spool, lambda: True, cap=len(kept) + 100)
    writer.write()
    writer.write()
    assert handoff.snapshot_path(spool.folder).read_bytes() == kept
    assert caplog.text.count("can't be handed over (too big)") == 1  # once, not every heartbeat
    assert "xxxx" not in caplog.text


def test_the_writer_writes_on_a_change_and_every_heartbeat_until_the_hub_closes(spool):
    hub = published_hub()
    writer = SnapshotWriter(hub, spool, lambda: True, heartbeat=0.05)
    path = handoff.snapshot_path(spool.folder)
    writer.start()
    try:
        deadline = time.monotonic() + 5
        while not path.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        first = json.loads(path.read_bytes())["made"]
        while json.loads(path.read_bytes())["made"] == first:  # the heartbeat, with nothing changed
            assert time.monotonic() < deadline
            time.sleep(0.01)
        hub.publish("weather", {"rain": "Rain now (90%)"})
        while json.loads(path.read_bytes())["version"] != hub.version:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        hub.close()
        writer.stop(timeout=5)
    assert not writer._thread.is_alive()


def test_unhealthy_pollers_are_said_in_the_file(spool):
    def broken():
        raise RuntimeError("no")

    for healthy, said in ((lambda: False, False), (broken, False), (lambda: True, True)):
        SnapshotWriter(published_hub(), spool, healthy).write()
        assert json.loads(handoff.snapshot_path(spool.folder).read_bytes())["healthy"] is said


def test_removing_the_snapshot(spool):
    good_file(spool)
    handoff.remove(spool.folder)
    assert not handoff.snapshot_path(spool.folder).exists()
    handoff.remove(spool.folder)  # and again, with none there


# Reading


def test_hub_to_file_to_mirror_gives_the_same_snapshot_bar_the_version(spool):
    hub = published_hub()
    SnapshotWriter(hub, spool, lambda: True).write()
    mirror = mirror_of(spool)
    assert mirror.check() is True
    version, body = mirror.current()
    assert body == hub.current()[1].replace(b'"version":%d' % hub.version, b'"version":%d' % version, 1)
    assert json.loads(body)["version"] == version
    assert b"\n" not in body and body.isascii()  # one line of ASCII: safe in an event stream


def test_before_the_first_file_an_empty_snapshot_shows(spool):
    mirror = mirror_of(spool)
    assert mirror.check() is False
    version, body = mirror.current()
    shown = json.loads(body)
    assert version == shown["version"] == 1 and shown["title"] == "Glossop" and shown["buses"] is None
    assert mirror.lookup(BUS_ID) is None and mirror.get("area") is None
    assert mirror.age() == float("inf") and not mirror.healthy()


def test_quiet_comes_through_and_an_older_fetch_service_may_leave_it_out(spool):
    hub = published_hub()
    hub.refresh(quiet=True)
    SnapshotWriter(hub, spool, lambda: True).write()
    mirror = mirror_of(spool)
    assert mirror.check() is True and json.loads(mirror.current()[1])["quiet"] is True
    older = json.loads(handoff.snapshot_path(spool.folder).read_bytes())
    del older["snapshot"]["quiet"]
    older["made"] += 1
    put(spool, older)
    assert mirror.check() is True and "quiet" not in json.loads(mirror.current()[1])


def test_row_strips_come_through_and_an_older_fetch_service_may_leave_them_out(spool):
    hub = Hub("Glossop", WalkTimes(train=10, bus={"A": 8}), row_strips=False)
    SnapshotWriter(hub, spool, lambda: True).write()
    mirror = mirror_of(spool)
    assert mirror.check() is True and json.loads(mirror.current()[1])["row_strips"] is False
    older = json.loads(handoff.snapshot_path(spool.folder).read_bytes())
    del older["snapshot"]["row_strips"]
    older["made"] += 1
    put(spool, older)
    # The page takes a snapshot without them as on.
    assert mirror.check() is True and "row_strips" not in json.loads(mirror.current()[1])


def test_scrolling_comes_through_and_an_older_fetch_service_may_leave_it_out(spool):
    hub = Hub("Glossop", WalkTimes(train=10, bus={"A": 8}), scrolling=False)
    SnapshotWriter(hub, spool, lambda: True).write()
    mirror = mirror_of(spool)
    assert mirror.check() is True and json.loads(mirror.current()[1])["scrolling"] is False
    older = json.loads(handoff.snapshot_path(spool.folder).read_bytes())
    del older["snapshot"]["scrolling"]
    older["made"] += 1
    put(spool, older)
    # The page takes a snapshot without it as on.
    assert mirror.check() is True and "scrolling" not in json.loads(mirror.current()[1])


def test_until_the_first_file_the_mirror_says_whether_text_scrolls(spool):
    assert json.loads(mirror_of(spool).current()[1])["scrolling"] is True
    assert json.loads(mirror_of(spool, scrolling=False).current()[1])["scrolling"] is False


def test_until_the_first_file_the_mirror_says_whether_rows_draw_their_strips(spool):
    assert json.loads(mirror_of(spool).current()[1])["row_strips"] is True
    assert json.loads(mirror_of(spool, row_strips=False).current()[1])["row_strips"] is False


def test_what_the_page_shows_comes_through_and_an_older_fetch_service_may_leave_it_out(spool):
    hub = Hub("Glossop", WalkTimes(train=10, bus={"A": 8}), show={"weather", "next_to_catch"})
    SnapshotWriter(hub, spool, lambda: True).write()
    mirror = mirror_of(spool)
    assert mirror.check() is True and json.loads(mirror.current()[1])["show"] == ["next_to_catch", "weather"]
    older = json.loads(handoff.snapshot_path(spool.folder).read_bytes())
    del older["snapshot"]["show"]
    older["made"] += 1
    put(spool, older)
    # The page takes a snapshot without it as showing everything.
    assert mirror.check() is True and "show" not in json.loads(mirror.current()[1])


def test_until_the_first_file_the_mirror_says_what_the_page_shows(spool):
    assert json.loads(mirror_of(spool).current()[1])["show"] == list(show.keys("dashboard"))
    assert json.loads(mirror_of(spool, show=["announce"]).current()[1])["show"] == ["announce"]


def test_lookups_previous_and_the_area_come_through(spool):
    good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    assert mirror.lookup(BUS_ID) == {"bus": {"id": BUS_ID, "destination": "Hyde"}, "history": HISTORY}
    assert mirror.lookup(OLD_ID) == {"bus": {"id": OLD_ID}}  # from the previous snapshot
    assert mirror.lookup("0" * 16) is None
    assert mirror.get("area") == AREA and mirror.get("other") is None


def test_age_and_health_follow_the_files_made(spool):
    clock = Clock()
    good_file(spool, clock)
    mirror = mirror_of(spool, clock)
    mirror.check()
    clock.now += 179
    assert mirror.age() == 179 and mirror.healthy()
    clock.now += 2
    assert not mirror.healthy()  # the fetch service has gone quiet
    put(spool, {**good_file(spool, clock), "healthy": False})
    mirror.check()
    assert mirror.age() == 0 and not mirror.healthy()  # fresh, but the pollers aren't


def test_the_version_goes_up_across_a_fetch_service_restart(spool):
    clock = Clock()
    mirror = mirror_of(spool, clock)
    first = published_hub()
    first.publish("weather", {"rain": None})
    SnapshotWriter(first, spool, lambda: True, clock=clock).write()
    mirror.check()
    seen = mirror.current()[0]
    clock.now += 1
    again = SnapshotWriter(first, spool, lambda: True, clock=clock)
    again.write()  # a new run of the same hub: a new version
    mirror.check()
    assert mirror.current()[0] == seen + 1
    restarted = published_hub()  # a lower hub version than the first's
    assert restarted.version < first.version
    clock.now += 1
    SnapshotWriter(restarted, spool, lambda: True, clock=clock).write()
    mirror.check()
    assert mirror.current()[0] == seen + 2


def test_a_heartbeat_with_nothing_new_keeps_the_version_but_its_time(spool):
    clock = Clock()
    hub = published_hub()
    writer = SnapshotWriter(hub, spool, lambda: True, clock=clock)
    writer.write()
    mirror = mirror_of(spool, clock)
    mirror.check()
    version, body = mirror.current()
    clock.now += 30
    hub.refresh()
    writer.write()
    assert mirror.check() is True
    assert mirror.current()[0] == version and mirror.age() == 0
    assert without_version(mirror.current()[1]) == without_version(hub.current()[1])


def test_wait_wakes_on_a_new_file(spool):
    good_file(spool)
    mirror = mirror_of(spool, every=0.01)
    mirror.start()
    try:
        version = mirror.current()[0]
        assert mirror.wait(version, 0.05) == version  # nothing new
        hub = published_hub("Ashton")
        threading.Timer(0.05, lambda: SnapshotWriter(hub, spool, lambda: True).write()).start()
        started = time.monotonic()
        assert mirror.wait(version, 5) == version + 1
        assert time.monotonic() - started < 4
        assert json.loads(mirror.current()[1])["buses"]["rows"][0]["destination"] == "Ashton"
    finally:
        mirror.close()
    assert mirror.closed and mirror.wait(version + 1, 5) == version + 1  # closed: no waiting


# Refused files


def _with(data, **change):
    return {**data, **change}


def _deep(depth):
    value = 1
    for _ in range(depth):
        value = [value]
    return value


REFUSED = {
    "unknown fields": lambda d: _with(d, extra=1),
    "missing fields": lambda d: {k: v for k, v in d.items() if k != "healthy"},
    "wrong v": lambda d: _with(d, v=2),
    "v true": lambda d: _with(d, v=True),
    "run not hex": lambda d: _with(d, run="ZZZZZZZZZZZZZZZZ"),
    "run too long": lambda d: _with(d, run="a" * 17),
    "made in the future": lambda d: _with(d, made=d["made"] + 60),
    "made not a number": lambda d: _with(d, made="now"),
    "version not whole": lambda d: _with(d, version=1.5),
    "snapshot not an object": lambda d: _with(d, snapshot=[]),
    "snapshot with more": lambda d: _with(d, snapshot={**d["snapshot"], "script": "x"}),
    "quiet not true or false": lambda d: _with(d, snapshot={**d["snapshot"], "quiet": "yes"}),
    "row_strips not true or false": lambda d: _with(d, snapshot={**d["snapshot"], "row_strips": 0}),
    "row_strips null": lambda d: _with(d, snapshot={**d["snapshot"], "row_strips": None}),
    "scrolling not true or false": lambda d: _with(d, snapshot={**d["snapshot"], "scrolling": "off"}),
    "show not a list": lambda d: _with(d, snapshot={**d["snapshot"], "show": "weather"}),
    "show naming what the page has no item for": lambda d: _with(d, snapshot={**d["snapshot"], "show": ["clock_page"]}),
    "show naming one twice": lambda d: _with(d, snapshot={**d["snapshot"], "show": ["weather", "weather"]}),
    "lookup id in the wrong form": lambda d: _with(d, lookups={"../../etc/passwd": {}}),
    "lookup id in capitals": lambda d: _with(d, lookups={BUS_ID.upper(): {}}),
    "previous id too long": lambda d: _with(d, previous={BUS_ID + "0": {}}),
    "a lookup not an object": lambda d: _with(d, lookups={BUS_ID: "x"}),
    "a lookup with more": lambda d: _with(d, lookups={BUS_ID: {**d["lookups"][BUS_ID], "script": {}}}),
    "a bus map not an object": lambda d: _with(d, lookups={BUS_ID: {"bus": "x"}}),
    "a history short of fields": lambda d: _with(d, lookups={BUS_ID: {"history": {"mode": "bus"}}}),
    "a history route not text": lambda d: _with(d, previous={BUS_ID: {"history": {**HISTORY, "route": 3}}}),
    "a history mode null": lambda d: _with(d, lookups={BUS_ID: {"history": {**HISTORY, "mode": None}}}),
    "too many lookups": lambda d: _with(d, lookups={f"{n:016x}": {} for n in range(handoff.MAX_LOOKUPS + 1)}),
    "area not an object": lambda d: _with(d, area=[1]),
    "healthy not true or false": lambda d: _with(d, healthy=1),
    "too deep": lambda d: _with(d, area={"x": _deep(handoff.MAX_DEPTH)}),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_file_that_doesnt_fit_is_refused_and_the_last_good_one_stays(spool, caplog, name):
    clock = Clock()
    good = good_file(spool, clock)
    mirror = mirror_of(spool, clock)
    mirror.check()
    kept = mirror.current()
    bad = REFUSED[name](json.loads(json.dumps(good)))
    bad["snapshot"] = bad["snapshot"] if isinstance(bad["snapshot"], list) else {**bad["snapshot"], "title": "SECRET"}
    put(spool, bad)
    clock.now += 10
    assert mirror.check() is False
    assert mirror.current() == kept and mirror.lookup(BUS_ID) is not None and mirror.age() == 10
    assert "Refused the fetch service's snapshot" in caplog.text and "SECRET" not in caplog.text


@pytest.mark.parametrize("raw", [
    b'{"v": 1, "made": NaN}',
    b'{"v": 1, "made": Infinity}',
    b"\xff\xfe not utf-8",
    b"[" * 2000 + b"]" * 2000,
    b"not json",
])
def test_a_file_that_isnt_strict_json_is_refused(spool, caplog, raw):
    good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    put(spool, raw=raw)
    assert mirror.check() is False and mirror.current() == kept
    assert "Refused" in caplog.text


def test_nan_in_a_whole_file_is_refused(spool):
    good = good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    put(spool, raw=json.dumps(good).replace(f'"made": {json.dumps(good["made"])}', '"made": NaN').encode())
    assert b"NaN" in handoff.snapshot_path(spool.folder).read_bytes()
    assert mirror.check() is False and mirror.current() == kept


def test_a_link_is_never_followed(spool, tmp_path):
    good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({**good_file(spool), "run": "b" * 16}))
    path = handoff.snapshot_path(spool.folder)
    path.unlink()
    path.symlink_to(elsewhere)
    assert mirror.check() is False and mirror.current() == kept


def test_a_hard_link_is_refused(spool, tmp_path):
    good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    put(spool, {**good_file(spool), "run": "b" * 16})
    os.link(handoff.snapshot_path(spool.folder), tmp_path / "second-name.json")  # nlink 2
    assert mirror.check() is False and mirror.current() == kept


def test_a_file_of_another_owner_is_refused(spool, monkeypatch):
    good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    put(spool, {**good_file(spool), "run": "b" * 16})
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 1)
    assert mirror.check() is False
    monkeypatch.setattr(os, "geteuid", lambda: real)
    assert mirror.current() == kept


def test_a_file_over_the_cap_is_refused_unread(spool):
    good = good_file(spool)
    mirror = mirror_of(spool)
    mirror.check()
    kept = mirror.current()
    body = json.dumps({**good, "run": "b" * 16}).encode()
    put(spool, raw=body + b" " * (MAX_SNAPSHOT_BYTES + 1 - len(body)))
    assert handoff.snapshot_path(spool.folder).stat().st_size == MAX_SNAPSHOT_BYTES + 1
    assert mirror.check() is False and mirror.current() == kept


def test_a_refused_file_is_logged_once_and_a_good_one_after_it_is_taken(spool, caplog):
    good = good_file(spool)
    mirror = mirror_of(spool)
    put(spool, {**good, "v": 2})
    assert [mirror.check(), mirror.check()] == [False, False]
    assert caplog.text.count("Refused") == 1  # the same file isn't read again
    put(spool, good)
    assert mirror.check() is True and mirror.lookup(BUS_ID) is not None


def test_a_missing_file_keeps_the_last_one_going_stale(spool):
    clock = Clock()
    good_file(spool, clock)
    mirror = mirror_of(spool, clock)
    mirror.check()
    kept = mirror.current()
    handoff.remove(spool.folder)
    clock.now += 200
    assert mirror.check() is False and mirror.current() == kept and not mirror.healthy()


# Telling the fetch service a browser is there


def test_touch_posts_at_most_one_active_a_minute(spool):
    monotonic = Clock(1000.0)
    poster = Poster(spool.folder, monotonic=monotonic)
    mirror = mirror_of(spool, poster=poster, monotonic=monotonic)
    for _ in range(5):
        mirror.touch()
    assert len(os.listdir(spool.requests)) == 1 and mirror.active(600)
    monotonic.now += 59
    mirror.touch()
    assert len(os.listdir(spool.requests)) == 1
    monotonic.now += 1
    mirror.touch()
    names = os.listdir(spool.requests)
    assert len(names) == 2
    for name in names:
        kind, args = check_request(json.loads((spool.requests / name).read_bytes()), name[:-5], time.time())
        assert (kind, args) == ("active", {})


def test_a_missing_or_stale_snapshot_wakes_the_fetch_service_while_the_dashboard_is_wanted(spool):
    clock, monotonic = Clock(), Clock(1000.0)
    wanted = [False]
    asked = []

    def is_wanted():
        asked.append(True)
        return wanted[0]

    poster = Poster(spool.folder, monotonic=monotonic, every=0)
    mirror = mirror_of(spool, clock, poster=poster, wanted=is_wanted, monotonic=monotonic)
    mirror.wake_if_stale()
    assert os.listdir(spool.requests) == [] and len(asked) == 1  # not wanted: the dashboard view is off
    wanted[0] = True
    mirror.wake_if_stale()
    assert len(asked) == 1  # the settings are read at most once a minute
    monotonic.now += handoff.ACTIVE_EVERY_S
    mirror.wake_if_stale()
    assert len(os.listdir(spool.requests)) == 1  # missing, and wanted: started
    for name in os.listdir(spool.requests):
        os.unlink(spool.requests / name)
    good_file(spool, clock)
    mirror.check()
    monotonic.now += handoff.ACTIVE_EVERY_S
    clock.now += handoff.STALE_S
    mirror.wake_if_stale()
    assert os.listdir(spool.requests) == []  # fresh enough
    clock.now += 1
    mirror.wake_if_stale()
    assert len(os.listdir(spool.requests)) == 1  # stale: the fetch service may have stopped


# End to end: the dashboard's event stream, served from a mirror


def test_the_event_stream_through_the_server_from_a_mirror(spool, tmp_path):
    from tests.web.test_server import Running, make_app, read_block

    static = tmp_path / "static"
    static.mkdir()
    good_file(spool)
    mirror = mirror_of(spool, every=0.01, poster=Poster(spool.folder))
    mirror.start()
    running = Running(make_app(static, hub=mirror))
    try:
        response, body = running.request("/api/snapshot")
        version, current = mirror.current()
        assert response.status == 200 and body == current and response.getheader("ETag") == f'"v{version}"'
        response, body = running.request(f"/api/bus/{BUS_ID}")
        assert json.loads(body)["destination"] == "Hyde"
        response, body = running.request("/api/area")
        assert json.loads(body) == AREA
        conn, response = running.stream()
        try:
            assert read_block(response) == ["retry: 5000"]
            assert read_block(response) == [f"id: {version}", "event: snapshot", f"data: {current.decode()}"]
            SnapshotWriter(published_hub("Ashton"), spool, lambda: True).write()  # the fetch service started again
            block = read_block(response)
            while block == [": ping"]:
                block = read_block(response)
            assert block[:2] == [f"id: {version + 1}", "event: snapshot"]
            sent = json.loads(block[2][len("data: "):])
            assert sent["version"] == version + 1 and sent["buses"]["rows"][0]["destination"] == "Ashton"
        finally:
            conn.close()
        assert len(os.listdir(spool.requests)) == 1  # the browser was said to the fetch service, once
    finally:
        running.close()
