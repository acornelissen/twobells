import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from departureboard import discover, timetable
from departureboard.discover import (
    Cancelled,
    Dataset,
    DiscoveryError,
    discover_operators,
    list_datasets,
)

FIXTURES = Path(__file__).parent / "fixtures"
TXC_341 = (FIXTURES / "txc_bnml_341.xml").read_bytes()  # filed under BNML, says BNGN inside
TXC_237 = (FIXTURES / "txc_bnml_237.xml").read_bytes()
TXC_61 = (FIXTURES / "txc_hipk_61.xml").read_bytes()
LISTS = json.loads((FIXTURES / "bods_datasets_synthetic.json").read_text())
STOP_A = "1000DGHS0900"  # Henry Street, Glossop: the 341 and the 237
STOP_B = "100000008350"  # the 237 and the 61
STOP_M = "1800EH02111"  # over the border in Greater Manchester: the 237 only
STOPS = {"A": STOP_A, "B": STOP_B}
SUMMER = date(2026, 8, 1)  # while the 237's summer timetable runs
KEY = "secret-key"

# A document that names none of the stops is never parsed: these would fail if they were.
ELSEWHERE = b'<?xml version="1.0"?><TransXChange xmlns="http://www.transxchange.org.uk/"><broken'


def _link(n):
    return f"https://data.bus-data.dft.gov.uk/timetable/dataset/{n}/download/"


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeBods:
    """BODS's data set list (the fixture's pages) and its downloads."""

    def __init__(self):
        self.lists = json.loads(json.dumps(LISTS))
        self.blobs = {
            _link(1): _zip({"341.xml": TXC_341, "237.xml": TXC_237, "201.xml": ELSEWHERE}),
            _link(2): TXC_61,
            _link(3): _zip({"skylink.xml": ELSEWHERE}),
            _link(4): _zip({"201.xml": ELSEWHERE, "203.xml": ELSEWHERE}),
        }
        self.listed = []
        self.fetched = []

    def http_get(self, url, max_bytes):
        self.listed.append(url)
        query = parse_qs(urlsplit(url).query)
        page = self.lists[query["adminArea"][0]]
        return json.dumps(page).encode()

    def find(self, api_key, area, at_most):
        return list_datasets(api_key, area, at_most, http_get=self.http_get)

    def fetch(self, url, file):
        self.fetched.append(url)
        file.write(self.blobs[url])

    def discover(self, stops=STOPS, scratch=None, **options):
        options.setdefault("today", SUMMER)
        return discover_operators(stops, KEY, scratch or self.scratch, find=self.find, fetch=self.fetch, **options)


@pytest.fixture
def bods(tmp_path):
    fake = FakeBods()
    fake.scratch = tmp_path / "state"
    return fake


# Finding them.


def test_finds_each_operator_by_the_code_its_data_set_is_filed_under(bods):
    found = bods.discover()
    # The 341's document says BNGN, but its data set is filed under BNML, which is what
    # [bus] operators and the timetable's data set search need.
    assert found.operators == ["BNML", "HIPK"]
    assert found.lines == {"BNML": ["237", "341"], "HIPK": ["61"]}
    assert found.document_operators == {"BNML": ["BNGN"]}
    assert found.stop_lines == {"A": ["237", "341"], "B": ["61", "237"]}
    assert found.datasets_scanned == 3  # area 100 only: both stops are in Derbyshire
    assert found.journeys_found > 0
    assert found.notes == []


def test_searches_every_area_of_your_stops_once_per_data_set(bods):
    found = bods.discover({**STOPS, "M": STOP_M})
    assert [parse_qs(urlsplit(url).query)["adminArea"] for url in bods.listed] == [["100"], ["180"]]
    # Data set 1 is in both areas but fetched once; data set 5's link isn't BODS's, so it never is.
    assert bods.fetched == [_link(1), _link(2), _link(3), _link(4)]
    assert found.datasets_scanned == 4
    assert found.stop_lines["M"] == ["237"]


def test_the_operators_found_build_the_timetable(bods, tmp_path):
    found = bods.discover()

    def find_by_noc(api_key, noc, line=None):
        # As BODS's ?noc= search: the data sets filed under that code.
        pages = [bods.lists["100"], bods.lists["180"]]
        return sorted({r["url"] for page in pages for r in page["results"] if noc in r["noc"]})

    def build(operators):
        return timetable.build_schedule(
            STOPS, operators, KEY, datetime(2026, 8, 1, 3, 0), scratch=tmp_path,
            find_datasets=find_by_noc, fetch=bods.fetch, naptan=lambda atcos: {})

    schedule = build(found.operators)
    assert {(j.line, j.operator) for j in schedule.journeys} == {("341", "BNGN"), ("237", "BNML"), ("61", "HIPK")}
    # The documents' own code finds nothing: nothing is filed under BNGN.
    with pytest.raises(timetable.TimetableError, match="No timetabled journeys"):
        build(found.document_operators["BNML"])


def test_documents_without_your_stops_are_never_parsed(bods, monkeypatch):
    parsed = []
    original = timetable._parse_document

    def spy(xml, atcos, noc):
        parsed.append(xml)
        return original(xml, atcos, noc)

    monkeypatch.setattr(timetable, "_parse_document", spy)
    bods.discover()
    assert sorted(parsed) == sorted([TXC_341, TXC_237, TXC_61])  # not ELSEWHERE, which would fail


def test_a_timetable_that_has_ended_is_left_out(bods):
    # The 237's summer timetable ended on 29 August.
    found = bods.discover(today=date(2026, 10, 9))
    assert found.lines == {"BNML": ["341"], "HIPK": ["61"]}
    assert found.stop_lines == {"A": ["341"], "B": ["61"]}


def test_downloads_are_gone_once_searched(bods):
    bods.discover()
    assert list(bods.scratch.iterdir()) == []


def test_patterns_are_kept_for_route_strips_when_asked(bods):
    assert bods.discover().patterns == {}
    found = bods.discover(with_patterns=True)
    assert set(found.patterns) == {"341", "237", "61"}
    assert any(STOP_A in pattern.stops for pattern in found.patterns["341"])


def test_progress_follows_the_data_sets(bods):
    seen = []
    bods.discover(progress=lambda done, total: seen.append((done, total)))
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_nothing_at_your_stops_is_an_error(bods):
    with pytest.raises(DiscoveryError, match="No timetabled bus calls at your stops in 3 BODS data sets"):
        bods.discover({"A": "1000NOWHERE1"})


def test_a_stop_must_be_an_atco_code(bods):
    with pytest.raises(DiscoveryError, match="not an ATCO code"):
        bods.discover({"A": "../etc"})
    with pytest.raises(DiscoveryError, match="at least one stop"):
        bods.discover({})


# Which operator code a data set is found by.


@pytest.mark.parametrize(
    "filed, inside, expected",
    [
        (("BNML",), {"BNGN"}, "BNML"),  # the 341: filed under BNML, BNGN inside
        (("BNML", "BNGN"), {"BNGN"}, "BNGN"),  # filed under both: the documents' own
        (("HIPK",), {"HIPK"}, "HIPK"),
        ((), {"BNGN"}, "BNGN"),  # BODS gives none: the documents' own is the best guess
        ((), set(), None),
        ((), {"bad code!"}, None),
    ],
)
def test_filed_under(filed, inside, expected):
    assert discover._filed_under(Dataset("https://x", filed), inside) == expected


# Limits.


def test_a_data_set_that_fails_is_skipped_and_noted(bods):
    original = bods.fetch

    def fetch(url, file):
        if url == _link(2):
            raise OSError(f"failed {url}?api_key={KEY}")
        original(url, file)

    bods.fetch = fetch
    found = bods.discover()
    assert found.operators == ["BNML"]
    assert len(found.notes) == 1 and "Skipped a data set" in found.notes[0]
    assert KEY not in found.notes[0]


def test_a_download_larger_than_allowed_is_skipped(bods):
    # The 61 comes as a bare document; the others are zipped, and smaller.
    assert max(len(bods.blobs[_link(n)]) for n in (1, 3, 4)) < len(TXC_61)
    found = bods.discover(max_download_bytes=len(TXC_61) - 1)
    assert found.operators == ["BNML"]
    assert "larger than" in found.notes[0]


def test_all_downloads_together_are_capped(bods):
    with pytest.raises(DiscoveryError, match="Gave up after downloading"):
        bods.discover(max_total_bytes=len(bods.blobs[_link(1)]) + 10)


def test_the_search_has_a_time_budget(bods):
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(DiscoveryError, match="Gave up after 5 minutes"):
        bods.discover(time_budget=300, clock=lambda: next(ticks))


def test_data_sets_beyond_the_cap_are_left_out_and_noted(bods):
    found = bods.discover({**STOPS, "M": STOP_M}, max_datasets=2)
    assert found.datasets_scanned == 2
    assert bods.fetched == [_link(1), _link(2)]
    assert found.notes == ["Only the first 2 BODS data sets were searched"]


def test_cancelling_stops_the_search(bods):
    original = bods.fetch

    def fetch(url, file):
        original(url, file)
        stop.append(True)

    stop = []
    bods.fetch = fetch
    with pytest.raises(Cancelled):
        bods.discover(cancelled=lambda: bool(stop))
    assert bods.fetched == [_link(1)]
    assert list(bods.scratch.iterdir()) == []


# The data set list.


def _page(count, start=0, next_link=True):
    results = [{"url": _link(start + n), "noc": ["BNML"]} for n in range(count)]
    return {"count": 10_000, "next": "https://elsewhere.example/?api_key=x" if next_link else None,
            "results": results}


def test_list_asks_by_area_and_offset_and_hides_the_key():
    asked = []

    def http_get(url, max_bytes):
        asked.append(url)
        offset = int(parse_qs(urlsplit(url).query)["offset"][0])
        return json.dumps(_page(100 if offset == 0 else 30, offset)).encode()

    found = list_datasets(KEY, "100", 250, http_get=http_get)
    assert [d.url for d in found] == [_link(n) for n in range(130)]
    queries = [parse_qs(urlsplit(url).query) for url in asked]
    assert [q["offset"] for q in queries] == [["0"], ["100"]]  # a short page is the last
    assert all(q["adminArea"] == ["100"] and q["status"] == ["published"] for q in queries)
    assert all(url.startswith("https://data.bus-data.dft.gov.uk/api/v1/dataset/?") for url in asked)


def test_list_pagination_is_capped():
    asked = []

    def endless(url, max_bytes):
        asked.append(url)
        return json.dumps(_page(100, len(asked) * 100)).encode()

    assert len(list_datasets(KEY, "180", 250, http_get=endless)) == 250
    assert len(asked) == 3

    asked.clear()

    def junk(url, max_bytes):  # full pages of nothing usable, with a next link, forever
        asked.append(url)
        return json.dumps({"next": "more", "results": [{"url": "http://insecure"}] * 100}).encode()

    assert list_datasets(KEY, "180", 300, http_get=junk) == []
    assert len(asked) == discover.MAX_PAGES


def test_list_keeps_only_bods_links_and_operator_codes():
    page = {"results": [
        {"url": _link(1), "noc": ["BNML", "BNML", "bad code", 7, "BNGN"]},
        {"url": _link(2), "noc": "HIPK"},
        {"url": "http://data.bus-data.dft.gov.uk/timetable/dataset/3/download/", "noc": ["X1"]},
        {"url": "https://evil.example/4.zip", "noc": ["X2"]},
        {"url": "https://data.bus-data.dft.gov.uk.evil.example/5.zip"},
        {"noc": ["X3"]},
        "not a data set",
    ]}
    found = list_datasets(KEY, "100", http_get=lambda url, limit: json.dumps(page).encode())
    assert found == [Dataset(_link(1), ("BNML", "BNGN")), Dataset(_link(2), ("HIPK",))]


def test_list_errors_never_show_the_key():
    def failing(url, max_bytes):
        raise OSError(f"HTTP Error 500 for {url} (api_key={KEY})")

    with pytest.raises(DiscoveryError) as error:
        list_datasets(KEY, "100", http_get=failing)
    assert KEY not in str(error.value)
    assert "data set list failed" in str(error.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"results": "none"}', b"\xff"])
def test_list_refuses_answers_that_are_not_a_list_of_data_sets(body):
    with pytest.raises(DiscoveryError):
        list_datasets(KEY, "100", http_get=lambda url, limit: body)


def test_list_refuses_an_area_that_is_not_a_code():
    with pytest.raises(DiscoveryError, match="admin area"):
        list_datasets(KEY, "10&x=1", http_get=lambda url, limit: b"{}")
