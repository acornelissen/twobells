import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.dashboard import repay
from twobells.dashboard.history import History
from twobells.dashboard.repay import ArrivalWatch, band, watched
from twobells.sources.arrivalboard import ArrivalsRefused, parse_arrivals
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


def naive(hhmm):
    return at(hhmm).replace(tzinfo=None)


def glossop(without=(), **changes):
    """The Glossop board, less the trains in `without`, with the 22:25's calling point at Manchester changed."""
    raw = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    raw["trainServices"] = [s for s in raw["trainServices"] if s["serviceID"] not in without]
    for service in raw["trainServices"]:
        if service["serviceID"] == FIRST:
            service["subsequentCallingPoints"][0]["callingPoint"][-1].update(changes)
    return parse_board_rich(raw)


def arrival_board(*services, **changes):
    """The Manchester arrival board with only `services` on it, the 22:25's changed."""
    raw = json.loads((FIXTURES / "ldbws_manchester_arrivals.json").read_text())
    raw["trainServices"] = [s for s in raw["trainServices"] if isinstance(s, dict) and s.get("serviceID") in services]
    for service in raw["trainServices"]:
        if service["serviceID"] == FIRST:
            service.update(changes)
    return parse_arrivals(raw, "GLO")


class Arrivals:
    """The arrival board, as set; every ask is counted."""

    def __init__(self):
        self.asked = []
        self.board = []
        self.error = None

    def arrivals(self, crs, from_crs):
        self.asked.append((crs, from_crs))
        if self.error:
            raise self.error
        return self.board


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


def _watch(history, details=None, arrivals=None, monotonic=None):
    return ArrivalWatch(history, "GLO", "MAN", PREFIX, details=details, arrivals=arrivals,
                        monotonic=monotonic or Monotonic())


@pytest.mark.parametrize("minutes, found", [(0, None), (14, None), (15, 15), (29, 15), (30, 30), (95, 30), (-3, None)])
def test_the_delay_repay_bands(minutes, found):
    assert band(minutes) == found


def test_the_trains_on_the_board_that_call_there():
    rows = watched(glossop(et="23:05"), naive("22:20"), PREFIX, "MAN")
    assert [(r.scheduled, r.identity, r.booked, r.expected) for r in rows] == [
        ("22:25", FIRST, naive("22:57"), naive("23:05")),
        ("22:55", SECOND, naive("23:27"), naive("23:27")),  # "On time"
    ]
    first = rows[0]
    assert (first.route, first.day, first.station, first.station_name, first.operator, first.destination) == (
        MANCHESTER, FRIDAY, "MAN", "Manchester Piccadilly", "Northern", "Manchester Piccadilly")
    assert (first.actual, first.cancelled) == (None, False)
    assert watched(glossop(), naive("22:20"), PREFIX, "SHF") == []  # none of them call there


@pytest.mark.parametrize("changes, expected, cancelled", [
    ({"et": "Delayed"}, None, False),
    ({"et": "No report"}, None, False),
    ({"et": "soon"}, None, False),
    ({"et": "Cancelled"}, None, True),
    ({"isCancelled": True}, None, True),
])
def test_what_the_board_says_about_its_time_there(changes, expected, cancelled):
    first = watched(glossop(**changes), naive("22:20"), PREFIX, "MAN")[0]
    assert (first.expected, first.cancelled) == (expected, cancelled)


def test_a_train_due_after_midnight_keeps_the_day_it_left():
    rows = watched(glossop(st="00:05", et="00:20"), naive("22:20"), PREFIX, "MAN")
    assert (rows[0].day, rows[0].booked, rows[0].expected) == (FRIDAY, datetime(2026, 10, 10, 0, 5),
                                                               datetime(2026, 10, 10, 0, 20))


def test_rail_replacement_buses_are_left_out():
    raw = json.loads((FIXTURES / "ldbws_glossop_details.json").read_text())
    raw["busServices"] = [raw["trainServices"].pop(1)]
    rows = watched(parse_board_rich(raw), naive("22:20"), PREFIX, "MAN")
    assert [r.identity for r in rows] == [SECOND]


def test_followed_on_the_arrival_board_until_it_gets_there(history):
    arrivals = Arrivals()
    watch = _watch(history, arrivals=arrivals)
    assert watch.poll(glossop(), at("22:20")) == []
    assert arrivals.asked == []  # on the board, and not due yet: nothing to ask
    # Gone from the board at 22:41, due at Manchester in 10 minutes: one ask a poll.
    arrivals.board = arrival_board(FIRST, SECOND)
    watch.poll(glossop(without=[FIRST]), at("22:48"))
    assert arrivals.asked == [("MAN", "GLO")]
    watch.poll(glossop(without=[FIRST]), at("23:12"))
    assert history.following(naive("23:12"))[0].expected == naive("23:14")
    arrivals.board = arrival_board(SECOND)  # it has got there
    watch.poll(glossop(without=[FIRST]), at("23:15"))
    watch.poll(glossop(without=[FIRST]), at("23:16"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["scheduled"], arrived["booked"], arrived["arrived"], arrived["reported"]) == (
        "22:25", "2026-10-09T22:57:00", "2026-10-09T23:14:00", False)
    assert len(arrivals.asked) == 3  # not at 23:16: it had got there by its estimate, seen at 23:12


def test_a_reported_time_on_the_arrival_board_is_taken(history):
    arrivals = Arrivals()
    watch = _watch(history, arrivals=arrivals)
    watch.poll(glossop(), at("22:20"))
    arrivals.board = arrival_board(FIRST, ata="23:15")
    watch.poll(glossop(without=[FIRST]), at("23:16"))
    assert history.arrivals("MAN", FRIDAY)[0]["arrived"] == "2026-10-09T23:15:00"


def test_one_not_on_the_board_is_found_by_when_it_left(history):
    arrivals = Arrivals()
    watch = _watch(history, arrivals=arrivals)
    watch.poll(glossop(), at("22:20"))
    arrivals.board = arrival_board(FIRST, serviceID="9000099GLSP____", ata="23:20")
    watch.poll(glossop(without=[FIRST]), at("23:21"))
    assert history.arrivals("MAN", FRIDAY)[0]["arrived"] == "2026-10-09T23:20:00"


def test_followed_with_service_details_one_train_at_a_time(history):
    details, arrivals = Details(), Arrivals()
    watch = _watch(history, details=details, arrivals=arrivals)
    watch.poll(glossop(without=[SECOND]), at("22:20"))
    details.manchester = {"et": "23:30"}
    watch.poll(glossop(without=[FIRST, SECOND]), at("22:50"))
    assert details.asked == [FIRST] and arrivals.asked == []  # details, when there is a key for them
    details.manchester = {"et": "23:30", "at": "23:31"}
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:31"))
    [arrived] = history.arrivals("MAN", FRIDAY)
    assert (arrived["arrived"], arrived["reported"]) == ("2026-10-09T23:31:00", True)
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:32"))
    assert details.asked == [FIRST, FIRST]  # done with


def test_a_train_darwin_forgets_is_left_to_its_last_estimate(history):
    details = Details()
    watch = _watch(history, details=details)
    watch.poll(glossop(et="22:59"), at("22:20"))
    details.manchester = {"et": "22:59"}
    watch.poll(glossop(without=[FIRST]), at("22:57"))
    details.error = ServiceGone("gone")
    assert watch.poll(glossop(without=[FIRST]), at("23:01")) == []
    assert history.arrivals("MAN", FRIDAY)[0]["arrived"] == "2026-10-09T22:59:00"


def test_a_refused_key_stops_following_for_six_hours(history):
    for source, error in ((Details(), DetailsUnavailable("refused")), (Arrivals(), ArrivalsRefused("refused"))):
        monotonic = Monotonic()
        is_details = isinstance(source, Details)
        watch = _watch(history, details=source if is_details else None,
                       arrivals=None if is_details else source, monotonic=monotonic)
        watch.poll(glossop(), at("22:20"))
        source.error = error
        assert watch.poll(glossop(without=[FIRST]), at("22:50")) == [error]
        watch.poll(glossop(without=[FIRST]), at("22:51"))
        assert len(source.asked) == 1
        monotonic.now += repay.OFF_FOR
        source.error = None
        watch.poll(glossop(without=[FIRST]), at("22:52"))
        assert len(source.asked) == 2


def test_other_failures_are_tried_again_next_poll(history):
    arrivals, details = Arrivals(), Details()
    for watch, source in ((_watch(history, arrivals=arrivals), arrivals), (_watch(history, details=details), details)):
        watch.poll(glossop(), at("22:20"))
        source.error = DetailsError("down") if source is details else OSError("down")
        assert watch.poll(glossop(without=[FIRST]), at("22:50")) == [source.error]
        watch.poll(glossop(without=[FIRST]), at("22:51"))
        assert len(source.asked) == 2


def test_with_nothing_to_follow_with_it_only_records(history):
    watch = _watch(history)
    watch.poll(glossop(without=[SECOND], et="23:00"), at("22:20"))
    assert watch.poll(glossop(without=[FIRST, SECOND]), at("22:59")) == []
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:02"))
    assert history.arrivals("MAN", FRIDAY) == []  # seen at 22:20, due at 23:00: nobody saw it get there
    watch.poll(glossop(without=[FIRST, SECOND]), at("23:59"))
    assert [train.scheduled for train in history.following(naive("23:59"))] == ["22:25"]
    watch.poll(glossop(without=[FIRST, SECOND]), at("00:00").replace(day=10))
    assert history.following(naive("23:59")) == [] and history.arrivals("MAN", FRIDAY) == []
