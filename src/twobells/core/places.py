"""Places from the lists bundled with the package, read without the network.

Rail stations, Metrolink tram stops, London's stops for TfL's live times and
the grid of NaPTAN areas, each from its file in data/
(tools/build_setup_data.py); whether a position is in the UK; the rate
limit shared by everything that asks FOSSGIS's foot router; and BODS's
address, which both the bus sources and the timetable ask. Setup's lookups
(setup/lookups.py), the settings, the timetable's roads and the dashboard
all use them.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from .bounded import finite

# The United Kingdom with a margin, Scilly to Shetland and Northern Ireland to Kent.
LAT_RANGE = (49.0, 61.0)
LON_RANGE = (-8.7, 2.0)

_CRS = re.compile(r"^[A-Z]{3}$")
# A Metrolink stop's NaPTAN code: 9400ZZMA and the three letters TfGM calls it by.
TRAM_STOP = re.compile(r"^9400ZZMA[A-Z]{3}$")
# A London stop TfL's live times are for, by TfL's code for it: an
# Underground (LU), DLR (DL), London Trams (CR) or cable car (AL) station,
# or a river bus pier (930G and its three or four letters).
LONDON_STOP = re.compile(r"^(?:940GZZ(?:LU|DL|CR|AL)[A-Z]{3}|930G[A-Z]{3,4})$")
LONDON_KINDS = ("tube", "dlr", "tram", "cable-car", "river-bus")
_LINE_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_AREA = re.compile(r"^[0-8][0-9]{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def in_uk(lat: float, lon: float) -> bool:
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def _number(value, strings: bool = False) -> float | None:
    """A finite number; with `strings`, a short numeric string counts too
    (Nominatim and CSV files give numbers as text). Else None."""
    if isinstance(value, bool):
        return None
    if strings and isinstance(value, str) and len(value) <= 32:
        try:
            value = float(value)
        except ValueError:
            return None
    return finite(value)


def _position(lat, lon, strings: bool = False) -> tuple[float, float] | None:
    lat, lon = _number(lat, strings), _number(lon, strings)
    return (lat, lon) if lat is not None and lon is not None and in_uk(lat, lon) else None


def _text(value, limit: int) -> str:
    """A string with control characters and runs of spaces removed, cut to `limit`; "" if not a string."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value[: limit * 4]).split())[:limit].strip()


# FOSSGIS's foot router (OSRM), for setup's walk times and the timetable's
# roads, under its policy (https://routing.openstreetmap.de/about.html): at
# most one request a second, from a valid user agent. One limit covers both,
# as the fetch service can look up walk times while a search asks for roads.


class RateLimiter:
    """At most one call to `wait` returns per `interval` seconds, across threads."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: float | None = None

    def wait(self) -> None:
        with self._lock:  # held while sleeping, so callers queue up in turn
            now = self._clock()
            if self._next is not None and now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


ROUTER_USER_AGENT = "twobells/0.1 (departure board; https://github.com/acornelissen/twobells)"
ROUTER_LIMIT = RateLimiter(1.0)

# The Bus Open Data Service's API, for its live positions (sources/bods.py)
# and the timetables setup and the route command search.
BODS_BASE_URL = "https://data.bus-data.dft.gov.uk/api/v1"


# Rail stations, from the bundled list.


def _data_rows(text: str) -> csv.DictReader:
    return csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))


def parse_stations(text: str) -> tuple[tuple[str, str, float, float], ...]:
    """(name, crs, lat, lon) rows of a station list, bad rows skipped."""
    rows = []
    for row in _data_rows(text):
        name, crs = _text(row.get("name"), 80), row.get("crs") or ""
        position = _position(row.get("lat"), row.get("lon"), strings=True)
        if name and _CRS.fullmatch(crs) and position:
            rows.append((name, crs, *position))
    return tuple(rows)


@lru_cache(maxsize=1)
def bundled_stations() -> tuple[tuple[str, str, float, float], ...]:
    return parse_stations(files("twobells").joinpath("data/gb_stations.csv").read_text("utf-8"))


# Metrolink tram stops, from the bundled list.


def parse_tram_stops(text: str) -> tuple[tuple[str, str, float, float], ...]:
    """(name, code, lat, lon) rows of a tram stop list, bad rows skipped."""
    rows = []
    for row in _data_rows(text):
        name, code = _text(row.get("name"), 80), row.get("code") or ""
        position = _position(row.get("lat"), row.get("lon"), strings=True)
        if name and TRAM_STOP.fullmatch(code) and position:
            rows.append((name, code, *position))
    return tuple(rows)


@lru_cache(maxsize=1)
def bundled_tram_stops() -> tuple[tuple[str, str, float, float], ...]:
    return parse_tram_stops(files("twobells").joinpath("data/metrolink_stops.csv").read_text("utf-8"))


def tram_stop_names() -> dict[str, str]:
    """{code: name} of every Metrolink stop and every London stop, from the
    bundled lists: a London stop's name as the views say it (stop_title)."""
    names = {code: name for name, code, _, _ in bundled_tram_stops()}
    names.update((stop.code, stop_title(stop.name, stop.kind)) for stop in bundled_london_stops())
    return names


# London's stops for TfL's live times, from the bundled list: each with its
# kind and the TfL lines calling there.


@dataclass(frozen=True)
class LondonStop:
    name: str  # NaPTAN's, without what it is: "Bank", "Westminster Pier"
    code: str  # TfL's, like 940GZZLUBNK
    kind: str  # one of LONDON_KINDS
    lines: tuple[str, ...]  # TfL's line ids, like ("central", "northern")
    lat: float
    lon: float


def stop_title(name: str, kind: str) -> str:
    """A stop's name as the views say it: a DLR station's says so ("Bank
    DLR"), as most share a name with the Underground station beside them."""
    return f"{name} DLR" if kind == "dlr" else name


def parse_london_stops(text: str) -> tuple[LondonStop, ...]:
    """The rows of a London stop list, bad rows skipped."""
    rows = []
    for row in _data_rows(text):
        name, code, kind = _text(row.get("name"), 80), row.get("code") or "", row.get("kind") or ""
        lines = tuple(str(row.get("lines") or "").split())
        position = _position(row.get("lat"), row.get("lon"), strings=True)
        if (name and LONDON_STOP.fullmatch(code) and kind in LONDON_KINDS and position and len(lines) <= 20
                and all(_LINE_ID.fullmatch(line) for line in lines)):
            rows.append(LondonStop(name, code, kind, lines, *position))
    return tuple(rows)


@lru_cache(maxsize=1)
def bundled_london_stops() -> tuple[LondonStop, ...]:
    return parse_london_stops(files("twobells").joinpath("data/london_stops.csv").read_text("utf-8"))


def london_stops_by_code() -> dict[str, LondonStop]:
    return {stop.code: stop for stop in bundled_london_stops()}


# Which NaPTAN areas have bus stops in each tenth of a degree, from the
# bundled grid: NaPTAN's API serves whole areas and has no "near here" search.


def parse_area_grid(text: str) -> dict[tuple[int, int], tuple[str, ...]]:
    grid = {}
    for row in _data_rows(text):
        try:
            cell = int(row.get("lat10") or ""), int(row.get("lon10") or "")
        except ValueError:
            continue
        areas = tuple(a for a in (row.get("areas") or "").split() if _AREA.fullmatch(a))
        if areas:
            grid[cell] = areas
    return grid


@lru_cache(maxsize=1)
def bundled_area_grid() -> dict[tuple[int, int], tuple[str, ...]]:
    return parse_area_grid(files("twobells").joinpath("data/naptan_area_grid.csv").read_text("utf-8"))
