import io
import json
import tomllib
import zipfile

import pytest

from departureboard.timetable.routesetup import (
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
from tests.paths import FIXTURES

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
    long = Pattern((*tuple(f"S{i}" for i in range(20)), HENRY_A), (60,) * 20, 4)
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


def test_a_download_can_be_read_from_a_file(tmp_path):
    zipped = tmp_path / "zipped"
    zipped.write_bytes(_zip({"a.xml": TXC, "b.xml": b"<x/>"}))
    plain = tmp_path / "plain"
    plain.write_bytes(TXC)
    with open(zipped, "rb") as file:
        assert list(timetable_documents(file)) == [TXC, b"<x/>"]
    with open(plain, "rb") as file:
        assert list(timetable_documents(file)) == [TXC]


def test_an_oversized_plain_file_is_skipped_and_said(tmp_path):
    plain = tmp_path / "plain"
    plain.write_bytes(TXC)
    said = []
    with open(plain, "rb") as file:
        assert list(timetable_documents(file, 10, name="https://x/1", skipped=said.append)) == []
    assert said == ["Skipped https://x/1: more than the 10 bytes this install can read"]


def test_a_skipped_file_is_logged_by_default(tmp_path, caplog):
    plain = tmp_path / "plain"
    plain.write_bytes(TXC)
    with open(plain, "rb") as file:
        assert list(timetable_documents(file, 10)) == []
    assert "Skipped the timetable file: more than the 10 bytes" in caplog.text


def test_too_many_files_in_a_zip_is_refused(monkeypatch):
    from departureboard.timetable import routesetup

    monkeypatch.setattr(routesetup, "MAX_ZIP_MEMBERS", 2)
    with pytest.raises(RouteSetupError, match="refusing"):
        list(timetable_documents(_zip({f"{i}.xml": b"<x/>" for i in range(3)})))


def test_an_oversized_file_in_a_zip_is_skipped_and_the_rest_read(caplog):
    blob = _zip({"a.xml": b"<a/>", "big.xml": b"<x>" + b"a" * 100 + b"</x>", "b.xml": b"<b/>"})
    assert list(timetable_documents(blob, 10)) == [b"<a/>", b"<b/>"]
    assert "Skipped big.xml: 107 bytes, more than the 10 bytes this install can read" in caplog.text


def test_a_name_from_a_zip_is_said_without_its_control_characters():
    said = []
    list(timetable_documents(_zip({"evil\nline.xml": b"<x>" + b"a" * 100 + b"</x>"}), 10, skipped=said.append))
    assert said == ["Skipped evil?line.xml: 107 bytes, more than the 10 bytes this install can read"]


def test_a_zip_unpacking_to_too_much_in_total_is_refused(monkeypatch):
    from departureboard.timetable import routesetup

    monkeypatch.setattr(routesetup, "MAX_UNZIPPED_BYTES", 150)
    files = {f"{i}.xml": b"<x>" + b"a" * 50 + b"</x>" for i in range(3)}  # 57 bytes each
    documents = timetable_documents(_zip(files))
    assert len(next(documents)) == 57
    assert len(next(documents)) == 57
    with pytest.raises(RouteSetupError, match="refusing"):
        next(documents)


def test_a_zip_within_the_total_is_read_whole(monkeypatch):
    from departureboard.timetable import routesetup

    monkeypatch.setattr(routesetup, "MAX_UNZIPPED_BYTES", 171)
    files = {f"{i}.xml": b"<x>" + b"a" * 50 + b"</x>" for i in range(3)}
    assert len(list(timetable_documents(_zip(files)))) == 3


def test_doctype_is_refused():
    with pytest.raises(RouteSetupError, match="DOCTYPE"):
        patterns(b'<!DOCTYPE x [<!ENTITY a "b">]><TransXChange/>', "341")


def test_doctype_after_a_long_comment_is_refused():
    evil = b"<!--" + b" " * 5000 + b'--><!DOCTYPE x [<!ENTITY a "b">]><TransXChange/>'
    with pytest.raises(RouteSetupError, match="DOCTYPE"):
        patterns(evil, "341")


def test_doctype_in_utf16_is_refused():
    evil = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><TransXChange/>'
    with pytest.raises(RouteSetupError, match="DOCTYPE"):
        patterns(evil.encode("utf-16"), "341")


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


def test_dataset_search_pages_past_the_first_hundred_but_not_forever():
    from urllib.parse import parse_qs, urlsplit

    calls = []

    def paged(url, limit):
        offset = int(parse_qs(urlsplit(url).query).get("offset", ["0"])[0])
        calls.append(offset)
        size = 100 if offset < 100 else 1
        return json.dumps({"results": [{"url": f"https://bods/{offset + n}.zip"} for n in range(size)]}).encode()

    urls = dataset_urls("k", "BNML", http_get=paged)
    assert len(urls) == 101 and calls == [0, 100]

    def endless(url, limit):
        offset = int(parse_qs(urlsplit(url).query).get("offset", ["0"])[0])
        calls.append(offset)
        return json.dumps({"results": [{"url": f"https://bods/{offset + n}.zip"} for n in range(100)]}).encode()

    calls.clear()
    assert len(dataset_urls("k", "BNML", http_get=endless)) == 500
    assert calls == [0, 100, 200, 300, 400]


def test_naptan_can_also_give_each_stop_s_sign_letter():
    found = parse_naptan(NAPTAN, {"1000DGHS0900"}, with_indicator=True)
    assert found["1000DGHS0900"] == ("Henry Street", 53.44384, -1.948999, "Stop A")
    assert parse_naptan(NAPTAN, {"1000DGHS0900"})["1000DGHS0900"] == ("Henry Street", 53.44384, -1.948999)
