from departureboard.darwin import DarwinSource, parse_board
from departureboard.model import Status


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


def test_parses_a_real_glossop_board():
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "ldbws_glossop_unfiltered.json"
    deps = parse_board(json.loads(fixture.read_text()))
    assert [(d.scheduled, d.destination) for d in deps] == [
        ("11:52", "Manchester Piccadilly"),
        ("12:05", "Hadfield"),
        ("12:22", "Manchester Piccadilly"),
        ("12:35", "Hadfield"),
    ]
    assert all(d.status is Status.ON_TIME and d.platform == "1" for d in deps)
    assert {d.service for d in deps} == {"Northern"}



def test_default_base_url_matches_the_marketplace_subscription():
    from departureboard.darwin import DEFAULT_BASE_URL

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


from departureboard.darwin import MESSAGE_LIMIT, parse_messages, plain_text  # noqa: E402


def load_fixture(name):
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def test_reads_incident_messages_from_a_board():
    messages = parse_messages(load_fixture("ldbws_glossop_nrcc.json"))
    assert messages == [
        "Disruption between Glossop and Manchester Piccadilly until the end of the day. "
        "Trains may be cancelled or delayed by up to 20 minutes. "
        "More details can be found in Latest Travel News.",
        "Lifts at Glossop & Hadfield are out of use. Step-free access is not available.",
    ]


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
        assert parse_messages({"nrccMessages": shape}) == ["One", "Two"], shape


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
    assert source.messages[1].startswith("Lifts at Glossop")
    source.fetch()
    assert source.messages == []
