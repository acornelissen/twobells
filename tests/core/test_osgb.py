import pytest

from twobells.core.osgb import distance_m, grid_to_latlon


@pytest.mark.parametrize(
    "easting, northing, lat, lon",
    [
        # NaPTAN rows that carry both a grid reference and a GPS position.
        (300400, 202800, 51.71505790608, -3.44308344608),  # Aberdare station
        (403485, 394118, 53.44384, -1.948999),  # Henry Street, Glossop
        (533135, 181640, 51.518, -0.08259),  # Liverpool Street, London
        (453058, 1186950, 60.562577, -1.034124),  # North Rd End, Yell, Shetland
    ],
)
def test_grid_reference_becomes_gps_position_within_ten_metres(easting, northing, lat, lon):
    got_lat, got_lon = grid_to_latlon(easting, northing)
    assert distance_m(got_lat, got_lon, lat, lon) < 10


def test_distance_matches_a_known_span():
    # One degree of latitude is about 111.2 km.
    assert distance_m(53.0, -2.0, 54.0, -2.0) == pytest.approx(111_195, rel=0.001)
    assert distance_m(53.44, -1.95, 53.44, -1.95) == 0
