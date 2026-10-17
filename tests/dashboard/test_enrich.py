from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.sources.test_livebus import (
    ARUNDEL,
    ASHTON,
    EAST,
    GEORGE,
    GRAPES,
    HALFWAY,
    HENRY_A,
    JUNCTION,
    MARKET_HALL,
    NIGHT,
    POSITIONS,
    SATURDAY,
    ST_MARYS_E,
    SUNLAWS,
    UTC,
    WEST,
    at,
    bus,
    every_day,
    fridays,
    journey,
    schedule,
    stop_at,
)
from twobells.core.model import Departure, Status
from twobells.dashboard.enrich import bus_arrival, bus_extras, inbound, inbound_text, is_ghost, train_arrival
from twobells.sources.bodsbus import BusBoard
from twobells.sources.darwin import CallingPoint, TrainService
from twobells.sources.livebus import live_board
from twobells.timetable.model import Call

LONDON = ZoneInfo("Europe/London")


# Trains


def point(name, crs, scheduled, expected=None, actual=None, cancelled=False):
    return CallingPoint(name, crs, scheduled, expected, actual, cancelled, None)


TO_MANCHESTER = (
    point("Dinting", "DGT", "07:15", "On time"),
    point("Guide Bridge", "GUI", "07:36", "On time"),
    point("Manchester Piccadilly", "MAN", "07:48", "On time"),
    point("Manchester Oxford Road", "MCO", "07:52", "On time"),
)


def service(std="07:12", expected=None, status=Status.ON_TIME, points=TO_MANCHESTER):
    departure = Departure("train", "Northern", "Manchester Oxford Road", std, expected, status, "1")
    return TrainService(departure, "1234GLSP", "Hadfield", "NT", 3, None, None, points)


def times(std="07:12", expected=None, day=date(2026, 10, 10)):
    """This station's scheduled_at and expected_at, as snapshot._train_times gives them."""
    hours, minutes = map(int, std.split(":"))
    scheduled_at = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=LONDON)
    if expected is None:
        return scheduled_at, scheduled_at
    hours, minutes = map(int, expected.split(":"))
    expected_at = scheduled_at.replace(hour=hours, minute=minutes)
    if expected_at < scheduled_at - timedelta(hours=12):
        expected_at += timedelta(days=1)
    return scheduled_at, expected_at


def test_a_train_arrives_at_the_filter_station():
    assert train_arrival(service(), "MAN", *times()) == {
        "name": "Manchester Piccadilly", "at": "07:48", "at_iso": "2026-10-10T07:48:00+01:00", "minutes": 36,
    }


def test_a_filter_station_not_on_the_route_falls_back_to_the_last_calling_point():
    found = train_arrival(service(), "SHF", *times())
    assert (found["name"], found["at"], found["minutes"]) == ("Manchester Oxford Road", "07:52", 40)
    assert train_arrival(service(), None, *times())["name"] == "Manchester Oxford Road"


def test_a_late_train_arrives_at_darwin_s_expected_time_and_counts_from_its_expected_departure():
    late = (point("Dinting", "DGT", "07:15", "07:22"), point("Manchester Piccadilly", "MAN", "07:48", "07:55"))
    found = train_arrival(service("07:12", "07:19", Status.LATE, late), "MAN", *times("07:12", "07:19"))
    assert (found["at"], found["at_iso"], found["minutes"]) == ("07:55", "2026-10-10T07:55:00+01:00", 36)


def test_arrival_across_midnight():
    night = (point("Manchester Piccadilly", "MAN", "00:22", "00:24"),)
    found = train_arrival(service("23:50", points=night), "MAN", *times("23:50", day=date(2026, 10, 9)))
    assert found == {
        "name": "Manchester Piccadilly", "at": "00:24", "at_iso": "2026-10-10T00:24:00+01:00", "minutes": 34,
    }
    # Late enough to leave after midnight too.
    found = train_arrival(service("23:50", "00:05", Status.LATE, night), "MAN",
                          *times("23:50", "00:05", day=date(2026, 10, 9)))
    assert (found["at_iso"], found["minutes"]) == ("2026-10-10T00:24:00+01:00", 19)


def test_arrival_across_the_clocks_changing_is_the_minutes_that_pass():
    # 25 October 2026: leaves 01:50 BST, gets in at 01:10 GMT, 20 minutes on.
    back = (point("Manchester Piccadilly", "MAN", "01:10", "On time"),)
    leaves = datetime(2026, 10, 25, 1, 50, tzinfo=LONDON)
    found = train_arrival(service("01:50", points=back), "MAN", leaves, leaves)
    assert found == {
        "name": "Manchester Piccadilly", "at": "01:10", "at_iso": "2026-10-25T01:10:00+00:00", "minutes": 20,
    }
    # 29 March 2026: leaves 00:50 GMT, gets in at 02:10 BST, 20 minutes on.
    forward = (point("Manchester Piccadilly", "MAN", "02:10", "On time"),)
    leaves = datetime(2026, 3, 29, 0, 50, tzinfo=LONDON)
    found = train_arrival(service("00:50", points=forward), "MAN", leaves, leaves)
    assert (found["at_iso"], found["minutes"]) == ("2026-03-29T02:10:00+01:00", 20)


def test_words_in_place_of_a_time():
    no_report = (point("Manchester Piccadilly", "MAN", "07:48", "No report"),)
    assert train_arrival(service(points=no_report), "MAN", *times())["at"] == "07:48"
    unknown = (point("Manchester Piccadilly", "MAN", "07:48", None),)
    assert train_arrival(service(points=unknown), "MAN", *times())["at"] == "07:48"
    # "Delayed" says the booked time is wrong and the right one unknown.
    delayed = (point("Manchester Piccadilly", "MAN", "07:48", "Delayed"),)
    assert train_arrival(service(points=delayed), "MAN", *times()) is None


def test_no_arrival_when_it_is_not_known():
    cancelled_there = (point("Manchester Piccadilly", "MAN", "07:48", "Cancelled", cancelled=True),)
    assert train_arrival(service(points=cancelled_there), "MAN", *times()) is None
    assert train_arrival(service(status=Status.CANCELLED), "MAN", times()[0], None) is None
    assert train_arrival(service(status=Status.LATE), "MAN", times()[0], None) is None  # "Delayed" here
    assert train_arrival(service(points=()), "MAN", *times()) is None
    # Darwin's times disagree: it would arrive before it leaves.
    early = (point("Manchester Piccadilly", "MAN", "07:10", "On time"),)
    assert train_arrival(service(points=early), "MAN", *times()) is None


def test_no_report_here_counts_from_the_booked_departure():
    found = train_arrival(service(status=Status.UNKNOWN), "MAN", times()[0], None)
    assert (found["at"], found["minutes"]) == ("07:48", 36)


# Buses

# Ends at stop A at 10:10; its bus works WEST, which starts there at 10:15.
INTO_A = journey(
    "237",
    [(JUNCTION, 600), (GRAPES, 602), (SUNLAWS, 604), (ST_MARYS_E, 606), (ARUNDEL, 608), (HENRY_A, 610)],
    "Glossop",
    "1177",
)
LEFT_JUNCTION = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)  # 10:00 BST


def inbound_bus(recorded, where=HALFWAY, aimed=LEFT_JUNCTION, origin=JUNCTION):
    return bus(*where, recorded, line="237", origin=origin, aimed=aimed)


def make_board(now, vehicles=(), plan=None, vehicles_ok=True, runs_on=every_day):
    plan = plan or schedule(EAST, WEST, INTO_A)
    rows, placed = live_board(plan, now, list(vehicles), runs_on, 10)
    return BusBoard(rows, plan, vehicles_ok, None, placed)


def row(board, line, stop="A"):
    return next(l for l in board.departures if l.departure.service == line and l.departure.stop == stop)


def test_a_bus_arrives_at_its_last_call_with_the_delay_it_has_now():
    board = make_board(at(10, 14), [bus(*HALFWAY, at(10, 14))])  # EAST, 9 min late
    assert bus_arrival(row(board, "341"), at(10, 14)) == {
        "name": "Hadfield", "at": "10:34", "at_iso": "2026-10-09T10:34:00+01:00", "minutes": 15,
    }
    on_time = bus_arrival(row(board, "237"), at(10, 14))  # WEST, no bus yet: the timetable
    assert (on_time["name"], on_time["at"], on_time["minutes"]) == ("Ashton", "11:00", 45)


def test_a_bus_arrival_across_midnight():
    board = make_board(at(0, 5, day=SATURDAY), plan=schedule(NIGHT), runs_on=fridays)
    assert bus_arrival(board.departures[0], at(0, 5, day=SATURDAY)) == {
        "name": "Hadfield", "at": "00:20", "at_iso": "2026-10-10T00:20:00+01:00", "minutes": 10,
    }
    left_ashton = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)
    late = stop_at(ARUNDEL, at(0, 13, day=SATURDAY), aimed=left_ashton)  # due there 00:08
    board = make_board(at(0, 13, day=SATURDAY), [late], plan=schedule(NIGHT), runs_on=fridays)
    assert bus_arrival(board.departures[0], at(0, 13, day=SATURDAY))["at"] == "00:25"


def test_a_bus_arrival_across_the_clocks_changing_is_its_run_time_after_it_leaves():
    board = make_board(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    east = row(board, "341")  # 15 minutes to Hadfield
    # 25 October 2026, polled at 01:40 BST: leaves 01:50 BST, there 15 minutes later at 01:05 GMT.
    back = replace(east, leaves=datetime(2026, 10, 25, 1, 50), expected=datetime(2026, 10, 25, 1, 50))
    assert bus_arrival(back, datetime(2026, 10, 25, 0, 40, tzinfo=UTC).astimezone(LONDON)) == {
        "name": "Hadfield", "at": "01:05", "at_iso": "2026-10-25T01:05:00+00:00", "minutes": 15,
    }
    # 29 March 2026: leaves 00:50 GMT, there at 02:05 BST.
    forward = replace(east, leaves=datetime(2026, 3, 29, 0, 50), expected=datetime(2026, 3, 29, 0, 50))
    found = bus_arrival(forward, datetime(2026, 3, 29, 0, 40, tzinfo=LONDON))
    assert (found["at"], found["at_iso"], found["minutes"]) == ("02:05", "2026-03-29T02:05:00+01:00", 15)


def test_inbound_exactly_one_bus_coming_to_work_it():
    board = make_board(at(10, 9), [inbound_bus(at(10, 9))])  # due at Sunlaws/St Marys 10:05: 4 min late
    found = inbound(row(board, "237"), board.schedule.positions)
    assert found == {
        "from": "The Junction", "due": "10:14", "late_min": 4, "where": "3 stops away",
        "text": "Bus arriving from The Junction, 4 min late (due 10:14)", "followed": False,
    }
    # Only departures that start at your stop are worked by an inbound bus.
    assert inbound(row(board, "237", "B"), board.schedule.positions) is None
    assert inbound(row(board, "341"), board.schedule.positions) is None


def test_no_inbound_without_a_placed_bus():
    board = make_board(at(10, 9))
    assert inbound(row(board, "237"), board.schedule.positions) is None
    off_the_road = inbound_bus(at(10, 9), where=(53.445897, -1.970527))
    board = make_board(at(10, 9), [off_the_road])
    assert board.journeys == []
    assert inbound(row(board, "237"), board.schedule.positions) is None


def test_two_buses_that_could_work_it_are_a_guess_too_far():
    also_into_a = replace(INTO_A, journey_code="1179", calls=tuple(
        Call(c.atco, c.minute - 4) for c in INTO_A.calls))  # left The Junction 09:56, at A 10:06
    plan = schedule(EAST, WEST, INTO_A, also_into_a)
    both = [inbound_bus(at(10, 9)), inbound_bus(at(10, 9), where=(POSITIONS[ARUNDEL].lat, POSITIONS[ARUNDEL].lon),
                                                aimed=LEFT_JUNCTION - timedelta(minutes=4))]
    board = make_board(at(10, 9), both, plan=plan)
    assert len(board.journeys) == 2
    assert inbound(row(board, "237"), board.schedule.positions) is None
    one = make_board(at(10, 9), both[:1], plan=plan)
    assert inbound(row(one, "237"), one.schedule.positions)["from"] == "The Junction"


def test_an_inbound_bus_must_arrive_up_to_half_an_hour_before():
    for arrives, works in ((610, True), (615, True), (585, True), (584, False), (616, False)):
        into = replace(INTO_A, calls=tuple(Call(c.atco, c.minute + arrives - 610) for c in INTO_A.calls))
        aimed = LEFT_JUNCTION + timedelta(minutes=arrives - 610)
        recorded = at(10, 0) + timedelta(minutes=arrives - 610 + 6)  # on time, between Sunlaws and St Marys
        board = make_board(recorded, [inbound_bus(recorded, aimed=aimed)], plan=schedule(EAST, WEST, into))
        assert len(board.journeys) == 1  # placed every time: only the time decides
        found = inbound(row(board, "237"), board.schedule.positions)
        assert (found is not None) == works, arrives


def test_an_inbound_bus_of_another_line_does_not_count():
    other = replace(INTO_A, line="236")
    vehicle = bus(*HALFWAY, at(10, 9), line="236", origin=JUNCTION, aimed=LEFT_JUNCTION)
    board = make_board(at(10, 9), [vehicle], plan=schedule(EAST, WEST, other))
    assert len(board.journeys) == 1
    assert inbound(row(board, "237"), board.schedule.positions) is None


def test_a_journey_ending_near_your_stop_counts_and_one_further_off_does_not():
    # Market Hall is under 100 m from Henry Street; George Street about 200 m.
    for end, counts in ((MARKET_HALL, True), (GEORGE, False)):
        into = replace(INTO_A, calls=(*INTO_A.calls[:-1], Call(end, 610)))
        board = make_board(at(10, 9), [inbound_bus(at(10, 9))], plan=schedule(EAST, WEST, into))
        assert len(board.journeys) == 1
        found = inbound(row(board, "237"), board.schedule.positions)
        assert (found is not None) == counts, end


def test_an_inbound_bus_works_the_first_departure_after_it_arrives():
    later = replace(WEST, journey_code="1180", calls=tuple(Call(c.atco, c.minute + 15) for c in WEST.calls))
    board = make_board(at(10, 9), [inbound_bus(at(10, 9))], plan=schedule(EAST, WEST, later, INTO_A))
    first, second = [l for l in board.departures if l.departure.service == "237" and l.departure.stop == "A"]
    assert (first.departure.scheduled, second.departure.scheduled) == ("10:15", "10:30")
    assert inbound(first, board.schedule.positions) is not None
    assert inbound(second, board.schedule.positions) is None


def test_where_the_inbound_bus_is_and_where_it_came_from():
    at_your_stop = inbound_bus(at(10, 12), where=(POSITIONS[HENRY_A].lat, POSITIONS[HENRY_A].lon))
    board = make_board(at(10, 12), [at_your_stop])
    found = inbound(row(board, "237"), board.schedule.positions)
    assert (found["where"], found["late_min"], found["due"]) == ("At your stop", 2, "10:12")
    # From a stop the timetable has no position for: the name the tracker sends, tidied.
    from_ashton = replace(INTO_A, calls=((Call(ASHTON, 540), *INTO_A.calls[1:])))
    vehicle = replace(inbound_bus(at(10, 9), origin=ASHTON, aimed=LEFT_JUNCTION - timedelta(hours=1)),
                      origin_name="Ashton_under_Lyne_‮Interchange")
    board = make_board(at(10, 9), [vehicle], plan=schedule(EAST, WEST, from_ashton))
    found = inbound(row(board, "237"), board.schedule.positions)
    assert found["from"] == "Ashton under Lyne Interchange"
    assert found["text"].startswith("Bus arriving from Ashton under Lyne Interchange, ")
    nameless = replace(vehicle, origin_name="")
    board = make_board(at(10, 9), [nameless], plan=schedule(EAST, WEST, from_ashton))
    found = inbound(row(board, "237"), board.schedule.positions)
    assert found["from"] is None and found["text"].startswith("Bus arriving, ")


@pytest.mark.parametrize("origin, late, due, words", [
    ("Ashton", 4, "23:29", "Bus arriving from Ashton, 4 min late (due 23:29)"),
    ("Ashton", 0, "23:25", "Bus arriving from Ashton, on time (due 23:25)"),
    (None, 1, "07:01", "Bus arriving, 1 min late (due 07:01)"),
])
def test_inbound_wording_gives_the_inbound_bus_s_lateness_never_a_new_departure_time(origin, late, due, words):
    assert inbound_text(origin, late, due) == words


def test_a_late_inbound_bus_guessed_by_line_makes_the_departure_late():
    very_late = inbound_bus(at(10, 14))  # 9 min late, due at A 10:19: after WEST leaves at 10:15
    board = make_board(at(10, 14), [very_late])
    west = row(board, "237")
    extras = bus_extras(west, board, at(10, 14))
    assert extras["inbound"]["due"] == "10:19" and extras["inbound"]["late_min"] == 9
    assert extras["inbound"]["followed"] is False  # no block to follow: a guess by line
    # Live, as one followed by its block: two minutes to turn round once in.
    assert (west.departure.status, west.departure.expected, west.expected) == (
        Status.LATE, "10:21", datetime(2026, 10, 9, 10, 21),
    )


# Ghost buses


def test_ghost_threshold():
    board = make_board(at(10, 0))
    b_stop = row(board, "237", "B")  # 10:18, no bus placed
    assert not is_ghost(b_stop, at(10, 12, 59), True, None)
    assert is_ghost(b_stop, at(10, 13), True, None)  # five minutes to go
    assert is_ghost(b_stop, at(10, 18, 30), True, None)  # its time, and still nothing
    assert not is_ghost(b_stop, at(10, 13), False, None)  # live positions failed: say nothing
    assert not is_ghost(b_stop, at(10, 13), True, {"from": "The Junction"})  # a bus is coming to work it


def test_a_live_bus_is_never_a_ghost():
    board = make_board(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    assert not is_ghost(row(board, "341"), at(10, 14), True, None)


def test_bus_extras_put_it_together():
    board = make_board(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    extras = {(l.departure.service, l.departure.stop): bus_extras(l, board, at(10, 14)) for l in board.departures}
    assert extras["341", "A"]["ghost"] is False and extras["341", "A"]["arrives"]["at"] == "10:34"
    assert extras["237", "A"] == {
        "arrives": {"name": "Ashton", "at": "11:00", "at_iso": "2026-10-09T11:00:00+01:00", "minutes": 45},
        "inbound": None, "ghost": True,
    }
    assert extras["237", "B"]["ghost"] is True and extras["237", "B"]["inbound"] is None
    failed = make_board(at(10, 14), vehicles_ok=False)
    assert all(not bus_extras(l, failed, at(10, 14))["ghost"] for l in failed.departures)
