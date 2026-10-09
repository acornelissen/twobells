import dataclasses
from datetime import datetime, timedelta

from departureboard.app import Board, SourceConfig, run_once
from departureboard.model import Departure, Status
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
