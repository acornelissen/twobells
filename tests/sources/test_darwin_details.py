import json

import pytest

from tests.paths import FIXTURES
from twobells.core.model import Status
from twobells.sources import darwin
from twobells.sources.darwin import (
    CallingPoint,
    DarwinSource,
    Message,
    TrainBoard,
    parse_board,
    parse_board_rich,
)


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
    # No category in this one: its words make it station information.
    assert board.messages == [Message("Help points at this station are out of use.", None, None, "station")]
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


def test_a_train_whose_every_later_stop_is_cancelled_says_it_no_longer_calls_there():
    points = [point(isCancelled=True), point("Manchester Piccadilly", "22:54", crs="MAN", et="Cancelled")]
    train = one(subsequentCallingPoints=[{"callingPoint": points}])
    assert train.departure.calling_at == (
        "Does not call at Dinting or Manchester Piccadilly. This train is formed of 3 coaches.")


def test_a_dividing_train_calls_at_every_part_s_stops_in_time_order():
    to_hadfield = [point("Dinting", "22:25"), point("Hadfield", "22:31", crs="HDF")]
    to_glossop = [point("Dinting", "22:25"), point("Glossop", "22:29", crs="GLO")]
    parts = [{"callingPoint": to_hadfield}, {"callingPoint": to_glossop}]
    train = one(
        destination=[{"locationName": "Hadfield", "crs": "HDF"}, {"locationName": "Glossop", "crs": "GLO"}],
        subsequentCallingPoints=parts,
    )
    # Each destination's stops are named, the shared ones once.
    assert [p.name for p in train.calling_points] == ["Dinting", "Glossop", "Hadfield"]
    assert train.departure.calling_at == "Calling at: Dinting, Glossop and Hadfield. This train is formed of 3 coaches."


def test_a_dividing_train_s_stops_are_merged_across_midnight_and_one_part_is_kept_as_it_is():
    before = [point("Dinting", "23:55"), point("Hadfield", "00:05", crs="HDF")]
    after = [point("Dinting", "23:55"), point("Glossop", "23:59", crs="GLO"), point("Padfield", "00:02")]
    train = one(std="23:50", subsequentCallingPoints=[{"callingPoint": before}, {"callingPoint": after}])
    assert [p.name for p in train.calling_points] == ["Dinting", "Glossop", "Padfield", "Hadfield"]
    # One part is Darwin's order, whatever its times say.
    odd = [point("Godley", "22:40"), point("Dinting", "22:25")]
    assert [p.name for p in one(subsequentCallingPoints=[{"callingPoint": odd}]).calling_points] == [
        "Godley", "Dinting"]


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


# Rail replacement buses: Darwin lists them under busServices, shaped like
# its trains but with serviceType "bus" and no coaches.


def bus(**overrides):
    base = service(serviceType="bus", length=0, platform="BUS", serviceID="9000009GLSP____",
                   subsequentCallingPoints=None)
    base.update(overrides)
    return base


def test_a_board_of_replacement_buses_alone_has_them():
    board = parse_board_rich({"locationName": "Glossop", "busServices": [bus(), bus(std="22:52")]})
    assert [s.departure.scheduled for s in board.services] == ["22:22", "22:52"]
    first = board.services[0]
    assert first.departure.mode == "train"  # it stands in for a train, in the train section
    assert first.departure.replacement_bus is True
    assert first.departure.destination == "Manchester Piccadilly"
    assert first.departure.status is Status.ON_TIME
    assert first.departure.platform is None  # a bus has no platform; "BUS" isn't one
    assert first.length is None
    assert first.service_id == "9000009GLSP____"


def test_the_plain_board_reads_replacement_buses_too():
    [dep] = parse_board({"busServices": [bus(etd="22:30")]})
    assert dep.replacement_bus is True
    assert dep.status is Status.LATE and dep.expected == "22:30"


def test_trains_are_not_replacement_buses():
    assert one().departure.replacement_bus is False


def test_a_service_darwin_calls_a_bus_is_one_whichever_list_it_is_in():
    assert one(serviceType="bus").departure.replacement_bus is True
    assert one(serviceType=["bus"]).departure.replacement_bus is False


def test_trains_and_replacement_buses_are_merged_by_time():
    board = parse_board_rich({
        "trainServices": [service(std="22:08"), service(std="22:38")],
        "busServices": [bus(std="22:22"), bus(std="22:52")],
    })
    times = [(s.departure.scheduled, s.departure.replacement_bus) for s in board.services]
    assert times == [("22:08", False), ("22:22", True), ("22:38", False), ("22:52", True)]


def test_the_merge_keeps_times_after_midnight_after_those_before():
    board = parse_board_rich({
        "trainServices": [service(std="23:50"), service(std="00:20")],
        "busServices": [bus(std="00:05")],
    })
    assert [s.departure.scheduled for s in board.services] == ["23:50", "00:05", "00:20"]


def test_replacement_buses_are_checked_like_trains():
    board = parse_board_rich({"busServices": [bus(std=None), "x", 7, bus(operator={"name": "Northern"})]})
    [only] = board.services
    assert only.departure.service == ""
    assert parse_board_rich({"busServices": "22:22"}).services == []
    assert parse_board_rich({"busServices": bus()}).services[0].departure.replacement_bus is True


# Ferries: Darwin lists them under ferryServices, shaped like its trains but
# with serviceType "ferry" and no coaches. A ferry is a connection of its
# own, not a replacement for a train.


def ferry(**overrides):
    base = service(serviceType="ferry", length=0, platform="", serviceID="9000010PMH_____",
                   operator="Wightlink", operatorCode="WL", origin=[{"locationName": "Portsmouth Harbour"}],
                   destination=[{"locationName": "Ryde Pier Head", "crs": "RYP"}],
                   subsequentCallingPoints=[{"callingPoint": [point("Ryde Pier Head", "22:44", crs="RYP")]}])
    base.update(overrides)
    return base


def test_a_board_of_ferries_alone_has_them():
    board = parse_board_rich({"locationName": "Portsmouth Harbour", "ferryServices": [ferry(), ferry(std="23:15")]})
    assert [s.departure.scheduled for s in board.services] == ["22:22", "23:15"]
    first = board.services[0]
    assert first.departure.mode == "train"  # listed with the trains, in the train section
    assert first.departure.ferry is True
    assert first.departure.replacement_bus is False  # a connection, not a stand-in
    assert first.departure.destination == "Ryde Pier Head"
    assert first.departure.service == "Wightlink"
    assert first.departure.platform is None
    assert first.length is None
    assert first.service_id == "9000010PMH_____"


def test_a_ferry_has_no_platform_whatever_darwin_puts_there():
    assert parse_board_rich({"ferryServices": [ferry(platform="2")]}).services[0].departure.platform is None


def test_the_plain_board_reads_ferries_too():
    [dep] = parse_board({"ferryServices": [ferry(etd="22:30")]})
    assert dep.ferry is True
    assert dep.status is Status.LATE and dep.expected == "22:30"


def test_trains_and_replacement_buses_are_not_ferries():
    assert one().departure.ferry is False
    assert parse_board_rich({"busServices": [bus()]}).services[0].departure.ferry is False


def test_a_service_darwin_calls_a_ferry_is_one_whichever_list_it_is_in():
    assert one(serviceType="ferry").departure.ferry is True
    assert one(serviceType=["ferry"]).departure.ferry is False
    [found] = parse_board_rich({"busServices": [bus(serviceType="ferry")]}).services
    assert (found.departure.ferry, found.departure.replacement_bus) == (True, False)
    [found] = parse_board_rich({"ferryServices": [ferry(serviceType="bus")]}).services
    assert (found.departure.ferry, found.departure.replacement_bus) == (False, True)


def test_trains_buses_and_ferries_are_merged_by_time():
    board = parse_board_rich({
        "trainServices": [service(std="22:08"), service(std="22:38")],
        "busServices": [bus(std="22:52")],
        "ferryServices": [ferry(std="22:22")],
    })
    kinds = [(s.departure.scheduled, s.departure.replacement_bus, s.departure.ferry) for s in board.services]
    assert kinds == [("22:08", False, False), ("22:22", False, True), ("22:38", False, False),
                     ("22:52", True, False)]


def test_ferries_are_checked_like_trains():
    board = parse_board_rich({"ferryServices": [ferry(std="22:2"), None, ferry(operator=["Wightlink"])]})
    [only] = board.services
    assert only.departure.service == ""
    assert parse_board_rich({"ferryServices": "22:22"}).services == []
    assert parse_board_rich({"ferryServices": ferry()}).services[0].departure.ferry is True


# A train's formation (LDBWS FormationData), which some operators send:
# its coaches from the front, each with its class, toilet and loading.


def coach(number="1", **overrides):
    base = {"number": number, "coachClass": "Standard", "toilet": {"value": "Standard", "status": "InService"},
            "loading": 20, "loadingSpecified": True}
    base.update(overrides)
    return base


def test_reads_the_coaches_of_a_train_from_the_front():
    train = one(formation={"coaches": [
        coach("A", coachClass="First", toilet={"value": "None", "status": "Unknown"}, loading=12),
        coach("B", toilet={"value": "Accessible", "status": "InService"}, loading=55),
        coach("C", coachClass="Mixed", toilet={"value": "Standard", "status": "NotInService"}, loading=90),
    ]}, detachFront=True, isReverseFormation=True)
    assert train.formation == darwin.Formation(coaches=(
        darwin.Coach("A", "first", None, False, 12),
        darwin.Coach("B", "standard", "accessible", False, 55),
        darwin.Coach("C", "mixed", "standard", True, 90),
    ), average_loading=None)
    assert train.detach_front is True and train.reverse_formation is True


def test_a_train_without_a_formation_has_none():
    train = one()
    assert train.formation is None
    assert train.detach_front is False and train.reverse_formation is False
    for empty in (None, {}, {"coaches": []}, {"coaches": None}, {"loadingCategory": {"code": "1"}}):
        assert one(formation=empty).formation is None, empty


def test_darwin_s_words_are_read_in_any_case_and_other_shapes_too():
    train = one(formation={"coaches": {"coach": [coach(coachClass=" FIRST ", toilet="accessible")]}})
    [found] = train.formation.coaches
    assert (found.coach_class, found.toilet, found.toilet_out_of_order) == ("first", "accessible", False)
    single = one(formation={"coaches": coach(toilet={"Value": "Standard", "Status": "notinservice"})})
    assert single.formation.coaches[0].toilet == "standard" and single.formation.coaches[0].toilet_out_of_order
    assert one(formation={"avgLoading": 40}).formation == darwin.Formation((), 40)


@pytest.mark.parametrize("field, bad", [
    ("number", "ABC"), ("number", 1), ("number", "<b"), ("number", ""),
    ("coachClass", "Premium"), ("coachClass", ["First"]), ("coachClass", "constructor"),
    ("toilet", {"value": "Bucket", "status": "InService"}), ("toilet", ["Accessible"]), ("toilet", 3),
    ("loading", 101), ("loading", -1), ("loading", "50"), ("loading", 50.5), ("loading", True),
])
def test_odd_coach_fields_are_left_out_not_raised(field, bad):
    [found] = one(formation={"coaches": [coach(**{field: bad})]}).formation.coaches
    expected = {"number": "1", "coach_class": "standard", "toilet": "standard", "loading": 20}
    expected[{"coachClass": "coach_class"}.get(field, field)] = None
    assert (found.number, found.coach_class, found.toilet, found.loading) == tuple(expected.values())


def test_a_loading_darwin_says_isn_t_there_is_not_read():
    [found] = one(formation={"coaches": [coach(loading=0, loadingSpecified=False)]}).formation.coaches
    assert found.loading is None


def test_a_toilet_with_no_type_is_never_out_of_order():
    [found] = one(formation={"coaches": [coach(toilet={"value": "Unknown", "status": "NotInService"})]}).formation.coaches
    assert found.toilet is None and found.toilet_out_of_order is False


def test_a_formation_with_a_coach_that_isn_t_one_is_left_out():
    # A gap would put every coach after it in the wrong place.
    for odd in ("Coach 2", 7, None, ["x"]):
        assert one(formation={"coaches": [coach("1"), odd, coach("3")]}).formation is None, odd
    assert one(formation={"coaches": [coach(str(i)) for i in range(31)]}).formation is None
    assert len(one(formation={"coaches": [coach(str(i)) for i in range(30)]}).formation.coaches) == 30
    for odd in ("formation", 7, ["x"], {"coaches": "x"}, {"avgLoading": 101}, {"avgLoading": "40"}):
        assert one(formation=odd).formation is None, odd


def test_flags_must_be_true_not_merely_truthy():
    for odd in ("true", 1, [True], {"x": 1}):
        train = one(detachFront=odd, isReverseFormation=odd)
        assert train.detach_front is False and train.reverse_formation is False, odd


def test_a_replacement_bus_has_no_formation():
    board = parse_board_rich({"busServices": [bus(formation={"coaches": [coach()]}, detachFront=True,
                                                  isReverseFormation=True)]})
    [found] = board.services
    assert found.formation is None and found.detach_front is False and found.reverse_formation is False


def test_a_ferry_has_no_formation():
    board = parse_board_rich({"ferryServices": [ferry(formation={"coaches": [coach()]}, detachFront=True,
                                                      isReverseFormation=True)]})
    [found] = board.services
    assert found.formation is None and found.detach_front is False and found.reverse_formation is False


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
    assert source.messages == [m.text for m in board.messages]  # the e-paper board's notice takes the text


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
    assert DarwinSource(api_key="k", crs="GLO", with_details=True).fetch_board().services == []
    assert seen == [darwin.DETAILS_MAX_BYTES]


@pytest.mark.parametrize("with_details", [False, True])
def test_a_fetch_keeps_its_services_for_where_the_trains_are(with_details):
    payload = load_fixture("ldbws_glossop_details.json")
    _, fake_get = recording(payload)
    source = DarwinSource(api_key="k", crs="GLO", http_get=fake_get, with_details=with_details)
    assert source.services == []
    departures = source.fetch()
    assert [s.service_id for s in source.services][:2] == ["9000001GLSP____", "9000002GLSP____"]
    assert [s.departure for s in source.services] == departures


def test_a_failed_fetch_keeps_the_last_services():
    payload = load_fixture("ldbws_glossop_details.json")
    answers = [payload, OSError("down")]

    def fake_get(url, headers):
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    source = DarwinSource(api_key="k", crs="GLO", http_get=fake_get)
    source.fetch()
    with pytest.raises(OSError):
        source.fetch()
    assert len(source.services) == 5


# The "Calling at" line (core/calling.py), carried with the departure so the
# board can save it and a screen scroll it.


def test_each_train_has_its_calling_at_line():
    board = parse_board_rich(load_fixture("ldbws_glossop_details.json"))
    assert board.services[1].departure.calling_at == (
        "Calling at: Dinting, Broadbottom, Hattersley, Godley, Newton for Hyde, Flowery Field, Guide Bridge, "
        "Ashburys and Manchester Piccadilly. This train is formed of 2 coaches."
    )
    assert board.services[0].departure.calling_at == "Calling at: Hadfield only. This train is formed of 2 coaches."


def test_stops_no_longer_called_at_are_said_apart():
    train = one(subsequentCallingPoints=[{"callingPoint": [
        point(), point("Hattersley", "22:30", isCancelled=True), point("Godley", "22:32", et="Cancelled"),
        point("Manchester Piccadilly", "22:54", crs="MAN"),
    ]}])
    assert [p.skipped for p in train.calling_points] == [False, True, True, False]
    assert train.departure.calling_at == (
        "Calling at: Dinting and Manchester Piccadilly. Does not call at Hattersley or Godley. "
        "This train is formed of 3 coaches."
    )


def test_without_a_length_the_coaches_are_counted_or_left_unsaid():
    assert one(length=None).departure.calling_at == "Calling at: Dinting and Manchester Piccadilly."
    coaches = {"coaches": [coach("1"), coach("2"), coach("3"), coach("4")]}
    assert one(length=None, formation=coaches).departure.calling_at.endswith("formed of 4 coaches.")


def test_a_cancelled_train_or_one_without_calling_points_has_no_line():
    assert one(etd="Cancelled", isCancelled=True).departure.calling_at is None
    assert one(subsequentCallingPoints=None).departure.calling_at is None
    assert parse_board({"trainServices": [service(subsequentCallingPoints=None)]})[0].calling_at is None


def test_a_replacement_bus_says_it_is_one():
    [found] = parse_board_rich({"busServices": [bus(subsequentCallingPoints=[{"callingPoint": [point()]}])]}).services
    assert found.departure.calling_at == "Calling at: Dinting only. This is a replacement bus."


def test_a_ferry_says_it_is_one_and_never_counts_coaches():
    [found] = parse_board_rich({"ferryServices": [ferry(length=3)]}).services
    assert found.departure.calling_at == "Calling at: Ryde Pier Head only. This is a ferry."
