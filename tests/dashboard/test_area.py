import json
from dataclasses import replace
from datetime import date

from tests.sources.test_livebus import EAST, HALFWAY, HENRY_A, ST_MARYS_W, WEST, at, bus, schedule
from twobells.dashboard.area import area_view, timetable_changes
from twobells.sources.bodsbus import BusBoard
from twobells.sources.livebus import live_board
from twobells.timetable.model import Calendar, Call

NOW = at(10, 14, 30)


def board(vehicles):
    found, placed = live_board(schedule(), NOW, vehicles, lambda journey, day: True, 10)
    return BusBoard(found, schedule(), True, None, placed, list(vehicles))


def test_live_buses_on_our_lines_around_our_stops():
    placed = bus(*HALFWAY, at(10, 14))  # on the 09:00 from Ashton, between Sunlaws Street and St Marys Road
    unmatched = bus(53.4440, -1.9600, at(10, 13, 50), origin="", aimed=None, bearing=float("nan"))
    other_line = bus(*HALFWAY, at(10, 14), line="999")
    far_away = bus(53.4800, -2.2400, at(10, 14))  # Manchester: outside the timetable's area
    view = area_view(board([placed, unmatched, other_line, far_away]), NOW)
    assert [v["line"] for v in view["vehicles"]] == ["341", "341"]
    first, second = view["vehicles"]
    assert first == {
        "line": "341", "destination": "Hadfield", "lat": 53.443503, "lon": -1.957393, "bearing": 90.0,
        "recorded_at": "2026-10-09T10:14:00+01:00", "age_s": 30, "next_stop": "St Marys Road",
    }
    assert second["next_stop"] is None and second["bearing"] is None and second["age_s"] == 40
    assert view["fetched_at"] == "2026-10-09T10:14:30+01:00"


def test_a_bus_with_a_position_too_big_for_a_float_is_left_off():
    # JSON reads 10**400 as a whole number; the map checks each vehicle it is given itself.
    good = bus(*HALFWAY, at(10, 14))
    found, placed = live_board(schedule(), NOW, [good], lambda journey, day: True, 10)
    vehicles = [good, replace(good, lat=10**400), replace(good, bearing=10**400)]
    view = area_view(BusBoard(found, schedule(), True, None, placed, vehicles), NOW)
    assert len(view["vehicles"]) == 2 and [v["bearing"] for v in view["vehicles"]] == [90.0, None]


def test_a_bus_sending_a_bare_time_is_read_as_utc():
    vehicle = replace(bus(*HALFWAY, at(10, 14)), recorded_at="2026-10-09T09:14:00")
    found = area_view(board([vehicle]), NOW)["vehicles"][0]
    assert found["recorded_at"] == "2026-10-09T10:14:00+01:00" and found["age_s"] == 30


def test_our_stops_and_the_routes_are_drawn_from_the_timetable():
    view = area_view(board([]), NOW)
    assert view["vehicles"] == []
    assert view["stops"] == [
        {"label": "A", "name": "Henry Street", "lat": 53.44384, "lon": -1.948999},
        {"label": "B", "name": "St Marys Road", "lat": 53.44343, "lon": -1.955353},
    ]
    routes = {(r["line"], r["destination"]): r["path"] for r in view["routes"]}  # straight: no roads saved
    assert list(routes) == [("237", "Ashton"), ("341", "Hadfield")]
    assert len(routes[("341", "Hadfield")]) == 7  # The Junction to Howard Arms; Ashton and Hadfield aren't placed
    assert routes[("237", "Ashton")][0] == [53.44384, -1.948999]
    west, south, east, north = view["box"]
    assert west < -1.965966 and east > -1.948157 and south < 53.443239 and north > 53.444467


def test_the_routes_follow_the_timetable_s_roads():
    from tests.sources.test_livebus import HOWARD, MARKET_HALL

    roads = {f"{HENRY_A}>{MARKET_HALL}": ((53.4620, -1.9495),), f"{HENRY_A}>{HOWARD}": ((53.4436, -1.9485),)}
    view = area_view(BusBoard([], replace(schedule(), shapes=roads), True, None, [], []), NOW)
    routes = {r["line"]: r["path"] for r in view["routes"]}
    # Without roads, straight from stop to stop.
    stops = {r["line"]: r["path"] for r in area_view(board([]), NOW)["routes"]}
    assert routes["237"] == [stops["237"][0], [53.4620, -1.9495], *stops["237"][1:]]
    assert routes["341"] == [*stops["341"][:-1], [53.4436, -1.9485], stops["341"][-1]]
    assert all(set(r) == {"line", "destination", "path"} for r in view["routes"])  # each stop is sent once


def test_the_longest_journey_stands_for_its_line_and_destination():
    short = replace(EAST, calls=EAST.calls[3:6], journey_code="short")
    view = area_view(BusBoard([], schedule(short, EAST), True, None, [], []), NOW)
    assert len(view["routes"][0]["path"]) == 7


def test_inbound_only_journeys_draw_no_route():
    # Ending at a stand near stop A, it gives no departures: its road isn't drawn.
    to_stand = replace(EAST, destination="Glossop", inbound_only=True)
    view = area_view(BusBoard([], schedule(EAST, WEST, to_stand), True, None, [], []), NOW)
    assert [(r["line"], r["destination"]) for r in view["routes"]] == [("237", "Ashton"), ("341", "Hadfield")]


def test_no_stop_codes_or_vehicle_references_reach_the_page():
    text = json.dumps(area_view(board([replace(bus(*HALFWAY, at(10, 14)), vehicle_ref="SECRET-REF")]), NOW))
    assert HENRY_A not in text and ST_MARYS_W not in text and "SECRET-REF" not in text


def test_what_the_server_sends_is_what_the_page_was_built_for():
    from tests.dashboard.test_static import AREA, check

    placed = bus(*HALFWAY, at(10, 14))
    unmatched = bus(53.4440, -1.9600, at(10, 13, 50), origin="", aimed=None, bearing=None)
    check(area_view(board([placed, unmatched]), NOW), AREA)


# Timetable changes

TODAY = date(2026, 10, 9)  # a Friday
EVERY_DAY = frozenset(range(7))


def running(journey, start=None, end=None, days=EVERY_DAY, shift=0):
    calls = tuple(Call(c.atco, c.minute + shift) for c in journey.calls)
    return replace(journey, calls=calls, calendar=Calendar(days=days, start=start, end=end))


def test_a_new_timetable_with_other_times_is_a_change_from_its_first_day():
    old = running(EAST, end=date(2026, 10, 24))
    new = running(EAST, start=date(2026, 10, 25), shift=5)  # five minutes later from Sunday 25th
    assert timetable_changes(schedule(old, new, running(WEST)), TODAY) == [
        {"line": "341", "operator": "BNML", "from": "2026-10-25"},
    ]


def test_journeys_that_end_with_nothing_new_are_a_change_too():
    ends = running(WEST, end=date(2026, 10, 15))
    assert timetable_changes(schedule(running(EAST), ends), TODAY) == [
        {"line": "237", "operator": "BNML", "from": "2026-10-16"},
    ]


def test_the_same_times_under_a_new_calendar_are_no_change():
    old = running(EAST, end=date(2026, 10, 24))
    same = running(EAST, start=date(2026, 10, 25))
    assert timetable_changes(schedule(old, same), TODAY) == []


def test_only_the_next_28_days_count_and_each_line_once():
    later = [running(EAST, end=date(2026, 11, 30)), running(EAST, start=date(2026, 12, 1), shift=3)]
    assert timetable_changes(schedule(*later), TODAY) == []
    twice = [running(EAST, end=date(2026, 10, 14)), running(EAST, start=date(2026, 10, 15), end=date(2026, 10, 20),
                                                              shift=2),
             running(EAST, start=date(2026, 10, 21), shift=4)]
    assert timetable_changes(schedule(*twice), TODAY) == [{"line": "341", "operator": "BNML", "from": "2026-10-15"}]


def test_a_weekday_only_change_is_seen_on_its_own_weekday():
    # Saturdays change from the 17th; weekdays stay as they are.
    weekdays = running(EAST, days=frozenset(range(5)))
    saturdays = running(EAST, end=date(2026, 10, 16), days=frozenset({5}))
    new_saturdays = running(EAST, start=date(2026, 10, 17), days=frozenset({5}), shift=10)
    assert timetable_changes(schedule(weekdays, saturdays, new_saturdays), TODAY) == [
        {"line": "341", "operator": "BNML", "from": "2026-10-17"},
    ]


def test_inbound_only_journeys_are_no_change():
    # They give no departures, so a new timetable for them changes nothing you see.
    old = replace(running(EAST, end=date(2026, 10, 24)), inbound_only=True)
    new = replace(running(EAST, start=date(2026, 10, 25), shift=5), inbound_only=True)
    assert timetable_changes(schedule(old, new, running(WEST)), TODAY) == []
