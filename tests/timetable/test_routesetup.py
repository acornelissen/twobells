import io
import json
import re
import tomllib
import tracemalloc
import zipfile
import zlib
from collections import Counter

import pytest

from departureboard.core import safexml
from departureboard.timetable import routesetup
from departureboard.timetable.routesetup import (
    MAX_NAPTAN_BYTES,
    MAX_STOPS,
    Pattern,
    RouteSetupError,
    dataset_urls,
    find_patterns,
    naptan_stops,
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
NS = {"t": "http://www.transxchange.org.uk/"}


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


def _patterns_whole_tree(xml, line):
    """patterns as it was, parsing the whole document as one tree (about 8
    times its size): kept only to check that reading it an element at a
    time gives the same."""
    root = safexml.fromstring(xml)
    line_ids = {
        node.get("id")
        for node in root.iterfind(".//t:Services/t:Service/t:Lines/t:Line", NS)
        if (node.findtext("t:LineName", "", NS) or "").strip() == line
    }
    if not line_ids:
        return []
    sections = {}
    for section in root.iterfind(".//t:JourneyPatternSections/t:JourneyPatternSection", NS):
        links = []
        for link in section.iterfind("t:JourneyPatternTimingLink", NS):
            links.append(
                (
                    link.findtext("t:From/t:StopPointRef", "", NS).strip(),
                    link.findtext("t:To/t:StopPointRef", "", NS).strip(),
                    routesetup._seconds(link.findtext("t:RunTime", None, NS)),
                )
            )
        sections[section.get("id")] = links
    journey_patterns = {}
    for service in root.iterfind(".//t:Services/t:Service", NS):
        service_lines = {n.get("id") for n in service.iterfind("t:Lines/t:Line", NS)}
        if not service_lines & line_ids:
            continue
        for pattern in service.iterfind(".//t:StandardService/t:JourneyPattern", NS):
            refs = [r.text.strip() for r in pattern.iterfind("t:JourneyPatternSectionRefs", NS) if r.text]
            journey_patterns[pattern.get("id")] = refs
    uses = Counter(
        journey.findtext("t:JourneyPatternRef", "", NS).strip()
        for journey in root.iterfind(".//t:VehicleJourneys/t:VehicleJourney", NS)
        if journey.findtext("t:LineRef", "", NS).strip() in line_ids
    )
    found = []
    for pattern_id, refs in journey_patterns.items():
        links = [link for ref in refs for link in sections.get(ref, [])]
        if not links or not uses[pattern_id]:
            continue
        stops = (links[0][0], *tuple(link[1] for link in links))
        found.append(Pattern(stops, tuple(link[2] for link in links), uses[pattern_id]))
    return found


def _reordered(xml):
    """`xml` with its journeys first and its services last."""
    head, rest = xml.split(b"<StopPoints>", 1)
    rest = b"<StopPoints>" + rest
    pieces = {name: re.search(rb"<%s>.*?</%s>" % (name, name), rest, re.S).group(0)
              for name in (b"Services", b"VehicleJourneys")}
    for piece in pieces.values():
        rest = rest.replace(piece, b"")
    return head + pieces[b"VehicleJourneys"] + rest.replace(b"</TransXChange>", pieces[b"Services"] + b"</TransXChange>")


def _twice(xml, tag, change=lambda element: element):
    """`xml` with its first `tag` element given again, changed, after the others."""
    element = re.search(rb"<%s[ >].*?</%s>" % (tag, tag), xml, re.S).group(0)
    return xml.replace(b"</%ss>" % tag, change(element) + b"</%ss>" % tag, 1)


def _without_first_link(section):
    return re.sub(rb"<JourneyPatternTimingLink[ >].*?</JourneyPatternTimingLink>", b"", section, count=1, flags=re.S)


# Every sample document, and odd ones: services after their journeys, a
# service given twice, a section given again with a stop fewer.
_SAMPLES = {path.name: path.read_bytes() for path in sorted(FIXTURES.glob("txc_*.xml"))}
_SAMPLES["services last"] = _reordered(_SAMPLES["txc_bnml_341.xml"])
_SAMPLES["a service twice"] = _twice(_SAMPLES["txc_bnml_341.xml"], b"Service")
_SAMPLES["a section twice"] = _twice(_SAMPLES["txc_hipk_61.xml"], b"JourneyPatternSection", _without_first_link)


@pytest.mark.parametrize("name", list(_SAMPLES))
def test_patterns_read_an_element_at_a_time_are_those_of_the_whole_tree(name):
    xml = _SAMPLES[name]
    lines = {line.decode().strip() for line in re.findall(rb"<LineName>([^<]*)</LineName>", xml)}
    for line in (*sorted(lines), "no such line", ""):
        assert patterns(xml, line) == _patterns_whole_tree(xml, line), line
    assert any(patterns(xml, line) for line in lines)


def test_patterns_never_hold_a_document_as_a_whole_tree():
    xml = _SAMPLES["txc_bnml_237.xml"]
    head, rest = xml.split(b"<VehicleJourneys>", 1)
    journeys, tail = rest.split(b"</VehicleJourneys>", 1)
    big = head + b"<VehicleJourneys>" + journeys * (4_000_000 // len(journeys)) + b"</VehicleJourneys>" + tail
    line = re.search(rb"<LineName>([^<]*)</LineName>", xml).group(1).decode()
    tracemalloc.start()
    try:
        found = patterns(big, line)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found
    assert peak < len(big) / 2  # whole, about 8 times


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


def _zip(files, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
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


def test_a_zip_as_big_as_the_largest_real_one_is_within_the_total():
    # BODS data set 2100 (First South Yorkshire, October 2026) unpacks to
    # 1,293,595,148 bytes in 159 files. The total is only a zip-bomb guard:
    # the files are read one at a time.
    from departureboard.timetable import routesetup

    assert routesetup.MAX_UNZIPPED_BYTES >= 3 * 1_293_595_148


def test_a_big_zip_of_real_timetables_is_read_whole(monkeypatch):
    # Data set 2100 against the total, scaled down: compressed as BODS's
    # are, none of its files comes near the ratio refused.
    from departureboard.timetable import routesetup

    files = {f"{i}.xml": TXC for i in range(40)}
    monkeypatch.setattr(routesetup, "MAX_UNZIPPED_BYTES", 3 * len(TXC) * len(files))
    assert list(timetable_documents(_zip(files, zipfile.ZIP_DEFLATED))) == [TXC] * len(files)


def test_a_zip_bomb_is_refused_before_it_is_unpacked():
    # A megabyte of zeros deflates to about a kilobyte.
    blob = _zip({"a.xml": TXC, "bomb.xml": bytes(1_000_000)}, zipfile.ZIP_DEFLATED)
    documents = timetable_documents(blob)
    assert next(documents) == TXC
    with pytest.raises(RouteSetupError, match=r"bomb\.xml unpacks to more than 200 times its size; refusing"):
        next(documents)


def _understated(data: bytes, padding: int) -> bytes:
    """A zip of `data` deflated as bomb.xml, whose header says it takes
    `padding` more bytes of the zip than it does (junk after its deflated
    bytes, never read), so its ratio looks small."""
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw deflate, as in a zip
    taken = packer.compress(data) + packer.flush() + bytes(padding)
    blob = bytearray(_zip({"bomb.xml": taken}))  # stored, then said to be deflated
    local, central = 0, blob.index(b"PK\x01\x02")
    assert blob[central + 46 : central + 46 + len("bomb.xml")] == b"bomb.xml"
    for header, method, crc, size in ((local, 8, 14, 22), (central, 10, 16, 24)):
        blob[header + method : header + method + 2] = zipfile.ZIP_DEFLATED.to_bytes(2, "little")
        blob[header + crc : header + crc + 4] = zlib.crc32(data).to_bytes(4, "little")
        blob[header + size : header + size + 4] = len(data).to_bytes(4, "little")
    return bytes(blob)


def test_a_file_unpacking_to_far_more_than_it_took_is_refused_whatever_its_header_says(monkeypatch):
    from departureboard.timetable import routesetup

    monkeypatch.setattr(routesetup, "MAX_RATIO", 20)
    blob = _understated(bytes(2_000_000), 500_000)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        bomb = archive.getinfo("bomb.xml")
    assert bomb.file_size <= 20 * bomb.compress_size  # its header alone passes
    with pytest.raises(RouteSetupError, match=r"bomb\.xml unpacks to more than 20 times its size; refusing"):
        list(timetable_documents(blob))


def test_a_file_unpacking_to_what_it_took_is_read_whatever_its_header_says():
    assert list(timetable_documents(_understated(TXC, 500_000))) == [TXC]


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


def test_naptan_s_stops_are_read_from_the_response_as_from_its_text():
    wanted = {"1000DGHS0900", "1000DDDV3835", "1000NOWHERE1"}
    for body in (NAPTAN.encode(), "﻿".encode() + NAPTAN.encode(), NAPTAN.replace("\n", "\r\n").encode()):
        asked = []

        def get(url, limit, body=body, asked=asked):
            asked.append(limit)
            return body

        for with_indicator in (False, True):
            assert naptan_stops(wanted, http_get=get, with_indicator=with_indicator) == parse_naptan(
                NAPTAN, wanted, with_indicator)
        assert asked == [MAX_NAPTAN_BYTES] * 2


def test_naptan_s_stops_are_read_without_copying_the_whole_response():
    # A build asks for them beside the journeys it holds, with room for
    # MAX_NAPTAN_BYTES of response (tests/test_deploy.py): decoded whole, it
    # took five times that again.
    header, *rows = NAPTAN.splitlines(keepends=True)
    body = (header + "".join(rows) * (5_000_000 // len(NAPTAN))).encode()
    tracemalloc.start()
    try:
        found = naptan_stops({HENRY_A}, http_get=lambda url, limit: body)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert found == {HENRY_A: ("Henry Street", 53.44384, -1.948999)}
    assert peak < len(body) / 10
