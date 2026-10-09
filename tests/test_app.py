import dataclasses

import pytest
from datetime import datetime, timedelta

from departureboard.app import Board, SourceConfig, run_once
from departureboard.model import Departure, Status
from departureboard.panel import PHAT
from departureboard.render import Palette
from departureboard.schedule import PollPolicy, parse_window
from departureboard.state import StateStore

THU_0700 = datetime(2026, 10, 8, 7, 0)


class FakeSource:
    def __init__(self, name, departures=()):
        self.name = name
        self.departures = list(departures)
        self.calls = 0
        self.error = None

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.departures)


class FakeDisplay:
    palette = Palette(white=0, black=1, accent=2)
    panel = PHAT

    def __init__(self):
        self.shown = []

    def show(self, image):
        self.shown.append(image)


def train(scheduled="07:12", expected=None, status=Status.ON_TIME):
    return Departure("train", "Northern", "Manchester Piccadilly", scheduled, expected, status)


def bus(scheduled="07:20"):
    return Departure("bus", "341", "Hyde", scheduled, None, Status.ON_TIME)


def morning(minutes):
    return PollPolicy((parse_window("Mon-Fri 06:45-09:00"),), timedelta(minutes=minutes))


def make_board(trains, buses, bus_budget=28):
    return Board(
        title="GLO > MAN",
        sources=[
            SourceConfig(trains, morning(2), stale_after=timedelta(minutes=6)),
            SourceConfig(buses, morning(5), stale_after=timedelta(minutes=15), daily_budget=bus_budget),
        ],
    )


def setup(bus_budget=28):
    trains = FakeSource("train", [train()])
    buses = FakeSource("bus", [bus()])
    return trains, buses, make_board(trains, buses, bus_budget), FakeDisplay(), {}


def test_first_run_fetches_everything_and_renders():
    trains, buses, board, display, state = setup()
    result = run_once(board, state, display, THU_0700)
    assert (trains.calls, buses.calls) == (1, 1)
    assert result.rendered
    assert len(display.shown) == 1
    assert [r.left for r in result.view.trains.rows] == ["07:12"]


def test_unchanged_data_within_interval_neither_fetches_nor_renders():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert (trains.calls, buses.calls) == (1, 1)
    assert not result.rendered


def test_each_source_follows_its_own_interval():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert (trains.calls, buses.calls) == (2, 1)


def test_changed_data_triggers_render():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    trains.departures = [train(expected="07:19", status=Status.LATE)]
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert result.rendered
    assert result.view.trains.rows[0].right == "+7"


def test_unchanged_data_never_redraws_however_long_it_has_been():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:30")]
    buses.departures = [bus("08:40")]
    run_once(board, state, display, THU_0700)
    for minutes in range(1, 60):
        assert not run_once(board, state, display, THU_0700 + timedelta(minutes=minutes)).rendered
    assert len(display.shown) == 1


def test_bus_fetch_refused_when_daily_budget_spent():
    trains, buses, board, display, state = setup(bus_budget=1)
    run_once(board, state, display, THU_0700)
    run_once(board, state, display, THU_0700 + timedelta(minutes=5))
    assert buses.calls == 1


def test_budget_is_charged_one_hit_per_stop():
    trains, buses, board, display, state = setup(bus_budget=3)
    buses.cost = 2
    run_once(board, state, display, THU_0700)
    run_once(board, state, display, THU_0700 + timedelta(minutes=5))
    assert buses.calls == 1


def test_failed_fetch_keeps_previous_departures():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:30")]
    run_once(board, state, display, THU_0700)
    trains.error = OSError("network down")
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert [r.left for r in result.view.trains.rows] == ["08:30"]
    assert not result.view.trains.stale


def test_source_marked_stale_when_failures_outlast_threshold():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:30")]
    run_once(board, state, display, THU_0700)
    trains.error = OSError("network down")
    result = None
    for minutes in (2, 4, 6, 8):
        result = run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert result.view.trains.stale
    assert result.rendered


def test_nothing_fetched_outside_windows():
    trains, buses, board, display, state = setup()
    saturday = datetime(2026, 10, 10, 7, 0)
    result = run_once(board, state, display, saturday)
    assert (trains.calls, buses.calls) == (0, 0)
    assert not result.view.trains.stale


def test_state_survives_a_round_trip_through_disk(tmp_path):
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    store = StateStore(tmp_path / "state.json")
    store.save(state)

    restored = store.load()
    result = run_once(board, restored, display, THU_0700 + timedelta(minutes=1))

    assert (trains.calls, buses.calls) == (1, 1)
    assert not result.rendered
    assert [r.left for r in result.view.trains.rows] == ["07:12"]


def test_run_loop_ticks_and_advances_a_simulated_clock():
    from departureboard.app import run_loop
    from departureboard.fake import SimClock

    trains, buses, board, display, state = setup()
    clock = SimClock(THU_0700)
    seen, sleeps = [], []
    run_loop(
        board,
        state,
        display,
        clock,
        tick_seconds=2,
        ticks=3,
        sleep=sleeps.append,
        on_tick=lambda now, result: seen.append(now),
    )
    assert seen == [THU_0700 + timedelta(minutes=m) for m in range(3)]
    assert sleeps == [2, 2, 2]


def test_run_loop_saves_state_each_tick(tmp_path):
    from departureboard.app import run_loop

    trains, buses, board, display, state = setup()
    store = StateStore(tmp_path / "state.json")
    run_loop(board, state, display, lambda: THU_0700, 0, ticks=1, sleep=lambda s: None, store=store)
    assert store.load()["sources"]["train"]["departures"]


def test_failed_fetch_logs_one_line_without_traceback(caplog):
    trains, buses, board, display, state = setup()
    trains.error = OSError("network down")
    with caplog.at_level("WARNING"):
        run_once(board, state, display, THU_0700)
    [record] = [r for r in caplog.records if "train" in r.getMessage()]
    assert "network down" in record.getMessage()
    assert record.exc_info is None


def minutely(**overrides):
    """Board whose trains poll every minute, so changes land between redraws."""
    trains, buses, board, display, state = setup()
    sources = [SourceConfig(trains, morning(1), stale_after=timedelta(minutes=6))]
    board = dataclasses.replace(board, sources=sources, **overrides)
    return trains, board, display, state


def test_changes_within_two_minutes_of_a_redraw_are_held_back():
    trains, board, display, state = minutely()
    run_once(board, state, display, THU_0700)
    trains.departures = [train(expected="07:19", status=Status.LATE)]
    held = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert trains.calls == 2
    assert held.view.trains.rows[0].right == "+7"
    assert not held.rendered
    assert len(display.shown) == 1


def test_held_change_is_drawn_once_two_minutes_have_passed():
    trains, board, display, state = minutely()
    run_once(board, state, display, THU_0700)
    trains.departures = [train(expected="07:19", status=Status.LATE)]
    run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert result.rendered
    assert len(display.shown) == 2


def test_minimum_redraw_gap_is_configurable():
    trains, board, display, state = minutely(min_redraw=timedelta(minutes=5))
    run_once(board, state, display, THU_0700)
    trains.departures = [train(expected="07:19", status=Status.LATE)]
    assert not run_once(board, state, display, THU_0700 + timedelta(minutes=4)).rendered
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=5)).rendered


def test_board_walk_times_produce_leave_text():
    from departureboard.walk import WalkTimes

    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, walk=WalkTimes(train=7, bus={"*": 5}))
    result = run_once(board, state, display, THU_0700)
    assert result.view.trains.leave == "Leave 07:05 (07:12)"
    assert result.view.buses.rows[0].leave == "leave 07:15"


def test_board_theme_picks_wording_and_renderer():
    from departureboard.themes import theme_named

    trains, buses, board, display, state = setup()
    trains.departures = [train(status=Status.CANCELLED)]
    board = dataclasses.replace(board, theme=theme_named("sign"))
    result = run_once(board, state, display, THU_0700)
    assert result.view.trains.rows[0].right == "Cancelled"
    counts = {c: n for n, c in display.shown[0].getcolors()}
    assert counts[display.palette.black] > counts.get(display.palette.white, 0)


SATURDAY = datetime(2026, 10, 10, 12, 0)


def test_prime_fetches_every_source_outside_its_windows():
    trains, buses, board, display, state = setup()
    trains.departures = [train("12:30")]
    result = run_once(board, state, display, SATURDAY, prime=True)
    assert (trains.calls, buses.calls) == (1, 1)
    assert result.view.trains.rows


def test_prime_still_respects_the_daily_bus_budget():
    trains, buses, board, display, state = setup(bus_budget=1)
    buses.cost = 2
    run_once(board, state, display, SATURDAY, prime=True)
    assert buses.calls == 0
    assert trains.calls == 1


def test_without_prime_nothing_is_fetched_outside_windows():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, SATURDAY)
    assert (trains.calls, buses.calls) == (0, 0)


def test_run_loop_primes_only_the_first_tick():
    from departureboard.app import run_loop
    from departureboard.fake import SimClock

    trains, buses, board, display, state = setup()
    run_loop(board, state, display, SimClock(SATURDAY), 0, ticks=3, sleep=lambda s: None, prime=True)
    assert (trains.calls, buses.calls) == (1, 1)


class FakeScreen(FakeDisplay):
    from departureboard.panel import HDMI_1280X400 as panel


def test_screen_gets_a_running_clock():
    trains, buses, board, display, state = setup()
    result = run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=15))
    assert result.view.clock == "07:00:15"


def test_epaper_gets_no_clock():
    trains, buses, board, display, state = setup()
    assert run_once(board, state, display, THU_0700).view.clock is None


def test_screen_redraws_every_second_without_a_minimum_gap():
    trains, buses, board, display, state = setup()
    screen = FakeScreen()
    for second in range(3):
        assert run_once(board, state, screen, THU_0700 + timedelta(seconds=second)).rendered
    assert len(screen.shown) == 3
    assert screen.shown[0].size == (1280, 400)


def test_screen_does_not_redraw_when_nothing_changed():
    trains, buses, board, display, state = setup()
    screen = FakeScreen()
    run_once(board, state, screen, THU_0700)
    assert not run_once(board, state, screen, THU_0700).rendered


class CountingStore:
    def __init__(self):
        self.saves = 0

    def save(self, state):
        self.saves += 1


def test_loop_writes_state_only_when_fetched_data_changes():
    from departureboard.app import run_loop

    trains, buses, board, display, state = setup()
    store = CountingStore()
    times = iter(THU_0700 + timedelta(seconds=s) for s in range(30))
    run_loop(board, state, FakeScreen(), lambda: next(times), 0, ticks=30, sleep=lambda s: None, store=store)
    # Redrawn 30 times, but both sources were fetched only on the first tick.
    assert store.saves == 1


# --- Route strip page (screens only) ---

from departureboard.app import Tracker  # noqa: E402
from departureboard.bods import Vehicle  # noqa: E402
from departureboard.route import RouteStop, RouteStrip  # noqa: E402

STRIP = RouteStrip(
    "341",
    "A",
    (
        RouteStop("The Junction", 53.444467, -1.965966),
        RouteStop("Grapes Inn", 53.444223, -1.962774),
        RouteStop("Sunlaws Street", 53.443494, -1.959026),
        RouteStop("St Marys Road", 53.443511, -1.955759),
        RouteStop("Arundel Street", 53.443519, -1.95326),
        RouteStop("Henry Street", 53.44384, -1.948999),
    ),
)


class FakeVehicles:
    def __init__(self, vehicles=()):
        self.vehicles = list(vehicles)
        self.calls = 0
        self.error = None

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.vehicles)


def vehicle_at(index, line="341"):
    stop = STRIP.stops[index]
    return Vehicle(line, "Hyde", "inbound", stop.lat, stop.lon, "2026-10-08T07:00:00+01:00", 90.0)


def bus_on(line, scheduled, stop="A"):
    return Departure("bus", line, "Hyde", scheduled, None, Status.ON_TIME, stop=stop)


def tracked_setup(buses_due, vehicles=()):
    trains, buses, board, display, state = setup()
    buses.departures = list(buses_due)
    positions = FakeVehicles(vehicles)
    tracker = Tracker(positions, {"341": STRIP}, morning(0.5))
    board = dataclasses.replace(board, tracker=tracker)
    return board, positions, state


# 07:00:05 is in the departures part of the 25 s cycle; 07:00:20 in the bus part.
DEPARTURES_TIME = THU_0700 + timedelta(seconds=5)
ROUTE_TIME = THU_0700 + timedelta(seconds=20)


def test_screen_alternates_between_departures_and_the_bus():
    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    screen = FakeScreen()
    first = run_once(board, state, screen, DEPARTURES_TIME)
    second = run_once(board, state, screen, ROUTE_TIME)
    assert (first.page, second.page) == ("departures", "route")
    assert second.rendered
    assert second.route.title == "341 to Hyde"
    assert second.route.bus_at == pytest.approx(2, abs=0.01)
    assert second.route.where == "3 stops away"


def test_epaper_never_shows_the_bus_page_or_polls_positions():
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    result = run_once(board, state, FakeDisplay(), ROUTE_TIME)
    assert (result.page, result.route) == ("departures", None)
    assert positions.calls == 0


def test_no_bus_page_without_a_tracked_bus_to_catch():
    board, _, state = tracked_setup([bus_on("237", "07:20")])
    result = run_once(board, state, FakeScreen(), ROUTE_TIME)
    assert (result.page, result.route) == ("departures", None)


def test_bus_page_follows_the_next_bus_you_can_still_reach():
    from departureboard.walk import WalkTimes

    # Walk 7 min: the 07:05 has gone by the time you'd get there, the 07:25 has not.
    board, _, state = tracked_setup([bus_on("341", "07:05"), bus_on("341", "07:25")], [vehicle_at(4), vehicle_at(1)])
    board = dataclasses.replace(board, walk=WalkTimes(bus={"*": 7}))
    route = run_once(board, state, FakeScreen(), ROUTE_TIME).route
    assert route.due == "07:25"
    # The nearer bus is the 07:05; the one wanted is the one behind it.
    assert route.bus_at == pytest.approx(1, abs=0.01)


def test_bus_page_says_when_positions_stop_arriving():
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    positions.error = OSError("down")
    route = run_once(board, state, FakeScreen(), THU_0700 + timedelta(minutes=3, seconds=20)).route
    assert route.bus_at is None
    assert route.where == "Position not updating"


def test_no_bus_page_outside_the_bus_windows():
    board, positions, state = tracked_setup([bus_on("341", "12:20")], [vehicle_at(2)])
    result = run_once(board, state, FakeScreen(), SATURDAY + timedelta(seconds=20), prime=True)
    assert result.route is None
    assert positions.calls == 0


def test_positions_are_polled_on_their_own_interval():
    board, positions, state = tracked_setup([bus_on("341", "07:20")])
    for second in range(0, 61, 5):
        run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=second))
    assert positions.calls == 3  # at 0, 30 and 60 s


def test_positions_are_not_written_to_disk():
    from departureboard.app import _fetched

    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    assert "vehicles" in state
    assert "vehicles" not in _fetched(state)


def test_run_loop_switches_to_reloaded_settings():
    from departureboard.app import run_loop

    trains, buses, board, display, state = setup()
    renamed = dataclasses.replace(board, title="Renamed")
    answers = iter([None, renamed, None])
    titles = []
    times = iter(THU_0700 + timedelta(minutes=m) for m in range(0, 30, 3))
    run_loop(board, state, display, lambda: next(times), 0, ticks=3, sleep=lambda s: None,
             on_tick=lambda now, result: titles.append(result.view.title), reload=lambda: next(answers))
    assert titles == ["GLO > MAN", "Renamed", "Renamed"]


# --- Departures age out (oz5.2) ---


def test_morning_departures_do_not_come_back_in_the_evening():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:45")]
    buses.departures = [bus("08:40")]
    run_once(board, state, display, THU_0700.replace(hour=8, minute=30))
    # 08:40 is within 12 hours of 20:41 on the clock, but it was this morning.
    result = run_once(board, state, display, THU_0700.replace(hour=20, minute=41))
    assert result.view.trains.rows == ()
    assert result.view.buses.rows == ()
    assert not result.view.buses.stale


def test_departures_after_midnight_stay_until_they_leave():
    trains, buses, board, display, state = setup()
    trains.departures = []
    buses.departures = [bus("00:20")]
    run_once(board, state, display, THU_0700.replace(hour=23, minute=50), prime=True)
    friday = THU_0700 + timedelta(days=1)
    assert [r.left for r in run_once(board, state, display, friday.replace(hour=0, minute=10)).view.buses.rows] == ["341"]
    assert run_once(board, state, display, friday.replace(hour=12, minute=30)).view.buses.rows == ()
