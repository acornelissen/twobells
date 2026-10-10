import json
import logging
import math
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from departureboard import setup
from departureboard.osgb import distance_m
from departureboard.setup import (
    AnswerCache,
    Place,
    RateLimiter,
    SetupError,
    Station,
    Stop,
    areas_near,
    bank_holiday_division,
    config_fragment,
    estimate_minutes,
    lookup_postcode,
    nearby_stations,
    nearby_stops,
    normalise_postcode,
    parse_nominatim,
    parse_stations,
    search_address,
    stop_labels,
    walk_minutes,
)

FIXTURES = Path(__file__).parent / "fixtures"
NAPTAN = (FIXTURES / "naptan_setup_glossop.csv").read_bytes()

# Hadfield station's postcode (National Rail lists the station at SK13 1AH),
# a public place, standing in for a home, at its postcodes.io position.
HOME = Place(53.461964, -1.965633, "SK13 1AH", "England", "High Peak")
HOME_TEXT = ("53.461964", "-1.965633", "53.46196", "-1.96563", "53.4620", "-1.9656")
# Glossop station: the stop searches look around it, as the NaPTAN fixture
# holds the town centre's stops.
TOWN = Place(53.44447, -1.94907, "Glossop station")

# Shaped like postcodes.io's answer for one postcode (trimmed: the real one
# has thirty-odd fields, every one of them ignored here but these).
POSTCODE_ANSWER = {
    "status": 200,
    "result": {
        "postcode": "SK13 1AH",
        "quality": 1,
        "eastings": 402379,
        "northings": 396134,
        "country": "England",
        "longitude": -1.965633,
        "latitude": 53.461964,
        "region": "East Midlands",
        "outcode": "SK13",
        "incode": "1AH",
        "admin_district": "High Peak",
        "admin_county": "Derbyshire",
        "parish": "High Peak, unparished area",
        "codes": {"admin_district": "E07000037", "admin_county": "E10000007"},
    },
}

# Shaped like Nominatim's /search?format=jsonv2&addressdetails=1 answer.
NOMINATIM_ANSWER = [
    {
        "place_id": 277274911,
        "licence": "Data © OpenStreetMap contributors, ODbL 1.0. http://osm.org/copyright",
        "osm_type": "node",
        "osm_id": 464752337,
        "lat": "53.4614990",
        "lon": "-1.9663028",
        "category": "highway",
        "type": "bus_stop",
        "place_rank": 30,
        "importance": 0.0000522,
        "addresstype": "highway",
        "name": "Hadfield Station",
        "display_name": "Hadfield Station, Station Road, Hadfield, High Peak, Derbyshire, England, SK13 1AH, United Kingdom",
        "address": {
            "highway": "Hadfield Station",
            "road": "Station Road",
            "town": "Hadfield",
            "city": "High Peak",
            "county": "Derbyshire",
            "state_district": "East Midlands",
            "state": "England",
            "postcode": "SK13 1AH",  # OSM files this stop under SK13 1DL, among houses
            "country": "United Kingdom",
            "country_code": "gb",
        },
        "boundingbox": ["53.4614490", "53.4615490", "-1.9663528", "-1.9662528"],
    },
    {
        "place_id": 223456,
        "lat": "55.8609",
        "lon": "-4.2514",
        "display_name": "Station Road, Glasgow, Glasgow City, Scotland, G1 1AA, United Kingdom",
        "address": {"city": "Glasgow", "state": "Scotland", "country_code": "gb"},
    },
]

# Shaped like the OSRM table answer for sources=0 and three destinations.
OSRM_ANSWER = {
    "code": "Ok",
    "durations": [[0, 61.2, 600, None]],
    "sources": [{"hint": "abc", "distance": 3.1, "name": "Station Road", "location": [-1.965612, 53.461988]}],
    "destinations": [{"hint": "def", "distance": 1.0, "name": "", "location": [-1.9656, 53.4620]}] * 4,
}


class Getter:
    """A fake http_get that behaves like http.get_bytes: it records each URL
    and refuses a body over the caller's cap."""

    def __init__(self, body=b"", error: BaseException | None = None):
        self.body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.error = error
        self.urls: list[str] = []
        self.caps: list[int] = []

    def __call__(self, url: str, max_bytes: int) -> bytes:
        self.urls.append(url)
        self.caps.append(max_bytes)
        if self.error is not None:
            raise self.error
        if len(self.body) > max_bytes:
            raise ValueError(f"Response larger than {max_bytes} bytes")
        return self.body


def refuse(url, max_bytes):
    raise AssertionError("no request expected")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def no_home_in(text: str) -> None:
    for coordinate in HOME_TEXT:
        assert coordinate not in text


# Postcodes.


@pytest.mark.parametrize(
    "typed, normal",
    [("sk131ah", "SK13 1AH"), (" SK13  1AH ", "SK13 1AH"), ("m1 1ae", "M1 1AE"), ("SW1A1AA", "SW1A 1AA"),
     ("ec1a 1bb", "EC1A 1BB"), ("W1A 0AX", "W1A 0AX")],
)
def test_postcodes_are_normalised(typed, normal):
    assert normalise_postcode(typed) == normal


@pytest.mark.parametrize(
    "typed",
    ["", "SK13", "12345", "SK13 1AHA", "SK13 1A/", "../../admin", "SK13 1AH; DROP", "SK13%201AH", "S" * 5000,
     None, 1338],
)
def test_anything_but_a_postcode_is_refused_before_any_request(typed):
    with pytest.raises(SetupError, match="isn't a UK postcode"):
        lookup_postcode(typed, http_get=refuse)


def test_postcode_lookup_asks_postcodes_io_with_the_postcode_quoted():
    getter = Getter(POSTCODE_ANSWER)
    place = lookup_postcode("sk13 1ah", http_get=getter)
    assert getter.urls == ["https://api.postcodes.io/postcodes/SK13%201AH"]
    assert getter.caps == [setup.MAX_POSTCODE_BYTES]
    assert place == HOME


@pytest.mark.parametrize(
    "change",
    [
        {"latitude": 40.4168, "longitude": -3.7038},  # Madrid
        {"latitude": None, "longitude": None},  # postcodes.io gives null for some
        {"latitude": "53.4620"},  # a string where a number belongs
        {"longitude": True},
        {"latitude": float("nan")},
    ],
)
def test_postcode_answer_without_a_uk_position_is_refused(change):
    answer = json.loads(json.dumps(POSTCODE_ANSWER))
    answer["result"].update(change)
    body = json.dumps(answer, allow_nan=True).encode()
    with pytest.raises(SetupError, match="no UK position"):
        lookup_postcode("SK13 1AH", http_get=Getter(body))


@pytest.mark.parametrize(
    "body",
    [b"<html>Bad gateway</html>", b"[" * 10_000 + b"]" * 10_000, b'{"status": 200}', b"[]",
     b'{"status": 200, "result": "SK13 1AH"}'],
)
def test_postcode_answer_of_the_wrong_shape_is_refused(body):
    with pytest.raises(SetupError):
        lookup_postcode("SK13 1AH", http_get=Getter(body))


def test_postcode_answer_text_is_cleaned_and_cut():
    answer = json.loads(json.dumps(POSTCODE_ANSWER))
    answer["result"].update(country=["England"], admin_district="High\nPeak " + "x" * 500, postcode="<b>")
    place = lookup_postcode("SK13 1AH", http_get=Getter(answer))
    assert place.country == ""
    assert place.admin_district.startswith("High Peak x") and len(place.admin_district) == 80
    assert place.label == "SK13 1AH"  # what was asked, as the answer's wasn't a postcode


def test_oversized_postcode_answer_is_refused():
    body = json.dumps({**POSTCODE_ANSWER, "pad": "x" * setup.MAX_POSTCODE_BYTES}).encode()
    with pytest.raises(SetupError, match="larger than"):
        lookup_postcode("SK13 1AH", http_get=Getter(body))


def test_unknown_postcode_says_so():
    error = HTTPError("https://api.postcodes.io/postcodes/ZZ99%209ZZ", 404, "Not Found", {}, None)
    with pytest.raises(SetupError, match="doesn't know that postcode"):
        lookup_postcode("ZZ99 9ZZ", http_get=Getter(error=error))


def test_postcode_failure_message_has_neither_the_url_nor_the_postcode():
    url = "https://api.postcodes.io/postcodes/SK13%201AH"
    with pytest.raises(SetupError) as caught:
        lookup_postcode("SK13 1AH", http_get=Getter(error=URLError(f"timed out fetching {url} for SK13 1AH")))
    assert "postcodes.io/postcodes" not in str(caught.value) and "1AH" not in str(caught.value)


def test_address_failure_message_leaves_out_the_address():
    with pytest.raises(SetupError, match="Address search failed") as caught:
        search("Station Road, Hadfield", Getter(error=URLError("no answer for Station Road, Hadfield")))
    assert "Station" not in str(caught.value)


@pytest.mark.parametrize(
    "country, division",
    [("England", "england-and-wales"), ("Wales", "england-and-wales"), ("Scotland", "scotland"),
     ("Northern Ireland", "northern-ireland"), ("Channel Islands", None), ("Isle of Man", None), ("", None),
     (None, None)],
)
def test_bank_holiday_division_follows_the_country(country, division):
    assert bank_holiday_division(country) == division


# Address search.


def search(text, getter, clock=None, cache=None):
    clock = clock or FakeClock()
    return search_address(text, http_get=getter, limiter=RateLimiter(1.0, clock, clock.sleep),
                          cache=cache or AnswerCache(clock=clock))


def test_address_search_asks_nominatim_for_gb_places():
    getter = Getter(NOMINATIM_ANSWER)
    places = search("  Station  Road, Hadfield ", getter)
    parts = urlsplit(getter.urls[0])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://nominatim.openstreetmap.org/search"
    query = parse_qs(parts.query)
    assert query["format"] == ["jsonv2"] and query["countrycodes"] == ["gb"] and query["limit"] == ["5"]
    assert query["q"] == ["Station Road, Hadfield"]
    assert getter.caps == [setup.MAX_NOMINATIM_BYTES]
    assert places[0] == Place(53.461499, -1.9663028, NOMINATIM_ANSWER[0]["display_name"], "England", "High Peak")
    assert places[1].country == "Scotland" and places[1].admin_district == "Glasgow"


def test_address_text_is_quoted_into_the_query():
    getter = Getter([])
    search("1 High St & Co?q=x#y", getter)
    assert "&Co" not in getter.urls[0] and "#" not in getter.urls[0]
    assert parse_qs(urlsplit(getter.urls[0]).query)["q"] == ["1 High St & Co?q=x#y"]


@pytest.mark.parametrize("text", ["", "ab", "  a  ", "x" * 121, None])
def test_address_text_must_be_3_to_120_characters(text):
    with pytest.raises(SetupError, match="3 to 120"):
        search(text, refuse)


def test_bad_nominatim_entries_are_skipped_and_at_most_five_kept():
    entries = [
        {"lat": "48.8566", "lon": "2.3522", "display_name": "Paris"},
        {"lat": "53.44", "lon": "-1.95"},  # no name
        {"lat": "fifty-three", "lon": "-1.95", "display_name": "Words"},
        {"lat": 53.44, "lon": True, "display_name": "Bool"},
        "not a place",
        *[{"lat": "53.44", "lon": "-1.95", "display_name": f"Place {i}"} for i in range(8)],
    ]
    places = parse_nominatim(json.dumps(entries).encode())
    assert places == []  # the first five entries were all bad
    places = parse_nominatim(json.dumps(entries[5:]).encode())
    assert [p.label for p in places] == [f"Place {i}" for i in range(5)]


@pytest.mark.parametrize("body", [b'{"error": "Bad request"}', b"<html>", b'"text"'])
def test_nominatim_answer_that_isnt_a_list_is_refused(body):
    with pytest.raises(SetupError):
        search("Station Road", Getter(body))


def test_oversized_nominatim_answer_is_refused():
    body = json.dumps([{"display_name": "x" * setup.MAX_NOMINATIM_BYTES}]).encode()
    with pytest.raises(SetupError, match="larger than"):
        search("Station Road", Getter(body))


def test_nominatim_is_asked_at_most_once_a_second_without_real_sleeping():
    clock, getter = FakeClock(), Getter(NOMINATIM_ANSWER)
    limiter, cache = RateLimiter(1.0, clock, clock.sleep), AnswerCache(clock=clock)
    search_address("Station Road", http_get=getter, limiter=limiter, cache=cache)
    clock.now += 0.25
    search_address("Norfolk Street", http_get=getter, limiter=limiter, cache=cache)
    assert clock.slept == [pytest.approx(0.75)]
    clock.now += 5
    search_address("High Street West", http_get=getter, limiter=limiter, cache=cache)
    assert len(clock.slept) == 1 and len(getter.urls) == 3


def test_the_rate_limit_is_shared_by_default():
    assert search_address.__defaults__[1] is setup.NOMINATIM_LIMIT


def test_repeated_search_comes_from_the_cache_without_waiting():
    clock, getter = FakeClock(), Getter(NOMINATIM_ANSWER)
    limiter, cache = RateLimiter(1.0, clock, clock.sleep), AnswerCache(clock=clock)
    first = search_address("Station Road", http_get=getter, limiter=limiter, cache=cache)
    again = search_address("station   road", http_get=getter, limiter=limiter, cache=cache)
    assert again == first and len(getter.urls) == 1 and clock.slept == []


def test_cache_forgets_old_answers_and_keeps_only_a_few():
    clock = FakeClock()
    cache = AnswerCache(size=2, max_age=60, clock=clock)
    cache.put("a", (1,))
    clock.now += 61
    assert cache.get("a") is None
    cache.put("a", (1,))
    cache.put("b", (2,))
    cache.get("a")
    cache.put("c", (3,))
    assert cache.get("b") is None and cache.get("a") == (1,) and cache.get("c") == (3,)


def test_failed_search_is_not_cached():
    clock = FakeClock()
    cache = AnswerCache(clock=clock)
    with pytest.raises(SetupError):
        search("Station Road", Getter(error=TimeoutError("timed out")), clock, cache)
    assert search("Station Road", Getter(NOMINATIM_ANSWER), clock, cache)


def test_nominatim_getter_names_the_project(monkeypatch):
    seen = {}

    def fake_get_bytes(url, max_bytes, timeout=15.0, headers=None):
        seen.update(headers)
        return b"[]"

    monkeypatch.setattr(setup, "get_bytes", fake_get_bytes)
    setup.nominatim_get("https://nominatim.openstreetmap.org/search?q=x", 100)
    assert seen["User-Agent"].startswith("inky-departures/")
    assert "github.com/acornelissen/inky-departures" in seen["User-Agent"]


# Rail stations.

STATIONS_CSV = """# A comment line
name,crs,lat,lon
Glossop,GLO,53.44447,-1.94907
Hadfield,HDF,53.46074,-1.96532
Dinting,DTG,53.44916,-1.97000
Manchester Piccadilly,MAN,53.47736,-2.23095
Lowercase,gl0,53.44,-1.95
Paris Nord,PNX,48.88,2.35
No Position,NOP,,
"""


def test_station_list_skips_bad_rows():
    assert [row[1] for row in parse_stations(STATIONS_CSV)] == ["GLO", "HDF", "DTG", "MAN"]


def test_nearby_stations_are_sorted_by_distance_and_limited():
    stations = parse_stations(STATIONS_CSV)
    found = nearby_stations(HOME.lat, HOME.lon, limit=2, stations=stations)
    assert [s.crs for s in found] == ["HDF", "DTG"]
    assert found[0] == Station("Hadfield", "HDF", 53.46074, -1.96532, round(distance_m(
        HOME.lat, HOME.lon, 53.46074, -1.96532)))
    assert [s.crs for s in nearby_stations(HOME.lat, HOME.lon, stations=stations)] == ["HDF", "DTG", "GLO"]
    everything = nearby_stations(HOME.lat, HOME.lon, within_km=50, stations=stations)
    assert [s.crs for s in everything] == ["HDF", "DTG", "GLO", "MAN"]
    assert [s.distance_m for s in everything] == sorted(s.distance_m for s in everything)


def test_bundled_station_list_finds_hadfield_first():
    found = nearby_stations(HOME.lat, HOME.lon, limit=3)
    assert [s.crs for s in found[:2]] == ["HDF", "DTG"]
    assert all(re.fullmatch("[A-Z]{3}", s.crs) for s in found)


def test_bundled_station_list_is_whole_and_unique():
    rows = setup.bundled_stations()
    assert len(rows) > 2500
    assert len({row[1] for row in rows}) == len(rows)
    assert {"GLO", "HDF", "MAN", "EUS", "EDB", "CDF"} <= {row[1] for row in rows}


@pytest.mark.parametrize(
    "args",
    [(48.85, 2.35), (53.44, "x"), (math.nan, -1.95), (True, -1.95)],
)
def test_station_search_refuses_a_point_outside_the_uk(args):
    with pytest.raises(SetupError):
        nearby_stations(*args, stations=())


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 51}, {"within_km": 0}, {"within_km": 101}])
def test_station_search_refuses_silly_limits(kwargs):
    with pytest.raises(SetupError):
        nearby_stations(HOME.lat, HOME.lon, stations=(), **kwargs)


# Bus stops.

GRID = {(534, -20): ("100", "180"), (534, -19): ("100",), (535, -20): ("180", "250")}


def test_areas_come_from_every_cell_the_circle_touches():
    assert areas_near(HOME.lat, HOME.lon, 800, GRID) == ["100", "180"]
    assert areas_near(53.495, -1.95, 800, GRID) == ["100", "180", "250"]  # near the cell's north edge
    assert areas_near(51.5, -0.12, 800, GRID) == []


def test_bundled_grid_puts_hadfield_in_derbyshire_and_greater_manchester():
    assert areas_near(HOME.lat, HOME.lon, 800) == ["100", "180"]


def test_nearby_stops_downloads_only_the_areas_and_sorts_by_distance():
    getter = Getter(NAPTAN)
    stops = nearby_stops(TOWN.lat, TOWN.lon, http_get=getter, grid=GRID)
    assert len(getter.urls) == 1
    parts = urlsplit(getter.urls[0])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://naptan.api.dft.gov.uk/v1/access-nodes"
    assert parse_qs(parts.query) == {"dataFormat": ["csv"], "atcoAreaCodes": ["100,180"]}
    assert "53.44" not in getter.urls[0] and "-1.94" not in getter.urls[0]
    assert [s.atco for s in stops] == [
        "1000DGHS9999", "1000DGHS0901", "1000DGHS0900", "1000DGHS1150", "1000DGHSW936", "1000DGHS3403",
    ]
    assert [s.distance_m for s in stops] == sorted(s.distance_m for s in stops)
    henry = stops[2]
    assert henry == Stop("1000DGHS0900", "Henry Street", "Stop A", "E", 53.44384, -1.948999, 70)


def test_stop_fields_are_cleaned():
    stops = {s.atco: s for s in nearby_stops(TOWN.lat, TOWN.lon, http_get=Getter(NAPTAN), grid=GRID)}
    odd = stops["1000DGHS9999"]
    assert odd.name == "Odd Bearing Stop" and odd.bearing == "" and odd.indicator == "opp"


def test_stops_given_only_a_grid_reference_get_a_gps_position():
    market = next(s for s in nearby_stops(TOWN.lat, TOWN.lon, http_get=Getter(NAPTAN), grid=GRID)
                  if s.atco == "1000DGHSW936")
    assert distance_m(market.lat, market.lon, 53.443239, -1.949828) < 10  # NaPTAN's own GPS for it


def test_inactive_tram_far_bad_and_unplaced_stops_are_left_out():
    atcos = {s.atco for s in nearby_stops(TOWN.lat, TOWN.lon, http_get=Getter(NAPTAN), grid=GRID)}
    assert not atcos & {"1000DGHS3404", "1000DGHSTRM1", "../../etc", "1800EH48511", "1000DGNOWHER"}
    # Woolley Close is left out only for being 3.1 km away.
    wide = {s.atco for s in setup.stops_near(NAPTAN.decode(), TOWN.lat, TOWN.lon, 5000, 50)}
    assert "1800EH48511" in wide and not wide & {"1000DGHS3404", "1000DGHSTRM1", "../../etc", "1000DGNOWHER"}


def test_stop_limit_keeps_the_closest():
    stops = nearby_stops(TOWN.lat, TOWN.lon, limit=2, http_get=Getter(NAPTAN), grid=GRID)
    assert [s.atco for s in stops] == ["1000DGHS9999", "1000DGHS0901"]


def test_no_areas_nearby_means_no_request():
    assert nearby_stops(51.5, -0.12, http_get=refuse, grid=GRID) == []


@pytest.mark.parametrize("kwargs", [{"within_m": 10}, {"within_m": 5000}, {"limit": 0}, {"limit": 51}])
def test_stop_search_refuses_silly_limits(kwargs):
    with pytest.raises(SetupError):
        nearby_stops(HOME.lat, HOME.lon, http_get=refuse, grid=GRID, **kwargs)


@pytest.mark.parametrize("body", ["ATCOCode\n\"unterminated".encode() + b"x" * 200_000, b"\xff\xfe\x00bad"])
def test_unreadable_naptan_file_is_refused(body):
    with pytest.raises(SetupError, match="NaPTAN"):
        nearby_stops(HOME.lat, HOME.lon, http_get=Getter(body), grid=GRID)


def test_naptan_failure_is_a_plain_message():
    with pytest.raises(SetupError, match="NaPTAN download failed") as caught:
        nearby_stops(HOME.lat, HOME.lon, http_get=Getter(error=URLError("unreachable")), grid=GRID)
    assert "atcoAreaCodes" not in str(caught.value)


# Walk times.

STATION = Station("Hadfield", "HDF", 53.46074, -1.96532, 141)
STOP_A = Stop("1000DGHS0900", "Henry Street", "Stop A", "E", 53.44384, -1.948999, 2299)
STOP_B = Stop("1000DGHS0901", "Henry Street", "Stop B", "W", 53.4439, -1.94905, 2292)
FAR = Stop("1800EH48511", "Woolley Close", "nr", "E", 53.458888, -1.989039, 1590)
PLACES = {"HDF": STATION, "1000DGHS0900": STOP_A, "1800EH48511": FAR}


def test_walk_times_come_from_one_router_request_rounded_up():
    getter = Getter(OSRM_ANSWER)
    walk = walk_minutes(HOME, PLACES, http_get=getter)
    assert len(getter.urls) == 1
    assert getter.urls[0] == (
        "https://routing.openstreetmap.de/routed-foot/table/v1/foot/"
        "-1.965633,53.461964;-1.965320,53.460740;-1.948999,53.443840;-1.989039,53.458888?sources=0"
    )
    assert walk["HDF"] == (2, "route")  # 61.2 s
    assert walk["1000DGHS0900"] == (10, "route")  # 600 s exactly
    assert walk["1800EH48511"] == (estimate_minutes(HOME, FAR), "estimate")  # no route found


def test_estimate_is_the_straight_line_plus_30_percent_at_4_8_kmh():
    # 0.01 degrees of latitude is 1112 m: 1445 m walked, 18.1 min, so 19.
    assert estimate_minutes(Place(53.0, -2.0, ""), Place(53.01, -2.0, "")) == 19


@pytest.mark.parametrize(
    "answer",
    [
        {"code": "NoTable", "message": "no"},
        {"code": "Ok", "durations": [[0, 61.2]]},  # one short
        {"code": "Ok", "durations": [[0, 61.2, 600, 30], [0, 1, 2, 3]]},  # two sources
        {"code": "Ok", "durations": [[0, "61", 600, 30]]},
        {"code": "Ok", "durations": [[0, -5, 600, 30]]},
        {"code": "Ok", "durations": [[0, True, 600, 30]]},
        {"code": "Ok"},
        ["Ok"],
    ],
)
def test_router_answers_of_the_wrong_shape_fall_back_to_estimates(answer):
    walk = walk_minutes(HOME, PLACES, http_get=Getter(answer))
    assert walk == {key: (estimate_minutes(HOME, place), "estimate") for key, place in PLACES.items()}


@pytest.mark.parametrize(
    "getter",
    [
        Getter(b"<html>502 Bad Gateway</html>"),
        Getter(json.dumps({**OSRM_ANSWER, "pad": "x" * setup.MAX_OSRM_BYTES}).encode()),
        Getter(error=URLError("timed out")),
        Getter(error=HTTPError("https://routing.openstreetmap.de/x", 429, "Too Many Requests", {}, None)),
    ],
)
def test_router_failure_falls_back_to_estimates(getter):
    walk = walk_minutes(HOME, PLACES, http_get=getter)
    assert all(kind == "estimate" for _, kind in walk.values())
    assert walk["HDF"] == (estimate_minutes(HOME, STATION), "estimate")


def test_a_walk_longer_than_six_hours_is_the_router_lost():
    answer = {"code": "Ok", "durations": [[0, 61.2, 600, 6 * 3600 + 1]]}
    assert walk_minutes(HOME, PLACES, http_get=Getter(answer))["1800EH48511"][1] == "estimate"


def test_no_destinations_means_no_request():
    assert walk_minutes(HOME, {}, http_get=refuse) == {}


def test_too_many_destinations_are_refused():
    many = {str(i): STOP_A for i in range(setup.MAX_WALK_DESTINATIONS + 1)}
    with pytest.raises(SetupError):
        walk_minutes(HOME, many, http_get=refuse)


def test_a_destination_outside_the_uk_is_refused():
    with pytest.raises(SetupError):
        walk_minutes(HOME, {"x": Place(48.85, 2.35, "Paris")}, http_get=refuse)


# Logs never carry where home is.


def test_logs_never_contain_coordinates_postcodes_or_addresses(caplog):
    caplog.set_level(logging.DEBUG)
    url_error = URLError("failed for https://routing.openstreetmap.de/routed-foot/table/v1/foot/"
                         "-1.965633,53.461964;-1.965320,53.460740?sources=0 near 53.461964, -1.965633")
    walk_minutes(HOME, PLACES, http_get=Getter(error=url_error))
    walk_minutes(HOME, PLACES, http_get=Getter(b"not json"))
    lookup_postcode("SK13 1AH", http_get=Getter(POSTCODE_ANSWER))
    with pytest.raises(SetupError):
        lookup_postcode("SK13 1AH", http_get=Getter(error=URLError("SK13 1AH at 53.461964,-1.965633")))
    search("Station Road, Hadfield", Getter(NOMINATIM_ANSWER))
    nearby_stops(HOME.lat, HOME.lon, http_get=Getter(NAPTAN), grid=GRID)
    nearby_stations(HOME.lat, HOME.lon)
    assert "Walk router failed" in caplog.text  # the fallback is logged
    no_home_in(caplog.text)
    assert not any(number in caplog.text for number in ("53.46", "-1.96", "53.44", "-1.94"))
    assert not any(text in caplog.text for text in ("SK13", "Station Road", "Henry Street"))


# The config fragment.


def test_config_fragment_holds_codes_labels_walks_and_a_rounded_position():
    walk = {"HDF": (2, "route"), "1000DGHS0900": (1, "route"), "1000DGHS0901": (3, "estimate")}
    fragment = config_fragment(HOME, STATION, [(None, STOP_A), (None, STOP_B)], walk)
    assert fragment == {
        "train": {"crs": "HDF", "walk_minutes": 2},
        "bus": {
            "stops": {"A": "1000DGHS0900", "B": "1000DGHS0901"},
            "walk_minutes": {"A": 1, "B": 3},
        },
        "weather": {"lat": 53.46, "lon": -1.97},
    }


def test_config_fragment_has_no_exact_home_coordinates():
    fragment = config_fragment(HOME, STATION, [(None, STOP_A)], {})
    numbers = []

    def walk_values(value):
        if isinstance(value, dict):
            for v in value.values():
                walk_values(v)
        elif isinstance(value, float):
            numbers.append(value)

    walk_values(fragment)
    assert numbers == [53.46, -1.97]
    no_home_in(repr(fragment))


def test_config_fragment_leaves_out_what_wasnt_chosen():
    assert config_fragment(HOME, None, [], {}) == {"weather": {"lat": 53.46, "lon": -1.97}}
    fragment = config_fragment(HOME, STATION, [(None, STOP_A)], {})
    assert fragment["train"] == {"crs": "HDF"} and "walk_minutes" not in fragment["bus"]


def stop(atco, indicator):
    return Stop(atco, "Somewhere", indicator, "N", 53.44, -1.95, 10)


@pytest.mark.parametrize(
    "indicators, labels",
    [
        (["Stop A", "Stop B"], ["A", "B"]),
        (["B", "Stand 12"], ["B", "12"]),
        (["Bay C3", "stop D"], ["C3", "D"]),
        (["opp", "adj"], ["A", "B"]),
        (["Stop A", "Stop A"], ["A", "B"]),  # not unique
        (["Stop C", "opp"], ["A", "B"]),  # not every stop has one
        (["Opp", "NE-bound"], ["A", "B"]),
    ],
)
def test_labels_default_to_the_letter_on_the_sign_when_unique(indicators, labels):
    stops = [(None, stop(f"1000DGHS{i:04d}", ind)) for i, ind in enumerate(indicators)]
    assert stop_labels(stops) == labels


def test_given_labels_are_kept_and_defaults_avoid_them():
    stops = [("A", stop("1000DGHS0001", "opp")), (None, stop("1000DGHS0002", "adj")),
             ("", stop("1000DGHS0003", "nr"))]
    assert stop_labels(stops) == ["A", "B", "C"]
    clash = [("A", stop("1000DGHS0001", "opp")), (None, stop("1000DGHS0002", "Stop A"))]
    assert stop_labels(clash) == ["A", "B"]


@pytest.mark.parametrize("label", ["ABCD", "a b", "A=", "../", "\n"])
def test_bad_labels_are_refused(label):
    with pytest.raises(SetupError):
        stop_labels([(label, STOP_A)])


def test_duplicate_labels_or_stops_are_refused():
    with pytest.raises(SetupError):
        stop_labels([("A", STOP_A), ("A", STOP_B)])
    with pytest.raises(SetupError):
        config_fragment(HOME, None, [("A", STOP_A), ("B", STOP_A)], {})


def test_too_many_stops_are_refused():
    with pytest.raises(SetupError):
        stop_labels([(None, stop(f"1000DGHS{i:04d}", "opp")) for i in range(setup.MAX_BOARD_STOPS + 1)])
