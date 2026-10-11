"""Testing a key with one harmless call, against fake services: no network."""

import io
import logging
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from departureboard.core import net
from departureboard.setup import keytest
from departureboard.setup.keytest import KeyResult
from departureboard.setup.spool.folder import WAITS
from tests.paths import FIXTURES

BOARD = (FIXTURES / "ldbws_glossop_details.json").read_bytes()
DETAILS = (FIXTURES / "ldbws_service_details_from_hadfield.json").read_bytes()
VEHICLES = (FIXTURES / "bods_glossop_synthetic.xml").read_bytes()
INCIDENTS = (FIXTURES / "kb_incidents_synthetic.xml").read_bytes()
TIMETABLE = (FIXTURES / "tapi_henry_street_a_late_evening.json").read_bytes()

RDM_KEY = "rdm-board-key-0001"
DETAILS_KEY = "rdm-details-key-0002"
KB_KEY = "rdm-kb-key-0003"
BODS_KEY = "bods-key-0004"
TAPI_ID = "tapi-id-0005"
TAPI_KEY = "tapi-key-0006"
SAVED = {"RDM_API_KEY": RDM_KEY, "RDM_DETAILS_API_KEY": DETAILS_KEY, "RDM_KB_API_KEY": KB_KEY,
         "BODS_API_KEY": BODS_KEY, "TAPI_APP_ID": TAPI_ID, "TAPI_APP_KEY": TAPI_KEY}
KB_URL = "https://api1.raildata.org.uk/kb-incidents/incidents"
CONFIG = {
    "train": {"crs": "man"},
    "bus": {"stops": {"A": "1000DGHS0900", "B": "100000008350"}, "routes": {"341": {"stop": "A", "stops": [
        {"name": "Hadfield", "lat": 53.46, "lon": -1.965},
        {"name": "Henry Street", "lat": 53.44384, "lon": -1.948999},
    ]}}},
    "dashboard": {"kb_incidents_url": KB_URL},
}
RDM_OTHER_PRODUCT = (b'{"fault":{"faultstring":"Invalid ApiKey for given resource",'
                     b'"detail":{"errorcode":"oauth.v2.InvalidApiKeyForGivenResource"}}}')
RDM_INVALID = b'{"fault":{"faultstring":"Invalid ApiKey","detail":{"errorcode":"oauth.v2.InvalidApiKey"}}}'


def http_error(url, code, body=b""):
    return HTTPError(url, code, "Error", {}, io.BytesIO(body))


class Fake:
    """Answers each request by the first rule whose text is in the URL: bytes
    are the body, (status, body) an HTTP error, and an exception is raised."""

    def __init__(self, *rules):
        self.rules = rules
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        for part, answer in self.rules:
            if part in url:
                if isinstance(answer, tuple):
                    raise http_error(url, *answer)
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        raise AssertionError(f"unexpected request to {url}")


def check(name, fake, value=None, config=CONFIG, env=SAVED):
    result = keytest.test_key(name, value, config, env, fetch=fake)
    assert result.state in keytest.STATES
    # Plain words, short enough that safe_message didn't cut them, and nothing redacted.
    assert len(result.message) <= 160 and not result.message.endswith("…"), result.message
    assert "REDACTED" not in result.message
    return result


# Live Departure Board.


def test_darwin_asks_for_one_row_of_the_configured_station():
    fake = Fake(("GetDepartureBoard", BOARD))
    result = check("RDM_API_KEY", fake)
    assert result == KeyResult("works", "It works: Live Departure Board answered for MAN.")
    [(url, headers)] = fake.calls
    assert url.startswith("https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/")
    assert "/GetDepartureBoard/MAN?" in url and parse_qs(urlsplit(url).query) == {"numRows": ["1"]}
    assert headers == {"x-apikey": RDM_KEY}


def test_a_section_switched_off_is_not_asked_about():
    # Its settings are kept for switching back on, but nothing is sent to them.
    elsewhere = "https://elsewhere.example.test/api"
    route = {"stop": "A", "stops": [{"name": "Glossop", "lat": 53.44, "lon": -1.95},
                                    {"name": "Hadfield", "lat": 53.46, "lon": -1.965}]}
    config = {"train": {"enabled": False, "crs": "MAN", "base_url": elsewhere},
              "bus": {"enabled": False, "stops": {"A": "1800SB12341"}, "routes": {"341": route}, "base_url": elsewhere}}
    darwin = Fake(("GetDepartureBoard", BOARD))
    assert check("RDM_API_KEY", darwin, config=config).message.endswith("answered for GLO.")
    assert darwin.calls[0][0].startswith("https://api1.raildata.org.uk/")
    tapi = Fake(("stop_timetables", TIMETABLE))
    check("TAPI_APP_KEY", tapi, config=config)
    assert tapi.calls[0][0].startswith("https://transportapi.com/v3/uk/bus/stop_timetables/1000DGHS0900.json?")
    bods = Fake(("datafeed", VEHICLES))
    check("BODS_API_KEY", bods, config=config)
    box = parse_qs(urlsplit(bods.calls[0][0]).query)["boundingBox"][0]
    assert box == "-1.95200,53.44084,-1.94600,53.44684"  # the fixed point, not the route strip's stop


def test_darwin_without_a_station_asks_about_glossop():
    fake = Fake(("GetDepartureBoard", BOARD))
    assert check("RDM_API_KEY", fake, config={"train": {"crs": "../x"}}).state == "works"
    assert "/GetDepartureBoard/GLO?" in fake.calls[0][0]


@pytest.mark.parametrize("answer, state", [
    ((401, RDM_INVALID), "wrong_key"),
    ((403, b"Forbidden"), "wrong_key"),
    ((401, RDM_OTHER_PRODUCT), "not_subscribed"),
    ((403, RDM_OTHER_PRODUCT), "not_subscribed"),
    ((500, b""), "service_down"),
    ((503, b"<html>down</html>"), "service_down"),
    ((404, b""), "unknown"),
    ((429, b""), "unknown"),
])
def test_darwin_refusals(answer, state):
    result = check("RDM_API_KEY", Fake(("GetDepartureBoard", answer)))
    assert result.state == state, result


def test_darwin_messages_say_what_to_do():
    assert "Consumer secret" in check("RDM_API_KEY", Fake(("Get", (401, RDM_INVALID)))).message
    message = check("RDM_API_KEY", Fake(("Get", (401, RDM_OTHER_PRODUCT)))).message
    assert "another Rail Data Marketplace product" in message and "Subscribe to Live Departure Board" in message
    assert "(error 503)" in check("RDM_API_KEY", Fake(("Get", (503, b"")))).message


@pytest.mark.parametrize("error, words", [
    (TimeoutError("timed out"), "took too long"),
    (TimeoutError("timed out"), "took too long"),
    (URLError(TimeoutError("timed out")), "took too long"),
    (URLError(ConnectionRefusedError(61, "refused")), "couldn't be reached"),
    (ConnectionResetError(54, "reset"), "couldn't be reached"),
])
def test_no_answer_is_service_down(error, words):
    result = check("RDM_API_KEY", Fake(("Get", error)))
    assert result.state == "service_down" and words in result.message


def test_no_answer_names_no_host():
    # The Pi's and the dashboard's wizards both show this, so it can't say "board".
    result = check("RDM_API_KEY", Fake(("Get", URLError(ConnectionRefusedError(61, "refused")))))
    assert result.message == ("The Rail Data Marketplace couldn't be reached. It may be down, or there may be no "
                              "internet here. Try again in a few minutes.")


def test_an_answer_that_is_not_json_is_unknown():
    assert check("RDM_API_KEY", Fake(("Get", b"<html>maintenance</html>"))).state == "unknown"
    assert check("RDM_API_KEY", Fake(("Get", b"[1, 2]"))).state == "unknown"


def test_a_pasted_key_is_tested_instead_of_the_saved_one():
    fake = Fake(("Get", BOARD))
    check("RDM_API_KEY", fake, value=" pasted-key-7777 ")
    assert fake.calls[0][1] == {"x-apikey": "pasted-key-7777"}


@pytest.mark.parametrize("name", ["RDM_API_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY", "BODS_API_KEY"])
def test_no_key_is_missing_and_asks_nothing(name):
    fake = Fake()
    for value in (None, "  "):
        result = check(name, fake, value=value, env={})
        assert result.state == "missing" and "Paste one" in result.message
    assert not fake.calls


def test_an_unknown_name_is_refused():
    with pytest.raises(ValueError):
        keytest.test_key("PATH", None, CONFIG, SAVED)


# Service Details.


def test_details_looks_up_a_train_from_the_board():
    fake = Fake(("GetDepartureBoard", BOARD), ("GetServiceDetails", DETAILS))
    assert check("RDM_DETAILS_API_KEY", fake).state == "works"
    (board_url, board_headers), (url, headers) = fake.calls
    assert board_headers == {"x-apikey": RDM_KEY}  # the board with its own key
    assert url.endswith("/GetServiceDetails/9000001GLSP____") and headers == {"x-apikey": DETAILS_KEY}
    assert url.startswith("https://api1.raildata.org.uk/1010-service-details1_2/")


@pytest.mark.parametrize("answer, state", [
    ((401, RDM_INVALID), "wrong_key"),
    ((401, RDM_OTHER_PRODUCT), "not_subscribed"),
    ((502, b""), "service_down"),
])
def test_details_refusals(answer, state):
    fake = Fake(("GetDepartureBoard", BOARD), ("GetServiceDetails", answer))
    assert check("RDM_DETAILS_API_KEY", fake).state == state


def test_details_with_no_trains_running_says_it_cannot_fully_check():
    empty = b'{"locationName": "Glossop", "crs": "GLO", "trainServices": null}'
    fake = Fake(("GetDepartureBoard", empty), ("GetServiceDetails", (404, b"")))
    result = check("RDM_DETAILS_API_KEY", fake)
    assert result.state == "unknown" and "accepted the key" in result.message and "no trains" in result.message
    assert fake.calls[1][0].endswith("/GetServiceDetails/000000000000000")


def test_details_without_a_board_key_still_checks_the_gateway():
    fake = Fake(("GetServiceDetails", b"null"))
    env = {"RDM_DETAILS_API_KEY": DETAILS_KEY}
    result = check("RDM_DETAILS_API_KEY", fake, env=env)
    assert result.state == "unknown" and "Live Departure Board key" in result.message
    assert len(fake.calls) == 1
    fake = Fake(("GetServiceDetails", (401, RDM_OTHER_PRODUCT)))
    assert check("RDM_DETAILS_API_KEY", fake, env=env).state == "not_subscribed"


def test_details_for_a_train_gone_since_the_board_is_accepted():
    fake = Fake(("GetDepartureBoard", BOARD), ("GetServiceDetails", (404, b"")))
    assert check("RDM_DETAILS_API_KEY", fake).state == "unknown"


# Knowledgebase incidents.


def test_kb_fetches_the_configured_address():
    fake = Fake((KB_URL, INCIDENTS))
    assert check("RDM_KB_API_KEY", fake) == KeyResult("works", "It works: Knowledgebase Incidents answered.")
    assert fake.calls[0][1]["x-apikey"] == KB_KEY


@pytest.mark.parametrize("answer, state", [
    ((401, RDM_INVALID), "wrong_key"),
    ((401, RDM_OTHER_PRODUCT), "not_subscribed"),
    (TimeoutError(), "service_down"),
    (b'{"not": "xml"}', "unknown"),
])
def test_kb_refusals(answer, state):
    assert check("RDM_KB_API_KEY", Fake((KB_URL, answer))).state == state


@pytest.mark.parametrize("url, words", [
    # The address can be saved on the Keys page as well as in config.toml: both are named.
    (None, "Set the engineering works address on the settings' Keys page"),
    ("https://set-kb-incidents-url-from-the-rdm-product-page.invalid/incidents",
     "Set the engineering works address on the settings' Keys page"),
    ("http://example.com/incidents", "The engineering works address isn't an https address"),
    ("https://user:pw@example.com/x", "The engineering works address isn't an https address"),
])
def test_kb_without_an_address_says_so(url, words):
    config = {"dashboard": {"kb_incidents_url": url}} if url else {}
    fake = Fake()
    result = check("RDM_KB_API_KEY", fake, config=config)
    assert result.state == "unknown" and words in result.message and not fake.calls


# Bus Open Data Service.


def test_bods_asks_for_a_small_box_around_the_route_strips_own_stops():
    fake = Fake(("datafeed", VEHICLES))
    assert check("BODS_API_KEY", fake).state == "works"
    [(url, headers)] = fake.calls
    query = parse_qs(urlsplit(url).query)
    assert url.startswith("https://data.bus-data.dft.gov.uk/api/v1/datafeed/?")
    assert query["api_key"] == [BODS_KEY]
    west, south, east, north = map(float, query["boundingBox"][0].split(","))
    assert west < -1.948999 < east and south < 53.44384 < north  # Henry Street, the strip's own stop
    assert not south < 53.46 < north  # not the whole route
    assert east - west < 0.01 and north - south < 0.01


def test_bods_without_route_strips_asks_about_a_fixed_point():
    fake = Fake(("datafeed", VEHICLES))
    check("BODS_API_KEY", fake, config={"bus": {"routes": {"1": {"stops": [{"lat": "x"}]}}}})
    box = parse_qs(urlsplit(fake.calls[0][0]).query)["boundingBox"][0]
    assert box == "-1.95200,53.44084,-1.94600,53.44684"


@pytest.mark.parametrize("answer, state", [
    ((401, b'{"detail":"Invalid token."}'), "wrong_key"),
    ((403, b""), "wrong_key"),
    ((500, b""), "service_down"),
    (URLError(TimeoutError("timed out")), "service_down"),
    (b'{"detail": "odd"}', "unknown"),
])
def test_bods_refusals(answer, state):
    assert check("BODS_API_KEY", Fake(("datafeed", answer))).state == state


# TransportAPI.


def test_tapi_asks_once_for_one_departure_from_a_configured_stop():
    fake = Fake(("stop_timetables", TIMETABLE))
    result = check("TAPI_APP_KEY", fake)
    assert result.state == "works" and "used 1 of today's lookups" in result.message
    [(url, _)] = fake.calls
    assert url.startswith("https://transportapi.com/v3/uk/bus/stop_timetables/1000DGHS0900.json?")
    query = parse_qs(urlsplit(url).query)
    assert query["app_id"] == [TAPI_ID] and query["app_key"] == [TAPI_KEY] and query["limit"] == ["1"]


def test_tapi_tests_the_pasted_one_with_the_saved_other():
    fake = Fake(("stop_timetables", TIMETABLE))
    check("TAPI_APP_ID", fake, value="new-id-1234", config={})
    query = parse_qs(urlsplit(fake.calls[0][0]).query)
    assert query["app_id"] == ["new-id-1234"] and query["app_key"] == [TAPI_KEY]
    assert "/stop_timetables/1000DGHS0900.json" in fake.calls[0][0]  # the fixed stop, with none configured


def test_tapi_needs_both_and_spends_nothing_without():
    fake = Fake()
    result = check("TAPI_APP_KEY", fake, env={"TAPI_APP_KEY": TAPI_KEY})
    assert result.state == "missing" and "app ID" in result.message and not fake.calls


@pytest.mark.parametrize("answer, state", [
    ((403, b'{"error": "Authorisation failed"}'), "wrong_key"),
    ((401, b""), "wrong_key"),
    (b'{"error": "Your app_id and app_key are not valid"}', "wrong_key"),
    ((403, b'{"error": "Usage limits are exceeded"}'), "unknown"),
    ((429, b""), "unknown"),
    (b'{"error": "daily quota reached"}', "unknown"),
    ((504, b""), "service_down"),
    (TimeoutError(), "service_down"),
])
def test_tapi_refusals(answer, state):
    assert check("TAPI_APP_KEY", Fake(("stop_timetables", answer))).state == state


# Keys never leave in a message or a log.


ECHOES = [
    lambda url, key: http_error(url, 401, f'{{"fault":"Invalid ApiKey {key}"}}'.encode()),
    lambda url, key: http_error(url, 500, f"x-apikey: {key}".encode()),
    lambda url, key: URLError(f"failed for {url} with {key}"),
    lambda url, key: ValueError(f"bad {key}"),
    lambda url, key: OSError(f"{url} {key}"),
]


@pytest.mark.parametrize("name", keytest.KEY_NAMES)
@pytest.mark.parametrize("echo", ECHOES)
def test_keys_never_appear_in_messages_or_logs(name, echo, caplog):
    caplog.set_level(logging.DEBUG)
    pasted = "pasted-secret-9999"

    def fake(url, headers):
        raise echo(url, pasted)

    config = dict(CONFIG)
    for value in (pasted, None):
        env = {**SAVED, name: pasted} if value is None else SAVED
        result = keytest.test_key(name, value, config, env, fetch=fake)
        assert pasted not in result.message and pasted[:8] not in result.message
        for key in SAVED.values():
            assert key not in result.message
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert caplog.records and pasted not in logged
    for key in SAVED.values():
        assert key not in logged


def test_the_literal_key_is_replaced_even_in_fixed_words():
    # A key that happens to be a word the messages use is still never shown.
    result = keytest.test_key("RDM_API_KEY", "works", CONFIG, {}, fetch=Fake(("Get", BOARD)))
    assert "works" not in result.message


# Seen live on 10 Oct 2026: BODS refuses Accept: application/xml with 406, and
# Darwin (past the gateway) answers a made-up train with its own 500.


def test_bods_asks_for_xml_the_way_bods_accepts():
    fake = Fake(("datafeed", VEHICLES))
    check("BODS_API_KEY", fake)
    accept = fake.calls[0][1]["Accept"]
    assert accept.startswith("text/xml") and "*/*" in accept


def test_details_darwin_s_own_error_for_a_made_up_train_means_the_key_got_through():
    empty = b'{"locationName": "Glossop", "crs": "GLO", "trainServices": null}'
    fake = Fake(("GetDepartureBoard", empty), ("GetServiceDetails", (500, b'{"Message":"Unexpected server error"}')))
    result = check("RDM_DETAILS_API_KEY", fake)
    assert result.state == "unknown" and "accepted the key" in result.message
    # The gateway's own failure still says the service is down.
    fake = Fake(("GetDepartureBoard", empty), ("GetServiceDetails", (500, b'{"fault":{"faultstring":"x"}}')))
    assert check("RDM_DETAILS_API_KEY", fake).state == "service_down"


# A service sending its answer slowly.


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_a_service_sending_slowly_is_given_up_at_the_deadline(drip_to, monkeypatch, path):
    # Each read gets a byte within the per-step timeout, so only the deadline stops it.
    monkeypatch.setattr(keytest, "CALL_DEADLINE", 0.3)
    drip_to(path)
    start = time.monotonic()
    result = keytest.test_key("BODS_API_KEY", BODS_KEY, CONFIG, {})
    assert result.state == "service_down" and "took too long" in result.message
    assert time.monotonic() - start < 1.0


def test_a_key_test_ends_before_the_wizard_stops_waiting(monkeypatch):
    seen = []
    monkeypatch.setattr(net, "urlopen", lambda request, timeout: seen.append(request) or io.BytesIO(BOARD))
    keytest.test_key("RDM_DETAILS_API_KEY", None, CONFIG, SAVED)
    assert len(seen) == 2  # the board for a train, then the train
    for request in seen:
        assert request.deadline <= time.monotonic() + keytest.CALL_DEADLINE
    # Every call at its deadline, with room for the fetch service to start
    # first (several seconds on a Pi Zero), within the wizard's wait.
    assert len(seen) * keytest.CALL_DEADLINE + 10 <= WAITS["testkey"]
