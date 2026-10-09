import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from departureboard.bodsbus import BusBoard
from departureboard.collector import Collector, Poller
from departureboard.dashconfig import DashboardOptions, DashboardSettings
from departureboard.darwin import parse_board_rich
from departureboard.history import Stats
from departureboard.snapshot import Hub
from departureboard.walk import WalkTimes
from departureboard.weather import parse_conditions

from test_livebus import HALFWAY, HENRY_A, at, bus, live, schedule

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


class Trains:
    def __init__(self):
        self.error = None
        self.calls = 0

    def fetch_board(self):
        self.calls += 1
        if self.error:
            raise self.error
        return parse_board_rich(json.loads((FIXTURES / "ldbws_glossop_details.json").read_text()))


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
        return BusBoard(live(self.clock(), vehicles), schedule(), self.vehicles_ok, note)

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

    def sweep(self, now):
        self.swept.append(now)

    def purge(self, now, days=90):
        self.purged.append((now, days))


class Setup:
    """A collector over fake sources, with its clock, hub and history."""

    def __init__(self, tmp_path, now=None, train=True, buses=True, weather=True, reason=None, options=None):
        self.clock = Clock(now or at(10, 14))
        self.monotonic = Monotonic()
        self.trains = Trains() if train else None
        self.buses = Buses(self.clock, reason) if buses else None
        self.upkeep_buses = self.buses  # the same fake stands for both instances
        self.weather = Weather() if weather else None
        self.settings = DashboardSettings(
            title="Glossop", timezone="Europe/London", state_dir=tmp_path, walk=WalkTimes(train=12, bus={"A": 5}),
            options=options or DashboardOptions(), train=self.trains, train_crs="GLO", train_filter="MAN",
            bus=self.buses, bus_upkeep=self.upkeep_buses, bus_stops={"A": HENRY_A}, weather=self.weather,
        )
        self.hub = Hub("Glossop", self.settings.walk, clock=self.clock, monotonic=self.monotonic)
        self.history = FakeHistory()
        self.holiday_calls = []
        self.collector = Collector(self.settings, self.hub, self.history, clock=self.clock,
                                   fetch_holidays=self._holidays, random=lambda: 0.5)

    def _holidays(self, folder, now):
        self.holiday_calls.append((folder, now))
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


# Buses


def test_buses_are_published_and_observed(tmp_path):
    setup = Setup(tmp_path)
    setup.collector.poll_buses()
    buses = setup.snapshot()["buses"]
    assert buses["timetable"]["state"] == "ready" and buses["stops"] == {"A": "Henry Street"}
    row = next(r for r in buses["rows"] if r["line"] == "341")
    assert row["live"] and row["expected"] == "10:19" and row["leave_at"] == "2026-10-09T10:14:00+01:00"
    mode, rows, _ = setup.history.observed[0]
    assert mode == "bus" and {o.route for o in rows} >= {"A|341|Hadfield"}
    assert setup.history.late_minutes == 2
    lookup = setup.hub.lookup(row["id"])
    assert lookup["bus"]["vehicle"]["lat"] == HALFWAY[0]


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
    assert setup.holiday_calls == [(tmp_path, datetime(2026, 10, 9, 10, 14))]
    assert setup.upkeep_buses.refreshes == [False]  # never forced: rebuilt only when due
    assert setup.history.swept == [datetime(2026, 10, 9, 10, 14)]
    assert setup.history.purged == [(datetime(2026, 10, 9, 10, 14), 30)]


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
