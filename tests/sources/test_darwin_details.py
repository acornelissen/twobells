import json

import pytest

from departureboard.core.model import Status
from departureboard.sources import darwin
from departureboard.sources.darwin import (
    CallingPoint,
    DarwinSource,
    TrainBoard,
    parse_board,
    parse_board_rich,
)
from tests.paths import FIXTURES


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def point(name="Dinting", st="22:25", **overrides):
    base = {"locationName": name, "crs": "DTG", "st": st, "et": "On time", "isCancelled": False, "length": 3}
    base.update(overrides)
    return base


def service(**overrides):
    base = {
        "std": "22:22",
        "etd": "On time",
        "platform": "1",
        "operator": "Northern",
        "operatorCode": "NT",
        "isCancelled": False,
        "length": 3,
        "serviceID": "9000002GLSP____",
        "origin": [{"locationName": "Hadfield", "crs": "HDF"}],
        "destination": [{"locationName": "Manchester Piccadilly", "crs": "MAN"}],
        "subsequentCallingPoints": [
            {"callingPoint": [point(), point("Manchester Piccadilly", "22:54", crs="MAN")]}
        ],
    }
    base.update(overrides)
    return base


def rich(*services, **board):
    return parse_board_rich({"trainServices": list(services), **board})


def one(**overrides):
    [train] = rich(service(**overrides)).services
    return train


# A synthetic board, shaped like GetDepBoardWithDetails at Glossop one Friday night.


def test_parses_a_detailed_glossop_board():
    board = parse_board_rich(load_fixture("ldbws_glossop_details.json"))

    assert isinstance(board, TrainBoard)
    assert board.generated_at == "2026-10-09T22:05:38.2500000+01:00"
    assert board.location == "Glossop"
    assert board.messages == ["Help points at this station are out of use."]
    assert [(s.departure.scheduled, s.service_id, s.origin, s.departure.destination) for s in board.services] == [
        ("22:08", "9000001GLSP____", "Manchester Piccadilly", "Hadfield"),
        ("22:25", "9000002GLSP____", "Hadfield", "Manchester Piccadilly"),
        ("22:38", "9000003GLSP____", "Manchester Piccadilly", "Hadfield"),
        ("22:55", "9000004GLSP____", "Hadfield", "Manchester Piccadilly"),
        ("23:10", "9000005GLSP____", "Manchester Piccadilly", "Hadfield"),
    ]
    for train in board.services:
        assert train.length == 2
        assert train.operator_code == "NT"
        assert train.departure.service == "Northern"
        assert train.departure.status is Status.ON_TIME
        assert train.departure.platform == "2"
        assert train.cancel_reason is None and train.delay_reason is None


def test_the_board_has_the_stops_after_glossop_with_times():
    board = parse_board_rich(load_fixture("ldbws_glossop_details.json"))
    to_hadfield, to_manchester = board.services[0], board.services[1]

    assert to_hadfield.calling_points == (
        CallingPoint("Hadfield", "HDF", "22:14", "On time", None, False, 2),
    )
    assert [(p.name, p.crs, p.scheduled) for p in to_manchester.calling_points] == [
        ("Dinting", "DTG", "22:28"),
        ("Broadbottom", "BDB", "22:32"),
        ("Hattersley", "HTY", "22:35"),
        ("Godley", "GDL", "22:37"),
        ("Newton for Hyde", "NWN", "22:39"),
        ("Flowery Field", "FLF", "22:41"),
        ("Guide Bridge", "GUI", "22:44"),
        ("Ashburys", "ABY", "22:51"),
        ("Manchester Piccadilly", "MAN", "22:57"),
    ]
    assert all(p.expected == "On time" and p.actual is None for p in to_manchester.calling_points)
    assert all(not p.cancelled and p.length == 2 for p in to_manchester.calling_points)
    # The 22:55 also calls at Gorton.
    assert "Gorton" in [p.name for p in board.services[3].calling_points]


def test_rich_departures_match_the_plain_parse_of_the_same_board():
    payload = load_fixture("ldbws_glossop_details.json")
    assert [s.departure for s in parse_board_rich(payload).services] == parse_board(payload)


# Services built here: the cases the board above doesn't have.


def test_cancelled_train_with_its_reason():
    train = one(
        etd="Cancelled",
        isCancelled=True,
        cancelReason="This train has been cancelled because of a shortage of train crew",
        subsequentCallingPoints=[
            {"callingPoint": [point(et="Cancelled", isCancelled=True)]}
        ],
    )
    assert train.departure.status is Status.CANCELLED
    assert train.cancel_reason == "This train has been cancelled because of a shortage of train crew"
    assert train.delay_reason is None
    [stop] = train.calling_points
    assert stop.cancelled is True and stop.expected == "Cancelled"


def test_delayed_train_with_its_reason_and_estimates():
    train = one(
        etd="22:31",
        delayReason="<p>This train has been delayed by a fault with the signalling system</p>",
        subsequentCallingPoints=[
            {"callingPoint": [point(et="22:34"), point("Broadbottom", "22:29", et="Delayed", at="22:30")]}
        ],
    )
    assert train.departure.status is Status.LATE
    assert train.departure.expected == "22:31"
    assert train.delay_reason == "This train has been delayed by a fault with the signalling system"
    assert [(p.expected, p.actual) for p in train.calling_points] == [("22:34", None), ("Delayed", "22:30")]


def test_reasons_wrapped_in_an_object_are_read_too():
    train = one(cancelReason={"value": "A late-running freight train"}, delayReason={"Value": "Flooding"})
    assert train.cancel_reason == "A late-running freight train"
    assert train.delay_reason == "Flooding"


def test_reasons_of_the_wrong_shape_are_dropped_and_long_ones_cut():
    for odd in (None, 7, True, [], ["text"], {"value": 3}, {"other": "x"}, "", "<p> </p>"):
        assert one(cancelReason=odd, delayReason=odd).cancel_reason is None, odd
    long = one(delayReason="Signalling problems near Guide Bridge. " * 20).delay_reason
    assert len(long) <= darwin.REASON_LIMIT and long.endswith("…")


def test_unknown_time_words_become_none():
    train = one(
        etd="Starts here",
        subsequentCallingPoints=[
            {"callingPoint": [point(et="Soon", at="24:00"), point("Broadbottom", "22:29", et="No report", at=7)]}
        ],
    )
    assert train.departure.status is Status.UNKNOWN
    assert train.departure.expected is None
    assert [(p.expected, p.actual) for p in train.calling_points] == [(None, None), ("No report", None)]


@pytest.mark.parametrize(
    "bad",
    ["../x", "a?b", "%2e", "x" * 65, "short", "9000002GLSP\n___", 9000002, None, ["9000002GLSP____"]],
)
def test_odd_service_ids_are_dropped(bad):
    assert one(serviceID=bad).service_id is None


def test_service_ids_in_darwins_alphabet_are_kept():
    for good in ("9000002GLSP____", "abc+/=-_09", "x" * 64):
        assert one(serviceID=good).service_id == good


def test_coach_counts_must_be_whole_and_plausible():
    for bad in ("3", 3.0, True, 0, -1, 31, None, [3]):
        assert one(length=bad).length is None, bad
    assert one(length=1).length == 1 and one(length=30).length == 30
    [stop] = one(subsequentCallingPoints=[{"callingPoint": [point(length="4")]}]).calling_points
    assert stop.length is None


def test_calling_points_are_capped():
    many = [point(f"Stop {i}", "22:30") for i in range(500)]
    train = one(subsequentCallingPoints=[{"callingPoint": many}])
    assert len(train.calling_points) == 150
    assert train.calling_points[-1].name == "Stop 149"


def test_names_are_cleaned_and_cut():
    train = one(
        origin=[{"locationName": "Hadfield\x1b[31m‮", "crs": "hdf"}],
        destination=[{"locationName": "Manchester Piccadilly\n", "crs": "MAN"}],
        subsequentCallingPoints=[{"callingPoint": [point("A" * 300, crs="DTGX")]}],
    )
    assert train.origin == "Hadfield[31m"
    assert train.departure.destination == "Manchester Piccadilly"
    [stop] = train.calling_points
    assert stop.name == "A" * darwin.NAME_LIMIT
    assert stop.crs is None


def test_calling_points_without_a_name_or_booked_time_are_skipped():
    points = [
        point(locationName=None),
        point(st="22.25"),
        point(st=None),
        "Dinting",
        7,
        point("Godley", "22:34"),
    ]
    train = one(subsequentCallingPoints=[{"callingPoint": points}])
    assert [p.name for p in train.calling_points] == ["Godley"]


def test_odd_calling_point_shapes_are_empty_not_errors():
    for odd in (None, 7, "x", [None, 3, "x"], [{"callingPoint": "x"}], [{"callingPoint": None}], {"x": 1}):
        assert one(subsequentCallingPoints=odd).calling_points == (), odd
    # A single object where a list belongs is read as a list of one.
    single = one(subsequentCallingPoints={"callingPoint": point()})
    assert [p.name for p in single.calling_points] == ["Dinting"]


def test_a_dividing_train_keeps_the_part_going_to_the_destination():
    to_hadfield = [point("Dinting", "22:25"), point("Hadfield", "22:31", crs="HDF")]
    to_glossop = [point("Dinting", "22:25"), point("Glossop", "22:29", crs="GLO")]
    parts = [{"callingPoint": to_glossop}, {"callingPoint": to_hadfield}]
    train = one(
        destination=[{"locationName": "Hadfield", "crs": "HDF"}],
        subsequentCallingPoints=parts,
    )
    assert train.calling_points[-1].name == "Hadfield"
    # Matched by name when Darwin leaves off the code.
    by_name = one(destination=[{"locationName": "Hadfield"}], subsequentCallingPoints=parts)
    assert by_name.calling_points[-1].name == "Hadfield"
    # With no part ending at the destination, the first part is kept.
    neither = one(destination=[{"locationName": "Buxton", "crs": "BUX"}], subsequentCallingPoints=parts)
    assert neither.calling_points[-1].name == "Glossop"


def test_services_without_a_booked_time_are_skipped():
    board = rich(service(std=None), service(std="25:00"), service(std=["22:22"]), "x", 7, service())
    assert [s.departure.scheduled for s in board.services] == ["22:22"]


def test_wrong_typed_service_fields_are_dropped_not_raised():
    train = one(
        operator={"name": "Northern"},
        operatorCode="<b>",
        origin="Hadfield",
        destination=[None, 7, {"locationName": 5}, {"locationName": "Hadfield"}],
        isCancelled="yes",
        etd=["On time"],
        platform=1,
    )
    assert train.departure.service == ""
    assert train.operator_code == ""
    assert train.origin == ""
    assert train.departure.destination == "Hadfield"
    assert train.departure.status is Status.UNKNOWN
    assert train.departure.platform is None


def test_board_level_oddities():
    assert rich().services == []
    assert parse_board_rich({}).services == []
    assert parse_board_rich({"trainServices": None, "generatedAt": "yesterday"}).generated_at is None
    assert parse_board_rich({"locationName": ["Glossop"]}).location is None
    for odd in (None, [], "board", 7):
        with pytest.raises(ValueError):
            parse_board_rich(odd)


# The source.


def recording(payload):
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return payload

    return calls, fake_get


def test_plain_fetch_still_asks_for_the_departure_board():
    calls, fake_get = recording({"trainServices": [service()]})
    source = DarwinSource(api_key="k", crs="GLO", rows=10, base_url="https://example.test/", http_get=fake_get)
    [dep] = source.fetch()
    assert calls == [("https://example.test/GetDepartureBoard/GLO?numRows=10", {"x-apikey": "k"})]
    assert dep.destination == "Manchester Piccadilly"


def test_detailed_source_asks_for_the_board_with_details():
    calls, fake_get = recording(load_fixture("ldbws_glossop_details.json"))
    source = DarwinSource(
        api_key="k", crs="GLO", filter_crs="MAN", rows=10,
        base_url="https://example.test", http_get=fake_get, with_details=True,
    )

    board = source.fetch_board()

    assert calls == [(
        "https://example.test/GetDepBoardWithDetails/GLO?numRows=10&filterCrs=MAN&filterType=to",
        {"x-apikey": "k"},
    )]
    assert len(board.services) == 5 and board.services[1].calling_points
    assert source.messages == board.messages


def test_detailed_fetch_gives_the_departures_of_the_rich_board():
    payload = load_fixture("ldbws_glossop_details.json")
    _, fake_get = recording(payload)
    source = DarwinSource(api_key="k", crs="GLO", http_get=fake_get, with_details=True)
    assert source.fetch() == [s.departure for s in parse_board_rich(payload).services]
    assert source.messages == ["Help points at this station are out of use."]


def test_detailed_source_uses_a_size_capped_request_by_default(monkeypatch):
    seen = []
    monkeypatch.setattr(
        darwin, "get_json_capped", lambda url, headers, max_bytes: seen.append(max_bytes) or {}
    )
    monkeypatch.setattr(darwin, "get_json", lambda url, headers: pytest.fail("uncapped request"))
    assert DarwinSource(api_key="k", crs="GLO", with_details=True).fetch_board().services == []
    assert seen == [darwin.DETAILS_MAX_BYTES]
