import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.dashboard import repay
from twobells.dashboard.history import History
from twobells.dashboard.performance import Performance, Setup
from twobells.dashboard.repay import ArrivalWatch, band, watched
from twobells.sources.darwin import (
    DetailsError,
    DetailsUnavailable,
    ServiceGone,
    parse_board_rich,
    parse_service_details,
)

LONDON = ZoneInfo("Europe/London")
FRIDAY = date(2026, 10, 9)
PREFIX = "GLO>MAN"
MANCHESTER = "GLO>MAN|Manchester Piccadilly"
# The Glossop board's trains to Manchester: the 22:25 (due 22:57) and the 22:55 (due 23:27).
FIRST, SECOND = "9000002GLSP____", "9000004GLSP____"


def at(hhmm):
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(2026, 10, 9, hours, minutes, tzinfo=LONDON)


def glossop(without=(), std=None, **changes):
    """The Glossop board, less the trains in `without`, with the 22:25's calling point at Manchester
    changed, and its time to leave if `std`."""
    raw = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    raw["trainServices"] = [s for s in raw["trainServices"] if s["serviceID"] not in without]
    for service in raw["trainServices"]:
        if service["serviceID"] == FIRST:
            service["subsequentCallingPoints"][0]["callingPoint"][-1].update(changes)
            service["std"] = std or service["std"]
    return parse_board_rich(raw)


class Details:
    """One train's journey, Manchester's times as set."""

    def __init__(self):
        self.asked = []
        self.error = None
        self.manchester = {"et": "On time"}

    def details(self, service_id):
        self.asked.append(service_id)
        if self.error:
            raise self.error
        point = {"locationName": "Manchester Piccadilly", "crs": "MAN", "st": "22:57", **self.manchester}
        return parse_service_details({"locationName": "Glossop", "crs": "GLO", "std": "22:25", "atd": "22:41",
                                      "subsequentCallingPoints": [{"callingPoint": [point]}]})


class Monotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def history(tmp_path):
    history = History(tmp_path)
    yield history
    history.close()


def _watch(history, details, monotonic=None):
    return ArrivalWatch(history, "MAN", PREFIX, details, monotonic=monotonic or Monotonic())


@pytest.mark.parametrize("minutes, found", [(0, None), (14, None), (15, 15), (29, 15), (30, 30), (95, 30), (-3, None)])
def test_the_delay_repay_bands(minutes, found):
    assert band(minutes) == found


def test_the_trains_on_the_board_that_call_there():
    rows = watched(glossop(et="23:05"), at("22:20"), PREFIX, "MAN")
    assert [(r.scheduled, r.identity, r.booked, r.expected) for r in rows] == [
        ("22:25", FIRST, at("22:57"), at("23:05")),
        ("22:55", SECOND, at("23:27"), at("23:27")),  # "On time"
    ]
    first = rows[0]
    assert (first.route, first.day, first.station, first.station_name, first.operator, first.destination) == (
        MANCHESTER, FRIDAY, "MAN", "Manchester Piccadilly", "Northern", "Manchester Piccadilly")
    assert (first.actual, first.cancelled) == (None, False)
    assert watched(glossop(), at("22:20"), PREFIX, "SHF") == []  # none of them call there


@pytest.mark.parametrize("changes, expected, cancelled", [
    ({"et": "Delayed"}, None, False),
    ({"et": "No report"}, None, False),
    ({"et": "soon"}, None, False),
    ({"et": "Cancelled"}, None, True),
    ({"isCancelled": True}, None, True),
])
def test_what_the_board_says_about_its_time_there(changes, expected, cancelled):
    first = watched(glossop(**changes), at("22:20"), PREFIX, "MAN")[0]
    assert (first.expected, first.cancelled) == (expected, cancelled)


def test_a_train_due_after_midnight_keeps_the_day_it_left():
    rows = watched(glossop(st="00:05", et="00:20"), at("22:20"), PREFIX, "MAN")
    assert (rows[0].day, rows[0].booked, rows[0].expected) == (
        FRIDAY, datetime(2026, 10, 10, 0, 5, tzinfo=LONDON), datetime(2026, 10, 10, 0, 20, tzinfo=LONDON))


def test_rail_replacement_buses_are_left_out():
    raw = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    raw["busServices"] = [raw["trainServices"].pop(1)]
    rows = watched(parse_board_rich(raw), at("22:20"), PREFIX, "MAN")
    assert [r.identity for r in rows] == [SECOND]


def test_ferries_are_left_out():
    raw = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    raw["ferryServices"] = [{**raw["trainServices"].pop(1), "serviceType": "ferry"}]
    rows = watched(parse_board_rich(raw), at("22:20"), PREFIX, "MAN")
    assert [r.identity for r in rows] == [SECOND]


def test_followed_with_service_details_only_once_it_has_left_and_is_due(history):
    details = Details()
    watch = _watch(history, details)
    assert watch.poll(glossop(without=[SECOND]), at("22:20")) == []
    assert details.asked == []  # on the board, and not due yet: nothing to ask
    watch.poll(glossop(without=[SECOND]), at("22:46"))
    assert details.asked == []  # due at 22:57, but still on your board: its calling point says
    details.manchester = {"et": "23:30"}
    watch.poll(glossop(without=[FIRST, SECOND]), at("22:50"))
    assert details.asked == [FIRST]  # gone, and due: one ask a poll
    # Now due at 23:30: not asked about again until 10 minutes before.
    assert history.following(at("23:19")) == []
    assert history.following(at("23:20"))[0].expected == at("23:30")
    details.manchester = {"et": "23:30", "at": "23:31"}
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:31"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["scheduled"], arrived["booked"], arrived["arrived"], arrived["reported"]) == (
        "22:25", "2026-10-09T21:57:00+00:00", "2026-10-09T22:31:00+00:00", True)
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:32"))
    assert details.asked == [FIRST, FIRST]  # done with


def test_a_train_darwin_forgets_is_left_to_its_last_estimate(history):
    details = Details()
    watch = _watch(history, details)
    watch.poll(glossop(et="22:59"), at("22:20"))
    details.manchester = {"et": "22:59"}
    watch.poll(glossop(without=[FIRST]), at("22:57"))
    details.error = ServiceGone("gone")
    assert watch.poll(glossop(without=[FIRST]), at("23:01")) == []
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["arrived"], arrived["reported"]) == ("2026-10-09T21:59:00+00:00", False)


def test_a_train_darwin_no_longer_knows_is_not_asked_about_again(history):
    details = Details()
    watch = _watch(history, details)
    watch.poll(glossop(et="23:10"), at("22:20"))
    details.error = ServiceGone("gone")
    # Not a failure to report, and not asked about again while it waits to be settled.
    assert watch.poll(glossop(without=[FIRST]), at("22:50")) == []
    for minute in ("22:52", "23:00", "23:20", "23:40"):
        assert watch.poll(glossop(without=[FIRST]), at(minute)) == []
    assert details.asked == [FIRST]


def test_each_train_is_asked_about_at_most_once_a_minute(history):
    details = Details()
    watch = _watch(history, details)
    details.manchester = {"et": "22:59"}
    watch.poll(glossop(et="22:59"), at("22:20"))
    board = glossop(without=[FIRST])
    watch.poll(board, at("22:50"))
    watch.poll(board, at("22:50").replace(second=30))
    assert details.asked == [FIRST]
    watch.poll(board, at("22:51"))
    assert details.asked == [FIRST, FIRST]
    # The clock set back doesn't hold it up until it catches up.
    watch.poll(board, at("22:50"))
    assert details.asked == [FIRST, FIRST, FIRST]


def test_a_failure_that_repeats_is_given_once(history):
    details = Details()
    watch = _watch(history, details)
    details.manchester = {"et": "22:59"}
    watch.poll(glossop(et="22:59"), at("22:20"))
    board = glossop(without=[FIRST])
    details.error = DetailsError("down")
    assert watch.poll(board, at("22:50")) == [details.error]
    assert watch.poll(board, at("22:51")) == []
    assert watch.poll(board, at("22:52")) == []
    other = details.error = DetailsError("timed out")
    assert watch.poll(board, at("22:53")) == [other]
    details.error = None
    assert watch.poll(board, at("22:54")) == []
    again = details.error = DetailsError("timed out")
    assert watch.poll(board, at("22:55")) == [again]  # after it worked, it is news again
    assert len(details.asked) == 6


def test_a_refused_key_stops_following_for_six_hours(history):
    details, monotonic = Details(), Monotonic()
    watch = _watch(history, details, monotonic)
    watch.poll(glossop(), at("22:20"))
    details.error = DetailsUnavailable("refused")
    assert watch.poll(glossop(without=[FIRST]), at("22:50")) == [details.error]
    watch.poll(glossop(without=[FIRST]), at("22:51"))
    assert len(details.asked) == 1
    monotonic.now += repay.OFF_FOR
    details.error = None
    watch.poll(glossop(without=[FIRST]), at("22:52"))
    assert len(details.asked) == 2


def test_other_failures_are_tried_again_next_poll(history):
    details = Details()
    watch = _watch(history, details)
    watch.poll(glossop(), at("22:20"))
    details.error = DetailsError("down")
    assert watch.poll(glossop(without=[FIRST]), at("22:50")) == [details.error]
    watch.poll(glossop(without=[FIRST]), at("22:51"))
    assert len(details.asked) == 2


def test_a_train_nobody_saw_arrive_is_dropped(history):
    details = Details()
    details.error = DetailsError("down")
    watch = _watch(history, details)
    watch.poll(glossop(without=[SECOND], et="23:00"), at("22:20"))
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:02"))
    assert history.arrivals("MAN", FRIDAY) == []  # seen at 22:20, due at 23:00: nobody saw it get there
    assert [train.scheduled for train in history.following(at("23:59"))] == ["22:25"]
    watch.poll(glossop(without=[FIRST, SECOND]), at("00:00").replace(day=10))
    assert history.following(at("23:59")) == [] and history.arrivals("MAN", FRIDAY) == []


# The clocks changing: minutes late are the minutes that passed.

GOING_BACK = date(2026, 10, 25)  # 01:00 to 01:59 comes twice: in BST, then in GMT
GOING_FORWARD = date(2026, 3, 29)  # 01:00 GMT is 02:00 BST


def on(day, hhmm, fold=0):
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, fold=fold, tzinfo=LONDON)


def _listed(history, now):
    setup = Setup({"train": 3, "bus": 2}, station="MAN", trains=True, details=True)
    found = Performance(history, lambda: now, lambda: setup).report()["delay_repay"]["trains"]
    return [(t["day"], t["booked"], t["arrived"], t["minutes_late"], t["band"]) for t in found]


def test_late_across_the_clocks_going_back(history):
    # Left at 01:20 BST, due at Manchester at 01:50 BST, there at 01:10 GMT: 20 minutes late, not 40 early.
    details = Details()
    watch = _watch(history, details)
    watch.poll(glossop([SECOND], std="01:20", st="01:50", et="01:58"), on(GOING_BACK, "01:05"))
    details.manchester = {"st": "01:50", "et": "01:10"}
    watch.poll(glossop([FIRST, SECOND]), on(GOING_BACK, "01:45"))
    details.manchester = {"st": "01:50", "et": "01:10", "at": "01:10"}
    watch.poll(glossop([FIRST, SECOND]), on(GOING_BACK, "01:11", fold=1))
    assert _listed(history, on(GOING_BACK, "09:00")) == [("2026-10-25", "01:50", "01:10", 20, 15)]


def test_late_across_the_clocks_going_forward(history):
    # Left at 00:20 GMT, due at Manchester at 00:50 GMT, there at 02:10 BST: 20 minutes late, not 80.
    details = Details()
    watch = _watch(history, details)
    watch.poll(glossop([SECOND], std="00:20", st="00:50", et="00:55"), on(GOING_FORWARD, "00:15"))
    details.manchester = {"st": "00:50", "et": "02:10"}
    watch.poll(glossop([FIRST, SECOND]), on(GOING_FORWARD, "00:45"))
    details.manchester = {"st": "00:50", "et": "02:10", "at": "02:10"}
    watch.poll(glossop([FIRST, SECOND]), on(GOING_FORWARD, "02:11"))
    assert _listed(history, on(GOING_FORWARD, "09:00")) == [("2026-03-29", "00:50", "02:10", 20, 15)]
