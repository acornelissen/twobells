import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.core.model import Departure, Status, strip_key
from twobells.sources import holidays
from twobells.sources.bodsbus import BodsBusSource, unusable
from twobells.timetable import build as timetable
from twobells.timetable.model import Calendar, Call, Journey, Schedule, StopPosition

LONDON = ZoneInfo("Europe/London")
# Real NaPTAN stops on High Street West, Glossop, heading east.
ARUNDEL, HENRY_A, HOWARD = "1000DGHS3404", "1000DGHS0900", "1000DGHS1150"
POSITIONS = {
    ARUNDEL: StopPosition("Arundel Street", 53.443519, -1.95326),
    HENRY_A: StopPosition("Henry Street", 53.44384, -1.948999),
    HOWARD: StopPosition("Howard Arms", 53.443301, -1.948157),
}
STOPS = {"A": HENRY_A}
# Leaves Arundel Street at 10:00, Henry Street at 10:02.
EAST = Journey(
    "341", "BNGN", "Hadfield", "4173",
    (Call(ARUNDEL, 600), Call(HENRY_A, 602), Call(HOWARD, 603)),
    Calendar(days=frozenset(range(7))),
)
BUILT = datetime(2026, 10, 5, 3, 0)


def schedule(**changes) -> Schedule:
    return replace(Schedule(BUILT, None, dict(STOPS), (EAST,), POSITIONS), **changes)


def at(hh, mm, ss=0, day=date(2026, 10, 9)):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=LONDON)


def siri(recorded: datetime, lat: float, lon: float, origin_aimed: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Siri xmlns="http://www.siri.org.uk/siri" version="2.0"><ServiceDelivery><VehicleMonitoringDelivery>
<VehicleActivity><RecordedAtTime>{recorded.isoformat()}</RecordedAtTime>
<MonitoredVehicleJourney><LineRef>341</LineRef><DirectionRef>outbound</DirectionRef>
<PublishedLineName>341</PublishedLineName><OperatorRef>BNML</OperatorRef>
<DestinationName>Hadfield</DestinationName><OriginRef>{ARUNDEL}</OriginRef>
<OriginAimedDepartureTime>{origin_aimed}</OriginAimedDepartureTime>
<VehicleLocation><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></VehicleLocation>
<Bearing>80.0</Bearing><VehicleRef>BNML-1</VehicleRef></MonitoredVehicleJourney></VehicleActivity>
</VehicleMonitoringDelivery></ServiceDelivery></Siri>""".encode()


# Halfway from Arundel Street to Henry Street at 10:04, timetabled there at
# 10:01: three minutes late.
HALFWAY = ((53.443519 + 53.44384) / 2, (-1.95326 + -1.948999) / 2)
LATE_BUS = siri(at(10, 4), *HALFWAY, "2026-10-09T09:00:00+00:00")


class Fallback:
    cost = 2

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return [Departure("bus", "341", "Hyde", "10:30", None, Status.UNKNOWN, stop="A")]


def source(folder, now, body=LATE_BUS, fallback=None, stops=STOPS, **kwargs):
    asked = []

    def http_get(url, max_bytes):
        asked.append(url)
        if isinstance(body, Exception):
            raise body
        return body

    made = BodsBusSource(
        folder, stops, "secret", LONDON, operators=["BNML"], rows=2,
        fallback=fallback, http_get=http_get, clock=lambda: now, **kwargs,
    )
    made.asked = asked
    return made


def test_departures_come_from_the_timetable_and_live_positions(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 4, 30))
    assert bus.cost == 0 and not bus.falling_back
    assert bus.fetch() == [Departure("bus", "341", "Hadfield", "10:02", "10:05", Status.LATE, stop="A")]
    assert "boundingBox=" in bus.asked[0]


def test_live_positions_are_asked_for_around_the_timetable_s_stops(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0))
    bus.fetch()
    box = bus.asked[0].split("boundingBox=")[1].split("&")[0].split("%2C")
    west, south, east, north = map(float, box)
    assert west < -1.95326 and east > -1.948157 and south < 53.443301 and north > 53.44384
    assert east - west < 0.05 and north - south < 0.05


def turning_in_from(lat: float, lon: float) -> Schedule:
    """The schedule with a 341 that turns round at Henry Street, coming in from
    (lat, lon) 21 calls away: its first stop is placed, as the timetable places
    every stop of a journey whose bus turns round at your stop, but is beyond
    the 20 calls before yours, so marked as placed only for that."""
    way = {f"WAY{n}": StopPosition(f"Way {n}", 53.4436, -1.95326 - n * 0.001) for n in range(1, 21)}
    first = {"FAR": StopPosition("Far away", lat, lon)}
    calls = (Call("FAR", 500), *(Call(atco, 500 + n) for n, atco in enumerate(reversed(way), 1)), Call(HENRY_A, 530))
    inbound = Journey("341", "BNGN", "Glossop", "4173", calls, Calendar(days=frozenset(range(7))))
    return schedule(journeys=(EAST, inbound), positions={**POSITIONS, **way, **first}, turning_only=frozenset(first))


def asked_box(bus) -> tuple[float, ...]:
    return tuple(map(float, bus.asked[0].split("boundingBox=")[1].split("&")[0].split("%2C")))


def test_the_box_takes_in_the_whole_way_of_a_bus_turning_round_at_your_stop(tmp_path):
    # From about 25 km south-west: the box grows to take it in, to 18 by 24 km
    # (Glossop's own is 16 by 28).
    timetable.save(tmp_path, turning_in_from(53.25, -2.2))
    bus = source(tmp_path, at(10, 0))
    bus.fetch()
    west, south, east, north = asked_box(bus)
    assert south < 53.25 and west < -2.2


def test_a_box_too_big_is_drawn_round_the_20_stops_before_yours_only(tmp_path):
    # From about 30 km south-west: a box taking it in, 25 by 29 km, would
    # answer with more than BODS's answers may be over a city, so every check
    # would lose its live positions.
    timetable.save(tmp_path, turning_in_from(53.2, -2.3))
    bus = source(tmp_path, at(10, 0))
    bus.fetch()
    west, south, east, north = asked_box(bus)
    assert west > -2.0 and south > 53.4
    assert west < -1.95326 - 20 * 0.001 and east > -1.948157  # the 20 calls before, and your stop


def test_without_live_positions_the_timetabled_times_still_show(tmp_path, caplog):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), body=OSError("network down"))
    assert bus.fetch() == [Departure("bus", "341", "Hadfield", "10:02", None, Status.UNKNOWN, stop="A")]
    assert "live positions failed" in caplog.text


def test_live_position_errors_never_log_the_key(tmp_path, caplog):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), body=OSError("GET https://x/?api_key=secret failed"))
    bus.fetch()
    assert "secret" not in caplog.text


def test_without_a_timetable_the_fallback_stands_in(tmp_path):
    fallback = Fallback()
    bus = source(tmp_path, at(10, 0), fallback=fallback)
    assert bus.falling_back and bus.cost == 2
    assert bus.fetch()[0].destination == "Hyde"
    assert fallback.calls == 1 and bus.asked == []


def test_without_a_timetable_or_fallback_the_fetch_fails_saying_what_to_run(tmp_path):
    bus = source(tmp_path, at(10, 0))
    assert not bus.falling_back and bus.cost == 0
    with pytest.raises(RuntimeError, match="twobells timetable"):
        bus.fetch()


def test_a_timetable_for_other_stops_is_not_used(tmp_path):
    timetable.save(tmp_path, schedule())
    fallback = Fallback()
    bus = source(tmp_path, at(10, 0), fallback=fallback, stops={"A": HENRY_A, "B": "100000008350"})
    assert bus.unusable() == "the bus timetable is for other stops"
    bus.fetch()
    assert fallback.calls == 1


def test_an_out_of_date_timetable_is_not_used():
    assert "over four weeks old" in unusable(schedule(), STOPS, BUILT + timedelta(days=28, minutes=1))


def test_a_timetable_four_weeks_old_is_still_used():
    assert unusable(schedule(), STOPS, BUILT + timedelta(days=28)) is None


def test_one_service_ending_leaves_the_timetable_in_use():
    # valid_until is the first end of any one service in it: when it is built
    # again, not when the rest stop running.
    assert unusable(schedule(valid_until=date(2026, 10, 8)), STOPS, datetime(2026, 10, 9, 10, 0)) is None


def test_a_rebuilt_timetable_is_read_again(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), body=OSError("offline"))
    assert bus.fetch()[0].destination == "Hadfield"
    timetable.save(tmp_path, schedule(journeys=(replace(EAST, destination="Hadfield Station"),)))
    path = tmp_path / timetable.FILE_NAME
    later = path.stat().st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(later, later))  # a coarse clock could otherwise give it the same time
    assert bus.fetch()[0].destination == "Hadfield Station"


GOV_UK = (FIXTURES / "govuk_bank_holidays.json").read_bytes()


def test_bank_holidays_follow_the_timetable_s_own_rules(tmp_path):
    good_friday = date(2026, 4, 3)
    holidays.bank_holidays(tmp_path, datetime(2026, 4, 1), fetch=lambda url, most: GOV_UK)
    not_on_good_friday = replace(EAST, calendar=Calendar(days=frozenset(range(7)), bank_holidays_off=frozenset({"GoodFriday"})))
    timetable.save(tmp_path, schedule(built=datetime(2026, 4, 1), journeys=(not_on_good_friday,)))
    assert source(tmp_path, at(10, 0, day=good_friday), body=OSError("offline")).fetch() == []
    assert source(tmp_path, at(10, 0, day=date(2026, 4, 2)), body=OSError("offline")).fetch() != []


def test_tomorrow_s_buses_follow_tomorrow_s_bank_holiday(tmp_path):
    # Leaves Henry Street at 00:02, except on Good Friday (3 April 2026).
    small_hours = Journey(
        "341", "BNGN", "Hadfield", "4174",
        (Call(ARUNDEL, 0), Call(HENRY_A, 2), Call(HOWARD, 3)),
        Calendar(days=frozenset(range(7)), bank_holidays_off=frozenset({"GoodFriday"})),
    )
    holidays.bank_holidays(tmp_path, datetime(2026, 4, 1), fetch=lambda url, most: GOV_UK)
    timetable.save(tmp_path, schedule(built=datetime(2026, 4, 1), journeys=(small_hours,)))
    late_thursday = at(23, 55, day=date(2026, 4, 2))
    assert source(tmp_path, late_thursday, body=OSError("offline")).fetch() == []
    late_friday = at(23, 55, day=date(2026, 4, 3))
    assert [d.scheduled for d in source(tmp_path, late_friday, body=OSError("offline")).fetch()] == ["00:02"]


def test_refresh_needs_operators(tmp_path):
    bus = BodsBusSource(tmp_path, STOPS, "secret", LONDON, clock=lambda: at(3, 0))
    with pytest.raises(ValueError, match="operators"):
        bus.refresh_timetable()


def test_refresh_builds_with_the_board_s_stops_and_operators(tmp_path, monkeypatch):
    seen = {}

    def fake_refresh(folder, stops, operators, now, api_key, *, force=False, **kwargs):
        seen.update(folder=folder, stops=stops, operators=operators, now=now, force=force)
        timetable.save(folder, schedule())
        return schedule()

    monkeypatch.setattr(timetable, "refresh", fake_refresh)
    bus = source(tmp_path, at(3, 0))
    assert bus.unusable() == "there is no bus timetable yet"
    bus.refresh_timetable(force=True)
    assert seen == {"folder": tmp_path, "stops": STOPS, "operators": ["BNML"], "now": datetime(2026, 10, 9, 3, 0), "force": True}
    assert bus.unusable() is None


def test_refresh_lets_go_of_the_timetable_it_has_read_while_it_builds(tmp_path, monkeypatch):
    # The dashboard's upkeep reads it (unusable) just before: its journeys
    # mustn't be held beside the build.
    import gc
    import weakref

    timetable.save(tmp_path, schedule(built=datetime(2026, 9, 1, 3, 0)))
    bus = source(tmp_path, at(3, 0))
    assert bus.unusable() is not None  # too old: due
    read = weakref.ref(bus._schedule())

    def fake_refresh(*args, **kwargs):
        gc.collect()
        assert read() is None
        return schedule()

    monkeypatch.setattr(timetable, "refresh", fake_refresh)
    assert bus.refresh_timetable() == schedule()


def test_letting_go_of_the_timetable_keeps_nothing_of_it_until_it_is_asked_for_again(tmp_path):
    # As the Pi's fetch service's dashboard does while a setup search builds
    # a timetable beside it (dashboard/collector.py).
    import gc
    import weakref

    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 4, 30))
    bus.fetch()
    read = weakref.ref(bus._schedule())
    assert bus.board is not None
    bus.let_go()
    gc.collect()
    assert read() is None and bus.board is None
    assert bus.unusable() is None  # read again


def test_refresh_skips_while_another_build_holds_the_store(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setattr(timetable, "refresh", lambda *a, **k: pytest.fail("built beside another build"))
    fd = os.open(tmp_path / timetable.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(timetable.TimetableBusy):
            source(tmp_path, at(3, 0)).refresh_timetable(force=True)
    finally:
        os.close(fd)


def test_the_note_says_how_a_fetch_fell_short(tmp_path):
    bus = source(tmp_path, at(10, 0), fallback=Fallback())
    bus.fetch()
    assert bus.note == "From TransportAPI: there is no bus timetable yet"
    timetable.save(tmp_path, schedule())
    bus.fetch()
    assert bus.note is None
    failing = source(tmp_path, at(10, 0), body=OSError("network down"))
    failing.fetch()
    assert failing.note == "Timetabled times only; live positions failed: network down"


def test_while_waiting_for_the_fallback_the_idle_note_says_why(tmp_path):
    bus = source(tmp_path, at(21, 40), fallback=Fallback())
    assert bus.idle_note() == "There is no bus timetable yet; TransportAPI stands in only at its own checking times"
    timetable.save(tmp_path, schedule())
    assert bus.idle_note() is None


def test_without_a_fallback_there_is_no_idle_note(tmp_path):
    # The fetch fails instead, and the error says what to run.
    assert source(tmp_path, at(21, 40)).idle_note() is None


# The dashboard's view: rows with the journeys and buses behind them


def test_fetch_live_gives_the_rows_with_their_journeys_and_buses(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 4, 30))
    board = bus.fetch_live()
    assert [l.departure for l in board.departures] == [
        Departure("bus", "341", "Hadfield", "10:02", "10:05", Status.LATE, stop="A")
    ]
    row = board.departures[0]
    assert row.journey == EAST and row.vehicle.vehicle_ref == "BNML-1"
    assert row.progress.at == pytest.approx(0.5, abs=0.02)
    assert board.schedule == schedule()
    assert board.vehicles_ok and board.note is None
    assert [(j.journey, j.vehicle) for j in board.journeys] == [(EAST, row.vehicle)]  # every placed journey
    assert bus.fetch() == [l.departure for l in board.departures]


def test_fetch_live_takes_its_own_number_of_rows(tmp_path):
    later = replace(EAST, journey_code="4174", calls=(Call(ARUNDEL, 610), Call(HENRY_A, 612), Call(HOWARD, 613)))
    timetable.save(tmp_path, schedule(journeys=(EAST, later)))
    bus = source(tmp_path, at(10, 0), body=OSError("offline"))
    assert len(bus.fetch_live().departures) == 2
    assert len(bus.fetch_live(rows=1).departures) == 1


def test_fetch_live_says_when_live_positions_failed(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), body=OSError("network down"))
    board = bus.fetch_live()
    assert not board.vehicles_ok
    assert board.note == "Timetabled times only; live positions failed: network down"
    assert [l.departure.scheduled for l in board.departures] == ["10:02"]
    assert board.departures[0].vehicle is None and board.journeys == []
    assert bus.note is None  # the source's note is fetch's alone


def test_fetch_live_never_falls_back(tmp_path):
    fallback = Fallback()
    bus = source(tmp_path, at(10, 0), fallback=fallback)
    with pytest.raises(RuntimeError) as raised:
        bus.fetch_live()
    assert str(raised.value) == "there is no bus timetable yet"
    assert fallback.calls == 0 and bus.asked == []


def test_fetch_live_raises_for_a_timetable_it_can_t_use(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), stops={"A": HENRY_A, "B": "100000008350"})
    with pytest.raises(RuntimeError, match="for other stops"):
        bus.fetch_live()


# A screen's row strips


def test_live_strips_say_where_each_row_s_bus_is(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 4, 30))
    strips = bus.live_strips()
    [departure] = bus.fetch()
    assert list(strips) == [strip_key(departure)]
    found = strips[strip_key(departure)]
    assert (found.where, found.stops, found.yours) == ("1 stop away", 3, 1)
    assert found.at == pytest.approx(0.5, abs=0.02)


def test_a_bus_followed_in_makes_its_departure_live_and_its_strip_says_so(tmp_path):
    # EAST's bus goes on to work OUT from Henry Street: one block, as TransXChange's BlockNumber says.
    out = Journey("341", "BNGN", "Hyde", "4174", (Call(HENRY_A, 610), Call(ARUNDEL, 612)), EAST.calendar, block="B1")
    timetable.save(tmp_path, schedule(journeys=(replace(EAST, block="B1"), out)))
    bus = source(tmp_path, at(10, 4, 30))
    late, followed = bus.fetch()  # EAST's bus is 3 min late: in at Howard Arms, by Henry Street, at 10:06
    assert late == Departure("bus", "341", "Hadfield", "10:02", "10:05", Status.LATE, stop="A")
    assert followed == Departure("bus", "341", "Hyde", "10:10", None, Status.ON_TIME, stop="A")
    strip = bus.live_strips(reuse=True)[strip_key(followed)]
    assert (strip.where, strip.at, strip.stops, strip.yours) == ("On its way, 1 min", None, 2, 0)


def test_live_strips_can_reuse_the_last_fetch_s_live_board(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 4, 30))
    assert bus.live_strips(reuse=True) == {}  # nothing fetched yet
    bus.fetch()
    assert len(bus.asked) == 1
    assert len(bus.live_strips(reuse=True)) == 1
    assert len(bus.asked) == 1  # no second request


def test_live_strips_leave_out_buses_without_a_live_position(tmp_path):
    timetable.save(tmp_path, schedule())
    bus = source(tmp_path, at(10, 0), body=OSError("network down"))
    assert bus.live_strips() == {}
    bus.fetch()
    assert bus.live_strips(reuse=True) == {}


def test_live_strips_raise_without_a_usable_timetable(tmp_path):
    fallback = Fallback()
    bus = source(tmp_path, at(10, 0), fallback=fallback)
    with pytest.raises(RuntimeError):
        bus.live_strips()
    bus.fetch()  # from TransportAPI, which knows nothing of where buses are
    assert bus.live_strips(reuse=True) == {}


HENRY_B = "1000DGHS0901"


def test_stop_names_come_from_the_timetable_by_label(tmp_path):
    stops = {"A": HENRY_A, "B": HENRY_B}
    positions = {**POSITIONS, HENRY_B: StopPosition("Henry Street", 53.4437, -1.9489)}
    timetable.save(tmp_path, schedule(stops=stops, positions=positions))
    assert source(tmp_path, at(10, 0), stops=stops).stop_names() == {"A": "Henry Street", "B": "Henry Street"}


def test_a_stop_missing_from_the_timetable_s_positions_has_no_name(tmp_path):
    stops = {"A": HENRY_A, "B": HENRY_B}
    timetable.save(tmp_path, schedule(stops=stops))
    assert source(tmp_path, at(10, 0), stops=stops).stop_names() == {"A": "Henry Street"}


def test_no_stop_names_without_a_usable_timetable(tmp_path):
    # Nor while TransportAPI stands in: its rows show the labels.
    assert source(tmp_path, at(10, 0), fallback=Fallback()).stop_names() == {}
    timetable.save(tmp_path, schedule(stops={"A": HOWARD}))  # a timetable for other stops
    assert source(tmp_path, at(10, 0)).stop_names() == {}


def test_the_last_buses_come_from_the_timetable_with_its_bank_holidays(tmp_path):
    good_friday = date(2026, 4, 3)
    holidays.bank_holidays(tmp_path, datetime(2026, 4, 1), fetch=lambda url, most: GOV_UK)
    # Leaves Henry Street at 23:02, except on Good Friday.
    late = replace(EAST, journey_code="4175", calls=tuple(replace(c, minute=c.minute + 13 * 60) for c in EAST.calls),
                   calendar=Calendar(days=frozenset(range(7)), bank_holidays_off=frozenset({"GoodFriday"})))
    timetable.save(tmp_path, schedule(built=datetime(2026, 4, 1), journeys=(EAST, late)))
    thursday = source(tmp_path, at(12, 0, day=date(2026, 4, 2)))
    assert [(b.line, b.stop, b.leaves.strftime("%H:%M")) for b in thursday.last_buses()] == [("341", "A", "23:02")]
    friday = source(tmp_path, at(12, 0, day=good_friday))
    assert [b.leaves.strftime("%H:%M") for b in friday.last_buses()] == ["10:02"]
    assert thursday.asked == [] and friday.asked == []  # the timetable alone: no calls


def test_the_last_buses_are_grouped_once_a_day(tmp_path, monkeypatch):
    from twobells.sources import lastbus

    timetable.save(tmp_path, schedule())
    worked = []
    day_groups = lastbus.day_groups
    monkeypatch.setattr(lastbus, "day_groups", lambda *args: worked.append(args[1]) or day_groups(*args))
    bus = source(tmp_path, at(9, 0))
    for minute in range(3):
        assert [b.remaining for b in bus.last_buses(at(9, minute))] == [1]
    assert [b.remaining for b in bus.last_buses(at(10, 4))] == [0]
    assert worked == [date(2026, 10, 8), date(2026, 10, 9)]
    bus.last_buses(at(0, 5, day=date(2026, 10, 10)))  # a new day
    assert worked[2:] == [date(2026, 10, 9), date(2026, 10, 10)]


def test_no_last_buses_without_a_usable_timetable(tmp_path):
    # Nor while TransportAPI stands in.
    assert source(tmp_path, at(12, 0), fallback=Fallback()).last_buses() == []
    timetable.save(tmp_path, schedule(stops={"A": HOWARD}))
    assert source(tmp_path, at(12, 0)).last_buses() == []
