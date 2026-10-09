from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from departureboard.bods import Vehicle, parse_siri_vm
from departureboard.livebus import departures, match, progress
from departureboard.model import Status
from departureboard.schedule_model import Calendar, Call, Journey, Schedule, StopPosition

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
DAY = date(2026, 10, 9)  # a Friday in British Summer Time
WINTER = date(2026, 11, 10)  # a Tuesday in GMT

# Real NaPTAN stops along High Street West, Glossop.
ASHTON = "1800ANIN0K1"  # not positioned: far up the route
JUNCTION = "1000DDHS3412"
GRAPES = "1000DGHS3410"
SUNLAWS = "1000DGHS3408"
ST_MARYS_E = "1000DGHS3406"
ARUNDEL = "1000DGHS3404"
HENRY_A = "1000DGHS0900"
HOWARD = "1000DGHS1150"
HADFIELD = "1000DHAD0001"  # not positioned
MARKET_HALL = "1000DGHSW936"
GEORGE = "1000DGHS3403"
ST_MARYS_W = "1000DGHS3405"
ASHTON_INT = "1800ANAR031"  # not positioned

POSITIONS = {
    JUNCTION: StopPosition("The Junction", 53.444467, -1.965966),
    GRAPES: StopPosition("Grapes Inn", 53.444223, -1.962774),
    SUNLAWS: StopPosition("Sunlaws Street", 53.443494, -1.959026),
    ST_MARYS_E: StopPosition("St Marys Road", 53.443511, -1.955759),
    ARUNDEL: StopPosition("Arundel Street", 53.443519, -1.95326),
    HENRY_A: StopPosition("Henry Street", 53.44384, -1.948999),
    HOWARD: StopPosition("Howard Arms", 53.443301, -1.948157),
    MARKET_HALL: StopPosition("Market Hall", 53.443239, -1.949828),
    GEORGE: StopPosition("George Street", 53.443347, -1.951905),
    ST_MARYS_W: StopPosition("St Marys Road", 53.44343, -1.955353),
}
EVERY_DAY = Calendar(days=frozenset(range(7)))


def journey(line, calls, destination, code, operator="BNML"):
    return Journey(line, operator, destination, code, tuple(Call(a, m) for a, m in calls), EVERY_DAY)


# Eastbound through stop A, leaving Ashton at 09:00 and Henry Street at 10:10.
EAST = journey(
    "341",
    [(ASHTON, 540), (JUNCTION, 600), (GRAPES, 602), (SUNLAWS, 604), (ST_MARYS_E, 606),
     (ARUNDEL, 608), (HENRY_A, 610), (HOWARD, 611), (HADFIELD, 625)],
    "Hadfield",
    "1175",
)
# Westbound, starting at stop A at 10:15 and calling at stop B at 10:18.
WEST = journey(
    "237",
    [(HENRY_A, 615), (MARKET_HALL, 616), (GEORGE, 617), (ST_MARYS_W, 618), (ASHTON_INT, 660)],
    "Ashton",
    "1178",
)
STOPS = {"A": HENRY_A, "B": ST_MARYS_W}


def schedule(*journeys):
    return Schedule(datetime(2026, 10, 9, 3, 0), None, dict(STOPS), journeys or (EAST, WEST), POSITIONS)


def every_day(journey, day):
    return True


def at(hh, mm, ss=0, day=DAY):
    return datetime.combine(day, time(hh, mm, ss), LONDON)


def bus(lat, lon, recorded, line="341", operator="BNML", origin=ASHTON, aimed=None, ref="", bearing=90.0):
    """A vehicle on EAST unless told otherwise; recorded is a London time."""
    aimed = aimed if aimed is not None or not origin else datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    return Vehicle(
        line, "Hadfield", "outbound", lat, lon, recorded.isoformat(), bearing,
        operator=operator, origin_ref=origin, origin_aimed=aimed, journey_ref=ref,
    )


def stop_at(atco, recorded, **kwargs):
    where = POSITIONS[atco]
    return bus(where.lat, where.lon, recorded, **kwargs)


# Halfway between Sunlaws Street (10:04) and St Marys Road (10:06), heading east.
HALFWAY = (53.4435025, -1.9573925)


def board(now, vehicles=(), rows=10, plan=None, runs_on=every_day, directions=None):
    found = departures(plan or schedule(), now, list(vehicles), runs_on, rows, directions)
    return [(d.stop, d.service, d.scheduled, d.expected, d.status) for d in found]


def east(rows):
    return [row for row in rows if row[1] == "341"]


# Matching


def test_matches_on_operator_line_and_when_it_left_its_first_stop():
    vehicle = bus(*HALFWAY, at(10, 5))  # left Ashton 08:00 UTC, 09:00 BST
    assert match(vehicle, [WEST, EAST], DAY, LONDON) is EAST


def test_origin_time_is_read_in_the_board_s_time_zone():
    summer = bus(*HALFWAY, at(10, 5), aimed=datetime(2026, 10, 9, 9, 0, tzinfo=UTC))
    assert match(summer, [EAST], DAY, LONDON) is None  # 10:00 BST, not the 09:00
    winter = bus(*HALFWAY, at(10, 5, day=WINTER), aimed=datetime(2026, 11, 10, 9, 0, tzinfo=UTC))
    assert match(winter, [EAST], WINTER, LONDON) is EAST  # 09:00 GMT


def test_falls_back_to_the_journey_code_without_an_origin():
    vehicle = bus(*HALFWAY, at(10, 5), origin="", ref="1175")
    assert match(vehicle, [WEST, EAST], DAY, LONDON) is EAST


def test_journey_code_is_not_used_when_the_origin_does_not_fit():
    late_start = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)
    vehicle = bus(*HALFWAY, at(10, 5), aimed=late_start, ref="1175")
    assert match(vehicle, [EAST], DAY, LONDON) is None


def test_another_line_does_not_match():
    assert match(bus(*HALFWAY, at(10, 5), line="342"), [EAST], DAY, LONDON) is None


def test_another_operator_code_still_matches_on_line_first_stop_and_time():
    # Bee Network timetables and live feeds can use different codes for one
    # operator (the 341 timetable says BNGN inside BNML's dataset).
    assert match(bus(*HALFWAY, at(10, 5), operator="BNGN"), [EAST], DAY, LONDON) is EAST


def test_operator_breaks_a_tie_between_journeys_that_otherwise_fit():
    other = replace(EAST, operator="HIPK", journey_code="9")
    assert match(bus(*HALFWAY, at(10, 5)), [EAST, other], DAY, LONDON) is EAST
    assert match(bus(*HALFWAY, at(10, 5), operator="SCMN"), [EAST, other], DAY, LONDON) is None


def test_journey_code_needs_the_operator_too():
    # Ticket machine codes are only unique within one operator.
    assert match(bus(*HALFWAY, at(10, 5), origin="", operator="HIPK", ref="1175"), [EAST], DAY, LONDON) is None
    assert match(bus(*HALFWAY, at(10, 5), origin="", operator="", ref="1175"), [EAST], DAY, LONDON) is None


def test_ambiguous_matches_are_ignored():
    twin = journey("341", [(c.atco, c.minute) for c in EAST.calls], "Hadfield", "1177")
    assert match(bus(*HALFWAY, at(10, 5)), [EAST, twin], DAY, LONDON) is None
    same_code = journey("341", [(ASHTON, 600), (HENRY_A, 670), (HADFIELD, 680)], "Hadfield", "1175")
    assert match(bus(*HALFWAY, at(10, 5), origin="", ref="1175"), [EAST, same_code], DAY, LONDON) is None


def test_a_vehicle_with_nothing_to_match_on_matches_nothing():
    assert match(bus(*HALFWAY, at(10, 5), origin="", ref=""), [EAST], DAY, LONDON) is None


def test_matches_a_real_bods_vehicle():
    body = (Path(__file__).parent / "fixtures" / "bods_glossop_live.xml").read_bytes()
    vehicle = next(v for v in parse_siri_vm(body) if v.journey_ref == "1178")
    # 237 from Henry Street at 19:35 UTC, which is 20:35 in Glossop.
    real = journey("237", [(HENRY_A, 1235), (ASHTON_INT, 1278)], "Ashton", "9999")
    other = journey("237", [(HENRY_A, 1175), (ASHTON_INT, 1218)], "Ashton", "1177")
    assert match(vehicle, [other, real], DAY, LONDON) is real


# Progress and delay


def test_bus_halfway_between_two_stops_is_late_by_the_timetable_there():
    placed = progress(bus(*HALFWAY, at(10, 8)), EAST, POSITIONS, DAY, LONDON)
    assert placed.at == pytest.approx(3.5, abs=0.02)  # between the 4th and 5th calls
    assert placed.delay == pytest.approx(180, abs=3)  # due there at 10:05, seen at 10:08


def test_delay_uses_when_the_position_was_recorded_in_any_offset():
    recorded = datetime(2026, 11, 10, 10, 8, tzinfo=UTC)  # 10:08 GMT
    placed = progress(bus(*HALFWAY, recorded), EAST, POSITIONS, WINTER, LONDON)
    assert placed.delay == pytest.approx(180, abs=3)


def test_early_mid_route_is_negative():
    placed = progress(bus(*HALFWAY, at(10, 3)), EAST, POSITIONS, DAY, LONDON)
    assert placed.delay == pytest.approx(-120, abs=3)


def test_waiting_early_at_the_first_stop_is_not_early():
    waiting = stop_at(HENRY_A, at(10, 10), line="237", origin=HENRY_A, bearing=240.0)
    placed = progress(waiting, WEST, POSITIONS, DAY, LONDON)
    assert (placed.at, placed.delay) == (0, 0)


def test_still_at_the_first_stop_after_its_time_is_late():
    waiting = stop_at(HENRY_A, at(10, 17), line="237", origin=HENRY_A, bearing=240.0)
    assert progress(waiting, WEST, POSITIONS, DAY, LONDON).delay == pytest.approx(120)


def test_a_bus_off_the_known_road_is_not_placed():
    on_dinting_vale = bus(53.445897, -1.970527, at(10, 0), bearing=120.0)
    assert progress(on_dinting_vale, EAST, POSITIONS, DAY, LONDON) is None


def test_a_bus_heading_the_other_way_is_not_placed():
    assert progress(bus(*HALFWAY, at(10, 5), bearing=270.0), EAST, POSITIONS, DAY, LONDON) is None


# Departures


def test_timetable_only_without_live_buses():
    assert board(at(10, 0)) == [
        ("A", "341", "10:10", None, Status.UNKNOWN),
        ("A", "237", "10:15", None, Status.UNKNOWN),
        ("B", "237", "10:18", None, Status.UNKNOWN),
    ]


def test_keeps_rows():
    assert [row[2] for row in board(at(10, 0), rows=2)] == ["10:10", "10:15"]


def test_a_late_bus_shows_its_delay_and_moves_down_the_board():
    late = bus(*HALFWAY, at(10, 14))  # 9 minutes behind
    assert board(at(10, 14), [late]) == [
        ("A", "237", "10:15", None, Status.UNKNOWN),
        ("B", "237", "10:18", None, Status.UNKNOWN),
        ("A", "341", "10:10", "10:19", Status.LATE),
    ]


def test_a_bus_on_time_or_early_is_shown_on_time():
    early = bus(*HALFWAY, at(10, 3))
    assert board(at(10, 3), [early])[0] == ("A", "341", "10:10", None, Status.ON_TIME)
    nearly = bus(*HALFWAY, at(10, 5, 29))
    assert board(at(10, 6), [nearly])[0] == ("A", "341", "10:10", None, Status.ON_TIME)


def test_lateness_rounds_to_the_nearest_minute_halves_up():
    at_arundel = stop_at(ARUNDEL, at(10, 13, 30))  # due there 10:08, so 5.5 minutes late
    assert east(board(at(10, 14), [at_arundel])) == [("A", "341", "10:10", "10:16", Status.LATE)]


def test_a_late_bus_stays_after_its_time_until_it_passes():
    at_arundel = stop_at(ARUNDEL, at(10, 13, 30))
    assert ("A", "341", "10:10", "10:16", Status.LATE) in board(at(10, 14), [at_arundel])
    gone_by = stop_at(HOWARD, at(10, 17))
    assert [row for row in board(at(10, 17), [gone_by]) if row[1] == "341"] == []


def test_a_bus_that_passed_early_is_gone_before_its_time():
    gone_by = stop_at(HOWARD, at(10, 9), bearing=135.0)
    assert [row[1] for row in board(at(10, 9), [gone_by])] == ["237", "237"]


def test_a_bus_at_your_stop_is_still_due():
    at_stop = stop_at(HENRY_A, at(10, 12))
    assert board(at(10, 12), [at_stop])[0] == ("A", "341", "10:10", "10:12", Status.LATE)


def test_a_timetabled_bus_goes_a_minute_after_its_time():
    assert board(at(10, 11))[0][2] == "10:10"
    assert board(at(10, 11, 1))[0][2] == "10:15"


def test_a_matched_bus_that_cannot_be_placed_is_shown_from_the_timetable():
    far_away = bus(53.445897, -1.970527, at(10, 0), bearing=120.0)
    assert board(at(10, 0), [far_away])[0] == ("A", "341", "10:10", None, Status.UNKNOWN)


def test_a_bus_for_another_journey_changes_nothing():
    stranger = bus(*HALFWAY, at(10, 14), aimed=datetime(2026, 10, 9, 7, 0, tzinfo=UTC))
    assert board(at(10, 0), [stranger]) == board(at(10, 0))


def test_the_newest_report_wins_when_two_buses_claim_one_journey():
    old = bus(*HALFWAY, at(10, 14))
    new = stop_at(ARUNDEL, at(10, 14, 30))  # due 10:08, so 6.5 late
    assert east(board(at(10, 15), [new, old]))[0][3] == "10:17"
    assert east(board(at(10, 15), [old, new]))[0][3] == "10:17"


def test_directions_filter_on_destination():
    assert {row[1] for row in board(at(10, 0), directions=["ashton"])} == {"237"}
    assert board(at(10, 0), directions=["Manchester"]) == []


def test_only_journeys_running_today():
    def weekends(journey, day):
        return day.weekday() >= 5

    assert board(at(10, 0), runs_on=weekends) == []


def test_a_journey_ending_at_your_stop_is_not_a_departure():
    terminating = journey("390", [(ASHTON, 590), (ARUNDEL, 608), (HENRY_A, 610)], "Glossop", "1802")
    assert board(at(10, 0), plan=schedule(terminating)) == []


# Midnight and time zones

SATURDAY = date(2026, 10, 10)
# Leaves Ashton at 23:30 on Friday and reaches stop A at 00:10 on Saturday.
NIGHT = journey(
    "341",
    [(ASHTON, 1410), (SUNLAWS, 1444), (ST_MARYS_E, 1446), (ARUNDEL, 1448), (HENRY_A, 1450), (HADFIELD, 1460)],
    "Hadfield",
    "1999",
)


def fridays(journey, day):
    return day.weekday() == 4


def test_yesterday_s_journey_after_midnight_is_shown():
    assert board(at(0, 5, day=SATURDAY), plan=schedule(NIGHT), runs_on=fridays) == [
        ("A", "341", "00:10", None, Status.UNKNOWN)
    ]
    # Early on Friday: Thursday had no such journey, and Friday's is 24 hours off.
    assert board(at(0, 5, day=DAY), plan=schedule(NIGHT), runs_on=fridays) == []


def test_nothing_more_than_twelve_hours_ahead():
    # Times show without a date, so the board reads them as within 12 hours.
    lunchtime = journey("341", [(ASHTON, 720), (HENRY_A, 780), (HADFIELD, 790)], "Hadfield", "1201")
    assert board(at(0, 59), plan=schedule(lunchtime)) == []
    assert board(at(1, 0), plan=schedule(lunchtime)) == [("A", "341", "13:00", None, Status.UNKNOWN)]


def test_yesterday_s_late_bus_is_matched_after_midnight():
    left_ashton = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)  # 23:30 BST Friday
    vehicle = stop_at(ARUNDEL, at(0, 13, day=SATURDAY), aimed=left_ashton)  # due there 00:08
    assert board(at(0, 13, day=SATURDAY), [vehicle], plan=schedule(NIGHT), runs_on=fridays) == [
        ("A", "341", "00:10", "00:15", Status.LATE)
    ]


def test_a_journey_code_that_fits_yesterday_and_today_is_not_used():
    vehicle = bus(*HALFWAY, at(10, 14), origin="", ref="1175")
    assert board(at(10, 14), [vehicle])[0] == ("A", "237", "10:15", None, Status.UNKNOWN)


def test_now_must_carry_its_time_zone():
    with pytest.raises(ValueError, match="aware"):
        departures(schedule(), datetime(2026, 10, 9, 10, 0), [], every_day, 5)


def test_board_in_winter_time():
    winter_bus = bus(*HALFWAY, at(10, 8, day=WINTER), aimed=datetime(2026, 11, 10, 9, 0, tzinfo=UTC))
    assert board(at(10, 8, day=WINTER), [winter_bus])[0] == ("A", "341", "10:10", "10:13", Status.LATE)
