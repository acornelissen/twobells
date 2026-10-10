import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from departureboard.bodsbus import BusBoard
from departureboard.collector import Collector, Poller
from departureboard.dashconfig import DashboardOptions, DashboardSettings
from departureboard.darwin import DetailsError, DetailsUnavailable, ServiceGone, parse_board_rich, parse_service_details
from departureboard.history import Stats
from departureboard.livebus import live_board
from departureboard.snapshot import Hub
from departureboard.walk import WalkTimes
from departureboard.weather import parse_conditions

from test_livebus import HALFWAY, HENRY_A, at, bus, schedule

LONDON = ZoneInfo("Europe/London")
FIXTURES = Path(__file__).parent / "fixtures"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Monotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def glossop_board():
    return json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())


class Trains:
    def __init__(self):
        self.error = None
        self.calls = 0
        self.payload = glossop_board()

    def fetch_board(self):
        self.calls += 1
        if self.error:
            raise self.error
        return parse_board_rich(self.payload)


# The board's trains, in order: 22:08, 22:25, 22:38, 22:55, 23:10.
IDS = ["9000001GLSP____", "9000002GLSP____", "9000003GLSP____", "9000004GLSP____", "9000005GLSP____"]


class Details:
    """Service details for any train, from the synthetic answer for the 22:55."""

    def __init__(self):
        self.asked = []
        self.errors = {}  # service ID -> exception, or "*" for every train

    def details(self, service_id):
        self.asked.append(service_id)
        error = self.errors.get(service_id) or self.errors.get("*")
        if error:
            raise error
        return parse_service_details(json.loads((FIXTURES / "ldbws_service_details_from_hadfield.json").read_text()))


class Buses:
    def __init__(self, clock, reason=None):
        self.clock = clock
        self.reason = reason
        self.error = None
        self.vehicles_ok = True
        self.refreshes = []
        self.on_refresh = None

    def unusable(self):
        return self.reason

    def fetch_live(self, rows=None):
        if self.error:
            raise self.error
        vehicles = [bus(*HALFWAY, at(10, 14))] if self.vehicles_ok else []
        note = None if self.vehicles_ok else "Timetabled times only; live positions failed: down"
        found, placed = live_board(schedule(), self.clock(), vehicles, lambda journey, day: True, 10)
        return BusBoard(found, schedule(), self.vehicles_ok, note, placed, vehicles)

    def refresh_timetable(self, force=False):
        self.refreshes.append(force)
        if self.on_refresh:
            self.on_refresh()


class Weather:
    def __init__(self):
        self.error = None

    def fetch_conditions(self):
        if self.error:
            raise self.error
        return parse_conditions(json.loads((FIXTURES / "openmeteo_conditions.json").read_text()))


class FakeHistory:
    def __init__(self):
        self.observed = []
        self.swept = []
        self.purged = []

    def observe(self, mode, rows, polled_at):
        self.observed.append((mode, rows, polled_at))

    def stats(self, mode, route, scheduled, day_type, *, n=10, late_minutes, holidays=frozenset()):
        self.late_minutes = late_minutes
        return Stats(days=10, late=4, cancelled=0, unknown=0, on_time=6, avg_late=6.2, worst=9)

    no_shows_error = None

    def no_shows(self, mode, route, scheduled, day_type, n=10, *, holidays=frozenset()):
        if self.no_shows_error:
            raise self.no_shows_error
        return (3, 10) if (mode, route) == ("bus", "A|237|Ashton") else (0, 10)

    def sweep(self, now):
        self.swept.append(now)

    def purge(self, now, days=90):
        self.purged.append((now, days))


class Setup:
    """A collector over fake sources, with its clock, hub and history."""

    def __init__(self, tmp_path, now=None, train=True, buses=True, weather=True, reason=None, options=None,
                 details=False, division="england-and-wales"):
        self.clock = Clock(now or at(10, 14))
        self.monotonic = Monotonic()
        self.trains = Trains() if train else None
        self.buses = Buses(self.clock, reason) if buses else None
        self.upkeep_buses = self.buses  # the same fake stands for both instances
        self.weather = Weather() if weather else None
        self.details = Details() if details else None
        self.settings = DashboardSettings(
            title="Glossop", timezone="Europe/London", state_dir=tmp_path, walk=WalkTimes(train=10, bus={"A": 8}),
            options=options or DashboardOptions(), train=self.trains, train_crs="GLO", train_filter="MAN",
            bus=self.buses, bus_upkeep=self.upkeep_buses, bus_stops={"A": HENRY_A}, weather=self.weather,
            train_details=self.details, bank_holiday_division=division,
        )
        self.hub = Hub("Glossop", self.settings.walk, clock=self.clock, monotonic=self.monotonic)
        self.history = FakeHistory()
        self.holiday_calls = []
        self.collector = Collector(self.settings, self.hub, self.history, clock=self.clock,
                                   fetch_holidays=self._holidays, random=lambda: 0.5, monotonic=self.monotonic)

    def _holidays(self, folder, now, division):
        self.holiday_calls.append((folder, now, division))
        return frozenset()

    def snapshot(self):
        return json.loads(self.hub.current()[1])


# Pollers


def test_a_poller_waits_its_interval_with_jitter():
    poller = Poller("x", lambda: None, 60, random=lambda: 0.5)
    assert poller.step() == 60
    assert Poller("x", lambda: None, 60, random=lambda: 0.0).step() == pytest.approx(54)
    assert Poller("x", lambda: None, 60, random=lambda: 1.0).step() == pytest.approx(66)


def test_a_failing_job_backs_off_up_to_five_minutes_then_recovers():
    outcomes = iter([RuntimeError("a"), RuntimeError("b"), RuntimeError("c"), RuntimeError("d"), None])

    def job():
        error = next(outcomes)
        if error:
            raise error

    poller = Poller("x", job, 30, random=lambda: 0.5)
    assert [poller.step() for _ in range(4)] == [60, 120, 240, 300]
    assert poller.failures == 4
    assert poller.step() == 30 and poller.failures == 0


def test_a_slow_job_backs_off_no_further_than_five_minutes():
    def job():
        raise RuntimeError("down")

    assert Poller("weather", job, 1800, random=lambda: 0.5).step() == 300


def test_a_quiet_poller_does_not_run_its_job():
    calls = []
    poller = Poller("x", lambda: calls.append(1), 60, quiet=lambda: True, random=lambda: 0.5)
    assert poller.step() == 5.0
    assert calls == []


def test_stop_ends_the_thread_at_once():
    poller = Poller("x", lambda: None, 3600)
    poller.start()
    assert poller.alive
    started = time.monotonic()
    poller.stop()
    assert poller.join(2)
    assert time.monotonic() - started < 1


# Trains


def test_trains_are_published_and_observed(tmp_path):
    setup = Setup(tmp_path, now=datetime(2026, 10, 9, 22, 3, tzinfo=LONDON))
    setup.collector.poll_trains()
    trains = setup.snapshot()["trains"]
    assert trains["station"] == "Glossop" and trains["filter"] == "MAN"
    assert len(trains["rows"]) == 5
    assert trains["status"] == {"last_success": "2026-10-09T22:03:00+01:00", "stale": False, "error": None,
                                "note": None}
    mode, rows, polled_at = setup.history.observed[0]
    assert mode == "train" and polled_at == datetime(2026, 10, 9, 22, 3)
    assert rows[0].route == "GLO>MAN|Hadfield"
    assert trains["rows"][0]["history"]["phrase"] == "Late 4 of the last 10 weekdays (avg 6 min)"
    assert setup.history.late_minutes == 3


def test_an_error_keeps_the_last_trains_and_marks_them_stale(tmp_path):
    setup = Setup(tmp_path, now=datetime(2026, 10, 9, 22, 3, tzinfo=LONDON))
    setup.collector.poll_trains()
    good = setup.snapshot()["trains"]["rows"]
    setup.clock.now += timedelta(minutes=1)
    setup.trains.error = RuntimeError("HTTP 500 for https://api.example/board?x-apikey=SECRET123")
    with pytest.raises(RuntimeError):
        setup.collector.poll_trains()
    trains = setup.snapshot()["trains"]
    assert trains["rows"] == good
    assert trains["status"]["stale"] is True
    assert trains["status"]["last_success"] == "2026-10-09T22:03:00+01:00"
    assert "SECRET123" not in setup.hub.current()[1].decode()
    assert trains["status"]["error"].startswith("HTTP 500")
    assert len(setup.history.observed) == 1  # a failed poll decides nothing

    setup.trains.error = None
    setup.collector.poll_trains()
    assert setup.snapshot()["trains"]["status"]["stale"] is False


def test_an_error_before_any_data_is_stale_with_no_rows(tmp_path):
    setup = Setup(tmp_path)
    setup.trains.error = RuntimeError("down")
    with pytest.raises(RuntimeError):
        setup.collector.poll_trains()
    trains = setup.snapshot()["trains"]
    assert trains["rows"] == [] and trains["status"]["stale"] and trains["status"]["error"] == "down"


# Train positions

TONIGHT = datetime(2026, 10, 9, 22, 3, tzinfo=LONDON)


def positions(setup):
    return [row["position"] for row in setup.snapshot()["trains"]["rows"]]


def test_without_a_details_client_trains_have_no_position(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT)
    setup.collector.poll_trains()
    rows = setup.snapshot()["trains"]["rows"]
    assert rows and all(r["position"] is None and r["previous_points"] == [] for r in rows)


def test_the_first_three_trains_are_placed(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3]
    found = positions(setup)
    assert [p is not None for p in found] == [True, True, True, False, False]
    assert found[0]["where"] == "At Glossop now"
    rows = setup.snapshot()["trains"]["rows"]
    assert rows[0]["previous_points"][0]["name"] == "Hadfield" and rows[3]["previous_points"] == []


def test_cancelled_trains_are_not_counted_in_the_three(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.trains.payload["trainServices"][1].update(etd="Cancelled", isCancelled=True)
    setup.collector.poll_trains()
    assert setup.details.asked == [IDS[0], IDS[2], IDS[3]]
    assert [p is not None for p in positions(setup)] == [True, False, True, True, False]


def test_details_are_kept_for_a_minute(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    setup.monotonic.now += 59
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3]
    setup.monotonic.now += 2
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3] * 2


def test_details_are_forgotten_when_a_train_leaves_the_board(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    first = setup.trains.payload["trainServices"].pop(0)
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3] + [IDS[3]]  # the new third train
    # Back again within the minute (a board that flickered): asked for afresh.
    setup.trains.payload["trainServices"].insert(0, first)
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3] + [IDS[3], IDS[0]]


def test_a_train_that_becomes_cancelled_loses_its_position(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    setup.trains.payload["trainServices"][0].update(etd="Cancelled", isCancelled=True)
    setup.collector.poll_trains()
    assert positions(setup)[0] is None
    assert setup.snapshot()["trains"]["rows"][0]["previous_points"] == []


def test_a_refused_key_turns_details_off_for_six_hours(tmp_path, caplog):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    assert positions(setup)[0] is not None
    setup.monotonic.now += 61
    setup.details.errors["*"] = DetailsUnavailable("Service details refused the key (HTTP 403)")
    setup.details.asked.clear()
    with caplog.at_level("WARNING", logger="departureboard.collector"):
        setup.collector.poll_trains()  # the board itself still works
        setup.monotonic.now += 3600
        setup.collector.poll_trains()
    assert setup.details.asked == [IDS[0]]  # one try, then no more
    assert all(p is None for p in positions(setup))
    assert setup.snapshot()["trains"]["status"]["error"] is None
    warnings = [r.getMessage() for r in caplog.records if "positions" in r.getMessage()]
    assert warnings == ["dashboard train positions off for 6 hours: Service details refused the key (HTTP 403)"]

    setup.monotonic.now += 5 * 3600 + 1  # six hours on
    setup.details.errors.clear()
    setup.collector.poll_trains()
    assert setup.details.asked == [IDS[0]] + IDS[:3]
    assert positions(setup)[0] is not None


def test_other_failures_skip_that_train_this_round(tmp_path, caplog):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.details.errors[IDS[1]] = DetailsError("HTTP Error 500: Internal Server Error")
    with caplog.at_level("WARNING", logger="departureboard.collector"):
        setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3]
    assert [p is not None for p in positions(setup)[:3]] == [True, False, True]
    assert any("skipped: HTTP Error 500" in r.getMessage() for r in caplog.records)
    # Asked again on the next poll, not switched off.
    setup.details.errors.clear()
    setup.collector.poll_trains()
    assert setup.details.asked == IDS[:3] + [IDS[1]]
    assert positions(setup)[1] is not None


def test_a_failure_keeps_what_was_known_and_never_logs_a_key(tmp_path, caplog):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    setup.monotonic.now += 61
    setup.details.errors["*"] = RuntimeError("timed out: x-apikey=SECRET123")
    with caplog.at_level("WARNING", logger="departureboard.collector"):
        setup.collector.poll_trains()
    assert positions(setup)[0] is not None
    assert "SECRET123" not in caplog.text and "SECRET123" not in setup.hub.current()[1].decode()


def test_a_train_darwin_no_longer_knows_loses_its_details(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.collector.poll_trains()
    setup.monotonic.now += 61
    setup.details.errors[IDS[0]] = ServiceGone("Darwin no longer knows this train")
    setup.collector.poll_trains()
    assert positions(setup)[0] is None and positions(setup)[1] is not None


def test_no_details_are_asked_for_when_the_board_fails(tmp_path):
    setup = Setup(tmp_path, now=TONIGHT, details=True)
    setup.trains.error = RuntimeError("down")
    with pytest.raises(RuntimeError):
        setup.collector.poll_trains()
    assert setup.details.asked == []


# Buses


def test_buses_are_published_and_observed(tmp_path):
    setup = Setup(tmp_path)
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"]["state"] == "ready" and buses["stops"] == {"A": "Henry Street"}
    row = next(r for r in buses["rows"] if r["line"] == "341")
    assert row["live"] and row["expected"] == "10:19" and row["leave_at"] == "2026-10-09T10:11:00+01:00"
    mode, rows, _ = setup.history.observed[0]
    assert mode == "bus" and {o.route for o in rows} >= {"A|341|Hadfield"}
    assert setup.history.late_minutes == 2
    lookup = setup.hub.lookup(row["id"])
    assert lookup["bus"]["vehicle"]["lat"] == HALFWAY[0]


def test_bus_rows_say_how_often_they_had_no_tracker(tmp_path):
    setup = Setup(tmp_path)
    setup.collector.poll_buses()
    rows = setup.snapshot()["buses"]["rows"]
    assert by_line(rows, "237")["history"]["no_show_phrase"] == "No tracker on 3 of the last 10 weekdays"
    assert by_line(rows, "341")["history"]["no_show_phrase"] is None  # always tracked: nothing to say
    assert all("ghost" in r and "arrives" in r and "inbound" in r for r in rows)


def test_an_unreadable_history_costs_only_the_no_show_phrase(tmp_path, caplog):
    setup = Setup(tmp_path)
    setup.history.no_shows_error = OSError("disk I/O error")
    setup.collector.poll_buses()
    rows = setup.snapshot()["buses"]["rows"]
    assert rows and all(r["history"]["no_show_phrase"] is None for r in rows)
    assert "history unreadable" in caplog.text


def by_line(rows, line):
    return next(r for r in rows if r["line"] == line)


def test_buses_without_live_positions_show_but_are_not_observed(tmp_path):
    setup = Setup(tmp_path)
    setup.buses.vehicles_ok = False
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["status"]["note"].startswith("Timetabled times only")
    assert buses["status"]["stale"] is False and buses["rows"]
    assert setup.history.observed == []


def test_an_error_keeps_the_last_buses(tmp_path):
    setup = Setup(tmp_path)
    setup.collector.poll_buses()
    setup.buses.error = OSError("timed out")
    with pytest.raises(OSError):
        setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["rows"] and buses["status"]["stale"] and buses["status"]["error"] == "timed out"
    assert buses["timetable"]["state"] == "ready"


def test_the_timetable_is_building_until_upkeep_has_tried(tmp_path):
    setup = Setup(tmp_path, reason="there is no bus timetable yet")
    assert setup.snapshot()["buses"]["timetable"]["state"] == "building"
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"]["state"] == "building" and buses["rows"] == []
    assert buses["status"]["note"].startswith("Building the bus timetable")
    assert buses["status"]["error"] is None


def test_the_timetable_is_building_during_the_first_build(tmp_path):
    setup = Setup(tmp_path, reason="there is no bus timetable yet")
    seen = []

    def meanwhile():
        setup.collector.poll_buses()
        seen.append(setup.snapshot()["buses"]["timetable"]["state"])

    setup.upkeep_buses.on_refresh = meanwhile
    setup.collector.upkeep()
    assert seen == ["building"]


def test_the_timetable_is_missing_after_a_failed_build(tmp_path):
    setup = Setup(tmp_path, reason="there is no bus timetable yet")
    setup.collector.upkeep()  # the fake builds nothing
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"] == {"state": "missing", "built": None, "valid_until": None, "journeys": None}
    assert buses["status"]["note"] == "There is no bus timetable yet"


def test_buses_show_once_the_timetable_is_built(tmp_path):
    setup = Setup(tmp_path, reason="there is no bus timetable yet")
    setup.upkeep_buses.on_refresh = lambda: setattr(setup.buses, "reason", None)
    setup.collector.upkeep()
    setup.collector.poll_buses()
    assert setup.snapshot()["buses"]["timetable"]["state"] == "ready"


# Weather


def test_weather_is_published_and_kept_on_error(tmp_path):
    setup = Setup(tmp_path, now=datetime(2026, 10, 9, 8, 20, tzinfo=LONDON))
    setup.collector.poll_weather()
    weather = setup.snapshot()["weather"]
    assert weather["now"]["text"] == "Light rain" and weather["hours"]
    setup.weather.error = ValueError("Forecast is not JSON")
    with pytest.raises(ValueError):
        setup.collector.poll_weather()
    stale = setup.snapshot()["weather"]
    assert stale["now"] == weather["now"] and stale["status"]["stale"]


def test_sections_not_configured_stay_null(tmp_path):
    setup = Setup(tmp_path, train=False, buses=False)
    body = setup.snapshot()
    assert body["trains"] is None and body["buses"] is None and body["weather"] is not None
    assert set(setup.collector.pollers()) == {"weather", "upkeep", "heartbeat"}


# Upkeep


def test_upkeep_refreshes_holidays_timetable_and_history(tmp_path):
    setup = Setup(tmp_path, options=DashboardOptions(history_days=30))
    setup.collector.upkeep()
    assert setup.holiday_calls == [(tmp_path, datetime(2026, 10, 9, 10, 14), "england-and-wales")]
    assert setup.upkeep_buses.refreshes == [False]  # never forced: rebuilt only when due
    assert setup.history.swept == [datetime(2026, 10, 9, 10, 14)]
    assert setup.history.purged == [(datetime(2026, 10, 9, 10, 14), 30)]


def test_upkeep_asks_for_the_configured_bank_holiday_division(tmp_path):
    setup = Setup(tmp_path, division="scotland")
    setup.collector.upkeep()
    assert [call[2] for call in setup.holiday_calls] == ["scotland"]


def test_upkeep_runs_at_start_then_hourly_even_when_quiet(tmp_path):
    setup = Setup(tmp_path, now=at(3, 0))
    upkeep = setup.collector.pollers()["upkeep"]
    assert upkeep.step() == 3600
    assert setup.upkeep_buses.refreshes == [False]


def test_upkeep_carries_on_after_a_failure_and_reports_it(tmp_path):
    setup = Setup(tmp_path)

    def broken():
        raise RuntimeError("BODS down")

    setup.upkeep_buses.on_refresh = broken
    with pytest.raises(RuntimeError, match="BODS down"):
        setup.collector.upkeep()
    assert setup.history.swept  # the history was still looked after


def _not_building(setup):
    return Collector(setup.settings, setup.hub, setup.history, clock=setup.clock, fetch_holidays=setup._holidays,
                     random=lambda: 0.5, monotonic=setup.monotonic, build_timetable=False)


def test_upkeep_without_build_timetable_never_downloads_one(tmp_path):
    # A Pi's fetch service: its timer and the setup search build the timetable.
    setup = Setup(tmp_path, reason="there is no bus timetable yet", options=DashboardOptions(history_days=30))
    collector = _not_building(setup)
    collector.upkeep()
    assert collector.pollers()["upkeep"].step() == 3600
    assert setup.upkeep_buses.refreshes == []
    assert len(setup.holiday_calls) == 2 and len(setup.history.swept) == 2  # the rest of upkeep still runs
    collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"]["state"] == "missing"  # not "building": nothing here will build it
    assert buses["status"]["note"] == "There is no bus timetable yet"


def test_upkeep_without_build_timetable_shows_the_timetable_others_build(tmp_path):
    setup = Setup(tmp_path, reason="there is no bus timetable yet")
    collector = _not_building(setup)
    setup.buses.reason = None  # the timer saved one
    collector.poll_buses()
    assert setup.snapshot()["buses"]["timetable"]["state"] == "ready"


def test_upkeep_skips_the_timetable_while_another_build_runs(tmp_path, caplog):
    from departureboard.timetable import TimetableBusy

    setup = Setup(tmp_path, reason="there is no bus timetable yet")

    def busy():
        raise TimetableBusy("Another bus timetable build is running")

    setup.upkeep_buses.on_refresh = busy
    with caplog.at_level("INFO"):
        setup.collector.upkeep()  # not a failure to back off from
    assert "another bus timetable build is running" in caplog.text
    assert setup.history.swept
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"]["state"] == "building"  # until the other build saves it
    assert buses["status"]["note"].startswith("Building the bus timetable")


# Quiet windows


def test_quiet_windows_stop_polling_unless_a_browser_is_about(tmp_path):
    setup = Setup(tmp_path, now=at(3, 0))
    trains = setup.collector.pollers()["trains"]
    assert setup.collector.quiet()
    assert trains.step() == 5.0 and setup.trains.calls == 0

    setup.hub.touch()
    assert not setup.collector.quiet()
    trains.step()
    assert setup.trains.calls == 1

    setup.monotonic.now += 601
    assert setup.collector.quiet()


def test_outside_the_quiet_windows_polling_goes_on(tmp_path):
    setup = Setup(tmp_path, now=at(5, 1))
    assert not setup.collector.quiet()


def test_quiet_windows_come_from_the_options(tmp_path):
    from departureboard.schedule import parse_window

    setup = Setup(tmp_path, now=at(12, 0), options=DashboardOptions(quiet_windows=(parse_window("Fri 11:00-13:00"),)))
    assert setup.collector.quiet()


# Running


def test_start_stop_and_health(tmp_path):
    setup = Setup(tmp_path)
    collector = setup.collector
    assert not collector.healthy()  # not started
    collector.start()
    try:
        assert collector.healthy()
        setup.monotonic.now += 181  # nothing rebuilt for over three minutes
        assert not collector.healthy()
        setup.hub.refresh()
        assert collector.healthy()
    finally:
        started = time.monotonic()
        assert collector.stop(timeout=3)
        assert time.monotonic() - started < 3
    assert not collector.healthy()


# Disruptions


class Feed:
    """A disruption feed: the real fixture, parsed, or an error."""

    def __init__(self, parse, path):
        self.parse, self.path = parse, path
        self.calls = 0
        self.error = None

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.parse(self.path.read_bytes())


def disrupted(tmp_path, now=datetime(2026, 10, 9, 23, 14, tzinfo=LONDON), bus=True, train=True, note=None):
    from departureboard.disruptions import parse_kb_incidents, parse_siri_sx

    setup = Setup(tmp_path, now=now)
    setup.situations = Feed(parse_siri_sx, FIXTURES / "bods_siri_sx_glossop.xml")
    setup.incidents = Feed(parse_kb_incidents, FIXTURES / "kb_incidents_synthetic.xml")
    setup.settings.bus_disruptions = setup.situations if bus else None
    setup.settings.train_incidents = setup.incidents if train else None
    setup.settings.train_incidents_note = note
    setup.collector = Collector(setup.settings, setup.hub, setup.history, clock=setup.clock,
                                fetch_holidays=setup._holidays, random=lambda: 0.5, monotonic=setup.monotonic)
    return setup


def titles(setup, mode=None):
    items = setup.snapshot()["disruptions"]["items"]
    return [i["title"] for i in items if mode is None or i["mode"] == mode]


def test_without_disruption_feeds_there_is_no_section(tmp_path):
    setup = Setup(tmp_path)
    assert "bus-disruptions" not in setup.collector.pollers()
    assert setup.snapshot()["disruptions"] is None


def test_disruption_feeds_are_polled_every_ten_and_thirty_minutes(tmp_path):
    pollers = disrupted(tmp_path).collector.pollers()
    assert pollers["bus-disruptions"].step() == 600 and pollers["train-incidents"].step() == 1800


def test_bus_disruptions_wait_for_the_timetable_to_know_what_is_ours(tmp_path):
    setup = disrupted(tmp_path)
    setup.collector.poll_bus_disruptions()
    part = setup.snapshot()["disruptions"]
    assert part["status"]["bus"]["note"] == "Waiting for the bus timetable" and part["items"] == []
    assert part["status"]["bus"]["last_success"] == "2026-10-09T23:14:00+01:00"
    setup.collector.poll_buses()  # the timetable: 341 and 237, both BNML's
    assert titles(setup, "bus") == ["Station Road, Hadfield - Roadworks"]
    assert setup.snapshot()["disruptions"]["status"]["bus"]["note"] is None
    assert setup.situations.calls == 1  # filtered again without asking again


def test_a_failed_disruption_poll_keeps_what_was_known_without_the_key(tmp_path):
    setup = disrupted(tmp_path)
    setup.collector.poll_buses()
    setup.collector.poll_bus_disruptions()
    setup.situations.error = RuntimeError("HTTP 500 for https://data.bus-data.dft.gov.uk/x?api_key=sekrit")
    with pytest.raises(RuntimeError):
        setup.collector.poll_bus_disruptions()
    part = setup.snapshot()["disruptions"]
    assert titles(setup, "bus") == ["Station Road, Hadfield - Roadworks"]
    assert part["status"]["bus"]["stale"] is True and "sekrit" not in json.dumps(part)


def test_rail_incidents_are_filtered_by_the_train_board(tmp_path):
    setup = disrupted(tmp_path, now=datetime(2026, 10, 9, 22, 3, tzinfo=LONDON))
    setup.collector.poll_train_incidents()
    assert titles(setup, "train") == []
    assert setup.snapshot()["disruptions"]["status"]["train"]["note"] == "Waiting for the train board"
    setup.collector.poll_trains()  # Northern trains via Dinting and Guide Bridge
    assert titles(setup, "train") == [
        "Disruption at Dinting", "Engineering works between Manchester Piccadilly and Glossop / Hadfield",
    ]
    # An empty board late at night keeps what is ours.
    setup.trains.payload = {"locationName": "Glossop", "trainServices": []}
    setup.collector.poll_trains()
    assert len(titles(setup, "train")) == 2


def test_the_knowledgebase_note_shows_when_its_address_is_missing(tmp_path):
    setup = disrupted(tmp_path, train=False, note="Engineering works are off: set [dashboard] kb_incidents_url")
    assert "train-incidents" not in setup.collector.pollers()
    status = setup.snapshot()["disruptions"]["status"]
    assert status["train"]["note"].startswith("Engineering works are off") and status["train"]["error"] is None


# The area map


def test_each_bus_poll_keeps_the_area_map_without_asking_again(tmp_path):
    setup = Setup(tmp_path)
    assert setup.hub.get("area") is None
    setup.collector.poll_buses()
    area = setup.hub.get("area")
    assert [(v["line"], v["next_stop"]) for v in area["vehicles"]] == [("341", "St Marys Road")]
    assert [s["label"] for s in area["stops"]] == ["A", "B"]
    setup.buses.error = RuntimeError("down")
    with pytest.raises(RuntimeError):
        setup.collector.poll_buses()
    assert setup.hub.get("area") == area  # the last one stays, its reports ageing
    setup.buses.reason = "the bus timetable has run out"
    setup.collector.poll_buses()
    assert setup.hub.get("area") is None


def test_timetable_changes_are_worked_out_once_per_timetable_and_day(tmp_path, monkeypatch):
    from departureboard import collector as module

    calls = []
    monkeypatch.setattr(module, "timetable_changes",
                        lambda schedule, day: calls.append(day) or [{"line": "341", "operator": "BNML",
                                                                    "from": "2026-10-25"}])
    plan = schedule()
    monkeypatch.setitem(globals(), "schedule", lambda: plan)  # the fake reads the same timetable each poll
    setup = Setup(tmp_path)
    assert setup.snapshot()["timetable_changes"] is None
    setup.collector.poll_buses()
    setup.collector.poll_buses()
    assert setup.snapshot()["timetable_changes"] == [{"line": "341", "operator": "BNML", "from": "2026-10-25"}]
    assert len(calls) == 1
    setup.clock.now += timedelta(days=1)
    setup.collector.poll_buses()
    assert len(calls) == 2
    setup.buses.reason = "the bus timetable has run out"
    setup.collector.poll_buses()
    assert setup.snapshot()["timetable_changes"] is None
