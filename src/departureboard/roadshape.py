"""The roads a bus takes between its stops, for the dashboard's bus maps.

BODS timetables say which stops a bus calls at, not the roads between them,
so the maps would draw straight lines through buildings and across fields.
Once a new timetable is saved (timetable.refresh: about once a week, and on
every setup search), each pair of consecutive positioned stops on its
journeys is sent once to the OpenStreetMap car router (OSRM, on the same free
FOSSGIS server setup.py asks for walks), and the road it finds is saved with
the timetable. A pair the last timetable has a road for, neither stop moved,
keeps that road and isn't asked about (timetable.add_roads). The maps never
ask the router: they draw what was saved.

The router's answers are untrusted. A road is kept only if the answer is the
shape OSRM sends and at most MAX_ROUTE_BYTES, its points are numbers inside
the UK (at most MAX_RAW_POINTS of them), it starts and ends within SNAP_M of
the two stops, and it is no longer than DETOUR times the straight line plus
DETOUR_EXTRA_M (a router lost, or sent round a one-way system). A kept road
is simplified (Douglas-Peucker, to within TOLERANCE_M, more if it needs to)
to at most MAX_POINTS points between the stops, rounded to about a metre.

Anything else leaves that pair straight, and so does running out: asking
stops after MAX_PAIRS pairs, after MAX_SECONDS, after MAX_FAILURES failures
in a row, when cancelled, and at once when the router can't be reached or
says to slow down. Pairs used by the most journeys are asked first.

FOSSGIS's usage policy for the router (https://routing.openstreetmap.de/about.html)
asks for at most one request a second from a valid user agent, and no heavy
use: so one request every REQUEST_INTERVAL seconds, naming the project and
how to reach it (USER_AGENT), each given up after REQUEST_DEADLINE however
slowly the router answers.

The car router doesn't know a bus may use bus gates and bus lanes, so where a
bus takes one it sends the car the long way round. The length check usually
refuses such a road, and that pair is drawn straight.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter
from typing import Callable, Iterable, Mapping
from urllib.error import HTTPError

from . import http
from .osgb import distance_m
from .schedule_model import Journey, StopPosition
from .setup import ROUTER_LIMIT, ROUTER_USER_AGENT, RateLimiter, in_uk
from .status import safe_message

log = logging.getLogger(__name__)

ROUTER_URL = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
# FOSSGIS's policy (https://routing.openstreetmap.de/about.html): at most one
# request a second, from a valid user agent; no scraping, no heavy use. The
# limit is setup's, shared with the foot router on the same server.
USER_AGENT = ROUTER_USER_AGENT
REQUEST_INTERVAL = 1.0  # seconds between requests, at least (ROUTER_LIMIT's)
TIMEOUT = 10.0  # seconds for each step of a request
REQUEST_DEADLINE = 15.0  # seconds for a whole request, however slowly the router sends
MAX_PAIRS = 300  # requests per build; the rest are drawn straight
MAX_SECONDS = 5 * 60  # per build: MAX_PAIRS at one a second
MAX_FAILURES = 5  # in a row, before the router is taken to be down
# A real answer for two stops a few km apart is a few kB.
MAX_ROUTE_BYTES = 300_000
MAX_RAW_POINTS = 5_000
MAX_POINTS = 500  # kept between two stops
TOLERANCE_M = 5.0
DETOUR = 3
DETOUR_EXTRA_M = 500
SNAP_M = 300  # the router starts from the nearest road; further than this, it found the wrong one
SHORTEST_M = 20  # stops closer than this are drawn straight, without asking
DIGITS = 5  # decimals kept: about a metre

Point = tuple[float, float]  # lat, lon
Pair = tuple[str, str]  # ATCO codes, in the order the bus calls
Fetch = Callable[[str, int], bytes]  # (url, max_bytes) -> body, as http.get_bytes

_M_PER_DEG = 111_320


def router_get(url: str, max_bytes: int) -> bytes:
    return http.get_bytes(url, max_bytes, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT},
                          deadline=REQUEST_DEADLINE)


def shape_key(pair: Pair) -> str:
    """How a pair is named in the saved timetable: "ATCO_A>ATCO_B"."""
    return f"{pair[0]}>{pair[1]}"


def segment_pairs(journeys: Iterable[Journey],
                  positions: Mapping[str, StopPosition]) -> dict[Pair, tuple[Point, Point]]:
    """Each pair of consecutive positioned calls on the journeys, once, with
    the two stops' positions: those used by the most journeys first."""
    counts: Counter[Pair] = Counter()
    for journey in journeys:
        placed = [call.atco for call in journey.calls if call.atco in positions]
        counts.update((a, b) for a, b in zip(placed, placed[1:]) if a != b)
    return {
        (a, b): ((positions[a].lat, positions[a].lon), (positions[b].lat, positions[b].lon))
        for (a, b), _ in counts.most_common()
    }


def path(atcos: Iterable[str], positions: Mapping[str, StopPosition],
         shapes: Mapping[str, Iterable[Point]]) -> list[list[float]]:
    """The road through the given stops, in order, as [lat, lon] pairs: each
    stop, then the saved road to the next (none where none was saved, so the
    map draws that part straight). Stops without a position are skipped."""
    placed = [atco for atco in atcos if atco in positions]
    found: list[list[float]] = []
    for index, atco in enumerate(placed):
        found.append([positions[atco].lat, positions[atco].lon])
        if index + 1 < len(placed):
            found.extend([lat, lon] for lat, lon in shapes.get(shape_key((atco, placed[index + 1])), ()))
    return found


# Reading the router's answer.


def _point(item) -> Point:
    if not isinstance(item, list) or len(item) != 2:
        raise ValueError("Router's line holds something that isn't a position")
    lon, lat = item
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Router's line holds something that isn't a position")
    if not in_uk(lat, lon):
        raise ValueError("Router's line leaves the UK")
    return float(lat), float(lon)


def _metres(a: Point, b: Point) -> float:
    return distance_m(a[0], a[1], b[0], b[1])


def _offset(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """How far p is from the segment a to b, all on a flat grid in metres."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def simplify(points: list[Point], tolerance_m: float) -> list[Point]:
    """The points Douglas-Peucker keeps: the line through them strays at most
    `tolerance_m` from the original. The first and last are always kept."""
    if len(points) < 3:
        return list(points)
    across = _M_PER_DEG * math.cos(math.radians(points[0][0]))
    grid = [(lon * across, lat * _M_PER_DEG) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:  # not recursive: a long road would run out of stack
        first, last = stack.pop()
        furthest, index = 0.0, 0
        for i in range(first + 1, last):
            offset = _offset(grid[i], grid[first], grid[last])
            if offset > furthest:
                furthest, index = offset, i
        if furthest > tolerance_m:
            keep[index] = True
            stack += [(first, index), (index, last)]
    return [point for point, kept in zip(points, keep) if kept]


def parse_route(body: bytes, start: Point, end: Point) -> list[Point]:
    """The road between two stops from an OSRM route answer (GeoJSON
    geometry), simplified and rounded, without the stops themselves; [] for
    a straight road. ValueError if the answer isn't usable (see the module's
    notes)."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError too
        raise ValueError("Router answer isn't JSON") from None
    if not isinstance(data, dict) or data.get("code") != "Ok":
        raise ValueError("Router didn't answer Ok")
    routes = data.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise ValueError("Router sent no route")
    geometry = routes[0].get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise ValueError("Router's route has no line")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not 2 <= len(coordinates) <= MAX_RAW_POINTS:
        raise ValueError("Router's line is empty or too long")
    road = [_point(item) for item in coordinates]
    if _metres(start, road[0]) > SNAP_M or _metres(road[-1], end) > SNAP_M:
        raise ValueError("Router's road doesn't reach the stops")
    line = [start, *road, end]
    length = sum(_metres(a, b) for a, b in zip(line, line[1:]))
    if length > DETOUR * _metres(start, end) + DETOUR_EXTRA_M:
        raise ValueError("Router's road is implausibly long")
    tolerance = TOLERANCE_M
    kept = simplify(line, tolerance)
    while len(kept) > MAX_POINTS + 2:
        tolerance *= 2
        kept = simplify(line, tolerance)
    return [(round(lat, DIGITS), round(lon, DIGITS)) for lat, lon in kept[1:-1]]


def _worth_asking(start: Point, end: Point) -> bool:
    """Whether to ask the router about two stops: both in the UK, and far
    enough apart for the road between them to matter."""
    return in_uk(*start) and in_uk(*end) and _metres(start, end) >= SHORTEST_M


def _unreachable(error: Exception) -> bool:
    """Whether asking again would be no use, or rude: the router can't be
    reached, is failing, or says to slow down."""
    if isinstance(error, HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, OSError)


def segment_shapes(
    pairs: Mapping[Pair, tuple[Point, Point]],
    fetch: Fetch = router_get,
    *,
    clock: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[int, int], None] = lambda done, total: None,
    limiter: RateLimiter = ROUTER_LIMIT,
) -> dict[Pair, list[Point]]:
    """The road between each pair of stops (ATCO codes, with their (lat, lon)
    positions), from the car router, one request per pair. A pair left out
    is drawn straight: the router failed, its answer wasn't plausible, the
    road is straight anyway, or asking had stopped (see the module's notes,
    and `cancelled()`, asked before each request). `progress(done, total)`
    follows the requests. Never raises for the router's sake."""
    deadline = clock() + MAX_SECONDS
    shapes: dict[Pair, list[Point]] = {}
    asked = failed = in_a_row = 0
    first_error = stopped = None
    wanted = {pair: ends for pair, ends in pairs.items() if _worth_asking(*ends)}
    total = min(len(wanted), MAX_PAIRS)
    progress(0, total)
    for pair, (start, end) in wanted.items():
        if asked >= MAX_PAIRS:
            stopped = f"asked about {MAX_PAIRS} stop pairs"
            break
        if cancelled():
            stopped = "cancelled"
            break
        limiter.wait()
        if clock() >= deadline:
            stopped = f"took {MAX_SECONDS} s"
            break
        asked += 1
        url = f"{ROUTER_URL}/{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}?overview=full&geometries=geojson"
        try:
            road = parse_route(fetch(url, MAX_ROUTE_BYTES), start, end)
        except Exception as error:  # noqa: BLE001 - any failure leaves this pair straight
            failed += 1
            in_a_row += 1
            first_error = first_error or safe_message(str(error).replace(url, "…"))
            if _unreachable(error) or in_a_row >= MAX_FAILURES:
                stopped = "the router isn't answering"
                break
            continue
        finally:
            progress(asked, total)
        in_a_row = 0
        if road:
            shapes[pair] = road
    if failed or stopped:
        log.warning("Bus map roads: %d of %d stop pairs failed%s; those are drawn straight. First error: %s",
                    failed, len(pairs), f", and stopped asking: {stopped}" if stopped else "", first_error or "none")
    log.info("Bus map roads: asked the router about %d of %d stop pairs", asked, len(pairs))
    return shapes
