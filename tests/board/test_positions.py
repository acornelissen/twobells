"""Where each shown train and bus is, on a screen's rows: asked for only on
screens, kept in memory only, and dropped rather than shown stale."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

from tests.paths import FIXTURES
from twobells.board.app import Board, SourceConfig, run_once
from twobells.board.panel import HDMI_1280X400, PHAT
from twobells.board.positions import Positions
from twobells.board.render import Palette
from twobells.board.state import StateStore
from twobells.core.model import Departure, Status, Strip, strip_key
from twobells.core.polling import PollPolicy, parse_window
from twobells.sources.darwin import (
    DetailsError,
    DetailsUnavailable,
    ServiceGone,
    TrainDetails,
    parse_board_rich,
    parse_service_details,
    train_position,
)

# Friday night: the fixture board's trains leave Glossop at 22:08, 22:25, 22:38, 22:55 and 23:10.
TONIGHT = datetime(2026, 10, 9, 22, 3)
IDS = ["9000001GLSP____", "9000002GLSP____", "9000003GLSP____", "9000004GLSP____", "9000005GLSP____"]
ALWAYS = (parse_window("Mon-Sun 00:00-23:59"),)
EVENINGS = (parse_window("Mon-Sun 21:00-22:10"),)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Trains:
    """Darwin's board for the fixture night, with its service IDs, as DarwinSource keeps them."""

    name = "train"

    def __init__(self):
        self.services = []
        self.calls = 0

    def fetch(self):
        self.calls += 1
        self.services = parse_board_rich(load_fixture("ldbws_glossop_details.json")).services
        return [s.departure for s in self.services]


BUS = Departure("bus", "341", "Hadfield", "22:20", None, Status.ON_TIME, stop="A")
WHERE = Strip("3 stops away", 7, 5, 2.5)


class Buses:
    name = "bus"

    def __init__(self, strips=None):
        self.strips = {strip_key(BUS): WHERE} if strips is None else strips
        self.asked = []  # reuse, each time live strips were asked for
        self.error = None

    def fetch(self):
        return [BUS]

    def live_strips(self, reuse=False):
        self.asked.append(reuse)
        if self.error:
            raise self.error
        return dict(self.strips)


class Details:
    """Service details for any train: the synthetic Hadfield train's."""

    def __init__(self):
        self.asked = []
        self.errors = {}  # service ID -> exception, or "*" for every train

    def details(self, service_id):
        self.asked.append(service_id)
        error = self.errors.get(service_id) or self.errors.get("*")
        if error:
            raise error
        return parse_service_details(load_fixture("ldbws_service_details_from_hadfield.json"))


class Display:
    palette = Palette(white=0, black=1, accent=2)

    def __init__(self, panel):
        self.panel = panel

    def show(self, image):
        pass


class Setup:
    def __init__(self, train_windows=ALWAYS, bus_windows=ALWAYS, panel=HDMI_1280X400, details=True):
        self.now = TONIGHT
        self.trains, self.buses, self.details = Trains(), Buses(), Details()
        cache = TrainDetails(self.details, monotonic=lambda: self.now.timestamp()) if details else None
        self.board = Board(
            title="Glossop",
            sources=[
                SourceConfig(self.trains, PollPolicy(train_windows, timedelta(minutes=2)), timedelta(minutes=6)),
                SourceConfig(self.buses, PollPolicy(bus_windows, timedelta(minutes=2)), timedelta(minutes=6)),
            ],
            positions=Positions(details=cache),
        )
        self.display = Display(panel)
        self.state = {}

    def tick(self, seconds=0):
        self.now = TONIGHT + timedelta(seconds=seconds)
        return run_once(self.board, self.state, self.display, self.now)

    def run(self, seconds):
        """Ticks once a second, as a screen does, to `seconds` on; the last view."""
        for second in range(seconds + 1):
            view = self.tick(second).view
        return view


def train_strip(view, index=0):
    return view.section("train").rows[index].strip


# Buses


def test_a_screen_s_bus_rows_say_where_the_bus_is():
    setup = Setup()
    view = setup.tick().view
    assert view.section("bus").rows[0].strip == WHERE


def test_the_live_board_of_a_departures_fetch_is_used_without_asking_again():
    setup = Setup()
    setup.tick()
    assert setup.buses.asked == [True]  # fetched this tick: its live board is reused


def test_bus_positions_are_asked_for_every_30_seconds():
    setup = Setup()
    setup.run(107)
    # A departures fetch at 0 s, then every 30 s; the next fetch (due at 108 s,
    # PollPolicy's slack) brings its own.
    assert setup.buses.asked == [True, False, False, False]
    setup.tick(108)
    assert setup.buses.asked == [True, False, False, False, True]


def test_a_failure_gives_no_bus_strip_rather_than_the_last_one():
    setup = Setup()
    setup.tick()
    setup.buses.error = RuntimeError("there is no bus timetable yet")
    assert setup.tick(30).view.section("bus").rows[0].strip is None
    setup.buses.error = None
    assert setup.tick(60).view.section("bus").rows[0].strip == WHERE


def test_bus_strips_older_than_two_minutes_are_dropped():
    setup = Setup(bus_windows=EVENINGS)  # the bus windows close at 22:10
    first = TONIGHT.replace(minute=9, second=50)
    assert run_once(setup.board, setup.state, setup.display, first).view.section("bus").rows[0].strip == WHERE
    later = first + timedelta(minutes=2)
    assert run_once(setup.board, setup.state, setup.display, later).view.section("bus").rows[0].strip == WHERE
    gone = later + timedelta(seconds=1)
    assert run_once(setup.board, setup.state, setup.display, gone).view.section("bus").rows[0].strip is None
    assert setup.buses.asked == [True]  # nothing asked once the windows closed


def test_without_where_it_is_nothing_is_asked_where_the_trains_and_buses_are():
    setup = Setup()
    setup.board = replace(setup.board, show=setup.board.show - {"where"})
    view = setup.run(90)
    assert setup.trains.calls == 1  # the departures are still fetched
    assert setup.details.asked == [] and setup.buses.asked == []
    assert all(r.strip is None for r in view.section("train").rows + view.section("bus").rows)


def test_anything_but_a_strip_is_dropped():
    setup = Setup()
    setup.buses.strips = {strip_key(BUS): {"where": "At your stop"}}
    assert setup.tick().view.section("bus").rows[0].strip is None


# Trains


def test_a_screen_s_train_rows_say_where_the_train_is():
    setup = Setup()
    view = setup.tick().view
    found = train_position(parse_service_details(load_fixture("ldbws_service_details_from_hadfield.json")),
                           TONIGHT)
    assert found.where == "At Glossop now"  # the dashboard's words
    assert train_strip(view) == Strip("At Glossop", len(found.strip_stops), found.yours, found.train_at)
    assert [r.strip is not None for r in view.section("train").rows] == [True, True, True, False, False]
    assert setup.details.asked == IDS[:3]


def test_only_trains_still_to_leave_are_asked_about():
    setup = Setup()
    run_once(setup.board, setup.state, setup.display, TONIGHT.replace(minute=9))
    assert setup.details.asked == IDS[1:4]  # Darwin still lists the 22:08, gone a minute


def test_each_train_is_asked_for_at_most_once_a_minute():
    setup = Setup()
    setup.run(179)
    assert setup.details.asked == IDS[:3] * 3  # at 0, 60 and 120 s


def test_a_failure_waits_a_minute_before_asking_again():
    setup = Setup()
    setup.details.errors["*"] = DetailsError("HTTP Error 500")
    setup.run(59)
    assert setup.details.asked == IDS[:3]  # once each, not every second
    setup.tick(60)
    assert setup.details.asked == IDS[:3] * 2


def test_a_train_darwin_no_longer_knows_is_not_asked_for_every_second():
    setup = Setup()
    setup.details.errors[IDS[0]] = ServiceGone("Darwin no longer knows this train")
    view = setup.run(59)
    assert setup.details.asked == IDS[:3]
    assert train_strip(view) is None and train_strip(view, 1) is not None


def test_a_refused_key_stops_asking():
    setup = Setup()
    setup.details.errors["*"] = DetailsUnavailable("Service details refused the key (HTTP 403)")
    view = setup.run(300)
    assert setup.details.asked == [IDS[0]]
    assert all(r.strip is None for r in view.section("train").rows)


def test_train_details_older_than_two_minutes_are_dropped():
    setup = Setup()
    setup.tick()
    setup.details.errors["*"] = DetailsError("timed out")
    assert train_strip(setup.run(120)) is not None
    assert train_strip(setup.tick(121).view) is None


def test_trains_are_only_asked_about_in_the_train_windows():
    setup = Setup(train_windows=(parse_window("Mon-Fri 06:30-09:30"),))
    result = run_once(setup.board, setup.state, setup.display, TONIGHT, prime=True)  # fetched, out of hours
    assert setup.trains.calls == 1 and setup.details.asked == []
    assert all(r.strip is None for r in result.view.section("train").rows)


def test_no_train_strips_without_a_details_key():
    setup = Setup(details=False)
    view = setup.tick().view
    assert all(r.strip is None for r in view.section("train").rows)
    assert view.section("bus").rows[0].strip == WHERE


# E-paper and the SD card


def test_e_paper_never_asks_where_anything_is():
    setup = Setup(panel=PHAT)
    view = setup.run(300)
    assert setup.buses.asked == [] and setup.details.asked == []
    assert all(r.strip is None for r in (*view.section("train").rows, *view.section("bus").rows))
    assert "strips" not in setup.state


def test_strips_are_never_written_to_the_card(tmp_path):
    setup = Setup()
    setup.tick()
    assert setup.state["strips"]  # kept in memory, beside the departures
    store = StateStore(tmp_path / "state.json")
    store.save(setup.state)
    saved = json.loads((tmp_path / "state.json").read_text())
    assert "strips" not in saved and saved["sources"]["bus"]["departures"]
