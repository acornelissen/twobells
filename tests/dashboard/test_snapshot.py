import dataclasses
import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from tests.sources.test_livebus import (
    EAST,
    HALFWAY,
    HENRY_A,
    JUNCTION,
    POSITIONS,
    ST_MARYS_W,
    WEST,
    at,
    bus,
    live,
    schedule,
)
from twobells.core.model import Departure, Status
from twobells.core.walk import WalkTimes
from twobells.dashboard.snapshot import (
    Hub,
    bus_observations,
    bus_row,
    buses_part,
    clock_at,
    row_id,
    status_block,
    status_spoken,
    status_text,
    timetable_block,
    train_observations,
    trains_part,
    weather_part,
)
from twobells.settings import show
from twobells.sources.bodsbus import BusBoard
from twobells.sources.darwin import parse_board_rich, parse_service_details
from twobells.sources.weather import parse_conditions

LONDON = ZoneInfo("Europe/London")
HEX16 = re.compile(r"[0-9a-f]{16}")
WALK = WalkTimes(train=10, bus={"A": 8, "B": 6})


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def glossop():
    return parse_board_rich(fixture("ldbws_glossop_details.json"))


FETCHED = datetime(2026, 10, 9, 22, 5, 38, tzinfo=LONDON)
# The 22:55 to Manchester and the 23:10 to Hadfield on that board, and their
# service details as at 22:55.
TO_MANCHESTER, TO_HADFIELD = "9000004GLSP____", "9000005GLSP____"
DETAILS_AT = datetime(2026, 10, 9, 22, 55, 19, tzinfo=LONDON)


def service_details(name, **changes):
    raw = fixture(f"ldbws_service_details_from_{name}.json")
    raw.update(changes)
    return parse_service_details({k: v for k, v in raw.items() if v is not None})


def known_details():
    return {TO_MANCHESTER: service_details("hadfield"), TO_HADFIELD: service_details("manchester")}


def train(std="07:12", etd="On time", **extra):
    service = {
        "std": std, "etd": etd, "platform": "1", "operator": "Northern", "operatorCode": "NT",
        "serviceID": f"{std.replace(':', '')}GLSP____", "length": 3,
        "origin": [{"locationName": "Hadfield", "crs": "HDF"}],
        "destination": [{"locationName": "Manchester Piccadilly", "crs": "MAN"}],
    }
    service.update(extra)
    return service


def trains(*services, fetched=datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), walk=WALK, phrases=None, details=None):
    board = parse_board_rich({"trainServices": list(services), "locationName": "Glossop"})
    kwargs = {"phrases": phrases} if phrases else {}
    return trains_part(board, fetched, status_block(), "GLO", "MAN", walk, "GLO>MAN", details=details, **kwargs)


def bus_part(now, vehicles=(), walk=WALK):
    found = live(now, vehicles)
    board = BusBoard(found, schedule(), True, None)
    part, lookups = buses_part(board, now, status_block(), timetable_block("ready", board.schedule, LONDON),
                               {"A": HENRY_A}, walk)
    return part, lookups, found


def extras_part(now, no_shows=None, vehicles_ok=True, blocks=False):
    """Buses at 10:14 with WEST's bus coming in on INTO_A, 9 min late, and EAST's bus live.
    With `blocks`, the two are in one block, so WEST's bus is followed in."""
    from tests.dashboard.test_enrich import INTO_A, LEFT_JUNCTION
    from tests.sources.test_livebus import every_day
    from twobells.sources.lastbus import day_groups, last_buses
    from twobells.sources.livebus import live_board

    plan = schedule(EAST, WEST, INTO_A)
    if blocks:
        plan = schedule(EAST, dataclasses.replace(WEST, block="T122"), dataclasses.replace(INTO_A, block="T122"))
    east = dataclasses.replace(bus(*HALFWAY, now), fleet_number="3017", registration="AB25 CDE")
    vehicles = [east, bus(*HALFWAY, now, line="237", origin=JUNCTION, aimed=LEFT_JUNCTION)]
    rows, placed = live_board(plan, now, vehicles if vehicles_ok else [], every_day, 10)
    board = BusBoard(rows, plan, vehicles_ok, None, placed)
    kwargs = {"no_shows": no_shows} if no_shows else {}
    local = now.replace(tzinfo=None)
    last = last_buses({local.date(): day_groups(plan, local.date())}, local)
    return buses_part(board, now, status_block(), timetable_block("ready", plan, LONDON),
                      {"A": HENRY_A, "B": ST_MARYS_W}, WALK, last=last, **kwargs)


def test_a_last_bus_shows_under_the_buses_in_the_evening_or_once_few_are_left():
    from twobells.dashboard.snapshot import last_bus
    from twobells.sources.lastbus import LastBus

    late = LastBus("341", "Hadfield", "A", date(2026, 10, 9), datetime(2026, 10, 10, 0, 20), 5)
    assert last_bus(late, at(18, 59)) == {
        "line": "341", "destination": "Hadfield", "stop": "A", "scheduled": "00:20",
        "scheduled_at": "2026-10-10T00:20:00+01:00", "remaining": 5, "show": False,
    }
    assert last_bus(late, at(19, 0))["show"] is True
    assert last_bus(LastBus("341", "Hadfield", "A", date(2026, 10, 9), datetime(2026, 10, 9, 13, 0), 3), at(12, 0))["show"]


def by(rows, **match):
    return next(r for r in rows if all(r[k] == v for k, v in match.items()))


# Ids


def test_ids_are_sixteen_hex_characters_of_a_hash_of_what_the_row_is():
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    first = part["rows"][0]
    assert first["id"] == hashlib.sha256(b"train|9000001GLSP____").hexdigest()[:16]
    assert all(HEX16.fullmatch(r["id"]) for r in part["rows"])
    assert row_id("bus", "x") != row_id("train", "x")


def test_ids_are_stable_from_one_snapshot_to_the_next():
    first, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    later, _ = trains_part(glossop(), FETCHED + timedelta(minutes=1), status_block(), "GLO", None, WALK, "GLO")
    assert [r["id"] for r in first["rows"]] == [r["id"] for r in later["rows"]]


def test_a_train_without_a_service_id_is_named_by_time_and_destination():
    part, _ = trains(train(serviceID=None))
    assert part["rows"][0]["id"] == row_id("train", "07:12+Manchester Piccadilly")


def test_bus_ids_hash_the_departure_key():
    part, lookups, found = bus_part(at(10, 0))
    assert [r["id"] for r in part["rows"]] == [row_id("bus", l.key) for l in found]
    assert set(lookups) == {r["id"] for r in part["rows"]}


def test_no_service_id_or_bus_key_reaches_the_json():
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    text = json.dumps(part)
    assert "GLSP" not in text and "serviceID" not in text
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO",
                          details=known_details(), now=DETAILS_AT)
    text = json.dumps(part)
    assert "GLSP" not in text and "serviceID" not in text and "At Glossop now" in text
    buses, lookups, found = bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    text = json.dumps(buses)
    assert HENRY_A not in text and all(l.key not in text for l in found)
    assert all(HENRY_A not in json.dumps(lookup["bus"]) for lookup in lookups.values())


# Trains


def test_a_darwin_board_becomes_rows():
    part, lookups = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    assert part["station"] == "Glossop" and part["filter"] is None
    assert part["messages"] == [
        {"text": "Help points at this station are out of use.", "category": None, "severity": None, "kind": "station"},
    ]
    first = part["rows"][0]
    assert first["scheduled"] == "22:08" and first["scheduled_at"] == "2026-10-09T22:08:00+01:00"
    assert first["expected"] == "22:08" and first["status"] == "on_time" and first["status_text"] == "On time"
    assert (first["service"], first["origin"], first["destination"]) == (
        "Northern", "Manchester Piccadilly", "Hadfield",
    )
    assert (first["platform"], first["length"], first["minutes_late"]) == ("2", 2, None)
    assert first["calling_points"] == [
        {"name": "Hadfield", "crs": "HDF", "scheduled": "22:14", "expected": "22:14", "actual": None,
         "cancelled": False},
    ]
    assert first["history"] == {"phrase": None}
    assert lookups[first["id"]]["history"]["route"] == "GLO|Hadfield"


def test_a_train_with_details_says_where_it_is_and_where_it_has_been():
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO",
                          details=known_details(), now=DETAILS_AT)
    to_manchester, to_hadfield = by(part["rows"], scheduled="22:55"), by(part["rows"], scheduled="23:10")
    assert to_manchester["position"] == {
        "where": "At Glossop now", "strip": {"stops": ["Hadfield", "Glossop", "Dinting"], "yours": 1, "train_at": 1},
    }
    assert to_manchester["previous_points"] == [
        {"name": "Hadfield", "crs": "HDF", "scheduled": "22:47", "expected": None, "actual": "22:47",
         "cancelled": False},
    ]
    assert to_hadfield["position"]["where"] == "Left Flowery Field"
    assert to_hadfield["position"]["strip"]["train_at"] is None
    previous = to_hadfield["previous_points"]
    assert [p["name"] for p in previous][:2] == ["Manchester Piccadilly", "Ashburys"]
    # Darwin's words become times or nothing, as for the stops after.
    assert by(previous, name="Gorton")["actual"] == "22:42"
    assert by(previous, name="Flowery Field")["actual"] is None
    assert by(previous, name="Godley")["expected"] == "22:54"
    for row in part["rows"]:
        if row not in (to_manchester, to_hadfield):
            assert row["position"] is None and row["previous_points"] == []


def test_a_train_is_placed_at_the_time_given_else_when_the_board_was_fetched():
    on_the_way = {TO_MANCHESTER: service_details("hadfield", ata=None)}  # left Hadfield 22:47, due 22:52
    at = datetime(2026, 10, 9, 22, 49, 30, tzinfo=LONDON)
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO", details=on_the_way, now=at)
    assert by(part["rows"], scheduled="22:55")["position"]["strip"]["train_at"] == 0.5
    part, _ = trains_part(glossop(), at, status_block(), "GLO", None, WALK, "GLO", details=on_the_way)
    assert by(part["rows"], scheduled="22:55")["position"]["strip"]["train_at"] == 0.5


def test_without_details_a_train_has_no_position():
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    assert all(r["position"] is None and r["previous_points"] == [] for r in part["rows"])
    unnamed, _ = trains(train(serviceID=None), details={"07:12+Manchester Piccadilly": service_details("hadfield")})
    assert unnamed["rows"][0]["position"] is None


def test_a_late_train_is_expected_later_and_left_for_later():
    part, _ = trains(train("07:12", "07:19", delayReason="This train has been delayed by a broken down train"))
    row = part["rows"][0]
    assert (row["status"], row["status_text"], row["minutes_late"]) == ("late", "Exp 07:19", 7)
    assert row["status_spoken"] == "Expected 07:19"
    assert row["expected_at"] == "2026-10-10T07:19:00+01:00"
    assert row["leave_at"] == "2026-10-10T07:09:00+01:00"  # 10 min walk
    assert row["delay_reason"] == "This train has been delayed by a broken down train"


def test_delayed_without_a_time_has_no_minutes_late():
    row = trains(train(etd="Delayed"))[0]["rows"][0]
    assert (row["status"], row["status_text"], row["minutes_late"], row["expected_at"]) == (
        "late", "Delayed", None, None,
    )
    assert row["leave_at"] == "2026-10-10T07:02:00+01:00"  # from the timetabled time


def test_a_cancelled_train_has_no_leave_time():
    row = trains(train(etd="Cancelled", isCancelled=True, cancelReason="A fault"))[0]["rows"][0]
    assert (row["status"], row["status_text"], row["leave_at"], row["cancel_reason"]) == (
        "cancelled", "Cancelled", None, "A fault",
    )


def test_no_walk_time_no_leave_time():
    row = trains(train(), walk=WalkTimes())[0]["rows"][0]
    assert row["leave_at"] is None


def test_no_report_is_said_in_words():
    row = trains(train(etd="No report"))[0]["rows"][0]
    assert (row["status"], row["status_text"]) == ("unknown", "No report")


def test_times_are_read_as_the_nearest_to_when_the_board_was_fetched():
    late_evening = datetime(2026, 10, 9, 23, 50, tzinfo=LONDON)
    row = trains(train("00:05", "00:12"), fetched=late_evening)[0]["rows"][0]
    assert row["scheduled_at"] == "2026-10-10T00:05:00+01:00"
    assert row["expected_at"] == "2026-10-10T00:12:00+01:00"
    assert clock_at("23:58", datetime(2026, 10, 10, 0, 3, tzinfo=LONDON)).isoformat() == "2026-10-09T23:58:00+01:00"


def test_times_carry_the_offset_of_their_own_day():
    winter = datetime(2026, 10, 25, 23, 0, tzinfo=LONDON)  # clocks went back that morning
    row = trains(train("23:10"), fetched=winter)[0]["rows"][0]
    assert row["scheduled_at"] == "2026-10-25T23:10:00+00:00"


def test_history_phrases_come_from_the_history():
    asked = []

    def phrases(mode, route, scheduled, day):
        asked.append((mode, route, scheduled, day))
        return "saturday", "Late 4 of the last 10 Saturdays (avg 6 min)"

    part, lookups = trains(train(), phrases=phrases)
    row = part["rows"][0]
    assert row["history"] == {"phrase": "Late 4 of the last 10 Saturdays (avg 6 min)"}
    assert asked == [("train", "GLO>MAN|Manchester Piccadilly", "07:12", date(2026, 10, 10))]
    assert lookups[row["id"]]["history"] == {
        "mode": "train", "route": "GLO>MAN|Manchester Piccadilly", "scheduled": "07:12",
        "label": "07:12 to Manchester Piccadilly", "day_type": "saturday",
        "phrase": "Late 4 of the last 10 Saturdays (avg 6 min)",
    }


def test_no_board_yet_is_an_empty_section():
    part, lookups = trains_part(None, None, status_block(), "GLO", "MAN", WALK, "GLO>MAN")
    assert part == {"station": "GLO", "filter": "MAN", "status": status_block(), "positions": None, "messages": [],
                    "rows": []}
    assert lookups == {}


def test_train_observations_for_the_history():
    board = parse_board_rich({"trainServices": [
        train("07:12", "07:19", delayReason="Signals"), train("07:42", "Cancelled", isCancelled=True),
        train("08:12"), train("08:42", "Delayed"),
    ]})
    fetched = datetime(2026, 10, 10, 7, 0, tzinfo=LONDON)
    seen = train_observations(board, fetched, "GLO>MAN")
    assert [(o.scheduled, o.status, o.delay_min, o.departs_at, o.reason) for o in seen] == [
        ("07:12", "late", 7, datetime(2026, 10, 10, 7, 19), "Signals"),
        ("07:42", "cancelled", None, datetime(2026, 10, 10, 7, 42), None),
        ("08:12", "on_time", 0, datetime(2026, 10, 10, 8, 12), None),
        ("08:42", "late", None, datetime(2026, 10, 10, 8, 42), None),
    ]
    assert seen[0].route == "GLO>MAN|Manchester Piccadilly" and seen[0].day == date(2026, 10, 10)
    assert seen[0].identity == "0712GLSP____" and seen[0].live


# Rail replacement buses: in the trains, said to be buses, kept apart in the history.


def replacement(std="07:12", etd="On time", **extra):
    return train(std, etd, serviceType="bus", platform="BUS", length=0, **extra)


def test_a_board_of_replacement_buses_alone_has_rows_that_say_so():
    board = parse_board_rich({"busServices": [replacement(), replacement("07:42")], "locationName": "Glossop"})
    part, lookups = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "GLO", "MAN",
                                WALK, "GLO>MAN")
    first, second = part["rows"]
    assert first["replacement_bus"] is True and second["replacement_bus"] is True
    assert (first["scheduled"], first["destination"], first["platform"], first["length"]) == (
        "07:12", "Manchester Piccadilly", None, None,
    )
    assert first["leave_at"] == "2026-10-10T07:02:00+01:00"  # walked to like a train
    history = lookups[first["id"]]["history"]
    assert history["mode"] == "train"
    assert history["route"] == "GLO>MAN bus|Manchester Piccadilly"
    assert history["label"] == "07:12 replacement bus to Manchester Piccadilly"


def test_trains_are_not_replacement_buses():
    part, lookups = trains(train())
    [row] = part["rows"]
    assert row["replacement_bus"] is False
    assert lookups[row["id"]]["history"]["route"] == "GLO>MAN|Manchester Piccadilly"


def test_a_train_and_its_replacement_bus_are_told_apart_in_the_history():
    board = parse_board_rich({"trainServices": [train("07:12")], "busServices": [replacement("07:12")]})
    seen = train_observations(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), "GLO>MAN")
    assert sorted(o.route for o in seen) == ["GLO>MAN bus|Manchester Piccadilly", "GLO>MAN|Manchester Piccadilly"]
    assert {o.platform for o in seen} == {"1", None}
    part, _ = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "GLO", "MAN", WALK,
                          "GLO>MAN")
    assert len({r["id"] for r in part["rows"]}) == 2


# Ferries: in the trains, said to be ferries, kept apart in the history.


def ferry(std="07:12", etd="On time", **extra):
    return train(std, etd, **{"serviceType": "ferry", "platform": None, "length": 0, "operator": "Wightlink",
                              "destination": [{"locationName": "Ryde Pier Head", "crs": "RYP"}], **extra})


def test_a_ferry_has_a_row_that_says_so_and_a_history_of_its_own():
    board = parse_board_rich({"trainServices": [train("07:12")], "ferryServices": [ferry("07:12")],
                              "locationName": "Portsmouth Harbour"})
    part, lookups = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "PMH", None,
                                WALK, "PMH")
    the_train, the_ferry = part["rows"]
    assert (the_ferry["ferry"], the_ferry["replacement_bus"]) == (True, False)
    assert the_train["ferry"] is False
    assert (the_ferry["destination"], the_ferry["platform"], the_ferry["length"], the_ferry["formation"]) == (
        "Ryde Pier Head", None, None, None,
    )
    assert the_ferry["id"] != the_train["id"]
    history = lookups[the_ferry["id"]]["history"]
    assert history["mode"] == "train"
    assert history["route"] == "PMH ferry|Ryde Pier Head"
    assert history["label"] == "07:12 ferry to Ryde Pier Head"
    seen = train_observations(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), "PMH")
    assert sorted(o.route for o in seen) == ["PMH ferry|Ryde Pier Head", "PMH|Manchester Piccadilly"]


def test_a_ferry_has_no_usual_platform_and_never_a_changed_one():
    asked = []
    board = parse_board_rich({"ferryServices": [ferry(formation=coaches(10, 20))]})
    part, _ = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "PMH", None, WALK,
                          "PMH", platforms=lambda route, scheduled: asked.append(route) or "1")
    [row] = part["rows"]
    assert asked == []
    assert (row["platform"], row["usual_platform"], row["platform_changed"], row["formation"]) == (
        None, None, False, None,
    )


def test_a_ferry_is_announced_as_the_next_ferry():
    board = parse_board_rich({"ferryServices": [ferry("07:12", platform="3", length=2)]})
    part, _ = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "PMH", None, WALK,
                          "PMH")
    [row] = part["rows"]
    [said] = row["announce"]
    assert said["kind"] == "next"
    assert said["text"].startswith("The next ferry to depart will be the 07:12 Wightlink service to Ryde Pier Head.")
    assert "platform" not in said["text"] and "coach" not in said["text"]


# A train's coaches, for its sheet: from the front, with where it is quietest.


def coaches(*loadings, **extra):
    return {"coaches": [{"number": str(i + 1), "coachClass": "First" if i == 0 else "Standard",
                         "toilet": {"value": "Accessible" if i == 1 else "None", "status": "InService"},
                         "loading": load} for i, load in enumerate(loadings)], **extra}


def formation_of(**extra):
    [row] = trains(train(**extra))[0]["rows"]
    return row["formation"]


def test_a_train_s_coaches_from_the_front_with_how_busy_each_is():
    found = formation_of(formation=coaches(80, 50, 10))
    assert found == {
        "coaches": [
            {"number": "1", "class": "first", "toilet": None, "toilet_out_of_order": False, "loading": 80,
             "busy": "Very busy"},
            {"number": "2", "class": "standard", "toilet": "accessible", "toilet_out_of_order": False,
             "loading": 50, "busy": "Busy"},
            {"number": "3", "class": "standard", "toilet": None, "toilet_out_of_order": False, "loading": 10,
             "busy": "Quiet"},
        ],
        "loading_text": "Quietest at the back",
        "detach_text": None,
        "reverse": False,
    }


@pytest.mark.parametrize("loadings, words", [
    ((10, 80), "Quietest at the front"),
    ((60, 70, 20, 65, 70), "Quietest in the middle"),
    ((60, 50, 40, 30, 20, 10, 5, 0), "Quietest at the back"),
    ((40, 45, 50), "Much the same all along the train"),
    ((40, None, None, None), None),  # too few coaches say
    ((40,), None),  # one coach has no quieter end
])
def test_where_it_is_quietest_in_words(loadings, words):
    found = formation_of(formation=coaches(*loadings))
    assert found["loading_text"] == words


def test_without_each_coach_s_loading_the_average_says_how_busy_overall():
    assert formation_of(formation={"avgLoading": 80})["loading_text"] == "Very busy overall"
    assert formation_of(formation={"avgLoading": 10})["coaches"] == []
    assert formation_of(formation=coaches(None, None, avgLoading=40))["loading_text"] == "Busy overall"


def test_the_front_detaching_here_is_said_with_the_count_when_darwin_shows_it():
    after = {"subsequentCallingPoints": [{"callingPoint": [
        {"locationName": "Dinting", "crs": "DTG", "st": "07:15", "et": "On time", "length": 2}]}]}
    assert formation_of(detachFront=True, length=6, **after)["detach_text"] == "Front 4 coaches detach here"
    assert formation_of(detachFront=True, length=3, **after)["detach_text"] == "Front 1 coach detaches here"
    assert formation_of(detachFront=True, length=2, **after)["detach_text"] == "Front coaches detach here"
    found = formation_of(detachFront=True)
    assert found == {"coaches": [], "loading_text": None, "detach_text": "Front coaches detach here", "reverse": False}


def test_a_reverse_formation_is_said():
    assert formation_of(isReverseFormation=True)["reverse"] is True


def test_most_trains_say_nothing_of_their_coaches():
    # Many operators send no formation: the row keeps its coach count alone.
    part, _ = trains(train())
    [row] = part["rows"]
    assert row["formation"] is None and row["length"] == 3
    assert formation_of(detachFront=False, isReverseFormation=False, formation={"coaches": []}) is None


def test_a_replacement_bus_never_has_coaches():
    board = parse_board_rich({"busServices": [replacement(formation=coaches(10, 20), detachFront=True)]})
    part, _ = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "GLO", "MAN",
                          WALK, "GLO>MAN")
    assert part["rows"][0]["formation"] is None


# Platform changes, the usual platform and via words.


def platforms_part(*services, usual=None):
    """The trains part, with `usual` as the platform every train usually leaves from."""
    asked = []

    def platforms(route, scheduled):
        asked.append((route, scheduled))
        return usual

    board = parse_board_rich({"trainServices": [s for s in services if s.get("serviceType") != "bus"],
                              "busServices": [s for s in services if s.get("serviceType") == "bus"]})
    part, _ = trains_part(board, datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "GLO", "MAN", WALK,
                          "GLO>MAN", platforms=platforms)
    return part["rows"], asked


def test_a_row_says_its_usual_platform_and_whether_this_is_it():
    [row], asked = platforms_part(train(platform="1"), usual="1")
    assert asked == [("GLO>MAN|Manchester Piccadilly", "07:12")]
    assert (row["platform"], row["usual_platform"], row["platform_changed"]) == ("1", "1", False)
    [row], _ = platforms_part(train(platform="2"), usual="1")
    assert (row["platform"], row["usual_platform"], row["platform_changed"]) == ("2", "1", True)


def test_no_usual_platform_and_no_platform_is_no_change():
    [row], _ = platforms_part(train(platform="2"))
    assert (row["usual_platform"], row["platform_changed"]) == (None, False)
    [row], _ = platforms_part(train(platform=None), usual="1")
    assert (row["platform"], row["platform_changed"]) == (None, False)


def test_a_change_since_the_last_fetch_is_a_change_whatever_the_history():
    board = parse_board_rich({"trainServices": [train(platform="1")]})
    [service] = board.services
    changed = dataclasses.replace(service, departure=dataclasses.replace(service.departure, platform_changed=True))
    marked = dataclasses.replace(board, services=[changed])
    part, _ = trains_part(marked,datetime(2026, 10, 10, 7, 0, tzinfo=LONDON), status_block(), "GLO", "MAN",
                          WALK, "GLO>MAN", platforms=lambda route, scheduled: "1")
    assert part["rows"][0]["platform_changed"] is True


def test_a_cancelled_train_never_has_a_changed_platform():
    cancelled = train(etd="Cancelled", isCancelled=True, platform="2")
    [row], _ = platforms_part(cancelled, usual="1")
    assert (row["platform"], row["usual_platform"], row["platform_changed"]) == ("2", "1", False)
    assert [item["kind"] for item in row["announce"]] == ["cancelled"]
    board = parse_board_rich({"trainServices": [cancelled]})
    [service] = board.services
    changed = dataclasses.replace(service, departure=dataclasses.replace(service.departure, platform_changed=True))
    part, _ = trains_part(dataclasses.replace(board, services=[changed]), datetime(2026, 10, 10, 7, 0, tzinfo=LONDON),
                          status_block(), "GLO", "MAN", WALK, "GLO>MAN")
    assert part["rows"][0]["platform_changed"] is False


def test_a_replacement_bus_has_no_usual_platform_and_never_a_changed_one():
    [row], asked = platforms_part(replacement(), usual="1")
    assert asked == []
    assert (row["platform"], row["usual_platform"], row["platform_changed"]) == (None, None, False)


def test_a_row_carries_its_via_words():
    dests = [{"locationName": "Manchester Piccadilly", "crs": "MAN", "via": "via Guide Bridge"}]
    [row], _ = platforms_part(train(destination=dests))
    assert (row["destination"], row["via"]) == ("Manchester Piccadilly", "via Guide Bridge")
    [row], _ = platforms_part(train())
    assert row["via"] is None


def test_a_row_carries_its_calling_at_line():
    tparts, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    to_manchester = tparts["rows"][1]
    assert to_manchester["calling_at"].startswith("Calling at: Dinting, Broadbottom, Hattersley")
    assert to_manchester["calling_at"].endswith("and Manchester Piccadilly. This train is formed of 2 coaches.")


def test_the_first_train_not_cancelled_is_announced_as_the_next():
    tparts, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    first, second = tparts["rows"][:2]
    assert first["announce"] == [{"kind": "next", "text": (
        "The next train to depart from platform 2 will be the 22:08 Northern service to Hadfield. "
        "Calling at Hadfield only. This train is formed of 2 coaches.")}]
    assert [item["kind"] for item in second["announce"]] == ["departs"]
    assert second["announce"][0]["text"].startswith(
        "The 22:25 Northern service to Manchester Piccadilly will depart from platform 2. Calling at Dinting, ")


def test_a_cancelled_or_late_train_is_announced_with_its_reason():
    part, _ = trains(
        train("07:12", "Cancelled", isCancelled=True,
              cancelReason="This train has been cancelled because of a shortage of train crew"),
        train("07:20", "07:30", platform="2", delayReason="This train has been delayed by a broken down train"),
    )
    cancelled, late = part["rows"]
    assert cancelled["announce"] == [{"kind": "cancelled", "text": (
        "We are sorry to announce that the 07:12 Northern service to Manchester Piccadilly has been cancelled. "
        "This is due to a shortage of train crew.")}]
    # The cancelled train isn't the next to depart; the late one is.
    assert [item["kind"] for item in late["announce"]] == ["delayed", "next"]
    assert late["announce"][0]["text"] == (
        "We are sorry to announce that the 07:20 Northern service to Manchester Piccadilly is delayed by "
        "approximately 10 minutes. This is due to a broken down train.")
    assert late["announce"][1]["text"] == (
        "The next train to depart from platform 2 will be the delayed 07:20 Northern service to Manchester "
        "Piccadilly. This train is formed of 3 coaches.")


def test_a_changed_platform_is_announced_as_an_alteration():
    [row], _ = platforms_part(train(platform="2"), usual="1")
    assert row["announce"][0] == {"kind": "platform", "text": (
        "Platform alteration: the 07:12 Northern service to Manchester Piccadilly will now depart from platform 2.")}


# Status words


@pytest.mark.parametrize("mode, status, expected, words, spoken", [
    ("train", Status.ON_TIME, None, "On time", "On time"),
    ("train", Status.LATE, "07:19", "Exp 07:19", "Expected 07:19"),
    ("train", Status.LATE, None, "Delayed", "Delayed"),
    ("train", Status.CANCELLED, None, "Cancelled", "Cancelled"),
    ("train", Status.UNKNOWN, None, "No report", "No report"),
    ("bus", Status.ON_TIME, None, "On time", "On time"),
    ("bus", Status.LATE, "10:19", "Exp 10:19", "Expected 10:19"),
    ("bus", Status.UNKNOWN, None, "Timetable only", "Timetable only"),
])
def test_status_text_is_always_words(mode, status, expected, words, spoken):
    departure = Departure(mode, "x", "y", "07:12", expected, status)
    assert (status_text(departure), status_spoken(departure)) == (words, spoken)


def test_every_row_has_words_for_its_status():
    rows = trains(train(), train("07:30", "07:41"), train("07:50", "Delayed"), train("08:00", "Cancelled"),
                  train("08:10", "No report"))[0]["rows"]
    rows += bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])[0]["rows"]
    for row in rows:
        assert re.fullmatch(r"[A-Z][a-z]+( [a-z]+)*( \d\d:\d\d)?", row["status_text"]), row["status_text"]
        assert re.fullmatch(r"[A-Z][a-z]+( [a-z]+)*( \d\d:\d\d)?", row["status_spoken"]), row["status_spoken"]


# Buses


def test_a_live_late_bus():
    part, lookups, _ = bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    row = by(part["rows"], line="341", stop="A")
    assert row["scheduled_at"] == "2026-10-09T10:10:00+01:00" and row["expected_at"] == "2026-10-09T10:19:00+01:00"
    assert (row["status"], row["status_text"], row["minutes_late"], row["live"]) == (
        "late", "Exp 10:19", 9, True,
    )
    assert row["status_spoken"] == "Expected 10:19"
    assert row["vehicle"] == {"age_s": 0, "recorded_at": "2026-10-09T10:14:00+01:00", "bearing": 90.0,
                              "fleet_number": None, "registration": None}
    strip = row["strip"]
    assert strip["stops"][strip["yours"]] == "Henry Street"
    assert strip["bus_at"] == pytest.approx(2.5, abs=0.05)  # halfway Sunlaws Street to St Marys Road
    assert strip["where"] == "3 stops away"
    assert row["history"] == {"phrase": None, "no_show_phrase": None}
    history = lookups[row["id"]]["history"]
    assert (history["mode"], history["route"], history["label"]) == ("bus", "A|341|Hadfield", "10:10 341 to Hadfield")


def test_leave_at_follows_each_stop_s_walk():
    part, _, _ = bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    a = by(part["rows"], line="341", stop="A")
    b = by(part["rows"], line="237", stop="B")
    assert a["leave_at"] == "2026-10-09T10:11:00+01:00"  # expected 10:19, 8 min walk to A
    assert b["leave_at"] == "2026-10-09T10:12:00+01:00"  # timetabled 10:18, 6 min walk to B
    star = bus_part(at(10, 14), walk=WalkTimes(bus={"*": 3}))[0]
    assert by(star["rows"], line="237", stop="B")["leave_at"] == "2026-10-09T10:15:00+01:00"
    none = bus_part(at(10, 14), walk=WalkTimes(train=5))[0]
    assert all(r["leave_at"] is None for r in none["rows"])


def test_every_row_carries_its_walk_in_whole_minutes():
    part, _, _ = bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    assert by(part["rows"], line="341", stop="A")["walk_min"] == 8
    assert by(part["rows"], line="237", stop="B")["walk_min"] == 6
    star = bus_part(at(10, 14), walk=WalkTimes(bus={"*": 3}))[0]
    assert {r["walk_min"] for r in star["rows"]} == {3}
    none = bus_part(at(10, 14), walk=WalkTimes(train=5))[0]
    assert {r["walk_min"] for r in none["rows"]} == {None}
    rows = trains(train(), train("07:30", "Cancelled"))[0]["rows"]
    assert [r["walk_min"] for r in rows] == [10, 10]  # a cancelled train is still 10 min away
    assert trains(train(), walk=WalkTimes())[0]["rows"][0]["walk_min"] is None


def test_a_timetable_only_bus():
    part, lookups, _ = bus_part(at(10, 0))
    row = by(part["rows"], line="237", stop="B")
    assert (row["status"], row["status_text"], row["live"], row["expected"], row["expected_at"]) == (
        "unknown", "Timetable only", False, None, None,
    )
    assert row["vehicle"] is None and row["minutes_late"] is None
    assert row["strip"]["bus_at"] is None and row["strip"]["where"] == "No live position"
    assert lookups[row["id"]]["bus"]["vehicle"] is None


def test_a_live_bus_says_its_fleet_number_and_registration_but_not_its_ref():
    vehicle = dataclasses.replace(bus(*HALFWAY, at(10, 14)), vehicle_ref="3017_-_AB25_CDE",
                                  fleet_number="3017", registration="AB25 CDE")
    part, lookups, _ = bus_part(at(10, 14), [vehicle])
    row = by(part["rows"], line="341", stop="A")
    assert (row["vehicle"]["fleet_number"], row["vehicle"]["registration"]) == ("3017", "AB25 CDE")
    found = lookups[row["id"]]["bus"]["vehicle"]
    assert (found["fleet_number"], found["registration"]) == ("3017", "AB25 CDE")
    assert "3017_-_AB25_CDE" not in json.dumps(part) + json.dumps(lookups[row["id"]]["bus"])


def test_the_bus_map_has_the_positioned_stops_and_the_bus():
    part, lookups, _ = bus_part(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    row = by(part["rows"], line="341", stop="A")
    found = lookups[row["id"]]["bus"]
    assert (found["id"], found["line"], found["destination"], found["stop"]) == (row["id"], "341", "Hadfield", "A")
    assert [(s["name"], s["time"], s["yours"]) for s in found["stops"]] == [
        ("The Junction", "10:00", False), ("Grapes Inn", "10:02", False), ("Sunlaws Street", "10:04", False),
        ("St Marys Road", "10:06", False), ("Arundel Street", "10:08", False), ("Henry Street", "10:10", True),
        ("Howard Arms", "10:11", False),
    ]
    assert found["stops"][5]["lat"] == POSITIONS[HENRY_A].lat
    assert found["vehicle"]["lat"] == HALFWAY[0] and found["vehicle"]["age_s"] == 0
    assert found["where"] == "3 stops away"
    # Without roads in the timetable, the path is straight from stop to stop.
    assert found["path"] == [[s["lat"], s["lon"]] for s in found["stops"]]


def test_the_bus_map_path_follows_the_timetable_s_roads():
    from dataclasses import replace as swap

    from tests.sources.test_livebus import GRAPES, SUNLAWS

    roads = {f"{JUNCTION}>{GRAPES}": ((53.4446, -1.9645), (53.4444, -1.9635)), f"{SUNLAWS}>NOWHERE": ((1.0, 1.0),)}
    now = at(10, 14)
    board = BusBoard(live(now, [bus(*HALFWAY, now)]), swap(schedule(), shapes=roads), True, None)
    part, lookups = buses_part(board, now, status_block(), timetable_block("ready", board.schedule, LONDON),
                               {"A": HENRY_A}, WALK)
    found = lookups[by(part["rows"], line="341", stop="A")["id"]]["bus"]
    stops = [[s["lat"], s["lon"]] for s in found["stops"]]
    # The Junction, the road to Grapes Inn, then straight on from stop to stop.
    assert found["path"] == [stops[0], [53.4446, -1.9645], [53.4444, -1.9635], *stops[1:]]


def test_stop_names_and_the_timetable():
    part, _, _ = bus_part(at(10, 0))
    # No sign letter known; Howard Arms and Market Hall are both next, once each: by name.
    assert part["stops"] == {"A": {"name": "Henry Street", "letter": None, "towards": "Howard Arms", "walk_min": 8}}
    assert part["timetable"] == {"state": "ready", "built": "2026-10-09T03:00:00+01:00", "valid_until": None,
                                 "journeys": 2}
    missing = buses_part(None, at(10, 0), status_block(note="Building"), timetable_block("building", None, LONDON),
                         {"A": HENRY_A}, WALK)[0]
    # No timetable, no stop data: nothing is made up.
    assert missing["stops"] == {"A": {"name": None, "letter": None, "towards": None, "walk_min": 8}}
    assert missing["rows"] == []
    assert missing["timetable"] == {"state": "building", "built": None, "valid_until": None, "journeys": None}
    with pytest.raises(ValueError):
        timetable_block("lost", None, LONDON)


def test_bus_rows_carry_arrivals_the_inbound_bus_and_ghosts():
    part, _ = extras_part(at(10, 14))
    east = by(part["rows"], line="341", stop="A")
    assert east["arrives"] == {"name": "Hadfield", "at": "10:34", "at_iso": "2026-10-09T10:34:00+01:00", "minutes": 15}
    assert east["inbound"] is None and east["ghost"] is False
    west_a = by(part["rows"], line="237", stop="A")
    assert west_a["inbound"] == {
        "from": "The Junction", "due": "10:19", "late_min": 9, "where": "3 stops away",
        "text": "Bus arriving from The Junction, 9 min late (due 10:19)", "followed": False,
    }
    # Guessed by line, it counts as live, the same to the rider as one followed by its block.
    assert (west_a["status_text"], west_a["expected"], west_a["live"]) == ("Exp 10:21", "10:21", True)
    assert (west_a["strip"]["where"], west_a["strip"]["bus_at"]) == ("Bus on its way, due in 5 min", None)
    assert west_a["vehicle"]["recorded_at"] == "2026-10-09T10:14:00+01:00"  # the bus guessed to be coming in
    assert west_a["ghost"] is False  # a tracked bus is on its way to work it
    west_b = by(part["rows"], line="237", stop="B")
    assert west_b["inbound"] is None and west_b["ghost"] is True  # due 10:18, nothing tracked
    assert west_b["arrives"]["at"] == "11:00"
    assert west_b["history"] == {"phrase": None, "no_show_phrase": None}


def test_a_bus_followed_in_by_its_block_makes_its_row_live():
    part, lookups = extras_part(at(10, 14), blocks=True)
    west_a = by(part["rows"], line="237", stop="A")
    assert (west_a["live"], west_a["status"], west_a["status_text"], west_a["status_spoken"]) == (
        True, "late", "Exp 10:21", "Expected 10:21")  # in at 10:19, 9 min late, then two minutes to turn round
    assert (west_a["expected"], west_a["minutes_late"]) == ("10:21", 6)
    assert west_a["vehicle"]["recorded_at"] == "2026-10-09T10:14:00+01:00"  # the bus followed in
    assert (west_a["strip"]["where"], west_a["strip"]["bus_at"]) == ("Bus on its way, due in 5 min", None)
    assert west_a["inbound"]["followed"] is True and west_a["inbound"]["due"] == "10:19"
    assert west_a["ghost"] is False
    seen = lookups[west_a["id"]]["bus"]
    assert (seen["vehicle"]["lat"], seen["vehicle"]["lon"]) == HALFWAY
    assert seen["where"] == "Bus on its way, due in 5 min"
    # Stop B is on the same journey, but not where it starts: timetable only.
    west_b = by(part["rows"], line="237", stop="B")
    assert (west_b["live"], west_b["vehicle"], west_b["inbound"]) == (False, None, None)


def test_no_ghosts_when_live_positions_failed():
    part, _ = extras_part(at(10, 14), vehicles_ok=False)
    assert part["rows"] and not any(r["ghost"] or r["inbound"] for r in part["rows"])


def test_no_show_phrases_come_from_the_history():
    asked = []

    def no_shows(route, scheduled, day):
        asked.append((route, scheduled, day))
        return "No tracker on 3 of the last 10 weekdays" if route == "B|237|Ashton" else None

    part, lookups = extras_part(at(10, 14), no_shows)
    west_b = by(part["rows"], line="237", stop="B")
    assert west_b["history"]["no_show_phrase"] == "No tracker on 3 of the last 10 weekdays"
    assert ("B|237|Ashton", "10:18", date(2026, 10, 9)) in asked
    assert by(part["rows"], line="341", stop="A")["history"]["no_show_phrase"] is None


def test_train_rows_carry_their_arrival():
    part, _ = trains(train("07:12", "07:19", subsequentCallingPoints=[{"callingPoint": [
        {"locationName": "Dinting", "crs": "DGT", "st": "07:15", "et": "07:22"},
        {"locationName": "Manchester Piccadilly", "crs": "MAN", "st": "07:48", "et": "07:55"},
    ]}]), train("07:42", "Cancelled", isCancelled=True), train("08:12"))
    late, cancelled, no_points = part["rows"]
    assert late["arrives"] == {
        "name": "Manchester Piccadilly", "at": "07:55", "at_iso": "2026-10-10T07:55:00+01:00", "minutes": 36,
    }
    assert cancelled["arrives"] is None and no_points["arrives"] is None


def test_a_board_s_trains_arrive_at_their_last_stop_without_a_filter():
    part, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO")
    first = part["rows"][0]
    assert first["arrives"] == {"name": "Hadfield", "at": "22:14", "at_iso": "2026-10-09T22:14:00+01:00", "minutes": 6}


def test_bus_observations_for_the_history():
    found = live(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    seen = {(o.route, o.scheduled): o for o in bus_observations(BusBoard(found, schedule(), True, None))}
    late = seen["A|341|Hadfield", "10:10"]
    assert (late.status, late.delay_min, late.live, late.departs_at, late.day) == (
        "late", 9, True, datetime(2026, 10, 9, 10, 19), date(2026, 10, 9),
    )
    timetabled = seen["B|237|Ashton", "10:18"]
    assert (timetabled.status, timetabled.delay_min, timetabled.live) == ("unknown", None, False)


# Weather


def conditions():
    return parse_conditions(fixture("openmeteo_conditions.json"))


def test_weather_now_hours_and_sun():
    now = datetime(2026, 10, 9, 8, 20, tzinfo=LONDON)
    part = weather_part(conditions(), now, status_block(), 50)
    assert part["now"] == {"temperature": 9.7, "feels_like": 6.4, "code": 61, "text": "Light rain", "wind": 15,
                           "gust": 31, "direction": 236, "is_day": True}
    assert part["hours"][0]["start"] == "2026-10-09T08:00:00+01:00"  # the hour under way
    assert len(part["hours"]) == 24
    assert set(part["hours"][0]) == {"start", "temperature", "probability", "precipitation", "code", "text", "wind",
                                     "is_day"}
    assert part["sun"]["sunrise"].startswith("2026-10-09T07:")
    assert part["sun"]["sunset"].startswith("2026-10-09T18:")


def test_the_rain_sentence_uses_the_threshold():
    now = datetime(2026, 10, 9, 8, 20, tzinfo=LONDON)
    hours = conditions().hours
    first_wet = next(h for h in hours if h.start + timedelta(hours=1) > now.replace(tzinfo=None)
                     and h.probability is not None and h.probability >= 1)
    under_way = first_wet.start <= now.replace(tzinfo=None)
    assert weather_part(conditions(), now, status_block(), 1)["rain"] == (
        f"Rain now ({first_wet.probability}%)" if under_way
        else f"Rain from {first_wet.start:%H:%M} ({first_wet.probability}%)"
    )
    assert weather_part(conditions(), now, status_block(), 100)["rain"] is None


def test_no_weather_yet():
    part = weather_part(None, datetime(2026, 10, 9, 8, 20, tzinfo=LONDON), status_block(), 50)
    assert part == {"status": status_block(), "rain": None, "now": None, "hours": [], "sun": None}


# The hub


class Clock:
    def __init__(self):
        self.mono = 1000.0

    def __call__(self):
        return self.mono


def hub(**kwargs):
    stamps = iter(datetime(2026, 10, 9, 7, 0, tzinfo=LONDON) + timedelta(seconds=n) for n in range(10_000))
    return Hub("Glossop", WALK, clock=lambda: next(stamps), **kwargs)


def snapshot(h):
    return json.loads(h.current()[1])


def test_the_first_snapshot_has_every_section_empty():
    h = hub()
    body = snapshot(h)
    assert list(body) == ["version", "generated_at", "title", "walk", "quiet", "row_strips", "show", "trains",
                          "buses", "weather", "disruptions", "timetable_changes"]
    assert body["version"] == 1 and body["title"] == "Glossop"
    assert body["walk"] == {"train": 10, "bus": {"A": 8, "B": 6}}
    assert body["quiet"] is False and body["row_strips"] is True
    assert body["show"] == list(show.keys("dashboard"))  # everything, without a list
    assert body["trains"] is body["buses"] is body["weather"] is body["disruptions"] is None
    assert body["timetable_changes"] is None


def test_the_version_goes_up_only_when_the_content_changes():
    h = hub()
    assert h.publish("trains", {"rows": [1]}) == 2
    first = snapshot(h)
    assert h.publish("trains", {"rows": [1]}) == 2  # same content: same version
    again = snapshot(h)
    assert again["generated_at"] != first["generated_at"]  # but rebuilt, with the time
    assert h.publish("buses", {"rows": []}) == 3
    assert h.version == 3
    with pytest.raises(ValueError):
        h.publish("ferries", {})


def test_the_snapshot_says_whether_rows_draw_their_position_strips():
    assert snapshot(hub(row_strips=False))["row_strips"] is False
    h = hub(row_strips=False)
    h.refresh(quiet=True)
    h.publish("trains", {"rows": []})
    assert snapshot(h)["row_strips"] is False  # it stays as the settings said


def test_the_snapshot_says_what_the_page_shows_in_the_registry_s_order():
    h = hub(show={"weather", "next_to_catch", "announce"})
    assert snapshot(h)["show"] == ["next_to_catch", "weather", "announce"]
    h.publish("trains", {"rows": []})
    assert snapshot(h)["show"] == ["next_to_catch", "weather", "announce"]
    assert snapshot(hub(show=()))["show"] == []


def test_a_refresh_says_whether_a_quiet_window_is_on_and_only_a_change_is_new():
    h = hub()
    h.refresh(quiet=True)
    assert snapshot(h)["quiet"] is True and h.version == 2
    h.refresh(quiet=True)
    h.refresh()  # without quiet, it stays as it was
    assert snapshot(h)["quiet"] is True and h.version == 2
    h.refresh(quiet=False)
    assert snapshot(h)["quiet"] is False and h.version == 3


def test_the_snapshot_is_one_line_of_ascii_json():
    h = hub()
    h.publish("trains", {"messages": ["Café closed\nall day"]})
    body = h.current()[1]
    assert b"\n" not in body and body.isascii()
    assert json.loads(body)["trains"]["messages"] == ["Café closed\nall day"]


def test_wait_times_out_with_the_same_version():
    h = hub()
    started = time.monotonic()
    assert h.wait(1, 0.05) == 1
    assert time.monotonic() - started < 1


def test_wait_returns_at_once_for_an_older_version():
    h = hub()
    h.publish("trains", {"rows": [1]})
    assert h.wait(1, 5) == 2


def test_wait_wakes_on_a_publish():
    h = hub()
    timer = threading.Timer(0.05, lambda: h.publish("weather", {"rain": None}))
    timer.start()
    started = time.monotonic()
    assert h.wait(1, 5) == 2
    assert time.monotonic() - started < 2
    timer.join()


def test_close_wakes_waiters():
    h = hub()
    threading.Timer(0.05, h.close).start()
    started = time.monotonic()
    assert h.wait(1, 5) == 1
    assert time.monotonic() - started < 2 and h.closed


def test_lookups_last_for_the_current_and_previous_snapshot_only():
    h = hub()
    h.publish("buses", {"rows": ["a"]}, {"aaaa": {"bus": 1}})
    h.publish("trains", {"rows": ["t"]}, {"tttt": {"history": 2}})
    h.publish("buses", {"rows": ["b"]}, {"bbbb": {"bus": 3}})
    assert h.lookup("bbbb") == {"bus": 3}
    assert h.lookup("aaaa") == {"bus": 1}  # gone from the board, but in the previous snapshot
    assert h.lookup("tttt") == {"history": 2}
    h.publish("buses", {"rows": ["c"]}, {"cccc": {"bus": 4}})
    assert h.lookup("aaaa") is None
    assert h.lookup("tttt") == {"history": 2}  # still on the board
    assert h.lookup("nope") is None


def test_an_unchanged_publish_keeps_the_previous_lookups():
    h = hub()
    h.publish("buses", {"rows": ["a"]}, {"aaaa": {"bus": 1}})
    h.publish("buses", {"rows": ["b"]}, {"bbbb": {"bus": 2}})
    h.publish("buses", {"rows": ["b"]}, {"bbbb": {"bus": 3}})
    assert h.lookup("aaaa") == {"bus": 1}
    assert h.lookup("bbbb") == {"bus": 3}  # the latest data for a row still on the board


def test_export_has_the_snapshot_its_lookups_and_the_area_taken_together():
    h = hub()
    h.publish("buses", {"rows": ["a"]}, {"aaaa": {"bus": 1}})
    h.publish("buses", {"rows": ["b"]}, {"bbbb": {"bus": 2}})
    h.put("area", {"vehicles": []})
    found = h.export()
    assert set(found) == {"version", "snapshot", "lookups", "previous", "area"}
    assert found["version"] == 3 and found["snapshot"] == snapshot(h)
    assert (found["lookups"], found["previous"], found["area"]) == ({"bbbb": {"bus": 2}}, {"aaaa": {"bus": 1}},
                                                                    {"vehicles": []})
    h.put("area", None)
    assert h.export()["area"] is None


def test_the_hub_is_a_snapshot_source():
    from twobells.dashboard.snapshot import SnapshotSource

    h: SnapshotSource = hub()
    for name in ("current", "wait", "lookup", "get", "touch", "active", "age", "close"):
        assert callable(getattr(h, name)), name
    assert h.closed is False


def test_touch_and_age_follow_the_clock():
    mono = Clock()
    h = hub(monotonic=mono)
    assert not h.active(600)
    h.touch()
    mono.mono += 599
    assert h.active(600) and h.age() == 599
    mono.mono += 2
    assert not h.active(600)
    h.publish("weather", {})
    assert h.age() == 0


def test_rain_already_falling_is_said_as_now_not_an_hour_gone_by():
    from datetime import datetime

    from twobells.dashboard.snapshot import rain_sentence
    from twobells.sources.weather import Hour

    hours = [Hour(datetime(2026, 10, 9, 22), 92, 0.4), Hour(datetime(2026, 10, 9, 23), 80, 0.3)]
    now = datetime(2026, 10, 9, 22, 39)
    assert rain_sentence(hours, now, now + timedelta(hours=2), 50) == "Rain now (92%)"
    dry_now = [Hour(datetime(2026, 10, 9, 22), 10, 0.0), Hour(datetime(2026, 10, 9, 23), 80, 0.3)]
    assert rain_sentence(dry_now, now, now + timedelta(hours=2), 50) == "Rain from 23:00 (80%)"
    assert rain_sentence(dry_now[:1], now, now + timedelta(hours=2), 50) is None


def test_calling_point_words_become_times_or_flags():
    from twobells.dashboard.snapshot import calling_point
    from twobells.sources.darwin import CallingPoint

    on_time = calling_point(CallingPoint("Dinting", "DGT", "07:15", "On time", None, False, None))
    assert (on_time["expected"], on_time["cancelled"]) == ("07:15", False)
    late = calling_point(CallingPoint("Dinting", "DGT", "07:15", "07:22", None, False, None))
    assert late["expected"] == "07:22"
    words = calling_point(CallingPoint("Dinting", "DGT", "07:15", "Cancelled", None, False, None))
    assert (words["expected"], words["cancelled"]) == (None, True)
    for word in ("Delayed", "No report"):
        assert calling_point(CallingPoint("Dinting", "DGT", "07:15", word, None, False, None))["expected"] is None
    passed = calling_point(CallingPoint("Hadfield", "HDF", "07:05", None, "On time", False, None))
    assert passed["actual"] == "07:05"


def real_disruptions():
    """The disruptions section from the trimmed SIRI-SX capture and the
    synthetic Knowledgebase fixture, as the collector makes it."""
    from tests.sources.test_disruptions import DINTING_LANE, KB, RAIL, area, situations
    from twobells.sources.disruptions import (
        bus_disruptions,
        disruptions_part,
        parse_kb_incidents,
        train_disruptions,
    )

    captured = datetime(2026, 10, 9, 23, 14, tzinfo=LONDON)
    bus = bus_disruptions(situations(), area(**DINTING_LANE), captured)
    train = train_disruptions(parse_kb_incidents(KB.read_bytes()), RAIL, captured)
    return disruptions_part(bus, status_block(), train, None, LONDON)


def real_timetable_changes():
    """A timetable change as the collector works it out: the 341 five minutes
    later from Sunday 25 October."""
    from dataclasses import replace as changed

    from tests.sources.test_livebus import EAST, WEST
    from tests.sources.test_livebus import schedule as plan
    from twobells.dashboard.area import timetable_changes
    from twobells.timetable.model import Calendar, Call

    old = changed(EAST, calendar=Calendar(days=frozenset(range(7)), end=date(2026, 10, 24)))
    new = changed(EAST, calls=tuple(Call(c.atco, c.minute + 5) for c in EAST.calls),
                  calendar=Calendar(days=frozenset(range(7)), start=date(2026, 10, 25)))
    found = timetable_changes(plan(old, new, WEST), date(2026, 10, 9))
    assert found
    return found


def test_what_the_server_sends_is_what_the_page_was_built_for():
    """The server and the page were built apart against docs/dashboard-api.md:
    run the server's real output through the page's own contract checker."""
    from tests.dashboard.test_static import BUS_DETAIL, SNAPSHOT, check

    h = hub()
    details = {**known_details(), "9000002GLSP____": service_details("hadfield", ata=None)}
    tparts, _ = trains_part(glossop(), FETCHED, status_block(), "GLO", None, WALK, "GLO",
                            details=details, now=DETAILS_AT.replace(minute=49))
    h.publish("trains", tparts)
    bparts, lookups = extras_part(at(10, 14), lambda route, scheduled, day: "No tracker on 3 of the last 10 weekdays")
    h.publish("buses", bparts)
    h.publish("weather", weather_part(conditions(), datetime(2026, 10, 9, 8, 20, tzinfo=LONDON), status_block(), 50))
    h.publish("disruptions", real_disruptions())
    h.publish("timetable_changes", real_timetable_changes())
    sent = snapshot(h)
    assert sent["trains"]["rows"] and sent["buses"]["rows"] and sent["weather"]["now"]
    positions = [r["position"] for r in sent["trains"]["rows"] if r["position"]]
    assert len(positions) == 3 and any(isinstance(p["strip"]["train_at"], float) for p in positions)
    assert any(r["previous_points"] for r in sent["trains"]["rows"])
    assert any(r["arrives"] for r in sent["trains"]["rows"])
    sent_buses = sent["buses"]["rows"]
    assert any(r["inbound"] for r in sent_buses) and any(r["ghost"] for r in sent_buses)
    assert all(r["arrives"] and r["history"]["no_show_phrase"] for r in sent_buses)
    assert {i["mode"] for i in sent["disruptions"]["items"]} == {"bus", "train"}
    assert sent["buses"]["last_buses"]
    check(sent, SNAPSHOT)
    live_row = by(bparts["rows"], line="341", stop="A")
    check(lookups[live_row["id"]]["bus"], BUS_DETAIL)


def test_the_snapshot_is_stamped_in_the_board_s_zone_not_the_host_s():
    # A container's clock zone is UTC; the board's times are in its own zone.
    tokyo = Hub("Glossop", WALK, tz=ZoneInfo("Asia/Tokyo"))
    assert json.loads(tokyo.current()[1])["generated_at"].endswith("+09:00")


def test_inbound_only_journeys_are_not_counted_nor_observed():
    from dataclasses import replace

    from tests.sources.test_livebus import every_day
    from twobells.sources.livebus import live_board

    only = replace(EAST, inbound_only=True)
    plan = schedule(only, WEST)
    assert timetable_block("ready", plan, LONDON)["journeys"] == 1
    rows, placed = live_board(plan, at(10, 14), [bus(*HALFWAY, at(10, 14))], every_day, 10)
    assert placed  # its bus is placed, for the inbound line
    seen = bus_observations(BusBoard(rows, plan, True, None, placed))
    assert {o.route for o in seen} == {"A|237|Ashton", "B|237|Ashton"}



def test_a_stop_has_the_letter_on_its_sign():
    from dataclasses import replace as swap

    from twobells.dashboard.snapshot import stop_details

    lettered = swap(schedule(), positions={**POSITIONS, HENRY_A: swap(POSITIONS[HENRY_A], indicator="Stop A")})
    found = stop_details({"A": HENRY_A, "Z": "nowhere"}, lettered, WALK)
    assert (found["A"]["name"], found["A"]["letter"]) == ("Henry Street", "A")
    assert found["Z"] == {"name": None, "letter": None, "towards": None, "walk_min": None}
    opposite = swap(schedule(), positions={**POSITIONS, HENRY_A: swap(POSITIONS[HENRY_A], indicator="opp")})
    assert stop_details({"A": HENRY_A}, opposite, WALK)["A"]["letter"] is None  # a word, not a letter


def test_two_stops_that_share_a_name_are_told_apart_by_the_stop_after():
    from dataclasses import replace as swap

    from tests.sources.test_livebus import ASHTON_INT, GEORGE, ST_MARYS_E, journey
    from twobells.dashboard.snapshot import stop_details

    # St Marys Road both ways, no letters: east goes on to Arundel Street,
    # west to Ashton Interchange (positioned here), twice to once over George Street.
    west = journey("237", [(ST_MARYS_W, 600), (ASHTON_INT, 640)], "Ashton", "2")
    other = journey("61", [(ST_MARYS_W, 610), (GEORGE, 612)], "Glossop", "3")
    plan = swap(schedule(EAST, WEST, west, other),
                positions={**POSITIONS, ASHTON_INT: swap(POSITIONS[GEORGE], name="Ashton Interchange")})
    found = stop_details({"E": ST_MARYS_E, "W": ST_MARYS_W}, plan, WalkTimes(bus={"E": 4}))
    assert found == {
        "E": {"name": "St Marys Road", "letter": None, "towards": "Arundel Street", "walk_min": 4},
        "W": {"name": "St Marys Road", "letter": None, "towards": "Ashton Interchange", "walk_min": None},
    }


def test_the_stop_after_never_has_the_stop_s_own_name_and_inbound_journeys_do_not_count():
    from dataclasses import replace as swap

    from tests.sources.test_livebus import GEORGE, HOWARD, MARKET_HALL, journey
    from twobells.dashboard.snapshot import stop_details

    # Two poles of one stop in a row, and a journey kept only for the inbound bus.
    twin = journey("61", [(HENRY_A, 600), (MARKET_HALL, 601), (GEORGE, 602)], "Glossop", "4")
    inbound = swap(journey("62", [(HENRY_A, 600), (HOWARD, 601)], "Hadfield", "5"), inbound_only=True)
    plan = swap(schedule(twin, inbound),
                positions={**POSITIONS, MARKET_HALL: swap(POSITIONS[MARKET_HALL], name="Henry Street")})
    assert stop_details({"A": HENRY_A}, plan, WALK)["A"]["towards"] is None


def test_bus_rows_and_maps_name_the_stop_with_its_sign_letter():
    from dataclasses import replace as swap

    lettered = {**POSITIONS, HENRY_A: swap(POSITIONS[HENRY_A], indicator="Stop A")}
    found = live(at(10, 14), [bus(*HALFWAY, at(10, 14))])
    row, lookup = bus_row(next(l for l in found if l.departure.stop == "A"), lettered, at(10, 14), WALK)
    assert row["stop_name"] == "Henry Street (Stop A)"
    assert lookup["bus"]["stop_name"] == "Henry Street (Stop A)"
    plain, _ = bus_row(next(l for l in found if l.departure.stop == "A"), POSITIONS, at(10, 14), WALK)
    assert plain["stop_name"] == "Henry Street"
