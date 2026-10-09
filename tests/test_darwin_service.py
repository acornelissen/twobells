"""GetServiceDetails: one train's whole journey, and where the train is now."""

import io
import json
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from departureboard import darwin, http
from departureboard.darwin import (
    CallingPoint,
    DetailsError,
    DetailsUnavailable,
    ServiceDetails,
    ServiceDetailsClient,
    ServiceGone,
    StationCall,
    TrainPosition,
    parse_service_details,
    train_position,
)

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "s3cr3t-details-key"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def point(name, st, **times):
    return {"locationName": name, "crs": "XXX", "st": st, "isCancelled": False, "length": 3, **times}


def raw_service(previous=(), subsequent=(), **here):
    base = {
        "locationName": "Glossop", "crs": "GLO", "sta": "22:49", "std": "22:52", "etd": "On time",
        "isCancelled": False, "length": 3, "generatedAt": "2026-10-09T22:40:00+01:00",
        "previousCallingPoints": [{"callingPoint": list(previous)}],
        "subsequentCallingPoints": [{"callingPoint": list(subsequent)}],
    }
    base.update(here)
    return base


def details(*args, **kwargs):
    return parse_service_details(raw_service(*args, **kwargs))


# The real answers, captured at Glossop one Friday night at 22:52.


def test_reads_a_train_that_started_at_hadfield():
    found = parse_service_details(load_fixture("ldbws_service_details_from_hadfield.json"))

    assert isinstance(found, ServiceDetails)
    assert found.previous == (CallingPoint("Hadfield", "HDF", "22:44", None, "On time", False, 3),)
    assert found.here == StationCall(
        "Glossop", "GLO", "22:52", "On time", None, False, 3,
        sta="22:49", eta=None, ata="On time", std="22:52", etd="On time", atd=None,
    )
    assert [p.name for p in found.subsequent] == [
        "Dinting", "Broadbottom", "Hattersley", "Godley", "Newton for Hyde", "Flowery Field",
        "Guide Bridge", "Gorton", "Ashburys", "Manchester Piccadilly",
    ]
    assert all(p.expected == "On time" and p.actual is None for p in found.subsequent)
    assert (found.length, found.cancel_reason, found.delay_reason) == (3, None, None)
    assert found.generated_at == "2026-10-09T07:00:00+01:00"


def test_reads_a_train_from_manchester_part_way_along():
    found = parse_service_details(load_fixture("ldbws_service_details_from_manchester.json"))

    assert [(p.name, p.scheduled, p.expected, p.actual) for p in found.previous] == [
        ("Manchester Piccadilly", "22:33", None, "On time"),
        ("Ashburys", "22:37", None, "On time"),
        ("Gorton", "22:39", None, "On time"),
        ("Guide Bridge", "22:43", None, "On time"),
        ("Flowery Field", "22:47", None, "No report"),
        ("Newton for Hyde", "22:49", "On time", None),
        ("Godley", "22:51", "On time", None),
        ("Hattersley", "22:53", "On time", None),
        ("Broadbottom", "22:56", "On time", None),
        ("Dinting", "23:01", "On time", None),
    ]
    assert (found.here.sta, found.here.eta, found.here.std, found.here.etd) == ("23:05", "On time", "23:07", "On time")
    assert found.here.ata is None and found.here.atd is None
    assert [p.name for p in found.subsequent] == ["Hadfield"]


def test_where_the_real_trains_were():
    now = datetime(2026, 10, 9, 22, 52, 19)
    hadfield = parse_service_details(load_fixture("ldbws_service_details_from_hadfield.json"))
    assert train_position(hadfield, now) == TrainPosition("At Glossop now", ("Hadfield", "Glossop", "Dinting"), 1, 1)

    manchester = parse_service_details(load_fixture("ldbws_service_details_from_manchester.json"))
    position = train_position(manchester, now)
    # Past Flowery Field, the sixth stop back: further back than the strip goes.
    assert position == TrainPosition(
        "Left Flowery Field",
        ("Newton for Hyde", "Godley", "Hattersley", "Broadbottom", "Dinting", "Glossop", "Hadfield"),
        5, None,
    )


# Where the train is


def test_not_started_yet_names_its_first_stop():
    found = details([point("Hadfield", "22:44", et="On time")], [point("Dinting", "22:55", et="On time")])
    assert train_position(found, datetime(2026, 10, 9, 22, 40)) == TrainPosition(
        "Not started yet: leaves Hadfield at 22:44", ("Hadfield", "Glossop", "Dinting"), 1, None,
    )
    late = details([point("Hadfield", "22:44", et="22:50")])
    assert train_position(late, datetime(2026, 10, 9, 22, 40)).where == "Not started yet: leaves Hadfield at 22:50"


def test_a_train_starting_here_has_not_started_from_here():
    found = details([], [point("Dinting", "22:55", et="On time")], etd="22:54")
    assert train_position(found, datetime(2026, 10, 9, 22, 40)) == TrainPosition(
        "Not started yet: leaves Glossop at 22:54", ("Glossop", "Dinting"), 0, None,
    )


def test_between_two_stops_it_is_placed_by_the_time():
    previous = [
        point("Guide Bridge", "22:30", at="On time"),
        point("Flowery Field", "22:34", et="On time"),
    ]
    found = details(previous)
    # Left at 22:30, due 22:34: at 22:31 it is a quarter of the way.
    position = train_position(found, datetime(2026, 10, 9, 22, 31))
    assert position == TrainPosition("Left Guide Bridge 22:30, on time", ("Guide Bridge", "Flowery Field", "Glossop"),
                                     2, 0.25)
    # Running late, it is never drawn at a stop it hasn't reached.
    assert train_position(found, datetime(2026, 10, 9, 22, 40)).train_at == 0.9
    # Seconds count too.
    assert train_position(found, datetime(2026, 10, 9, 22, 31, 30)).train_at == 0.375


def test_late_departures_are_said_in_minutes():
    found = details([point("Guide Bridge", "22:30", at="22:33"), point("Flowery Field", "22:34", et="22:37")])
    position = train_position(found, datetime(2026, 10, 9, 22, 35))
    assert position.where == "Left Guide Bridge, 3 min late"
    assert position.train_at == 0.5  # left 22:33, due 22:37
    early = details([point("Guide Bridge", "22:30", at="22:29")])
    assert train_position(early, datetime(2026, 10, 9, 22, 35)).where == "Left Guide Bridge 22:29, on time"


def test_between_the_last_stop_and_this_station_it_uses_the_arrival_time():
    found = details([point("Dinting", "22:45", at="On time")], sta="22:49", eta="22:51")
    position = train_position(found, datetime(2026, 10, 9, 22, 48))
    assert position.strip_stops == ("Dinting", "Glossop") and position.yours == 1
    assert position.train_at == 0.5  # left 22:45, due 22:51


def test_at_this_station_once_it_has_arrived():
    found = details([point("Dinting", "22:45", at="On time")], [point("Hadfield", "22:58", et="On time")],
                    ata="22:50")
    assert train_position(found, datetime(2026, 10, 9, 22, 51)) == TrainPosition(
        "At Glossop now", ("Dinting", "Glossop", "Hadfield"), 1, 1,
    )


def test_departed_once_it_has_left_this_station():
    found = details([point("Dinting", "22:45", at="On time")], [point("Hadfield", "22:58", et="On time")],
                    ata="22:50", atd="22:52")
    position = train_position(found, datetime(2026, 10, 9, 22, 55))
    assert position == TrainPosition("Departed Glossop", ("Dinting", "Glossop", "Hadfield"), 1, 1.5)
    arrived = details([], [point("Hadfield", "22:58", at="On time")], atd="On time")
    assert train_position(arrived, datetime(2026, 10, 9, 22, 59)).train_at == 1
    terminating = details([point("Dinting", "22:45", at="On time")], [], atd="On time")
    assert train_position(terminating, datetime(2026, 10, 9, 22, 59)).train_at == 1


def test_the_strip_shows_up_to_five_stops_before_this_station():
    previous = [point(f"Stop {i}", f"22:{10 + i:02d}", at="On time") for i in range(8)]
    position = train_position(details(previous, [point("Hadfield", "22:58")]), datetime(2026, 10, 9, 22, 20))
    assert position.strip_stops == ("Stop 3", "Stop 4", "Stop 5", "Stop 6", "Stop 7", "Glossop", "Hadfield")
    assert position.yours == 5
    assert position.where == "Left Stop 7 22:17, on time"
    assert 4 <= position.train_at < 5


def test_no_position_without_details_or_for_a_cancelled_train():
    assert train_position(None, datetime(2026, 10, 9, 22, 0)) is None
    assert train_position(details(isCancelled=True), datetime(2026, 10, 9, 22, 0)) is None
    assert train_position(details(etd="Cancelled"), datetime(2026, 10, 9, 22, 0)) is None


def test_a_point_marked_cancelled_has_not_been_passed():
    found = details([point("Dinting", "22:45", at="Cancelled", isCancelled=True)])
    assert train_position(found, datetime(2026, 10, 9, 22, 46)).where.startswith("Not started yet")


# Reading odd answers


@pytest.mark.parametrize("odd", [None, [], "service", 7, {}, {"locationName": "Glossop"},
                                 {"std": "22:52"}, {"locationName": "Glossop", "std": "25:00"}])
def test_an_answer_that_is_not_a_service_means_it_has_gone(odd):
    with pytest.raises(ServiceGone):
        parse_service_details(odd)


def test_wrong_typed_fields_are_dropped_not_raised():
    found = details(
        [point("Hadfield", "22:44", at=7), "Dinting", None, point(None, "22:45"), point("Dinting", "22.45")],
        [point("Dinting", "22:55", et=["On time"])],
        crs="glossop", eta=5, ata="soon", etd={"x": 1}, length="3", isCancelled="yes",
        cancelReason=["x"], delayReason={"value": "<b>Signalling</b> fault"}, generatedAt="tonight",
    )
    assert found.previous == (CallingPoint("Hadfield", "XXX", "22:44", None, None, False, 3),)
    assert found.subsequent == (CallingPoint("Dinting", "XXX", "22:55", None, None, False, 3),)
    here = found.here
    assert (here.crs, here.eta, here.ata, here.etd, here.length, here.cancelled) == (None, None, None, None, None, False)
    assert (found.cancel_reason, found.delay_reason, found.generated_at) == (None, "Signalling fault", None)
    assert found.length is None


def test_names_are_cleaned_and_lists_capped():
    many = [point(f"Stop {i}", "22:30", at="On time") for i in range(400)]
    found = details(many, locationName="Glossop\x1b[0m‮ " + "x" * 200)
    assert len(found.previous) == 150
    assert found.here.name.startswith("Glossop[0m x") and len(found.here.name) == darwin.NAME_LIMIT


def test_a_train_that_ends_here_uses_its_arrival():
    found = details([point("Dinting", "22:45", at="On time")], [], std=None, etd=None, sta="22:49", eta="22:51")
    assert (found.here.scheduled, found.here.expected) == ("22:49", "22:51")
    assert train_position(found, datetime(2026, 10, 9, 22, 48)).train_at == 0.5


# The client


def recording(answer):
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return calls, fake_get


def client(answer):
    calls, fake_get = recording(answer)
    return calls, ServiceDetailsClient(KEY, base_url="https://example.test/", http_get=fake_get)


def test_asks_for_the_service_by_its_quoted_id_with_the_key_in_a_header():
    calls, details_client = client(load_fixture("ldbws_service_details_from_hadfield.json"))
    found = details_client.details("9000001GLSP____")
    assert calls == [("https://example.test/GetServiceDetails/9000001GLSP____", {"x-apikey": KEY})]
    assert found.here.name == "Glossop"
    calls, details_client = client(load_fixture("ldbws_service_details_from_hadfield.json"))
    details_client.details("ab+c/d=e_-")
    assert calls[0][0] == "https://example.test/GetServiceDetails/ab%2Bc%2Fd%3De_-"


def test_the_default_address_is_the_service_details_product():
    assert darwin.DETAILS_BASE_URL.startswith("https://api1.raildata.org.uk/1010-service-details1_2/")
    calls, fake_get = recording(raw_service())
    ServiceDetailsClient(KEY, http_get=fake_get).details("9000001GLSP____")
    assert calls[0][0] == darwin.DETAILS_BASE_URL + "/GetServiceDetails/9000001GLSP____"


@pytest.mark.parametrize("bad", ["../x", "a?b", "%2e", "x" * 65, "short", "9000001GLSP\n___", "a b c d e f",
                                 9000001, None, ["9000001GLSP____"]])
def test_odd_service_ids_are_refused_before_any_request(bad):
    calls, details_client = client(raw_service())
    with pytest.raises(ValueError):
        details_client.details(bad)
    assert calls == []


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_means_details_are_unavailable(code):
    _, details_client = client(HTTPError(f"https://example.test/x?key={KEY}", code, "Forbidden", {}, None))
    with pytest.raises(DetailsUnavailable) as raised:
        details_client.details("9000001GLSP____")
    assert KEY not in str(raised.value) and str(code) in str(raised.value)


def test_not_found_means_the_service_has_gone():
    _, details_client = client(HTTPError("https://example.test/x", 404, "Not Found", {}, None))
    with pytest.raises(ServiceGone):
        details_client.details("9000001GLSP____")


@pytest.mark.parametrize("error", [
    HTTPError("https://example.test/x", 500, f"Server error for x-apikey: {KEY}", {}, None),
    URLError(f"no route to https://example.test/GetServiceDetails/x?apikey={KEY}"),
    OSError(f"timed out talking about {KEY}"),
    RuntimeError(""),
])
def test_other_failures_never_carry_the_key_or_a_query(error):
    _, details_client = client(error)
    with pytest.raises(DetailsError) as raised:
        details_client.details("9000001GLSP____")
    assert type(raised.value) is DetailsError
    text = str(raised.value)
    assert KEY not in text and "apikey=" not in text and text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(monkeypatch, body):
    seen = []

    def fake_urlopen(request, timeout):
        seen.append(request)
        return Response(body)

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    return seen


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff\xfe", b"[" * 10_000 + b"]" * 10_000])
def test_an_empty_or_unreadable_answer_means_the_service_has_gone(monkeypatch, body):
    serve(monkeypatch, body)
    with pytest.raises(ServiceGone):
        ServiceDetailsClient(KEY).details("9000001GLSP____")


def test_an_oversized_answer_is_refused_without_the_key(monkeypatch):
    seen = serve(monkeypatch, b" " * (darwin.DETAILS_MAX_BYTES + 1))
    with pytest.raises(DetailsError) as raised:
        ServiceDetailsClient(KEY).details("9000001GLSP____")
    assert type(raised.value) is DetailsError and "larger than" in str(raised.value)
    assert KEY not in str(raised.value)
    assert seen[0].get_header("X-apikey") == KEY


def test_a_real_sized_answer_is_read_through_the_capped_request(monkeypatch):
    serve(monkeypatch, (FIXTURES / "ldbws_service_details_from_manchester.json").read_bytes())
    found = ServiceDetailsClient(KEY).details("9000001GLSP____")
    assert len(found.previous) == 10
