import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.board.app import Board, SourceConfig, run_once
from twobells.board.panel import HDMI_1280X400, PHAT
from twobells.board.render import Palette
from twobells.board.state import StateStore
from twobells.board.view import CodeView, SetupView
from twobells.core.model import Departure, Status
from twobells.core.polling import PollPolicy, parse_window

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
    assert [r.left for r in result.view.section("train").rows] == ["07:12"]


def test_the_board_draws_a_section_for_each_of_its_modes_in_order():
    trains, buses, board, display, state = setup()
    view = run_once(board, state, display, THU_0700).view
    assert [(s.mode, s.label) for s in view.sections] == [("train", "Trains"), ("bus", "Buses")]
    buses_alone = dataclasses.replace(board, sources=board.sources[1:], modes=("bus",))
    view = run_once(buses_alone, {}, FakeDisplay(), THU_0700).view
    assert [s.mode for s in view.sections] == ["bus"]
    assert [r.left for r in view.section("bus").rows] == ["341"]


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
    assert result.view.section("train").rows[0].right == "Exp 07:19"


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
    assert [r.left for r in result.view.section("train").rows] == ["08:30"]
    assert not result.view.section("train").stale


def test_source_marked_stale_when_failures_outlast_threshold():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:30")]
    run_once(board, state, display, THU_0700)
    trains.error = OSError("network down")
    result = None
    for minutes in (2, 4, 6, 8):
        result = run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert result.view.section("train").stale
    assert result.rendered


def test_nothing_fetched_outside_windows():
    trains, buses, board, display, state = setup()
    saturday = datetime(2026, 10, 10, 7, 0)
    result = run_once(board, state, display, saturday)
    assert (trains.calls, buses.calls) == (0, 0)
    assert not result.view.section("train").stale


def test_state_survives_a_round_trip_through_disk(tmp_path):
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    store = StateStore(tmp_path / "state.json")
    store.save(state)

    restored = store.load()
    result = run_once(board, restored, display, THU_0700 + timedelta(minutes=1))

    assert (trains.calls, buses.calls) == (1, 1)
    assert not result.rendered
    assert [r.left for r in result.view.section("train").rows] == ["07:12"]


def at_platform(platform, scheduled="08:30"):
    return dataclasses.replace(train(scheduled), platform=platform)


def test_a_platform_unlike_the_last_fetchs_is_marked_changed_across_runs(tmp_path):
    trains, buses, board, display, state = setup()
    store = StateStore(tmp_path / "state.json")
    trains.departures = [at_platform("1")]
    result = run_once(board, state, display, THU_0700)
    assert result.view.section("train").rows[0].platform == "Plat 1"
    # The board's timer starts afresh each run: what was fetched comes from the file.
    store.save(state)
    state = store.load()
    trains.departures = [at_platform("2")]
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert result.view.section("train").rows[0].platform == "Plat 2 (changed)"
    store.save(state)
    state = store.load()
    # Still marked while it stays on its new platform.
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=4))
    assert result.view.section("train").rows[0].platform == "Plat 2 (changed)"


def test_a_platform_is_never_compared_with_yesterdays_train():
    trains, buses, board, display, state = setup()
    trains.departures = [at_platform("1")]
    run_once(board, state, display, THU_0700)
    # The same time next morning is another train.
    trains.departures = [at_platform("2")]
    result = run_once(board, state, display, THU_0700 + timedelta(days=1))
    assert result.view.section("train").rows[0].platform == "Plat 2"


def test_run_loop_ticks_and_advances_a_simulated_clock():
    from twobells.board.app import run_loop
    from twobells.emulator.fake import SimClock

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
    from twobells.board.app import run_loop

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
    assert held.view.section("train").rows[0].right == "Exp 07:19"
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
    from twobells.core.walk import WalkTimes

    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, walk=WalkTimes(train=7, bus={"*": 5}))
    result = run_once(board, state, display, THU_0700)
    assert result.view.section("train").leave == "Leave by 07:05 (07:12)"
    assert result.view.section("bus").rows[0].leave == "Leave by 07:15"


def test_board_theme_picks_wording_and_renderer():
    from twobells.board.themes import theme_named

    trains, buses, board, display, state = setup()
    trains.departures = [train(status=Status.CANCELLED)]
    board = dataclasses.replace(board, theme=theme_named("sign"))
    result = run_once(board, state, display, THU_0700)
    assert result.view.section("train").rows[0].right == "Cancelled"
    counts = {c: n for n, c in display.shown[0].getcolors()}
    assert counts[display.palette.black] > counts.get(display.palette.white, 0)


SATURDAY = datetime(2026, 10, 10, 12, 0)


def test_prime_fetches_every_source_outside_its_windows():
    trains, buses, board, display, state = setup()
    trains.departures = [train("12:30")]
    result = run_once(board, state, display, SATURDAY, prime=True)
    assert (trains.calls, buses.calls) == (1, 1)
    assert result.view.section("train").rows


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
    from twobells.board.app import run_loop
    from twobells.emulator.fake import SimClock

    trains, buses, board, display, state = setup()
    run_loop(board, state, display, SimClock(SATURDAY), 0, ticks=3, sleep=lambda s: None, prime=True)
    assert (trains.calls, buses.calls) == (1, 1)


class FakeScreen(FakeDisplay):
    from twobells.board.panel import HDMI_1280X400 as panel  # noqa: N811 - the display's panel attribute


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
    from twobells.board.app import run_loop

    trains, buses, board, display, state = setup()
    store = CountingStore()
    times = iter(THU_0700 + timedelta(seconds=s) for s in range(30))
    run_loop(board, state, FakeScreen(), lambda: next(times), 0, ticks=30, sleep=lambda s: None, store=store)
    # Redrawn 30 times, but both sources were fetched only on the first tick.
    assert store.saves == 1


# --- Route strip page (screens only) ---

from twobells.board.app import Tracker  # noqa: E402
from twobells.board.route import RouteStop, RouteStrip  # noqa: E402
from twobells.sources.bods import Vehicle  # noqa: E402

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
    from twobells.core.walk import WalkTimes

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


def pages(board, state, display, start, seconds):
    return [run_once(board, state, display, start + timedelta(seconds=s)).page for s in seconds]


def test_the_clock_page_is_off_unless_asked_for():
    trains, buses, board, display, state = setup()
    assert "clock_page" not in board.show
    assert set(pages(board, state, FakeScreen(), THU_0700, range(60))) == {"departures"}


def test_a_screen_shows_the_clock_for_10_s_after_the_departures():
    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, show=board.show | {"clock_page"})
    shown = pages(board, state, FakeScreen(), THU_0700, range(50))
    assert shown == (["departures"] * 15 + ["clock"] * 10) * 2
    clock = FakeScreen()
    result = run_once(board, state, clock, THU_0700 + timedelta(seconds=15))
    assert result.rendered and clock.shown[-1].size == (1280, 400)
    # A new second, a new picture: the seconds hand moves.
    assert run_once(board, state, clock, THU_0700 + timedelta(seconds=16)).rendered


def test_with_a_bus_to_follow_the_clock_comes_after_the_bus_page():
    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    board = dataclasses.replace(board, show=board.show | {"clock_page"})
    shown = pages(board, state, FakeScreen(), THU_0700, range(35))
    assert shown == ["departures"] * 15 + ["route"] * 10 + ["clock"] * 10


def test_e_paper_never_shows_the_clock_page():
    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, show=board.show | {"clock_page"})
    assert run_once(board, state, display, THU_0700 + timedelta(seconds=20)).page == "departures"


def test_the_code_page_beats_the_clock_page():
    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, show=board.show | {"clock_page"}, reset_code=Waiting(CODE))
    assert run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=20)).page == "code"


def test_positions_are_polled_on_their_own_interval():
    board, positions, state = tracked_setup([bus_on("341", "07:20")])
    for second in range(0, 61, 5):
        run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=second))
    assert positions.calls == 3  # at 0, 30 and 60 s


def test_without_the_bus_page_its_positions_are_not_asked_for():
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)  # with the bus page, they are
    assert positions.calls == 1 and "vehicles" in state
    board = dataclasses.replace(board, show=board.show - {"bus_page"})
    for second in range(0, 121, 5):
        result = run_once(board, state, FakeScreen(), ROUTE_TIME + timedelta(seconds=second))
        assert result.route is None
    assert positions.calls == 1
    assert "vehicles" not in state  # nor does the settings site say how asking for them goes


def test_positions_are_not_written_to_disk():
    from twobells.board.app import _fetched

    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    assert "vehicles" in state
    assert "vehicles" not in _fetched(state)


def test_positions_with_their_journey_start_save_and_read_back(tmp_path):
    # Live positions name the timetabled journey they run, with its start time
    # (a datetime); saving the state with them must not fail, and the bus page
    # must still place the bus from the saved state.
    aimed = datetime(2026, 10, 8, 6, 40, tzinfo=ZoneInfo("Europe/London"))
    tracked = dataclasses.replace(vehicle_at(2), operator="BNML", origin_ref="1800SB12345", origin_aimed=aimed)
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [tracked])
    run_once(board, state, FakeScreen(), THU_0700)
    store = StateStore(tmp_path / "state.json")
    store.save(state)
    saved = store.load()
    positions.vehicles = []
    positions.error = OSError("down")  # so the page can only use what was saved
    route = run_once(board, saved, FakeScreen(), ROUTE_TIME).route
    assert route.bus_at == pytest.approx(2, abs=0.01)


def test_a_saved_position_that_doesnt_read_back_is_skipped():
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    state["vehicles"]["items"].append({"line": "341", "origin_aimed": "not a time"})
    positions.error = OSError("down")
    route = run_once(board, state, FakeScreen(), ROUTE_TIME).route
    assert route.bus_at == pytest.approx(2, abs=0.01)


@pytest.mark.parametrize("change", [{"lat": 10**400}, {"lat": "53.4"}, {"operator": 5}, {"bearing": "east"}])
def test_the_bus_page_skips_a_bad_saved_position_and_places_the_good_one(change):
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    good = state["vehicles"]["items"][0]
    state["vehicles"]["items"].insert(0, {**good, **change})
    positions.error = OSError("down")
    route = run_once(board, state, FakeScreen(), ROUTE_TIME).route
    assert route.bus_at == pytest.approx(2, abs=0.01)


# A hand-edited or corrupt state.json: a number, not a time, or a time with an
# offset, which can't be compared with the board's clock (local, with none).
BAD_SAVED_TIMES = [5, "garbage", "2026-10-08T06:58:00+01:00", ["07:00"]]


@pytest.mark.parametrize("saved", BAD_SAVED_TIMES)
def test_a_bad_saved_position_time_reads_as_never_rather_than_stopping_the_board(saved):
    board, positions, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    run_once(board, state, FakeScreen(), THU_0700)
    state["vehicles"].update(last_success=saved, last_attempt=saved)
    positions.error = OSError("down")
    route = run_once(board, state, FakeScreen(), ROUTE_TIME).route
    assert route.bus_at is None  # no time it was fetched: too old to show
    assert positions.calls == 2  # never asked counts as due


def test_run_loop_switches_to_reloaded_settings():
    from twobells.board.app import run_loop

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
    assert result.view.section("train").rows == ()
    assert result.view.section("bus").rows == ()
    assert not result.view.section("bus").stale


def test_departures_after_midnight_stay_until_they_leave():
    trains, buses, board, display, state = setup()
    trains.departures = []
    buses.departures = [bus("00:20")]
    run_once(board, state, display, THU_0700.replace(hour=23, minute=50), prime=True)
    friday = THU_0700 + timedelta(days=1)
    assert [r.left for r in run_once(board, state, display, friday.replace(hour=0, minute=10)).view.section("bus").rows] == ["341"]
    assert run_once(board, state, display, friday.replace(hour=12, minute=30)).view.section("bus").rows == ()


# --- A failed draw keeps what was fetched (oz5.3) ---


class BrokenDisplay(FakeDisplay):
    def show(self, image):
        raise OSError("GPIO busy")


def test_failed_draw_is_not_recorded_so_the_next_run_draws_again():
    trains, buses, board, display, state = setup()
    with pytest.raises(OSError):
        run_once(board, state, BrokenDisplay(), THU_0700)
    assert "last_render" not in state
    assert state["sources"]["bus"]["last_attempt"]
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=1)).rendered


def test_run_loop_saves_state_before_a_draw_failure_escapes():
    from twobells.board.app import run_loop

    trains, buses, board, display, state = setup()
    store = CountingStore()
    with pytest.raises(OSError):
        run_loop(board, state, BrokenDisplay(), lambda: THU_0700, 0, ticks=1, sleep=lambda s: None, store=store)
    assert store.saves == 1


def test_malformed_saved_departures_are_skipped():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    state["sources"]["train"]["departures"].append({"mode": "train", "unexpected": True})
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert [r.left for r in result.view.section("train").rows] == ["07:12"]


# --- How the board is drawn counts as a change (oz5.12) ---


def test_new_look_alone_redraws_epaper_once_the_gap_has_passed():
    from twobells.board.themes import theme_named

    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    signed = dataclasses.replace(board, theme=theme_named("sign"))
    assert not run_once(signed, state, display, THU_0700 + timedelta(minutes=1)).rendered
    assert run_once(signed, state, display, THU_0700 + timedelta(minutes=2)).rendered
    assert not run_once(signed, state, display, THU_0700 + timedelta(minutes=4)).rendered


def test_new_accent_or_rotation_redraws_epaper():
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700)
    yellow = FakeDisplay()
    yellow.palette = Palette(white=0, black=1, accent=3)
    assert run_once(board, state, yellow, THU_0700 + timedelta(minutes=2)).rendered
    turned = FakeDisplay()
    turned.palette = yellow.palette
    turned.rotate = 180
    assert run_once(board, state, turned, THU_0700 + timedelta(minutes=4)).rendered


def test_turning_a_real_inky_round_redraws_it():
    from twobells.board.displays.display import InkyDisplay

    class Inky:
        WHITE, BLACK, RED = 0, 1, 2

        def set_image(self, image):
            pass

        def show(self):
            pass

    trains, buses, board, display, state = setup()
    run_once(board, state, InkyDisplay(device=Inky()), THU_0700)
    assert not run_once(board, state, InkyDisplay(device=Inky()), THU_0700 + timedelta(minutes=2)).rendered
    assert run_once(board, state, InkyDisplay(device=Inky(), rotate_180=True), THU_0700 + timedelta(minutes=4)).rendered


def test_clock_going_back_an_hour_does_not_pause_the_board():
    # At the end of summer time the local clock goes from 01:59 back to 01:00.
    trains, board, display, state = minutely()
    run_once(board, state, display, THU_0700.replace(hour=7, minute=59))
    trains.departures = [train("08:30", expected="08:37", status=Status.LATE)]
    result = run_once(board, state, display, THU_0700)
    assert trains.calls == 2
    assert result.rendered


def test_a_failed_fetch_records_a_short_error_without_keys():
    trains, buses, board, display, state = setup()
    trains.error = OSError("GET https://api.example/board?x=1 failed: x-apikey: secret-rdm-key")
    run_once(board, state, display, THU_0700)
    error = state["sources"]["train"]["last_error"]
    assert error["at"] == "2026-10-08T07:00:00"
    assert "secret-rdm-key" not in error["message"] and "x=1" not in error["message"]
    assert error["message"].startswith("GET https://api.example/board?…")


def test_a_failed_fetch_logs_no_keys(caplog):
    trains, buses, board, display, state = setup()
    buses.error = OSError("https://transportapi.com/v3/x.json?app_id=myid&app_key=tapi-secret refused")
    with caplog.at_level("WARNING"):
        run_once(board, state, display, THU_0700)
    assert "tapi-secret" not in caplog.text and "myid" not in caplog.text


def test_a_successful_fetch_clears_the_last_error():
    trains, buses, board, display, state = setup()
    trains.error = OSError("network down")
    run_once(board, state, display, THU_0700)
    trains.error = None
    run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert "last_error" not in state["sources"]["train"]
    assert state["sources"]["train"]["last_success"] == "2026-10-08T07:02:00"


def test_a_failed_position_fetch_records_an_error_and_success_clears_it():
    board, positions, state = tracked_setup([bus_on("341", "07:20")])
    positions.error = RuntimeError("https://data.bus-data.dft.gov.uk/api/v1/datafeed/?api_key=bods-secret timed out")
    run_once(board, state, FakeScreen(), THU_0700)
    error = state["vehicles"]["last_error"]
    assert error["at"] == "2026-10-08T07:00:00" and "bods-secret" not in error["message"]
    positions.error = None
    run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=30))
    assert "last_error" not in state["vehicles"]


class NoticeSource(FakeSource):
    """Like DarwinSource: messages from the last successful fetch."""

    def __init__(self, name, departures=(), messages=()):
        super().__init__(name, departures)
        self.next_messages = list(messages)
        self.messages = []

    def fetch(self):
        departures = super().fetch()
        self.messages = list(self.next_messages)
        return departures


def notice_setup(messages=("Lifts out of use.",)):
    trains = NoticeSource("train", [train()], messages)
    buses = FakeSource("bus", [bus()])
    return trains, make_board(trains, buses), FakeDisplay(), {}


def test_disruption_messages_are_kept_with_the_train_data_and_shown():
    trains, board, display, state = notice_setup()
    result = run_once(board, state, display, THU_0700)
    assert state["sources"]["train"]["messages"] == ["Lifts out of use."]
    assert result.view.notice == "Lifts out of use."


def test_a_cleared_message_goes_from_the_board():
    trains, board, display, state = notice_setup()
    run_once(board, state, display, THU_0700)
    trains.next_messages = []
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert result.view.notice is None
    assert result.rendered


def test_a_failed_fetch_keeps_the_last_messages_until_the_data_ages_out():
    trains, board, display, state = notice_setup()
    run_once(board, state, display, THU_0700)
    trains.error = OSError("down")
    trains.next_messages = []
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=2)).view.notice == "Lifts out of use."
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=6)).view.notice == "Lifts out of use."
    # Older than the train feed's stale_after (6 min): it may no longer be true.
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=7)).view.notice is None


def test_data_from_after_now_counts_as_stale_once_the_clock_goes_back():
    # As PollPolicy.is_due does: a last success later than now (the clock went
    # back) can't say how old the data is, so it isn't taken as fresh.
    trains, buses, board, display, state = setup()
    run_once(board, state, display, THU_0700 + timedelta(hours=1))
    trains.error = OSError("network down")
    result = run_once(board, state, display, THU_0700)
    assert trains.calls == 2  # fetched again straight away, and failed
    assert result.view.section("train").stale


def test_messages_from_after_now_are_not_kept_once_the_clock_goes_back():
    trains, board, display, state = notice_setup()
    run_once(board, state, display, THU_0700 + timedelta(hours=1))
    trains.error = OSError("down")
    assert run_once(board, state, display, THU_0700).view.notice is None


def test_messages_age_out_after_the_polling_window_too():
    trains, board, display, state = notice_setup()
    run_once(board, state, display, THU_0700.replace(hour=8, minute=58))
    assert run_once(board, state, display, THU_0700.replace(hour=9, minute=10)).view.notice is None


def test_sources_without_messages_store_none():
    trains, buses, board, display, state = setup()
    result = run_once(board, state, display, THU_0700)
    assert "messages" not in state["sources"]["train"]
    assert result.view.notice is None


def test_saved_messages_that_are_not_text_are_ignored():
    trains, board, display, state = notice_setup(messages=())
    run_once(board, state, display, THU_0700)
    state["sources"]["train"]["messages"] = [3, None, "Lifts out of use."]
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=1)).view.notice == "Lifts out of use."
    state["sources"]["train"]["messages"] = "not a list"
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=1)).view.notice is None


def test_darwin_messages_reach_the_board():
    import json

    from twobells.sources.darwin import DarwinSource

    payload = json.loads((FIXTURES / "ldbws_glossop_nrcc.json").read_text())
    source = DarwinSource(api_key="k", crs="GLO", http_get=lambda url, headers: payload)
    board = make_board(source, FakeSource("bus", [bus()]))
    result = run_once(board, {}, FakeDisplay(), THU_0700.replace(hour=11, minute=45), prime=True)
    assert result.view.notice.startswith("Engineering work between Dinting and Guide Bridge")
    assert " / Ticket machines at Glossop & Dinting take cards only." in result.view.notice


from twobells.board.app import Weather  # noqa: E402
from twobells.core.walk import WalkTimes  # noqa: E402
from twobells.sources.weather import Hour  # noqa: E402


class FakeWeather:
    """Dry until 07:00, 30% from 07:00, 70% from 08:00, on THU_0700's day."""

    def __init__(self):
        self.calls = 0
        self.error = None

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        chances = {7: 30, 8: 70, 9: 85}
        return [Hour(THU_0700.replace(hour=h), chances.get(h, 5), 0.0) for h in range(24)]


WEATHER_WALK = WalkTimes(train=7, bus={"*": 5})


def weather_setup(trains=(), buses=(), walk=WEATHER_WALK, threshold=50):
    weather = FakeWeather()
    board = dataclasses.replace(
        make_board(FakeSource("train", trains), FakeSource("bus", buses)),
        walk=walk,
        weather=Weather(weather, morning(30), threshold),
    )
    return weather, board, FakeDisplay(), {}


def at(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return THU_0700.replace(hour=hour, minute=minute)


def test_without_weather_settings_there_is_no_forecast_and_no_rain_line():
    trains, buses, board, display, state = setup()
    result = run_once(board, state, display, THU_0700)
    assert "weather" not in state
    assert result.view.rain is None


def test_weather_is_polled_every_30_minutes_inside_the_windows_only():
    weather, board, display, state = weather_setup()
    run_once(board, state, display, at("06:40"))  # before any window opens
    assert weather.calls == 0
    for minute in range(45, 60):
        run_once(board, state, display, at(f"06:{minute}"))
    assert weather.calls == 1
    run_once(board, state, display, at("07:14"))
    assert weather.calls == 1
    run_once(board, state, display, at("07:15"))
    assert weather.calls == 2
    run_once(board, state, display, at("09:30"))  # windows closed
    assert weather.calls == 2


def test_without_the_rain_warning_the_forecast_is_not_asked_for():
    weather, board, display, state = weather_setup(trains=[train("08:12")])
    board = dataclasses.replace(board, show=board.show - {"rain"})
    for minute in range(0, 60, 5):
        assert run_once(board, state, display, at(f"07:{minute:02}")).view.rain is None
    assert weather.calls == 0 and "weather" not in state


def test_rain_before_you_reach_the_train_is_shown():
    weather, board, display, state = weather_setup(trains=[train("08:12")])
    # Leave 08:05, at the station 08:12: rain is likely from 08:00.
    assert run_once(board, state, display, at("07:50")).view.rain == "Rain from 08:00 (70%)"


def test_rain_after_you_reach_the_train_is_not():
    weather, board, display, state = weather_setup(trains=[train("07:42")])
    # At the station 07:42; 07:00-08:00 is only 30%.
    assert run_once(board, state, display, at("07:20")).view.rain is None


def test_the_next_departure_you_can_catch_sets_the_window():
    # The 07:42 is out of reach at 07:40 with a 7 minute walk; the 08:12 is next.
    weather, board, display, state = weather_setup(trains=[train("07:42"), train("08:12")])
    assert run_once(board, state, display, at("07:40")).view.rain == "Rain from 08:00 (70%)"


def test_a_bus_you_can_catch_sooner_sets_the_window():
    # The bus at 07:55 (5 min walk) comes before the 08:12 train: at the stop before the rain.
    weather, board, display, state = weather_setup(trains=[train("08:12")], buses=[bus("07:55")])
    assert run_once(board, state, display, at("07:40")).view.rain is None


def test_without_walk_times_the_next_hour_counts():
    weather, board, display, state = weather_setup(trains=[train("09:12")], walk=WalkTimes())
    assert run_once(board, state, display, at("07:05")).view.rain == "Rain from 08:00 (70%)"
    weather, board, display, state = weather_setup(walk=WalkTimes())
    assert run_once(board, state, display, at("06:55")).view.rain is None  # 08:00 is over an hour away


def test_with_nothing_left_to_catch_the_next_hour_counts():
    weather, board, display, state = weather_setup(trains=[])
    assert run_once(board, state, display, at("07:05")).view.rain == "Rain from 08:00 (70%)"


def test_already_raining_says_from_now_on_a_screen():
    weather, board, display, state = weather_setup(trains=[train("08:42")])
    screen = FakeDisplay()
    screen.panel = HDMI_1280X400
    assert run_once(board, state, screen, at("08:10")).view.rain == "Rain from 08:10 (70%)"


def test_already_raining_on_epaper_gives_the_hours_start_so_it_need_not_redraw():
    weather, board, display, state = weather_setup(trains=[train("08:42")])
    assert run_once(board, state, display, at("08:10")).view.rain == "Rain from 08:00 (70%)"
    assert run_once(board, state, display, at("08:11")).view.rain == "Rain from 08:00 (70%)"


def test_threshold_comes_from_the_settings():
    weather, board, display, state = weather_setup(trains=[train("07:42")], threshold=30)
    assert run_once(board, state, display, at("07:20")).view.rain == "Rain from 07:00 (30%)"


def test_a_failed_forecast_keeps_the_last_one_until_it_is_too_old():
    weather, board, display, state = weather_setup(walk=WalkTimes())
    run_once(board, state, display, at("07:05"))
    weather.error = OSError("down")
    assert run_once(board, state, display, at("07:40")).view.rain == "Rain from 08:00 (70%)"
    assert weather.calls == 2
    # Older than max_age (3 hours; 30 minutes here), it isn't trusted, though it covers the time.
    later = dataclasses.replace(board, weather=dataclasses.replace(board.weather, max_age=timedelta(minutes=30)))
    assert run_once(later, state, display, at("07:40")).view.rain is None


def test_a_malformed_saved_forecast_is_replaced():
    weather, board, display, state = weather_setup(walk=WalkTimes())
    state["weather"] = "nonsense"
    assert run_once(board, state, display, at("07:05")).view.rain == "Rain from 08:00 (70%)"
    state["weather"]["hours"] = [{"start": 3}]
    weather.error = OSError("down")
    assert run_once(board, state, display, at("07:40")).view.rain is None


def test_rain_line_redraws_the_board():
    weather, board, display, state = weather_setup(walk=WalkTimes())
    run_once(board, state, display, at("06:45"))  # 07:00-08:00 is 30%: dry enough
    result = run_once(board, state, display, at("07:05"))
    assert result.view.rain and result.rendered


def test_loop_also_saves_every_few_minutes_so_the_status_panel_keeps_up():
    from twobells.board.app import run_loop

    trains, buses, board, display, state = setup()
    store = CountingStore()
    # Saturday: outside every window, so nothing is fetched, but the screen keeps
    # redrawing; the saved redraw time must not go stale for the status panel.
    times = iter(SATURDAY + timedelta(seconds=s) for s in range(0, 11 * 60, 10))
    run_loop(board, state, FakeScreen(), lambda: next(times), 0, ticks=66, sleep=lambda s: None, store=store)
    assert store.saves >= 3  # at the start, then at least every 5 minutes


def test_changing_the_text_size_redraws_the_epaper():
    from twobells.board.themes import theme_named, with_text_size

    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, theme=theme_named("plain"))
    run_once(board, state, display, THU_0700)
    bigger = dataclasses.replace(board, theme=with_text_size(theme_named("plain"), 12))
    assert run_once(bigger, state, display, THU_0700 + timedelta(minutes=3)).rendered


def fallback_board(buses):
    trains = FakeSource("train", [train()])
    return Board(
        title="GLO > MAN",
        sources=[
            SourceConfig(trains, morning(2), stale_after=timedelta(minutes=6)),
            SourceConfig(buses, morning(2), stale_after=timedelta(minutes=30), daily_budget=28,
                         fallback_policy=morning(12)),
        ],
    )


def test_a_source_falling_back_is_polled_at_the_fallback_pace():
    buses = FakeSource("bus", [bus()])
    buses.falling_back = True
    board, display, state = fallback_board(buses), FakeDisplay(), {}
    for minutes in range(0, 13):
        run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert buses.calls == 2  # 07:00 and 07:12


def test_a_source_not_falling_back_is_polled_at_its_own_pace():
    buses = FakeSource("bus", [bus()])
    buses.falling_back = False
    board, display, state = fallback_board(buses), FakeDisplay(), {}
    for minutes in range(0, 13):
        run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert buses.calls == 7  # every 2 minutes


def test_a_source_that_costs_nothing_never_spends_the_budget():
    buses = FakeSource("bus", [bus()])
    buses.cost = 0
    board, display, state = fallback_board(buses), FakeDisplay(), {}
    for minutes in range(0, 120, 2):
        run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert buses.calls == 60
    assert state["budgets"]["bus"]["used"] == 0


def test_a_source_s_note_is_saved_with_its_departures_and_cleared_after():
    buses = FakeSource("bus", [bus()])
    buses.note = "From TransportAPI: there is no bus timetable yet, key=abc"
    board, display, state = fallback_board(buses), FakeDisplay(), {}
    run_once(board, state, display, THU_0700)
    assert state["sources"]["bus"]["note"] == "From TransportAPI: there is no bus timetable yet, key=REDACTED"
    buses.note = None
    run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert "note" not in state["sources"]["bus"]


def test_a_source_falling_back_is_only_polled_in_the_fallback_windows():
    buses = FakeSource("bus", [bus()])
    buses.falling_back = True
    trains = FakeSource("train", [train()])
    board = Board(
        title="GLO > MAN",
        sources=[
            SourceConfig(trains, morning(2), stale_after=timedelta(minutes=6)),
            SourceConfig(buses, PollPolicy((parse_window("Mon-Sun 06:00-23:00"),), timedelta(minutes=2)),
                         stale_after=timedelta(minutes=30), daily_budget=28,
                         fallback_policy=PollPolicy((parse_window("Mon-Fri 17:00-18:00"),), timedelta(minutes=12))),
        ],
    )
    display, state = FakeDisplay(), {}
    run_once(board, state, display, THU_0700, prime=False)
    for minutes in range(1, 60):
        run_once(board, state, display, THU_0700 + timedelta(minutes=minutes))
    assert buses.calls == 0
    run_once(board, state, display, THU_0700.replace(hour=17))
    assert buses.calls == 1


class WaitingBuses(FakeSource):
    """A bus source with no timetable, whose fallback isn't due."""

    falling_back = True

    def __init__(self):
        super().__init__("bus", [bus()])
        self.waiting = "There is no bus timetable yet; TransportAPI stands in only at its own checking times"

    def idle_note(self):
        return self.waiting


def test_a_source_not_polled_while_falling_back_saves_why():
    buses = WaitingBuses()
    trains = FakeSource("train", [train()])
    board = Board(
        title="GLO > MAN",
        sources=[
            SourceConfig(trains, morning(2), stale_after=timedelta(minutes=6)),
            SourceConfig(buses, morning(2), stale_after=timedelta(minutes=30), daily_budget=28,
                         fallback_policy=PollPolicy((parse_window("Mon-Fri 17:00-18:00"),), timedelta(minutes=12))),
        ],
    )
    state = {}
    run_once(board, state, FakeDisplay(), THU_0700)
    assert buses.calls == 0
    assert state["sources"]["bus"]["note"] == buses.waiting
    buses.waiting = None  # the timetable arrived: the note stays until a fetch says otherwise
    run_once(board, state, FakeDisplay(), THU_0700 + timedelta(minutes=1))
    assert state["sources"]["bus"]["note"].startswith("There is no bus timetable yet")


# --- The settings reset code page ---


class Waiting:
    """A reset_code hook: the code page while `code` is set."""

    def __init__(self, code=None):
        self.code = code
        self.fail = False

    def __call__(self):
        if self.fail:
            raise PermissionError("reset.lock")
        return self.code


CODE = CodeView("123 456", "07:10", 1_800_000_600.0)


def pixels(image):
    return image.tobytes()


def test_a_waiting_code_takes_the_panel_straight_away():
    trains, board, display, state = minutely(reset_code=Waiting())
    first = run_once(board, state, display, THU_0700)
    assert first.page == "departures"
    board.reset_code.code = CODE
    # A minute later, inside min_redraw: drawn anyway, someone is waiting for it.
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert result.rendered and result.page == "code"
    assert pixels(display.shown[-1]) == pixels(board.theme.render_code(CODE, display.palette, display.panel))
    assert pixels(display.shown[-1]) != pixels(display.shown[0])


def test_the_code_page_is_drawn_once_and_departures_come_back_when_it_goes():
    trains, board, display, state = minutely(reset_code=Waiting(CODE))
    assert run_once(board, state, display, THU_0700).page == "code"
    assert not run_once(board, state, display, THU_0700 + timedelta(minutes=1)).rendered
    board.reset_code.code = None
    back = run_once(board, state, display, THU_0700 + timedelta(minutes=2, seconds=-59))
    assert back.rendered and back.page == "departures"
    assert pixels(display.shown[-1]) == pixels(board.theme.render(back.view, display.palette, display.panel))
    assert len(display.shown) == 2


def test_a_new_code_replaces_the_one_on_the_panel():
    trains, board, display, state = minutely(reset_code=Waiting(CODE))
    run_once(board, state, display, THU_0700)
    board.reset_code.code = CodeView("654 321", "07:11", CODE.expires + 60)
    assert run_once(board, state, display, THU_0700 + timedelta(minutes=1)).rendered


def test_departures_are_still_fetched_while_the_code_shows():
    trains, board, display, state = minutely(reset_code=Waiting(CODE))
    run_once(board, state, display, THU_0700)
    run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert trains.calls == 2
    assert state["sources"]["train"]["departures"]


def test_the_code_never_reaches_the_saved_state():
    trains, board, display, state = minutely(reset_code=Waiting(CODE))
    run_once(board, state, display, THU_0700)
    import hashlib
    import json

    saved = json.dumps(state)
    assert "123" not in saved.replace(str(CODE.expires), "")
    assert hashlib.sha256(b"123 456").hexdigest() not in saved


def test_a_fault_checking_for_a_code_leaves_the_departures_showing():
    trains, board, display, state = minutely(reset_code=Waiting(CODE))
    board.reset_code.fail = True
    result = run_once(board, state, display, THU_0700)
    assert result.rendered and result.page == "departures"


def test_the_code_page_beats_the_bus_page_on_a_screen():
    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, reset_code=Waiting(CODE))
    screen = FakeScreen()
    result = run_once(board, state, screen, THU_0700 + timedelta(seconds=20))
    assert result.page == "code" and result.route is None
    assert screen.shown[-1].size == (1280, 400)


@pytest.mark.parametrize("panel", ["inky-phat", "inky-phat-v1", "waveshare-10in85g", "hdmi-1280x400"])
@pytest.mark.parametrize("look", ["plain", "sign"])
def test_every_panel_and_look_draws_the_code_page(panel, look):
    from twobells.board.panel import PANELS
    from twobells.board.themes import THEMES

    trains, buses, board, _, state = setup()
    board = dataclasses.replace(board, theme=THEMES[look], reset_code=Waiting())

    class Display(FakeDisplay):
        pass

    Display.panel = PANELS[panel]
    display = Display()
    departures = run_once(board, state, display, THU_0700)
    board.reset_code.code = CODE
    code = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert departures.rendered and code.rendered
    image = display.shown[-1]
    assert image.size == PANELS[panel].size and image.mode == "P"
    assert pixels(image) != pixels(display.shown[0])
    assert len(image.getcolors()) >= 2  # something drawn, not a blank panel


def test_the_board_reads_the_code_the_settings_page_left(tmp_path):
    import time

    from twobells.board.app import reset_code_page
    from twobells.web.auth import ResetCodes

    page = reset_code_page(tmp_path, "Europe/London")
    assert page() is None
    codes = ResetCodes(tmp_path)
    now = time.time()
    code = codes.issue(now)
    shown = page()
    expected = datetime.fromtimestamp(now + 600, ZoneInfo("Europe/London")).strftime("%H:%M")
    assert shown == CodeView(f"{code[:3]} {code[3:]}", expected, now + 600)
    codes.verify(code, now + 1)
    assert page() is None


@pytest.mark.parametrize("panel", ["inky-phat-v1", "hdmi-1280x400"])
def test_the_emulator_shows_the_code_page(panel):
    import io

    from PIL import Image

    from twobells.board.panel import PANELS
    from twobells.emulator.server import EmulatorDisplay

    trains, buses, board, _, state = setup()
    board = dataclasses.replace(board, reset_code=Waiting(CODE))
    display = EmulatorDisplay(panel=PANELS[panel])
    assert run_once(board, state, display, THU_0700).page == "code"
    frame = Image.open(io.BytesIO(display.frame_png()))
    assert frame.size == PANELS[panel].size


# --- The set-me-up page, until the board is set up ---

SETUP_PAGE = SetupView("http://raspberrypi.local:8090/settings")


def first_run(**overrides):
    """A board not yet set up: nothing to fetch, and where to set it up."""
    return Board(title="Departures", sources=[], setup=SETUP_PAGE, **overrides), FakeDisplay(), {}


def test_a_board_not_yet_set_up_shows_where_to_do_it_once():
    board, display, state = first_run()
    result = run_once(board, state, display, THU_0700)
    assert result.rendered and result.page == "setup"
    assert pixels(display.shown[-1]) == pixels(board.theme.render_setup(SETUP_PAGE, display.palette, display.panel))
    for minutes in (1, 5, 60):  # e-paper wears: drawn once, not every tick
        assert not run_once(board, state, display, THU_0700 + timedelta(minutes=minutes)).rendered
    assert len(display.shown) == 1


def test_a_code_asked_for_to_claim_the_page_comes_before_the_setup_page():
    board, display, state = first_run(reset_code=Waiting())
    assert run_once(board, state, display, THU_0700).page == "setup"
    board.reset_code.code = CODE
    code = run_once(board, state, display, THU_0700 + timedelta(seconds=30))
    assert code.rendered and code.page == "code"
    board.reset_code.code = None
    back = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert back.rendered and back.page == "setup"  # straight back, whatever min_redraw says


def test_departures_take_over_straight_away_once_set_up():
    board, display, state = first_run()
    run_once(board, state, display, THU_0700)
    trains, set_up, _, _ = minutely()
    result = run_once(set_up, state, display, THU_0700 + timedelta(seconds=30))
    assert result.rendered and result.page == "departures"


def test_a_screen_not_yet_set_up_shows_no_bus_page():
    trains, buses, board, display, state = setup()
    board = dataclasses.replace(board, setup=SETUP_PAGE)
    result = run_once(board, state, FakeScreen(), THU_0700 + timedelta(seconds=20))
    assert result.page == "setup" and result.route is None


@pytest.mark.parametrize("panel", ["inky-phat", "inky-phat-v1", "waveshare-10in85g", "hdmi-1280x400"])
@pytest.mark.parametrize("look", ["plain", "sign"])
def test_every_panel_and_look_draws_the_setup_page(panel, look):
    from twobells.board.panel import PANELS
    from twobells.board.themes import THEMES

    class Display(FakeDisplay):
        pass

    Display.panel = PANELS[panel]
    board, _, state = first_run(theme=THEMES[look])
    display = Display()
    assert run_once(board, state, display, THU_0700).rendered
    image = display.shown[-1]
    assert image.size == PANELS[panel].size and image.mode == "P"
    assert len(image.getcolors()) >= 2  # something drawn, not a blank panel
    assert pixels(image) == pixels(THEMES[look].render_setup(SETUP_PAGE, display.palette, display.panel))


def test_a_delayed_train_stays_on_the_board_until_a_fetch_drops_it():
    # Darwin's "Delayed": late, with no time to go by. It must not vanish at its booked time.
    trains, buses, board, display, state = setup()
    trains.departures = [train("07:00", status=Status.LATE), train("07:30")]
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=1))
    assert [r.left for r in result.view.section("train").rows] == ["07:00", "07:30"]
    assert result.view.section("train").rows[0].right == "Delayed"

    result = run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert [r.left for r in result.view.section("train").rows] == ["07:00", "07:30"]

    trains.departures = [train("07:30")]
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=3))
    assert [r.left for r in result.view.section("train").rows] == ["07:30"]


@pytest.mark.parametrize("saved", ["07:12", 5, None])
def test_malformed_saved_departures_show_none_rather_than_stopping(saved):
    trains, buses, board, display, state = setup()
    trains.error = buses.error = OSError("network down")
    entry = {"last_success": "2026-10-08T06:58:00", "departures": saved}
    state["sources"] = {"train": dict(entry), "bus": dict(entry)}
    result = run_once(board, state, display, THU_0700)
    assert result.view.section("train").rows == () and result.view.section("bus").rows == ()


@pytest.mark.parametrize("saved", BAD_SAVED_TIMES)
def test_a_bad_saved_time_reads_as_missing_with_one_warning_rather_than_stopping(saved, caplog):
    from twobells.board.state import departure_to_dict

    trains, buses, board, display, state = setup()
    trains.error = buses.error = OSError("network down")
    entry = {"last_success": saved, "last_attempt": saved, "departures": [departure_to_dict(train("07:30"))]}
    state["sources"] = {"train": dict(entry), "bus": dict(entry)}
    state["last_render"] = {"signature": "old", "at": saved, "page": "departures"}
    with caplog.at_level("WARNING"):
        result = run_once(board, state, display, THU_0700)
    assert result.rendered
    assert (trains.calls, buses.calls) == (1, 1)  # never tried counts as due
    assert result.view.section("train").rows == ()  # undated, so not shown
    # One warning for each bad time, which is dropped, so it isn't warned of again.
    warned = [r for r in caplog.records if saved in (r.args or ())]
    assert len(warned) == 5
    assert "last_success" not in state["sources"]["train"] and "last_success" not in state["sources"]["bus"]
    caplog.clear()
    with caplog.at_level("WARNING"):
        run_once(board, state, display, THU_0700 + timedelta(minutes=2))
    assert not [r for r in caplog.records if saved in (r.args or ())]


# --- Scrolling text on a screen: frames between ticks only while something scrolls ---

from twobells.board.app import board_seconds, run_loop  # noqa: E402
from twobells.board.scroll import SCROLL_FRAME_S  # noqa: E402

LONG_DESTINATION = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley and Hyde Central"


class ScrollingScreen(FakeScreen):
    def __init__(self, scrolling=True):
        super().__init__()
        self.scrolling = scrolling
        self.frames = []
        self.scrolled = []

    def show_frame(self, frame, seconds):
        self.frames.append((frame, seconds))

    def scroll(self, seconds):
        self.scrolled.append(seconds)


class WallClock:
    """The wall clock, and a sleep that wakes at the next whole multiple of
    what it is given, as the screen loop's does; `late` makes some wake later."""

    def __init__(self, at, late=()):
        self.at, self.late, self.sleeps = at, dict(late), []

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        wait = seconds - self.at % seconds
        if wait < seconds / 1000:  # as sleep_to_the_tick: a whisker short of a boundary is on it
            wait += seconds
        self.at += wait + self.late.pop(len(self.sleeps), 0)


def test_a_screen_gets_a_frame_with_the_text_that_scrolls():
    trains = FakeSource("train", [dataclasses.replace(train(), destination=LONG_DESTINATION)])
    board = make_board(trains, FakeSource("bus", [bus()]))
    screen = ScrollingScreen()
    run_once(board, {}, screen, THU_0700 + timedelta(seconds=15))
    [(frame, seconds)] = screen.frames
    assert seconds == 7 * 3600 + 15  # the board's own clock
    assert [s.text for s in frame.scrolls] == [LONG_DESTINATION]


def test_with_scrolling_off_a_screen_gets_a_still_frame_and_redraws_when_it_changes():
    trains = FakeSource("train", [dataclasses.replace(train(), destination=LONG_DESTINATION)])
    board = dataclasses.replace(make_board(trains, FakeSource("bus", [bus()])), scrolling=False)
    screen = ScrollingScreen()
    state: dict = {}
    run_once(board, state, screen, THU_0700 + timedelta(seconds=15))
    [(frame, _)] = screen.frames
    assert frame.scrolls == ()
    # Turned back on, the board redraws at once, scrolling.
    run_once(dataclasses.replace(board, scrolling=True), state, screen, THU_0700 + timedelta(seconds=15))
    assert [s.text for s in screen.frames[-1][0].scrolls] == [LONG_DESTINATION]


def test_e_paper_gets_a_still_image_with_nothing_scrolling():
    trains = FakeSource("train", [dataclasses.replace(train(), destination=LONG_DESTINATION)])
    board = make_board(trains, FakeSource("bus", [bus()]))
    display = FakeDisplay()
    run_once(board, {}, display, THU_0700)
    [image] = display.shown
    assert image.size == PHAT.size


def test_board_seconds_are_the_board_clock_into_the_day():
    assert board_seconds(datetime(2026, 10, 8, 7, 3, 15)) == 7 * 3600 + 3 * 60 + 15


def test_while_text_scrolls_the_loop_draws_frames_between_ticks():
    trains, buses, board, display, state = setup()
    screen, wall = ScrollingScreen(), WallClock(1000.0)
    times = iter([THU_0700, THU_0700 + timedelta(seconds=1)])
    run_loop(board, state, screen, lambda: next(times), 1, ticks=2, sleep=wall.sleep,
             frame_seconds=SCROLL_FRAME_S, wall=wall)
    base = board_seconds(THU_0700)
    per_second = round(1 / SCROLL_FRAME_S)
    assert screen.scrolled == [base + k * SCROLL_FRAME_S for k in range(per_second)] + [
        base + 1 + k * SCROLL_FRAME_S for k in range(per_second)]
    assert wall.sleeps == [SCROLL_FRAME_S] * 2 * per_second
    assert wall.at == 1002.0  # each tick on its whole second


def test_with_nothing_scrolling_the_loop_goes_back_to_once_a_second():
    trains, buses, board, display, state = setup()
    screen, wall = ScrollingScreen(scrolling=False), WallClock(1000.0)
    times = iter([THU_0700, THU_0700 + timedelta(seconds=1)])
    run_loop(board, state, screen, lambda: next(times), 1, ticks=2, sleep=wall.sleep,
             frame_seconds=SCROLL_FRAME_S, wall=wall)
    assert screen.scrolled == []
    assert wall.sleeps == [1, 1]


def test_a_display_that_cannot_scroll_ticks_once_a_second():
    trains, buses, board, display, state = setup()
    sleeps = []
    times = iter([THU_0700, THU_0700 + timedelta(seconds=1)])
    run_loop(board, state, FakeScreen(), lambda: next(times), 1, ticks=2, sleep=sleeps.append,
             frame_seconds=SCROLL_FRAME_S)
    assert sleeps == [1, 1]


def test_a_late_frame_is_skipped_and_the_next_tick_is_not_put_off():
    trains, buses, board, display, state = setup()
    # The third frame wakes three frames late.
    screen, wall = ScrollingScreen(), WallClock(1000.0, late={3: 3 * SCROLL_FRAME_S})
    run_loop(board, state, screen, lambda: THU_0700, 1, ticks=1, sleep=wall.sleep,
             frame_seconds=SCROLL_FRAME_S, wall=wall)
    base = board_seconds(THU_0700)
    due = [round((s - base) / SCROLL_FRAME_S) for s in screen.scrolled]
    per_second = round(1 / SCROLL_FRAME_S)
    assert due == [0, 1, 2, 6, *range(7, per_second)]  # 3, 4 and 5 skipped; each placed by its own time
    assert wall.at == 1001.0


def test_a_tick_that_ran_long_still_ends_on_its_second():
    trains, buses, board, display, state = setup()
    screen, wall = ScrollingScreen(), WallClock(1000.9)  # drawing the board took most of the second
    run_loop(board, state, screen, lambda: THU_0700, 1, ticks=1, sleep=wall.sleep,
             frame_seconds=SCROLL_FRAME_S, wall=wall)
    assert wall.at == 1001.0


# A wide screen names each bus row's stop, from the bus source's timetable.


class NamedBusSource(FakeSource):
    def __init__(self, names):
        super().__init__("bus", [
            Departure("bus", "341", "Hyde", "07:20", None, Status.ON_TIME, stop="A"),
            Departure("bus", "61", "Buxton", "07:35", None, Status.ON_TIME, stop="B"),
            Departure("bus", "237", "Ashton", "07:41", None, Status.ON_TIME, stop="C"),
        ])
        self.names = names

    def stop_names(self):
        if isinstance(self.names, Exception):
            raise self.names
        return self.names


def named_board(names):
    return make_board(FakeSource("train", [train()]), NamedBusSource(names))


def test_a_screen_s_bus_rows_carry_their_stops_names_told_apart_when_they_share_one():
    board = named_board({"A": "Henry Street", "B": "Henry Street"})
    view = run_once(board, {}, FakeScreen(), THU_0700).view
    assert [(r.stop, r.stop_name) for r in view.section("bus").rows] == [
        ("A", "Henry Street A"), ("B", "Henry Street B"), ("C", None),
    ]
    assert view.section("bus").stop_names == ("Henry Street A", "Henry Street B")


def test_e_paper_never_asks_for_stop_names():
    board = named_board(AssertionError("asked"))
    view = run_once(board, {}, FakeDisplay(), THU_0700).view
    assert all(r.stop_name is None for r in view.section("bus").rows) and view.section("bus").stop_names == ()


def test_a_fault_reading_stop_names_leaves_the_labels():
    view = run_once(named_board(OSError("timetable unreadable")), {}, FakeScreen(), THU_0700).view
    assert [r.stop for r in view.section("bus").rows] == ["A", "B", "C"]
    assert all(r.stop_name is None for r in view.section("bus").rows)


class LastBusSource(FakeSource):
    """Buses, and the last bus of each line as BodsBusSource gives them."""

    def __init__(self, last):
        super().__init__("bus", [bus()])
        self.last = last
        self.asked = []

    def last_buses(self, now=None):
        self.asked.append(now)
        if isinstance(self.last, Exception):
            raise self.last
        return self.last


def last_bus_board(last):
    from twobells.sources.lastbus import LastBus

    found = [LastBus(line, "Hyde", "A", THU_0700.date(), THU_0700.replace(hour=hh, minute=mm), left)
             for line, hh, mm, left in last] if isinstance(last, list) else last
    return make_board(FakeSource("train", [train()]), LastBusSource(found))


def test_a_screen_says_the_last_bus_in_its_last_hour():
    board = last_bus_board([("341", 22, 0, 1), ("237", 23, 5, 3)])
    evening = THU_0700.replace(hour=22, minute=4)
    assert run_once(board, {}, FakeScreen(), evening).view.last_bus is None  # 61 minutes before
    assert run_once(board, {}, FakeScreen(), evening + timedelta(minutes=1)).view.last_bus == "Last bus 23:05"


def test_no_last_bus_once_it_has_gone_or_on_e_paper():
    gone = last_bus_board([("341", 22, 0, 0)])
    assert run_once(gone, {}, FakeScreen(), THU_0700.replace(hour=22, minute=30)).view.last_bus is None
    board = last_bus_board([("341", 23, 0, 1)])
    assert run_once(board, {}, FakeDisplay(), THU_0700.replace(hour=22, minute=30)).view.last_bus is None
    assert board.sources[1].source.asked == []  # e-paper never asks


def test_a_fault_finding_the_last_bus_costs_only_the_last_bus():
    board = last_bus_board(OSError("timetable unreadable"))
    view = run_once(board, {}, FakeScreen(), THU_0700).view
    assert view.last_bus is None and view.section("bus").rows


# What the board shows ([board] show, settings/show.py)


def test_without_the_bus_page_a_screen_stays_on_the_departures():
    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    board = dataclasses.replace(board, show=board.show - {"bus_page"})
    result = run_once(board, state, FakeScreen(), ROUTE_TIME)
    assert (result.page, result.route) == ("departures", None)


def test_without_the_bus_page_the_clock_page_follows_the_departures():
    board, _, state = tracked_setup([bus_on("341", "07:20")], [vehicle_at(2)])
    board = dataclasses.replace(board, show=(board.show - {"bus_page"}) | {"clock_page"})
    shown = pages(board, state, FakeScreen(), THU_0700, range(25))
    assert shown == ["departures"] * 15 + ["clock"] * 10


def test_without_the_last_bus_a_screen_never_says_it():
    board = last_bus_board([("341", 22, 0, 1), ("237", 23, 5, 3)])
    board = dataclasses.replace(board, show=board.show - {"last_bus"})
    evening = THU_0700.replace(hour=22, minute=5)
    assert run_once(board, {}, FakeScreen(), evening).view.last_bus is None


def test_without_disruption_the_station_s_messages_are_kept_but_not_shown():
    trains, board, display, state = notice_setup()
    board = dataclasses.replace(board, show=board.show - {"disruption"})
    result = run_once(board, state, display, THU_0700)
    assert state["sources"]["train"]["messages"] == ["Lifts out of use."]
    assert result.view.notice is None


# Station announcements through a screen's speaker (board/speaker.py)


class FakeSpeaker:
    def __init__(self):
        self.ticks = []

    def tick(self, trains, now, quiet):
        self.ticks.append(([dep.scheduled for dep in trains], now, quiet))
        return []


def speaker_run(board, display, times):
    from twobells.board.app import run_loop

    speaker = FakeSpeaker()
    moments = iter(times)
    run_loop(board, {}, display, lambda: next(moments), 0, ticks=len(times), sleep=lambda s: None,
             speaker=speaker, prime=True)
    return speaker.ticks


def speaking_board(**changes):
    trains, buses, board, _, _ = setup()
    trains.departures = [train("07:04")]
    return dataclasses.replace(board, show=board.show | {"announce_speaker"}, **changes)


def test_a_screen_s_speaker_is_given_the_trains_each_tick_while_the_board_shows_it():
    ticks = speaker_run(speaking_board(), FakeScreen(), [THU_0700, THU_0700 + timedelta(seconds=1)])
    assert ticks == [(["07:04"], THU_0700, False), (["07:04"], THU_0700 + timedelta(seconds=1), False)]


def test_the_speaker_is_quiet_outside_the_train_windows_and_in_the_quiet_hours():
    assert speaker_run(speaking_board(), FakeScreen(), [THU_0700.replace(hour=6)])[0][2] is True
    quiet = speaking_board(quiet=(parse_window("Mon-Sun 06:00-07:30"),))
    assert speaker_run(quiet, FakeScreen(), [THU_0700])[0][2] is True


def test_the_speaker_says_nothing_unless_chosen_on_e_paper_or_before_setup():
    assert speaker_run(dataclasses.replace(speaking_board(), show=frozenset()), FakeScreen(), [THU_0700]) == []
    assert speaker_run(speaking_board(), FakeDisplay(), [THU_0700]) == []
    setup_page = SetupView("http://twobells.local:8090/settings/setup/")
    assert speaker_run(speaking_board(setup=setup_page), FakeScreen(), [THU_0700]) == []


# London's metro, live from TfL: its section is named for its stops, and a
# fetch without its lines' status keeps the notice it had.


class LondonSource(FakeSource):
    label = "Underground"
    empty = "No trains"

    def __init__(self, departures, messages):
        super().__init__("metro", departures)
        self.next_messages = messages
        self.messages = []

    def fetch(self):
        found = super().fetch()
        self.messages = self.next_messages
        return found


def test_london_s_section_has_its_own_heading_and_keeps_its_notice_without_a_status():
    tubes = LondonSource([Departure("metro", "Central", "Epping", "07:04", None, Status.ON_TIME, stop="T")],
                         ["Central line: Minor delays."])
    board = Board(title="Bank", sources=[SourceConfig(tubes, morning(1), stale_after=timedelta(minutes=10))],
                  modes=("metro",))
    state = {}
    result = run_once(board, state, FakeDisplay(), THU_0700)
    section = result.view.section("metro")
    assert (section.label, section.empty_text, section.rows[0].right) == ("Underground", "No trains", "Live")
    assert result.view.notice == "Central line: Minor delays."
    tubes.next_messages = None  # the status couldn't be had this time
    result = run_once(board, state, FakeDisplay(), THU_0700 + timedelta(minutes=1))
    assert result.view.notice == "Central line: Minor delays."
