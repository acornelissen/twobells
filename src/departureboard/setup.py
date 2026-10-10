"""First-run setup: find home, the stations and stops near it, and the walk to each.

Pure functions plus thin clients. Each client takes an `http_get(url,
max_bytes) -> bytes`, so tests and callers can swap the network, and checks
everything that comes back before using it.

Where a home's position goes, and only at setup: a postcode to postcodes.io,
or typed address text to OpenStreetMap's Nominatim; then its coordinates,
once, to the OpenStreetMap foot router for walk times. Stations come from a
file bundled with the board and bus stops from NaPTAN by area code, so
neither learns where home is. Nothing here logs a postcode, an address or a
coordinate, and error messages never repeat a URL.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import string
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Protocol
from urllib.parse import quote, urlencode

from .errors import safe_message
from .holidays import ENGLAND_AND_WALES
from .http import get_bytes
from .osgb import distance_m, grid_to_latlon
from .routesetup import NAPTAN_URL

log = logging.getLogger(__name__)

POSTCODES_URL = "https://api.postcodes.io/postcodes"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://routing.openstreetmap.de/routed-foot/table/v1/foot"
# Nominatim's policy: say which application is asking, and how to find it.
NOMINATIM_USER_AGENT = (
    "inky-departures/0.1 (e-paper departure board setup; https://github.com/acornelissen/inky-departures)"
)

# Real responses are a few kB; NaPTAN for a few areas is a few MB.
MAX_POSTCODE_BYTES = 100_000
MAX_NOMINATIM_BYTES = 200_000
MAX_OSRM_BYTES = 200_000
MAX_NAPTAN_BYTES = 100_000_000

# The United Kingdom with a margin, Scilly to Shetland and Northern Ireland to Kent.
LAT_RANGE = (49.0, 61.0)
LON_RANGE = (-8.7, 2.0)

ADDRESS_LENGTH = (3, 120)
MAX_ADDRESS_RESULTS = 5
MAX_STATIONS = 50
MAX_STATION_KM = 100
MAX_STOPS = 50
STOP_RADIUS_M = (50, 3_000)
MAX_WALK_DESTINATIONS = 25  # one table request; the public router caps its size
MAX_BOARD_STOPS = 10

# The fallback when the router can't say: straight line, plus 30% for streets
# not running straight, at 4.8 km/h.
DETOUR = 1.3
WALK_M_PER_MINUTE = 4_800 / 60
LONGEST_WALK_S = 6 * 3600  # a "walk" longer than this is the router lost

BUS_STOP_TYPES = frozenset({"BCT", "BCS", "BCQ"})  # on-street stop, bus station bay, variable bay
BEARINGS = frozenset({"N", "NE", "E", "SE", "S", "SW", "W", "NW"})

_POSTCODE = re.compile(r"^[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2}$")
_CRS = re.compile(r"^[A-Z]{3}$")
_ATCO = re.compile(r"^[0-9]{3}[0-9A-Za-z]{1,9}$")
_AREA = re.compile(r"^[0-8][0-9]{2}$")
_LABEL = re.compile(r"^[A-Za-z0-9]{1,3}$")  # as the settings page allows
# "Stop A", "Stand 12", "Bay C3" or a bare "B": a letter the street sign shows.
_INDICATOR_CODE = re.compile(r"^(?:(?i:stop|stand|stance|bay)\s+)?([A-Z]{1,2}[0-9]?|[0-9]{1,2})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DECIMAL = re.compile(r"-?\d+\.\d+")

_DIVISIONS = {
    "England": ENGLAND_AND_WALES,
    "Wales": ENGLAND_AND_WALES,
    "Scotland": "scotland",
    "Northern Ireland": "northern-ireland",
}

HttpGetBytes = Callable[[str, int], bytes]


class SetupError(Exception):
    """Something setup can't do, in words fit to show the person setting up."""


class Located(Protocol):
    @property
    def lat(self) -> float: ...

    @property
    def lon(self) -> float: ...


@dataclass(frozen=True)
class Place:
    """Somewhere a postcode or address search found."""

    lat: float
    lon: float
    label: str  # "SK13 1AH", or Nominatim's full address
    country: str = ""  # "England", "Wales", "Scotland" or "Northern Ireland" when known
    admin_district: str = ""  # the council area, like "High Peak"


@dataclass(frozen=True)
class Station:
    name: str
    crs: str
    lat: float
    lon: float
    distance_m: int  # straight line from home


@dataclass(frozen=True)
class Stop:
    atco: str
    name: str
    indicator: str  # NaPTAN's, like "Stop A" or "opp"
    bearing: str  # the way buses face, like "NE"; "" if unknown
    lat: float
    lon: float
    distance_m: int  # straight line from home


# Checking what came back.


def in_uk(lat: float, lon: float) -> bool:
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def _check_point(lat, lon) -> None:
    for value in (lat, lon):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise SetupError("A position must be two numbers")
    if not in_uk(lat, lon):
        raise SetupError("That position is outside the UK")


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
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _position(lat, lon, strings: bool = False) -> tuple[float, float] | None:
    lat, lon = _number(lat, strings), _number(lon, strings)
    return (lat, lon) if lat is not None and lon is not None and in_uk(lat, lon) else None


def _text(value, limit: int) -> str:
    """A string with control characters and runs of spaces removed, cut to `limit`; "" if not a string."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value[: limit * 4]).split())[:limit].strip()


def _json(body: bytes, what: str):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise SetupError(f"{what} sent something that isn't JSON") from None


def _safe(error: BaseException, url: str, *private: str) -> str:
    """An error's text without the URL, any `private` text (a postcode, an
    address) or any decimal number (a coordinate)."""
    text = str(error) or type(error).__name__
    for secret in (url, *private):
        if secret:
            text = text.replace(secret, "…")
    return safe_message(_DECIMAL.sub("…", text))


def _fetch(http_get: HttpGetBytes, url: str, max_bytes: int, what: str, *private: str) -> bytes:
    try:
        return http_get(url, max_bytes)
    except Exception as error:  # noqa: BLE001 - one plain message, never the URL
        raise SetupError(f"{what} failed: {_safe(error, url, *private)}") from None


# Postcodes.


def normalise_postcode(postcode: str) -> str:
    """'sk131ah' as 'SK13 1AH'. SetupError if it isn't the shape of a UK postcode."""
    if not isinstance(postcode, str) or len(postcode) > 16:
        raise SetupError("That isn't a UK postcode")
    packed = "".join(postcode.split()).upper()
    if not _POSTCODE.match(packed):
        raise SetupError("That isn't a UK postcode")
    return f"{packed[:-3]} {packed[-3:]}"


def parse_postcode(body: bytes, asked: str) -> Place:
    """A Place from postcodes.io's answer for one postcode."""
    data = _json(body, "postcodes.io")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise SetupError("postcodes.io sent an answer without a result")
    position = _position(result.get("latitude"), result.get("longitude"))
    if position is None:
        raise SetupError("postcodes.io has no UK position for that postcode")
    label = _text(result.get("postcode"), 10)
    try:
        label = normalise_postcode(label)
    except SetupError:
        label = asked
    return Place(
        position[0],
        position[1],
        label,
        _text(result.get("country"), 40),
        _text(result.get("admin_district"), 80),
    )


def lookup_postcode(postcode: str, http_get: HttpGetBytes = get_bytes) -> Place:
    """Where a postcode is, from postcodes.io (no key needed)."""
    normal = normalise_postcode(postcode)
    url = f"{POSTCODES_URL}/{quote(normal, safe='')}"
    try:
        body = http_get(url, MAX_POSTCODE_BYTES)
    except Exception as error:  # noqa: BLE001 - one plain message, never the URL
        if getattr(error, "code", None) == 404:
            raise SetupError("postcodes.io doesn't know that postcode") from None
        raise SetupError(f"Postcode lookup failed: {_safe(error, url, normal, normal.replace(' ', ''))}") from None
    return parse_postcode(body, normal)


def bank_holiday_division(country: str) -> str | None:
    """The gov.uk bank holiday list for a country: 'england-and-wales',
    'scotland' or 'northern-ireland'. None for anywhere else (the Channel
    Islands, the Isle of Man) or an unknown country."""
    return _DIVISIONS.get(country.strip()) if isinstance(country, str) else None


# Address search, through Nominatim. Its policy allows one request a second
# from the whole application and asks that answers be cached.


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


class AnswerCache:
    """The last few answers, each kept for `max_age` seconds, in memory only."""

    def __init__(self, size: int = 32, max_age: float = 3600, clock: Callable[[], float] = time.monotonic):
        self._size = size
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, tuple]] = OrderedDict()

    def get(self, key: str) -> tuple | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if self._clock() - item[0] > self._max_age:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, key: str, value: tuple) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)


NOMINATIM_LIMIT = RateLimiter(1.0)
NOMINATIM_CACHE = AnswerCache()


def nominatim_get(url: str, max_bytes: int) -> bytes:
    return get_bytes(url, max_bytes, headers={"User-Agent": NOMINATIM_USER_AGENT, "Accept-Language": "en-GB"})


def _nominatim_place(item) -> Place | None:
    if not isinstance(item, dict):
        return None
    position = _position(item.get("lat"), item.get("lon"), strings=True)
    label = _text(item.get("display_name"), 200)
    if position is None or not label:
        return None
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    state = _text(address.get("state"), 40)  # England, Scotland... in Great Britain
    district = ""
    for key in ("city", "town", "county"):
        district = _text(address.get(key), 80)
        if district:
            break
    return Place(position[0], position[1], label, state if state in _DIVISIONS else "", district)


def parse_nominatim(body: bytes) -> list[Place]:
    """Usable places from Nominatim's jsonv2 search answer, bad entries skipped."""
    data = _json(body, "Nominatim")
    if not isinstance(data, list):
        raise SetupError("Nominatim sent an answer that isn't a list of places")
    return [p for p in map(_nominatim_place, data[:MAX_ADDRESS_RESULTS]) if p is not None]


def search_address(text: str, http_get: HttpGetBytes = nominatim_get, limiter: RateLimiter = NOMINATIM_LIMIT,
                   cache: AnswerCache = NOMINATIM_CACHE) -> list[Place]:
    """Up to five places in Great Britain matching an address, best first."""
    query = _text(text, ADDRESS_LENGTH[1] + 1)
    if not ADDRESS_LENGTH[0] <= len(query) <= ADDRESS_LENGTH[1]:
        raise SetupError(f"Type an address of {ADDRESS_LENGTH[0]} to {ADDRESS_LENGTH[1]} characters")
    key = query.casefold()
    cached = cache.get(key)
    if cached is not None:
        return list(cached)
    limiter.wait()
    params = {"format": "jsonv2", "countrycodes": "gb", "limit": MAX_ADDRESS_RESULTS, "addressdetails": 1, "q": query}
    url = f"{NOMINATIM_URL}?{urlencode(params)}"
    places = parse_nominatim(_fetch(http_get, url, MAX_NOMINATIM_BYTES, "Address search", query))
    cache.put(key, tuple(places))
    return places


# Rail stations, from the bundled list (tools/build_setup_data.py).


def _data_rows(text: str) -> csv.DictReader:
    return csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))


def parse_stations(text: str) -> tuple[tuple[str, str, float, float], ...]:
    """(name, crs, lat, lon) rows of a station list, bad rows skipped."""
    rows = []
    for row in _data_rows(text):
        name, crs = _text(row.get("name"), 80), row.get("crs") or ""
        position = _position(row.get("lat"), row.get("lon"), strings=True)
        if name and _CRS.match(crs) and position:
            rows.append((name, crs, *position))
    return tuple(rows)


@lru_cache(maxsize=1)
def bundled_stations() -> tuple[tuple[str, str, float, float], ...]:
    return parse_stations(files("departureboard").joinpath("data/gb_stations.csv").read_text("utf-8"))


def nearby_stations(lat: float, lon: float, limit: int = 5, within_km: float = 10,
                    stations: Sequence[tuple[str, str, float, float]] | None = None) -> list[Station]:
    """The nearest National Rail stations, closest first, by straight line.
    Works offline: the list is bundled."""
    _check_point(lat, lon)
    if not 1 <= limit <= MAX_STATIONS or not 0 < within_km <= MAX_STATION_KM:
        raise SetupError(f"Ask for 1 to {MAX_STATIONS} stations within {MAX_STATION_KM} km")
    found = []
    for name, crs, slat, slon in bundled_stations() if stations is None else stations:
        metres = distance_m(lat, lon, slat, slon)
        if metres <= within_km * 1000:
            found.append((metres, crs, Station(name, crs, slat, slon, round(metres))))
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found[:limit]]


# Bus stops, from NaPTAN. Its API serves whole areas (the first three digits
# of an ATCO code) and has no "near here" search, so a bundled grid says which
# areas have stops near home, and only those are downloaded. That also keeps
# home's position from NaPTAN, and finds stops over a council boundary.


def parse_area_grid(text: str) -> dict[tuple[int, int], tuple[str, ...]]:
    grid = {}
    for row in _data_rows(text):
        try:
            cell = int(row.get("lat10") or ""), int(row.get("lon10") or "")
        except ValueError:
            continue
        areas = tuple(a for a in (row.get("areas") or "").split() if _AREA.match(a))
        if areas:
            grid[cell] = areas
    return grid


@lru_cache(maxsize=1)
def bundled_area_grid() -> dict[tuple[int, int], tuple[str, ...]]:
    return parse_area_grid(files("departureboard").joinpath("data/naptan_area_grid.csv").read_text("utf-8"))


def areas_near(lat: float, lon: float, within_m: float,
               grid: Mapping[tuple[int, int], Sequence[str]] | None = None) -> list[str]:
    """NaPTAN areas with bus stops in any grid cell within `within_m` of the point."""
    grid = bundled_area_grid() if grid is None else grid
    dlat = within_m / 111_000
    dlon = within_m / (111_000 * max(math.cos(math.radians(lat)), 0.1))
    found = set()
    for lat10 in range(math.floor((lat - dlat) * 10), math.floor((lat + dlat) * 10) + 1):
        for lon10 in range(math.floor((lon - dlon) * 10), math.floor((lon + dlon) * 10) + 1):
            found.update(grid.get((lat10, lon10), ()))
    return sorted(found)


def naptan_position(row: Mapping[str, str | None]) -> tuple[float, float] | None:
    """A NaPTAN CSV row's GPS position: its latitude and longitude, or, as
    some areas give only that, its grid reference converted. None if neither
    is usable or it's outside the UK."""
    position = _position(row.get("Latitude"), row.get("Longitude"), strings=True)
    if position is not None:
        return position
    easting, northing = _number(row.get("Easting"), strings=True), _number(row.get("Northing"), strings=True)
    if easting is None or northing is None or not (0 <= easting <= 800_000 and 0 <= northing <= 1_300_000):
        return None
    return _position(*grid_to_latlon(easting, northing))


def stops_near(text: str, lat: float, lon: float, within_m: float, limit: int) -> list[Stop]:
    """Active bus stops in a NaPTAN CSV within `within_m` of a point, closest first."""
    found = []
    try:
        for row in csv.DictReader(io.StringIO(text)):
            atco = row.get("ATCOCode") or ""
            if row.get("Status") != "active" or row.get("StopType") not in BUS_STOP_TYPES or not _ATCO.match(atco):
                continue
            position = naptan_position(row)
            if position is None:
                continue
            metres = distance_m(lat, lon, *position)
            if metres > within_m:
                continue
            bearing = (row.get("Bearing") or "").strip().upper()
            stop = Stop(
                atco,
                _text(row.get("CommonName"), 80) or atco,
                _text(row.get("Indicator"), 40),
                bearing if bearing in BEARINGS else "",
                *position,
                round(metres),
            )
            found.append((metres, atco, stop))
    except csv.Error:
        raise SetupError("NaPTAN sent a file that isn't readable CSV") from None
    found.sort(key=lambda f: (f[0], f[1]))
    return [f[2] for f in found[:limit]]


def nearby_stops(lat: float, lon: float, within_m: float = 800, limit: int = 15,
                 http_get: HttpGetBytes = get_bytes,
                 grid: Mapping[tuple[int, int], Sequence[str]] | None = None) -> list[Stop]:
    """Bus stops within `within_m` metres in a straight line, closest first,
    from NaPTAN (no key needed). Only area codes are sent, never the position."""
    _check_point(lat, lon)
    if not STOP_RADIUS_M[0] <= within_m <= STOP_RADIUS_M[1] or not 1 <= limit <= MAX_STOPS:
        raise SetupError(f"Ask for 1 to {MAX_STOPS} stops within {STOP_RADIUS_M[0]} to {STOP_RADIUS_M[1]} m")
    areas = areas_near(lat, lon, within_m, grid)
    if not areas:
        return []
    query = urlencode({"dataFormat": "csv", "atcoAreaCodes": ",".join(areas)})
    body = _fetch(http_get, f"{NAPTAN_URL}?{query}", MAX_NAPTAN_BYTES, "NaPTAN download")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SetupError("NaPTAN sent a file that isn't UTF-8") from None
    return stops_near(text, lat, lon, within_m, limit)


# Walk times, from the OpenStreetMap foot router (OSRM). It is FOSSGIS's
# server, as for roadshape's bus map roads, under the same policy
# (https://routing.openstreetmap.de/about.html): at most one request a second,
# from a valid user agent. One limit covers both, as the setup service can
# look up walk times while a search asks for roads.

ROUTER_USER_AGENT = "inky-departures/0.1 (departure board; https://github.com/acornelissen/inky-departures)"
ROUTER_LIMIT = RateLimiter(1.0)


def foot_get(url: str, max_bytes: int) -> bytes:
    return get_bytes(url, max_bytes, headers={"User-Agent": ROUTER_USER_AGENT})


def estimate_minutes(home: Located, place: Located) -> int:
    """A walk guessed from the straight line: 30% longer, at 4.8 km/h, rounded up."""
    return math.ceil(distance_m(home.lat, home.lon, place.lat, place.lon) * DETOUR / WALK_M_PER_MINUTE)


def parse_osrm_table(body: bytes, count: int) -> list[float | None]:
    """Seconds from the source to each of `count` destinations, from an OSRM
    table answer with sources=0. None where the router found no sensible
    route. ValueError if the answer isn't that shape."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ValueError("Router answer isn't JSON") from None
    if not isinstance(data, dict) or data.get("code") != "Ok":
        raise ValueError("Router didn't answer Ok")
    rows = data.get("durations")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list) or len(rows[0]) != count + 1:
        raise ValueError("Router's table is the wrong size")
    seconds = []
    for value in rows[0][1:]:
        if value is None:
            seconds.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Router's table holds something that isn't a duration")
        seconds.append(float(value) if value <= LONGEST_WALK_S else None)
    return seconds


def walk_minutes(home: Located, destinations: Mapping[str, Located],
                 http_get: HttpGetBytes = foot_get,
                 limiter: RateLimiter = ROUTER_LIMIT) -> dict[str, tuple[int, str]]:
    """Walking minutes from home to each destination, by the key it was given
    under (a station's CRS or a stop's ATCO code, say): (minutes, "route") from
    one router request, rounded up, or (minutes, "estimate") from the straight
    line wherever the router failed or found no route."""
    if not destinations:
        return {}
    if len(destinations) > MAX_WALK_DESTINATIONS:
        raise SetupError(f"Ask for walks to at most {MAX_WALK_DESTINATIONS} places at once")
    _check_point(home.lat, home.lon)
    for place in destinations.values():
        _check_point(place.lat, place.lon)
    keys = list(destinations)
    estimates = {key: (estimate_minutes(home, destinations[key]), "estimate") for key in keys}
    points = [home, *(destinations[key] for key in keys)]
    url = f"{OSRM_URL}/{';'.join(f'{p.lon:.6f},{p.lat:.6f}' for p in points)}?sources=0"
    limiter.wait()
    try:
        seconds = parse_osrm_table(http_get(url, MAX_OSRM_BYTES), len(keys))
    except Exception as error:  # noqa: BLE001 - any failure falls back to estimates
        log.warning("Walk router failed (%s); using straight-line estimates", _safe(error, url))
        return estimates
    return {
        key: (math.ceil(s / 60), "route") if s is not None else estimates[key]
        for key, s in zip(keys, seconds, strict=True)
    }


# The settings for config.toml.


def _indicator_code(indicator: str) -> str | None:
    match = _INDICATOR_CODE.match(indicator.strip())
    return match.group(1) if match else None


def stop_labels(stops: Sequence[tuple[str | None, Stop]]) -> list[str]:
    """A board label for each stop: the one given, else the letter on its
    sign ("Stop A" is A) when every unlabelled stop has a different one, else
    A, B, C in order, skipping labels already taken."""
    if len(stops) > MAX_BOARD_STOPS:
        raise SetupError(f"Pick at most {MAX_BOARD_STOPS} stops")
    given = [label for label, _ in stops if label]
    for label in given:
        if not isinstance(label, str) or not _LABEL.match(label):
            raise SetupError("A stop label is 1 to 3 letters or digits")
    if len(set(given)) != len(given):
        raise SetupError("Two stops have the same label")
    codes = [_indicator_code(stop.indicator) for label, stop in stops if not label]
    if all(codes) and len(set(codes)) == len(codes) and not set(codes) & set(given):
        defaults = iter(codes)
    else:
        defaults = iter(c for c in string.ascii_uppercase if c not in given)
    return [label or next(defaults) for label, _ in stops]


def config_fragment(home: Located, station: Station | None, stops: Sequence[tuple[str | None, Stop]],
                    walk: Mapping[str, tuple[int, str]]) -> dict:
    """The settings setup found, as config.toml tables: [train] crs and
    walk_minutes, [bus.stops] and [bus.walk_minutes] by label, and [weather]
    lat and lon. `walk` is walk_minutes' answer, keyed by CRS and ATCO code.
    Home's position appears only in [weather], rounded to 2 places (about a
    kilometre), which is all a forecast needs."""
    _check_point(home.lat, home.lon)
    fragment: dict = {}
    if station is not None:
        if not _CRS.match(station.crs):
            raise SetupError("A station code is three capital letters")
        train: dict = {"crs": station.crs}
        if station.crs in walk:
            train["walk_minutes"] = int(walk[station.crs][0])
        fragment["train"] = train
    if stops:
        atcos = [stop.atco for _, stop in stops]
        if any(not _ATCO.match(a) for a in atcos) or len(set(atcos)) != len(atcos):
            raise SetupError("Each stop must be a different NaPTAN stop")
        labels = stop_labels(stops)
        bus: dict = {"stops": {label: stop.atco for label, (_, stop) in zip(labels, stops, strict=True)}}
        minutes = {label: int(walk[stop.atco][0])
                   for label, (_, stop) in zip(labels, stops, strict=True) if stop.atco in walk}
        if minutes:
            bus["walk_minutes"] = minutes
        fragment["bus"] = bus
    fragment["weather"] = {"lat": round(home.lat, 2), "lon": round(home.lon, 2)}
    return fragment
