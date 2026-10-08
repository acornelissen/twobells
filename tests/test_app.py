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
        refresh_every=timedelta(minutes=10),
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


def test_same_data_rerenders_after_refresh_period_to_update_stamp():
    trains, buses, board, display, state = setup()
    trains.departures = [train("08:30")]
    buses.departures = [bus("08:40")]
    run_once(board, state, display, THU_0700)
    result = run_once(board, state, display, THU_0700 + timedelta(minutes=10))
    assert result.rendered


def test_bus_fetch_refused_when_daily_budget_spent():
    trains, buses, board, display, state = setup(bus_budget=1)
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
