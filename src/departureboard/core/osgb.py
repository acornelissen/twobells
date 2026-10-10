"""Ordnance Survey grid references (eastings and northings) to GPS latitude
and longitude, and straight-line distances between two points.

NaPTAN gives some stops only as a grid reference, so setup converts them.
The method is the one in the Ordnance Survey's "A guide to coordinate
systems in Great Britain": undo the Transverse Mercator projection on the
Airy 1830 ellipsoid, then a Helmert shift from OSGB36 to WGS84. It is good
to about 5 metres, plenty for finding stops near a house.
"""

from __future__ import annotations

import math

# Airy 1830 ellipsoid and the National Grid's projection.
_AIRY_A = 6_377_563.396
_AIRY_B = 6_356_256.909
_F0 = 0.9996012717
_LAT0 = math.radians(49)
_LON0 = math.radians(-2)
_N0 = -100_000.0
_E0 = 400_000.0

# WGS84 (GRS80) ellipsoid.
_WGS_A = 6_378_137.0
_WGS_B = 6_356_752.314245

# Helmert shift from OSGB36 to WGS84: metres, parts per million, arc seconds.
# (The guide lists the opposite shift, WGS84 to OSGB36; these are its negation.)
_TX, _TY, _TZ = 446.448, -125.157, 542.060
_SCALE_PPM = -20.4894
_RX, _RY, _RZ = 0.1502, 0.2470, 0.8421

EARTH_RADIUS_M = 6_371_008.8


def _meridional_arc(lat: float) -> float:
    a, b = _AIRY_A, _AIRY_B
    n = (a - b) / (a + b)
    d, s = lat - _LAT0, lat + _LAT0
    return b * _F0 * (
        (1 + n + 5 / 4 * n**2 + 5 / 4 * n**3) * d
        - (3 * n + 3 * n**2 + 21 / 8 * n**3) * math.sin(d) * math.cos(s)
        + (15 / 8 * n**2 + 15 / 8 * n**3) * math.sin(2 * d) * math.cos(2 * s)
        - 35 / 24 * n**3 * math.sin(3 * d) * math.cos(3 * s)
    )


def _grid_to_osgb36(easting: float, northing: float) -> tuple[float, float]:
    """Latitude and longitude in radians on the OSGB36 datum."""
    a, b = _AIRY_A, _AIRY_B
    e2 = 1 - b**2 / a**2
    lat = _LAT0
    arc = 0.0
    for _ in range(20):  # converges in three or four
        lat = (northing - _N0 - arc) / (a * _F0) + lat
        arc = _meridional_arc(lat)
        if abs(northing - _N0 - arc) < 0.00001:
            break
    sin_lat, cos_lat, tan_lat = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * _F0 / math.sqrt(1 - e2 * sin_lat**2)
    rho = a * _F0 * (1 - e2) / (1 - e2 * sin_lat**2) ** 1.5
    eta2 = nu / rho - 1
    t2, t4, t6 = tan_lat**2, tan_lat**4, tan_lat**6
    sec_lat = 1 / cos_lat
    vii = tan_lat / (2 * rho * nu)
    viii = tan_lat / (24 * rho * nu**3) * (5 + 3 * t2 + eta2 - 9 * t2 * eta2)
    ix = tan_lat / (720 * rho * nu**5) * (61 + 90 * t2 + 45 * t4)
    x = sec_lat / nu
    xi = sec_lat / (6 * nu**3) * (nu / rho + 2 * t2)
    xii = sec_lat / (120 * nu**5) * (5 + 28 * t2 + 24 * t4)
    xiia = sec_lat / (5040 * nu**7) * (61 + 662 * t2 + 1320 * t4 + 720 * t6)
    de = easting - _E0
    out_lat = lat - vii * de**2 + viii * de**4 - ix * de**6
    out_lon = _LON0 + x * de - xi * de**3 + xii * de**5 - xiia * de**7
    return out_lat, out_lon


def _to_cartesian(lat: float, lon: float, a: float, b: float) -> tuple[float, float, float]:
    e2 = 1 - b**2 / a**2
    nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return (
        nu * math.cos(lat) * math.cos(lon),
        nu * math.cos(lat) * math.sin(lon),
        (1 - e2) * nu * math.sin(lat),
    )


def _from_cartesian(x: float, y: float, z: float, a: float, b: float) -> tuple[float, float]:
    e2 = 1 - b**2 / a**2
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        nu = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * nu * math.sin(lat), p)
    return lat, math.atan2(y, x)


def grid_to_latlon(easting: float, northing: float) -> tuple[float, float]:
    """WGS84 (GPS) latitude and longitude in degrees for a British National
    Grid easting and northing in metres."""
    lat, lon = _grid_to_osgb36(easting, northing)
    x, y, z = _to_cartesian(lat, lon, _AIRY_A, _AIRY_B)
    scale = 1 + _SCALE_PPM / 1e6
    rx, ry, rz = (math.radians(r / 3600) for r in (_RX, _RY, _RZ))
    x2 = _TX + scale * x - rz * y + ry * z
    y2 = _TY + rz * x + scale * y - rx * z
    z2 = _TZ - ry * x + rx * y + scale * z
    lat, lon = _from_cartesian(x2, y2, z2, _WGS_A, _WGS_B)
    return math.degrees(lat), math.degrees(lon)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line (great circle) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))
