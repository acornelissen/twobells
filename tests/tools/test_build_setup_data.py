"""The checks tools/build_setup_data.py makes on NaPTAN before it writes the
setup wizard's data files, fed small made-up extracts (no downloads)."""

import importlib.util

import pytest

from tests.paths import ROOT
from twobells.core.osgb import distance_m

_SCRIPT = ROOT / "tools" / "build_setup_data.py"


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("build_setup_data", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Aberdare station, from a NaPTAN row with both a grid reference and a GPS position.
ABERDARE_GRID = (300400, 202800)
ABERDARE_GPS = (51.71505790608, -3.44308344608)


def stop_point(atco, name, crs, location, status="active", stop_type="RLY"):
    return f"""
    <StopPoint Status="{status}">
      <AtcoCode>{atco}</AtcoCode>
      <Descriptor><CommonName>{name}</CommonName></Descriptor>
      <Place><Location>{location}</Location></Place>
      <StopClassification>
        <StopType>{stop_type}</StopType>
        <OffStreet><Rail><AnnotatedRailRef><CrsRef>{crs}</CrsRef></AnnotatedRailRef></Rail></OffStreet>
      </StopClassification>
    </StopPoint>"""


def gps(lat, lon):
    return f"<Translation><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></Translation>"


def grid(easting, northing):
    return f"<Easting>{easting}</Easting><Northing>{northing}</Northing>"


def naptan(*points):
    body = "".join(points)
    return f'<NaPTAN xmlns="http://www.naptan.org.uk/"><StopPoints>{body}</StopPoints></NaPTAN>'.encode()


def test_placeholder_zz_codes_are_dropped(build):
    rows = build.station_rows(naptan(
        stop_point("9100GLOSSOP", "Glossop Rail Station", "GLO", gps(53.4446, -1.9490)),
        stop_point("9100HERITGE", "Heritage Halt Rail Station", "ZZH", gps(53.0, -2.0)),
    ))

    assert [crs for _, crs, _, _ in rows] == ["GLO"]


def test_only_active_rail_stations_with_a_real_crs_are_kept(build):
    rows = build.station_rows(naptan(
        stop_point("9100CLOSED", "Closed Rail Station", "CLD", gps(53.0, -2.0), status="inactive"),
        stop_point("9100NOTRAIL", "Bus Bay", "BBY", gps(53.0, -2.0), stop_type="BCT"),
        stop_point("9100LOWER", "Lower Case Rail Station", "abc", gps(53.0, -2.0)),
        stop_point("9100ILFENBP", "Newbury Park", "NRC", gps(51.5755, 0.0900)),  # in WRONG_CRS
        stop_point("9100GLOSSOP", "Glossop Rail Station", "GLO", gps(53.4446, -1.9490)),
    ))

    assert rows == [("Glossop", "GLO", 53.4446, -1.949)]


def test_duplicate_crs_more_than_two_km_apart_is_refused(build):
    xml = naptan(
        stop_point("9100NEWBRYR", "Newbury Racecourse Rail Station", "NRC", gps(51.3982, -1.3080)),
        stop_point("9100FARAWAY", "Somewhere Else Rail Station", "NRC", gps(51.4300, -1.3080)),  # about 3.5 km north
    )

    with pytest.raises(SystemExit, match=r"NRC is on entries 4 km apart: .*9100FARAWAY.*Check and add to WRONG_CRS"):
        build.station_rows(xml)


def test_duplicate_crs_close_together_becomes_one_row_with_the_plainest_name(build):
    rows = build.station_rows(naptan(
        stop_point("9100SMTHWKH", "Smethwick Galton Bridge High Level Rail Station", "SGB", gps(52.5020, -1.9800)),
        stop_point("9100SMTHWKL", "Smethwick Galton Bridge Rail Station", "SGB", gps(52.5017, -1.9806)),
    ))

    assert rows == [("Smethwick Galton Bridge", "SGB", 52.5017, -1.9806)]


def test_station_with_only_a_grid_reference_is_converted_to_gps(build):
    rows = build.station_rows(naptan(
        stop_point("9100ABDARE", "Aberdare Rail Station", "ABA", grid(*ABERDARE_GRID)),
    ))

    [(name, crs, lat, lon)] = rows
    assert (name, crs) == ("Aberdare", "ABA")
    assert distance_m(lat, lon, *ABERDARE_GPS) < 10


def test_area_grid_converts_grid_references_and_keeps_local_bus_areas(build):
    lat10, lon10 = int(ABERDARE_GPS[0] * 10), -35  # floor of -34.43
    text = "\n".join([
        "ATCOCode,Status,StopType,Easting,Northing,Latitude,Longitude",
        f"554012345,active,BCT,{ABERDARE_GRID[0]},{ABERDARE_GRID[1]},,",  # grid reference only
        "149000003,active,BCS,,,52.05,-0.75",  # GPS position, another cell
        "554000001,inactive,BCT,,,51.71,-3.44",  # not active
        "910GABDARE,active,BCT,,,51.71,-3.44",  # national area (rail replacement bay)
        "554000002,active,RLY,,,51.71,-3.44",  # not a bus stop
    ])

    assert build.area_grid(text) == {(lat10, lon10): {"554"}, (520, -8): {"149"}}
