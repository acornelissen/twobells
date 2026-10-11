import pytest

from departureboard.core.model import Status
from departureboard.sources.darwin import DarwinSource, parse_board
from tests.paths import FIXTURES


def service(**overrides):
    base = {
        "std": "07:12",
        "etd": "On time",
        "platform": "1",
        "operator": "Northern",
        "isCancelled": False,
        "destination": [{"locationName": "Manchester Piccadilly", "crs": "MAN"}],
    }
    base.update(overrides)
    return base


def board(*services):
    return {"locationName": "Glossop", "crs": "GLO", "trainServices": list(services)}


def test_on_time_service():
    [dep] = parse_board(board(service()))
    assert dep.mode == "train"
    assert dep.service == "Northern"
    assert dep.destination == "Manchester Piccadilly"
    assert dep.scheduled == "07:12"
    assert dep.expected is None
    assert dep.status is Status.ON_TIME
    assert dep.platform == "1"


def test_late_service_with_estimate():
    [dep] = parse_board(board(service(etd="07:19")))
    assert dep.status is Status.LATE
    assert dep.expected == "07:19"


def test_estimate_matching_schedule_counts_as_on_time():
    [dep] = parse_board(board(service(etd="07:12")))
    assert dep.status is Status.ON_TIME


def test_delayed_without_estimate():
    [dep] = parse_board(board(service(etd="Delayed")))
    assert dep.status is Status.LATE
    assert dep.expected is None


def test_cancelled_service():
    [dep] = parse_board(board(service(etd="Cancelled", isCancelled=True)))
    assert dep.status is Status.CANCELLED


def test_unrecognised_estimate_is_unknown():
    [dep] = parse_board(board(service(etd="Starts here")))
    assert dep.status is Status.UNKNOWN


def test_split_destinations_are_joined():
    dests = [{"locationName": "Hadfield"}, {"locationName": "Glossop"}]
    [dep] = parse_board(board(service(destination=dests)))
    assert dep.destination == "Hadfield & Glossop"


def test_board_without_services_is_empty():
    assert parse_board({"locationName": "Glossop"}) == []
    assert parse_board(board()) == []


def test_source_requests_filtered_board_with_api_key():
    calls = []

    def fake_get(url, headers):
        calls.append((url, headers))
        return board(service())

    source = DarwinSource(
        api_key="secret",
        crs="GLO",
        filter_crs="MAN",
        rows=4,
        base_url="https://example.test/LDBWS/api/20220120",
        http_get=fake_get,
    )

    departures = source.fetch()

    [(url, headers)] = calls
    assert url == (
        "https://example.test/LDBWS/api/20220120/GetDepartureBoard/GLO"
        "?numRows=4&filterCrs=MAN&filterType=to"
    )
    assert headers == {"x-apikey": "secret"}
    assert len(departures) == 1


def test_source_without_filter_omits_filter_params():
    calls = []
    source = DarwinSource(
        api_key="k",
        crs="GLO",
        base_url="https://example.test",
        http_get=lambda url, headers: calls.append(url) or board(),
    )
    source.fetch()
    assert calls == ["https://example.test/GetDepartureBoard/GLO?numRows=3"]


def test_parses_a_glossop_board():
    import json

    fixture = FIXTURES / "ldbws_glossop_unfiltered.json"
    deps = parse_board(json.loads(fixture.read_text()))
    assert [(d.scheduled, d.destination) for d in deps] == [
        ("11:55", "Manchester Piccadilly"),
        ("12:08", "Hadfield"),
        ("12:25", "Manchester Piccadilly"),
        ("12:38", "Hadfield"),
    ]
    assert all(d.status is Status.ON_TIME and d.platform == "2" for d in deps)
    assert {d.service for d in deps} == {"Northern"}



def test_default_base_url_matches_the_marketplace_subscription():
    from departureboard.sources.darwin import DEFAULT_BASE_URL

    assert DEFAULT_BASE_URL == (
        "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/LDBWS/api/20220120"
    )


def test_platform_is_kept_only_when_it_looks_like_one():
    def platform(value):
        [dep] = parse_board(board(service(platform=value)))
        return dep.platform

    assert platform("2A") == "2A"
    assert platform(" 3 ") == "3"
    for odd in ("", None, 7, "<b>1</b>", "Platform one"):
        assert platform(odd) is None


from departureboard.sources.darwin import MESSAGE_LIMIT, Message, message_kind, parse_messages, plain_text  # noqa: E402


def load_fixture(name):
    import json

    return json.loads((FIXTURES / name).read_text())


def texts(board):
    return [m.text for m in parse_messages(board)]


def test_reads_incident_messages_from_a_board():
    messages = parse_messages(load_fixture("ldbws_glossop_nrcc.json"))
    assert messages == [
        Message("Engineering work between Dinting and Guide Bridge means trains may be delayed "
                "by up to 15 minutes today. See travel news for details.", "train", "minor", "service"),
        Message("Ticket machines at Glossop & Dinting take cards only. Cash is not accepted.",
                "station", "normal", "station"),
    ]


def test_a_closed_ticket_office_is_station_information_not_disruption():
    # As seen live at Glossop, with no category to go by.
    found = parse_messages({"nrccMessages": [{"Value": "The ticket office is currently closed at this station."}]})
    assert found == [Message("The ticket office is currently closed at this station.", None, None, "station")]


@pytest.mark.parametrize("text, kind", [
    ("Lifts at Glossop are out of use.", "station"),
    ("The toilets on platform 1 are closed.", "station"),
    ("The waiting room is locked this evening.", "station"),
    ("The car park is full.", "station"),
    ("Step-free access is not available at this station.", "station"),
    ("Help points at this station are out of use.", "station"),
    ("Disruption between Hadfield and Glossop until 10:00.", "service"),
    ("Trains may be cancelled or delayed.", "service"),
    ("No trains between Glossop and Hadfield today.", "service"),
    ("Glossop station is closed because of a fire alarm.", "service"),
    ("Replacement buses run between Glossop and Guide Bridge.", "service"),
    ("Engineering works this weekend.", "service"),  # not a facility: shown as disruption
])
def test_without_a_category_the_words_say_which_kind(text, kind):
    assert message_kind(text, None) == kind


def test_a_category_says_which_kind_unless_the_words_are_about_trains():
    assert message_kind("Engineering works this weekend.", "station") == "station"
    assert message_kind("The lifts are out of use.", "train") == "service"
    assert message_kind("The lifts are out of use.", "misc") == "service"
    # A station message that says trains won't stop is still disruption.
    assert message_kind("The station is closed: trains will not call here.", "station") == "service"


def test_category_and_severity_are_read_in_either_spelling_or_left_out():
    shapes = [
        ({"Value": "One", "category": "Station", "severity": "Major"}, ("station", "major")),
        ({"Value": "One", "Category": "PriorTrains", "Severity": 3}, ("prior_trains", "severe")),
        ({"Value": "One", "category": "station ", "severity": "0"}, ("station", "normal")),
        ({"Value": "One", "category": "<b>Train</b>", "severity": "dreadful"}, (None, None)),
        ({"Value": "One", "category": ["Train"], "severity": True}, (None, None)),
        ({"Value": "One", "category": 1, "severity": 9}, (None, None)),
    ]
    for shape, expected in shapes:
        found = parse_messages({"nrccMessages": [shape]})
        assert (found[0].category, found[0].severity) == expected, shape


def test_a_board_without_messages_has_none():
    assert parse_messages(load_fixture("ldbws_glossop_unfiltered.json")) == []


def test_html_tags_and_entities_become_plain_text():
    assert plain_text("<p>Lifts&nbsp;at <b>Glossop</b> &amp; Hadfield&#39;s</p><br/>closed") == (
        "Lifts at Glossop & Hadfield's closed"
    )
    assert plain_text('<a href="https://example.test/x">news</a>') == "news"
    assert plain_text("<p>tab\tand\nnew\x00line</p>") == "tab and newline"


def test_long_messages_are_cut_on_a_word_with_an_ellipsis():
    text = plain_text("<p>" + "Buses replace trains. " * 40 + "</p>")
    assert len(text) <= MESSAGE_LIMIT
    assert text.endswith("…") and text[-2] != " "
    assert text[:-1] in "Buses replace trains. " * 40
    unbroken = plain_text("x" * 1000)
    assert unbroken.endswith("…") and len(unbroken) == MESSAGE_LIMIT


def test_messages_are_read_whatever_their_shape():
    shapes = [
        ["<p>One</p>", "Two"],
        [{"value": "One"}, {"xhtmlMessage": "Two"}],
        {"message": ["One", {"Value": "Two"}]},
        [{"message": "One"}, {"Value": "Two"}, {"Value": "One"}],  # repeats are dropped
    ]
    for shape in shapes:
        assert texts({"nrccMessages": shape}) == ["One", "Two"], shape


def test_odd_message_shapes_are_ignored():
    deep = {"Value": {"Value": {"Value": {"Value": {"Value": "too deep"}}}}}
    for odd in (None, 7, True, [None, 3, {"category": "Train"}], deep, [" ", "<p></p>"]):
        assert parse_messages({"nrccMessages": odd}) == [], odd


def test_only_a_few_messages_are_kept():
    assert len(parse_messages({"nrccMessages": [f"Message {i}" for i in range(20)]})) == 3


def test_source_keeps_the_messages_from_its_last_fetch():
    boards = [load_fixture("ldbws_glossop_nrcc.json"), load_fixture("ldbws_glossop_unfiltered.json")]
    source = DarwinSource(api_key="k", crs="GLO", http_get=lambda url, headers: boards.pop(0))
    assert source.messages == []
    assert len(source.fetch()) == 4
    assert source.messages[1].startswith("Ticket machines at Glossop")
    source.fetch()
    assert source.messages == []


@pytest.mark.parametrize("std", ["soon", "7:12", "24:00", None, 712, ["07:12"]])
def test_a_train_whose_booked_time_is_not_a_clock_time_is_skipped(std):
    trains = parse_board(board(service(std=std), service(std="07:40")))
    assert [d.scheduled for d in trains] == ["07:40"]


def test_the_plain_board_checks_each_field_like_the_rich_one():
    odd = service(etd=1912, destination=["Manchester"], operator={"name": "Northern"}, isCancelled="false")
    [dep] = parse_board(board(odd))
    assert dep.status is Status.UNKNOWN
    assert dep.destination == ""
    assert dep.service == ""


def test_a_single_service_not_in_a_list_is_read():
    [dep] = parse_board({"trainServices": service(std="07:40")})
    assert dep.scheduled == "07:40"


def test_services_of_the_wrong_shape_are_skipped():
    assert parse_board(board("07:12", None, [service()])) == []
    assert parse_board({"trainServices": "07:12"}) == []
