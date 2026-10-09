import io
import json
import tomllib
import zipfile
from pathlib import Path

import pytest

from departureboard.routesetup import (
    MAX_STOPS,
    Pattern,
    RouteSetupError,
    dataset_urls,
    find_patterns,
    parse_naptan,
    patterns,
    route_toml,
    strip_before,
    timetable_documents,
)

FIXTURES = Path(__file__).parent / "fixtures"
TXC = (FIXTURES / "txc_glossop_synthetic.xml").read_bytes()
NAPTAN = (FIXTURES / "naptan_glossop_high_street_west.csv").read_text()
HENRY_A = "1000DGHS0900"


def test_reads_each_pattern_of_the_line_with_its_journey_count():
    found = {p.stops[0]: p for p in patterns(TXC, "341")}
    assert set(found) == {"1000DDDV3835", "1000DGHSW936", "1000DGHS3404"}
    east = found["1000DDDV3835"]
    assert east.journeys == 3
    assert len(east.stops) == 9 and east.stops[-2] == HENRY_A  # both sections, joined
    assert east.run_seconds[:2] == (120, 90)


def test_other_lines_are_left_out():
    assert all(p.journeys != 5 for p in patterns(TXC, "341"))
    assert [p.journeys for p in patterns(TXC, "237")] == [5]
    assert patterns(TXC, "999") == []


def test_strip_goes_back_far_enough_to_cover_the_walk_and_five_minutes():
    # Walk 2 min: 7 min of travel wanted. Run times back from Henry Street:
    # 2, 1, 1, 1.5, 2 min reaches 7.5 at The Junction.
    stops, covered, journeys = strip_before(patterns(TXC, "341"), HENRY_A, walk_minutes=2)
    assert stops[0] == "1000DDHS3412" and stops[-1] == HENRY_A
    assert covered == 450
    assert journeys == 3


def test_strip_is_capped_so_names_stay_readable():
    long = Pattern(tuple(f"S{i}" for i in range(20)) + (HENRY_A,), (60,) * 20, 4)
    stops, covered, _ = strip_before([long], HENRY_A, walk_minutes=30)
    assert len(stops) == MAX_STOPS
    assert covered == (MAX_STOPS - 1) * 60


def test_strip_stops_at_the_start_of_the_route():
    stops, _, _ = strip_before(patterns(TXC, "341"), HENRY_A, walk_minutes=7)
    assert stops[0] == "1000DDDV3835"  # the pattern's first stop
    assert len(stops) == 8


def test_busiest_pattern_through_your_stop_wins():
    # Not the one-journey short working that starts at Arundel Street.
    stops, _, journeys = strip_before(patterns(TXC, "341"), HENRY_A, walk_minutes=0)
    assert journeys == 3
    assert stops[0] == "1000DGHS3410"  # 5 min back: Grapes Inn


def test_a_stop_the_line_never_reaches_is_an_error():
    with pytest.raises(RouteSetupError, match="1000DGHSW936"):
        strip_before(patterns(TXC, "341"), "1000DGHSW936", walk_minutes=7)


def test_naptan_gives_names_and_positions():
    stops = parse_naptan(NAPTAN, {HENRY_A, "1000DGHS3404"})
    assert stops[HENRY_A] == ("Henry Street", 53.44384, -1.948999)
    assert stops["1000DGHS3404"][0] == "Arundel Street"


def test_printed_block_is_valid_config():
    atcos, _, _ = strip_before(patterns(TXC, "341"), HENRY_A, walk_minutes=2)
    block = route_toml("341", "A", atcos, parse_naptan(NAPTAN, set(atcos)), "341, 7 min of travel")
    route = tomllib.loads(block)["bus"]["routes"]["341"]
    assert route["stop"] == "A"
    assert [s["name"] for s in route["stops"]][-2:] == ["Arundel Street", "Henry Street"]
    assert route["stops"][-1]["lat"] == pytest.approx(53.44384)


def test_missing_naptan_stop_is_an_error():
    with pytest.raises(RouteSetupError, match="1000NOPE"):
        route_toml("341", "A", ["1000NOPE"], {}, "note")


def test_names_are_escaped_for_toml():
    block = route_toml("341", "A", ["X"], {"X": ('The "Quote" Inn', 53.0, -1.9)}, "note")
    assert tomllib.loads(block)["bus"]["routes"]["341"]["stops"][0]["name"] == 'The "Quote" Inn'


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_zip_downloads_yield_each_xml_file_and_lines_are_found_across_them():
    blob = _zip({"a.xml": TXC, "readme.txt": b"hi", "b.xml": b"<TransXChange xmlns='http://www.transxchange.org.uk/'/>"})
    documents = list(timetable_documents(blob))
    assert len(documents) == 2
    assert len(find_patterns(documents, "341")) == 3


def test_plain_xml_download_is_one_document():
    assert list(timetable_documents(TXC)) == [TXC]


def test_too_many_files_in_a_zip_is_refused(monkeypatch):
    from departureboard import routesetup

    monkeypatch.setattr(routesetup, "MAX_ZIP_MEMBERS", 2)
    with pytest.raises(RouteSetupError, match="refusing"):
        list(timetable_documents(_zip({f"{i}.xml": b"<x/>" for i in range(3)})))


def test_oversized_file_in_a_zip_is_refused(monkeypatch):
    from departureboard import routesetup

    monkeypatch.setattr(routesetup, "MAX_XML_BYTES", 10)
    with pytest.raises(RouteSetupError, match="refusing"):
        list(timetable_documents(_zip({"big.xml": b"<x>" + b"a" * 100 + b"</x>"})))


def test_doctype_is_refused():
    with pytest.raises(RouteSetupError, match="DOCTYPE"):
        patterns(b'<!DOCTYPE x [<!ENTITY a "b">]><TransXChange/>', "341")


def test_dataset_search_lists_download_urls_and_hides_the_key():
    calls = []

    def fake_get(url, limit):
        calls.append(url)
        return json.dumps({"results": [{"url": "https://bods/one.zip"}, {"url": ""}]}).encode()

    assert dataset_urls("secret", "BNML", http_get=fake_get) == ["https://bods/one.zip"]

    def listing(url, limit):
        return json.dumps({"results": [
            {"url": "https://bods/a.zip", "lines": ["237", "350"]},
            {"url": "https://bods/b.zip", "lines": ["341|Glossop - Hyde", "343"]},
            {"url": "https://bods/c.zip"},  # no line list: can't tell, so keep it
        ]}).encode()

    assert dataset_urls("k", "BNML", "341", http_get=listing) == ["https://bods/b.zip", "https://bods/c.zip"]
    assert "noc=BNML" in calls[0] and "status=published" in calls[0]

    def failing(url, limit):
        raise OSError(f"failed {url}")

    with pytest.raises(RouteSetupError) as error:
        dataset_urls("secret", "BNML", http_get=failing)
    assert "secret" not in str(error.value)
