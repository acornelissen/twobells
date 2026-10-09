from pathlib import Path

import pytest

from departureboard.bods import Vehicle, parse_siri_vm
from departureboard.model import Departure, Status
from departureboard.route import RouteStop, RouteStrip, locate, position_on, target

# Eastbound along High Street West, Glossop, to Henry Street Stop A (NaPTAN positions).
STRIP = RouteStrip(
    line="341",
    stop="A",
    stops=(
        RouteStop("The Junction", 53.444467, -1.965966),
        RouteStop("Grapes Inn", 53.444223, -1.962774),
        RouteStop("Sunlaws Street", 53.443494, -1.959026),
        RouteStop("St Marys Road", 53.443511, -1.955759),
        RouteStop("Arundel Street", 53.443519, -1.95326),
        RouteStop("Henry Street", 53.44384, -1.948999),
    ),
)
FIXTURE = parse_siri_vm((Path(__file__).parent / "fixtures" / "bods_glossop_synthetic.xml").read_bytes())
BY_REF = {v.vehicle_ref: v for v in FIXTURE}


def bus_at(lat, lon, bearing=90.0, line="341"):
    return Vehicle(line, "Hyde", "inbound", lat, lon, "2026-10-08T07:12:30+01:00", bearing)


def test_bus_between_two_stops_is_placed_between_them():
    position = position_on(STRIP, BY_REF["BNML-1001"])
    assert 2 < position < 3  # past Sunlaws Street, before St Marys Road


def test_bus_at_a_stop_is_placed_on_it():
    stop = STRIP.stops[3]
    assert position_on(STRIP, bus_at(stop.lat, stop.lon)) == pytest.approx(3, abs=0.01)


def test_bus_far_from_the_strip_is_not_on_it():
    assert position_on(STRIP, BY_REF["BNML-1002"]) is None


def test_bus_heading_the_other_way_is_not_counted():
    assert position_on(STRIP, BY_REF["BNML-1003"]) is None


def test_bus_without_a_bearing_is_taken_at_its_position():
    stop = STRIP.stops[1]
    assert position_on(STRIP, bus_at(stop.lat, stop.lon, bearing=None)) == pytest.approx(1, abs=0.01)


def test_bus_just_past_your_stop_has_gone():
    home = STRIP.stops[-1]
    # About 100 m further east.
    assert position_on(STRIP, bus_at(home.lat, home.lon + 0.0015)) is None


def test_bus_not_yet_at_the_first_stop_is_not_on_the_strip():
    first = STRIP.stops[0]
    # About 130 m further west, on the same road.
    assert position_on(STRIP, bus_at(first.lat + 0.0001, first.lon - 0.002)) is None


def test_locate_finds_the_first_bus_due():
    assert 2 < locate(STRIP, FIXTURE, ahead=0) < 3


def test_locate_says_none_when_the_wanted_bus_is_not_on_the_strip_yet():
    assert locate(STRIP, FIXTURE, ahead=1) is None


def test_locate_counts_back_from_your_stop():
    near = bus_at(*_lat_lon(4))
    far = bus_at(*_lat_lon(1))
    assert locate(STRIP, [far, near], ahead=0) == pytest.approx(4, abs=0.01)
    assert locate(STRIP, [far, near], ahead=1) == pytest.approx(1, abs=0.01)


def test_locate_ignores_other_lines():
    assert locate(STRIP, [bus_at(*_lat_lon(2), line="237")], ahead=0) is None


def _lat_lon(index):
    stop = STRIP.stops[index]
    return stop.lat, stop.lon


def bus(line, scheduled, stop="A"):
    return Departure("bus", line, "Hyde", scheduled, None, Status.ON_TIME, stop=stop)


def test_target_counts_earlier_buses_of_the_same_line_at_the_same_stop():
    buses = [bus("341", "07:20"), bus("237", "07:22"), bus("341", "07:25", stop="B"), bus("341", "07:35")]
    assert target(buses, buses[0]) == 0
    assert target(buses, buses[3]) == 1


def test_box_surrounds_the_strip_in_bods_order():
    west, south, east, north = STRIP.box(margin_deg=0.01)
    assert west < -1.965966 < -1.948999 < east
    assert south < 53.443494 < 53.444467 < north
