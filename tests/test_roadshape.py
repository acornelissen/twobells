import json
import logging
import math
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from test_livebus import (
    ARUNDEL,
    ASHTON,
    EAST,
    GRAPES,
    HENRY_A,
    HOWARD,
    JUNCTION,
    MARKET_HALL,
    POSITIONS,
    ST_MARYS_E,
    SUNLAWS,
    WEST,
    journey,
)

from departureboard.setup import lookups
from departureboard.setup.lookups import RateLimiter
from departureboard.timetable import roads as roadshape
from departureboard.timetable.roads import parse_route, path, segment_pairs, segment_shapes, shape_key, simplify

FIXTURES = Path(__file__).parent / "fixtures"
ROAD = (FIXTURES / "osrm_route_glossop.json").read_bytes()  # Sunlaws Street to St Marys Road, hand-made
NO_ROUTE = (FIXTURES / "osrm_route_noroute.json").read_bytes()
START = (53.443494, -1.959026)  # Sunlaws Street
END = (53.443511, -1.955759)  # St Marys Road
BEND = (53.4436, -1.9563)  # the road's one real bend, 10 m off the straight line


def answer(coordinates, code="Ok"):
    """An OSRM route answer with these [lon, lat] points."""
    return json.dumps({"code": code, "routes": [{"geometry": {"type": "LineString", "coordinates": coordinates},
                                                 "distance": 1.0, "duration": 1.0}]}).encode()


def straight(start, end, count):
    """`count` [lon, lat] points evenly along the straight line."""
    return [[start[1] + (end[1] - start[1]) * i / (count - 1), start[0] + (end[0] - start[0]) * i / (count - 1)]
            for i in range(count)]


# Reading one answer.


def test_the_road_between_two_stops_is_simplified_and_rounded():
    road = parse_route(ROAD, START, END)
    assert BEND in road
    assert (53.443448, -1.958367) not in road  # within 5 m of the simplified line: dropped
    assert 1 <= len(road) < 5
    assert all(lat == round(lat, 5) and lon == round(lon, 5) for lat, lon in road)
    # Only the road between: the stops themselves are where the map has them already.
    assert START not in road and END not in road


def test_a_straight_road_needs_no_points():
    assert parse_route(answer(straight(START, END, 40)), START, END) == []


@pytest.mark.parametrize("body", [
    NO_ROUTE,
    b"<html>502 Bad Gateway</html>",
    b"\xff\xfe",
    b"[]",
    answer([]),
    answer([[-1.959028, 53.443488]]),  # one point is no road
    answer(straight(START, END, 3), code="InvalidQuery"),
    json.dumps({"code": "Ok", "routes": []}).encode(),
    json.dumps({"code": "Ok", "routes": [{"geometry": "encoded"}]}).encode(),
    json.dumps({"code": "Ok", "routes": [{"geometry": {"type": "Point", "coordinates": [-1.95, 53.44]}}]}).encode(),
    answer([[-1.959028, 53.443488], ["-1.9558", "53.44352"], [-1.955761, 53.443509]]),
    answer([[-1.959028, 53.443488], [True, 53.44352], [-1.955761, 53.443509]]),
    answer([[-1.959028, 53.443488], [-1.9558, 53.44352, 120.0], [-1.955761, 53.443509]]),
    answer([[-1.959028, 53.443488], {"lat": 53.44352}, [-1.955761, 53.443509]]),
    b'{"code": "Ok", "routes": [{"geometry": {"type": "LineString", "coordinates": [[-1.959028, 53.443488], '
    b'[NaN, 53.44352], [-1.955761, 53.443509]]}}]}',
    b"[" * 100_000 + b"]" * 100_000,
])
def test_answers_of_the_wrong_shape_are_refused(body):
    with pytest.raises(ValueError):
        parse_route(body, START, END)


def test_a_road_outside_the_uk_is_refused():
    via_paris = [[-1.959028, 53.443488], [2.35, 48.85], [-1.955761, 53.443509]]
    with pytest.raises(ValueError, match="UK"):
        parse_route(answer(via_paris), START, END)


def test_a_road_much_longer_than_the_straight_line_is_refused():
    # 230 m apart: up to 3 x 230 + 500 = 1190 m is believable; a loop 600 m north and back isn't.
    loop = [[-1.959028, 53.443488], [-1.9575, 53.4489], [-1.955761, 53.443509]]
    with pytest.raises(ValueError, match="implausibly long"):
        parse_route(answer(loop), START, END)
    detour = [[-1.959028, 53.443488], [-1.9575, 53.4460], [-1.955761, 53.443509]]  # 280 m north and back
    assert parse_route(answer(detour), START, END) == [(53.446, -1.9575)]


def test_a_road_from_somewhere_else_is_refused():
    # The router starts from the nearest road; a start 1 km away means it found the wrong one.
    with pytest.raises(ValueError, match="reach the stops"):
        parse_route(answer([[-1.974, 53.4435], [-1.955761, 53.443509]]), START, END)


def test_an_answer_with_too_many_points_is_refused():
    with pytest.raises(ValueError, match="too long"):
        parse_route(answer(straight(START, END, roadshape.MAX_RAW_POINTS + 1)), START, END)


def test_a_winding_road_is_cut_to_the_most_points_kept():
    # A 3.3 km road turning a corner every 10 or 20 m, like teeth on a comb:
    # three times the straight line, and every corner survives 5 m.
    east, north = 0.0001506, 0.0001797  # 10 m and 20 m at Glossop
    points = []
    for k in range(331):
        ends = [(53.40, -1.95 + k * east), (53.40 + north, -1.95 + k * east)]
        points += ends if k % 2 == 0 else ends[::-1]
    start, end = points[0], points[-1]
    assert len(simplify(points, roadshape.TOLERANCE_M)) > roadshape.MAX_POINTS + 2
    road = parse_route(answer([[lon, lat] for lat, lon in points]), start, end)
    assert roadshape.MAX_POINTS / 4 < len(road) <= roadshape.MAX_POINTS


def test_simplify_keeps_the_ends_and_the_corners():
    corner = [(53.40, -1.95), (53.40, -1.949), (53.40, -1.948), (53.401, -1.948), (53.402, -1.948)]
    assert simplify(corner, 5) == [(53.40, -1.95), (53.40, -1.948), (53.402, -1.948)]
    wobble = [(53.40, -1.95), (53.40002, -1.949), (53.40, -1.948)]  # 2 m off the line
    assert simplify(wobble, 5) == [(53.40, -1.95), (53.40, -1.948)]
    assert simplify(wobble[:2], 5) == wobble[:2]


# Which pairs.


def test_pairs_are_consecutive_positioned_stops_each_once():
    again = journey("341", [(c.atco, c.minute + 60) for c in EAST.calls], "Hadfield", "1176")
    pairs = segment_pairs([EAST, again, WEST], POSITIONS)
    # Ashton and Hadfield have no position: EAST runs from The Junction to Howard Arms.
    assert list(pairs)[:6] == [(JUNCTION, GRAPES), (GRAPES, SUNLAWS), (SUNLAWS, ST_MARYS_E), (ST_MARYS_E, ARUNDEL),
                               (ARUNDEL, HENRY_A), (HENRY_A, HOWARD)]  # used twice, so first
    assert (HENRY_A, MARKET_HALL) in pairs and len(pairs) == 9
    a, b = pairs[(SUNLAWS, ST_MARYS_E)]
    assert a == (POSITIONS[SUNLAWS].lat, POSITIONS[SUNLAWS].lon)
    assert b == (POSITIONS[ST_MARYS_E].lat, POSITIONS[ST_MARYS_E].lon)


def test_a_gap_in_the_positions_joins_the_stops_either_side():
    gappy = journey("341", [(JUNCTION, 600), (ASHTON, 601), (GRAPES, 602), (GRAPES, 603)], "Glossop", "1")
    assert list(segment_pairs([gappy], POSITIONS)) == [(JUNCTION, GRAPES)]  # and a stop twice in a row is no pair


def test_the_path_joins_the_stops_with_the_saved_roads():
    shapes = {shape_key((SUNLAWS, ST_MARYS_E)): [BEND]}
    found = path([ASHTON, SUNLAWS, ST_MARYS_E, ARUNDEL], POSITIONS, shapes)
    assert found == [[POSITIONS[SUNLAWS].lat, POSITIONS[SUNLAWS].lon], list(BEND),
                     [POSITIONS[ST_MARYS_E].lat, POSITIONS[ST_MARYS_E].lon],
                     [POSITIONS[ARUNDEL].lat, POSITIONS[ARUNDEL].lon]]  # no road saved: straight
    assert path([], POSITIONS, shapes) == [] and path([ASHTON], POSITIONS, shapes) == []


# Asking the router.


class Router:
    """A fake router on a fake clock: `answers` in turn, then the hand-made road."""

    def __init__(self, answers=(), error=None):
        self.answers = list(answers)
        self.error = error
        self.urls = []
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def get(self, url, max_bytes):
        assert max_bytes == roadshape.MAX_ROUTE_BYTES
        self.urls.append(url)
        self.now += 0.1
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if self.answers else ROAD

    def shapes(self, pairs, **options):
        limiter = RateLimiter(roadshape.REQUEST_INTERVAL, self.clock, self.sleep)
        return segment_shapes(pairs, self.get, clock=self.clock, limiter=limiter, **options)


def pairs_along(count):
    """`count` pairs of stops, all where Sunlaws Street and St Marys Road are."""
    return {(f"A{i}", f"B{i}"): (START, END) for i in range(count)}


def test_one_request_per_pair_at_most_one_a_second():
    router = Router()
    pairs = {(SUNLAWS, ST_MARYS_E): (START, END), (ST_MARYS_E, SUNLAWS): (END, START)}
    shapes = router.shapes(pairs)
    assert router.urls == [
        "https://routing.openstreetmap.de/routed-car/route/v1/driving/"
        "-1.959026,53.443494;-1.955759,53.443511?overview=full&geometries=geojson",
        "https://routing.openstreetmap.de/routed-car/route/v1/driving/"
        "-1.955759,53.443511;-1.959026,53.443494?overview=full&geometries=geojson",
    ]
    assert shapes[(SUNLAWS, ST_MARYS_E)] == parse_route(ROAD, START, END)
    assert router.slept == [pytest.approx(0.9)]  # a second between requests, less the 0.1 s the first took


def test_stops_next_to_each_other_or_outside_the_uk_are_not_asked_about():
    router = Router()
    pairs = {("A", "B"): (START, (START[0] + 0.0001, START[1])),  # 11 m
             ("C", "D"): ((48.85, 2.35), START)}
    assert router.shapes(pairs) == {} and router.urls == []


def test_a_pair_the_router_fails_is_left_straight_and_the_rest_still_asked():
    router = Router([NO_ROUTE])
    shapes = router.shapes(pairs_along(3))
    assert len(router.urls) == 3
    assert set(shapes) == {("A1", "B1"), ("A2", "B2")}


def router_url(start, end):
    return (f"{roadshape.ROUTER_URL}/{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}"
            "?overview=full&geometries=geojson")


@pytest.mark.parametrize("error", [
    URLError("[Errno 8] nodename nor servname provided"),
    TimeoutError("timed out"),
    HTTPError("https://routing.openstreetmap.de/x", 429, "Too Many Requests", {}, None),
    HTTPError("https://routing.openstreetmap.de/x", 503, "Service Unavailable", {}, None),
])
def test_a_router_that_cannot_be_reached_or_says_slow_down_is_asked_once(error, caplog):
    router = Router(error=error)
    assert router.shapes(pairs_along(10)) == {}
    assert len(router.urls) == 1
    assert "stopped asking" in caplog.text


def test_failures_in_a_row_stop_the_asking():
    router = Router(error=HTTPError("https://routing.openstreetmap.de/x", 400, "Bad Request", {}, None))
    assert router.shapes(pairs_along(10)) == {}
    assert len(router.urls) == roadshape.MAX_FAILURES


def test_any_failure_leaves_the_pair_straight_without_raising():
    router = Router(error=RuntimeError("something odd"))
    assert router.shapes(pairs_along(2)) == {}


def test_at_most_max_pairs_are_asked_about(monkeypatch, caplog):
    monkeypatch.setattr(roadshape, "MAX_PAIRS", 3)
    router = Router()
    shapes = router.shapes(pairs_along(5))
    assert len(router.urls) == 3 and len(shapes) == 3
    assert "asked about 3 stop pairs" in caplog.text


def test_asking_stops_when_the_time_is_up(monkeypatch):
    router = Router()
    monkeypatch.setattr(roadshape, "MAX_SECONDS", 1.2)
    router.shapes(pairs_along(10))
    assert len(router.urls) == 2  # at 0 and 1 s


def test_the_log_never_repeats_the_url_or_a_key(caplog):
    caplog.set_level(logging.DEBUG)
    error = URLError(f"failed for {router_url(START, END)} api_key=SECRET123456")
    Router(error=error).shapes({("A", "B"): (START, END)})
    assert "routing.openstreetmap.de" not in caplog.text and "SECRET123456" not in caplog.text
    assert "the router isn't answering" in caplog.text


def test_cancelling_stops_the_asking_within_a_few_seconds(caplog):
    router = Router()
    start = router.now
    # Cancelled once the first request has been sent.
    shapes = router.shapes(pairs_along(10), cancelled=lambda: len(router.urls) > 0)
    assert len(router.urls) == 1 and len(shapes) == 1  # the road already found is kept
    assert router.now - start < 3
    assert "stopped asking: cancelled" in caplog.text


def test_progress_follows_the_pairs_asked_about():
    seen = []
    pairs = pairs_along(3) | {("A", "B"): (START, (START[0] + 0.0001, START[1]))}  # too close to ask about
    Router().shapes(pairs, progress=lambda done, total: seen.append((done, total)))
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_progress_counts_no_more_than_max_pairs(monkeypatch):
    monkeypatch.setattr(roadshape, "MAX_PAIRS", 2)
    seen = []
    Router().shapes(pairs_along(5), progress=lambda done, total: seen.append((done, total)))
    assert seen == [(0, 2), (1, 2), (2, 2)]


def test_the_default_asks_the_router_politely_through_the_capped_getter(monkeypatch):
    asked = []

    def get_bytes(url, max_bytes, timeout, headers, deadline):
        asked.append((url, max_bytes, timeout, headers, deadline))
        return ROAD

    monkeypatch.setattr(roadshape.net, "get_bytes", get_bytes)
    assert roadshape.router_get("https://x", 10) == ROAD
    assert asked == [("https://x", 10, roadshape.TIMEOUT, {"User-Agent": roadshape.USER_AGENT},
                      roadshape.REQUEST_DEADLINE)]
    # FOSSGIS's policy: a valid user agent, saying who is asking and how to reach them.
    assert roadshape.USER_AGENT.startswith("inky-departures/")
    assert "https://github.com/acornelissen/inky-departures" in roadshape.USER_AGENT
    assert roadshape.REQUEST_DEADLINE < 30 and roadshape.REQUEST_INTERVAL >= 1.0


def test_roads_and_walk_times_share_one_limit_on_the_router():
    # Both ask FOSSGIS's server; the setup service can look up walk times while
    # a search asks for roads, and the policy is one request a second in all.
    assert segment_shapes.__kwdefaults__["limiter"] is lookups.ROUTER_LIMIT
    assert lookups.walk_minutes.__defaults__[1] is lookups.ROUTER_LIMIT


def test_shapes_are_far_smaller_than_the_answer():
    road = parse_route(ROAD, START, END)
    assert len(json.dumps(road)) < len(ROAD) / 10
    assert all(math.isfinite(v) for point in road for v in point)
