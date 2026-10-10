import io
import json
import re
import stat
import threading
import time
import tomllib
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
    SetupBusy,
    SetupJob,
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


# The setup job.

SUMMER_NOW = datetime(2026, 8, 1, 3, 0)


def _naptan(atcos):
    return {atco: (f"Stop {atco}", 53.44, -1.95) for atco in atcos}


class Gate:
    """Holds a download until the test lets it go."""

    def __init__(self, fetch):
        self._fetch = fetch
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, url, file):
        self.entered.set()
        assert self.release.wait(5)
        self._fetch(url, file)


def _find_by_noc(bods):
    def find(api_key, noc, line=None):
        # As BODS's ?noc= search: the data sets filed under that code.
        pages = [bods.lists["100"], bods.lists["180"]]
        return sorted({r["url"] for page in pages for r in page["results"] if noc in r["noc"]})

    return find


def _job(bods, folder=None, **options):
    options.setdefault("fetch", bods.fetch)
    options.setdefault("find_datasets", _find_by_noc(bods))
    options.setdefault("naptan", _naptan)
    return SetupJob(folder or bods.scratch, STOPS, KEY, now=lambda: SUMMER_NOW, find=bods.find, **options)


def _finish(job):
    assert job.wait(5)
    return job.status()


def test_the_job_finds_operators_builds_the_timetable_and_returns_the_settings(bods):
    job = _job(bods)
    assert job.status() == {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}
    job.start()
    status = _finish(job)
    assert status["state"] == "done", status["message"]
    journeys = status["result"]["timetable_journeys"]
    assert status["message"] == f"Found 2 bus companies (BNML, HIPK) and built a timetable of {journeys} journeys"
    result = status["result"]
    assert result["fragment"] == {"bus": {"source": "bods", "operators": ["BNML", "HIPK"]}}
    assert result["lines"] == {"BNML": ["237", "341"], "HIPK": ["61"]}
    assert result["document_operators"] == {"BNML": ["BNGN"]}
    assert result["stop_lines"] == {"A": ["237", "341"], "B": ["61", "237"]}
    # The timetable is saved for exactly these stops and operators, so the board
    # uses it as soon as the settings are saved.
    saved = timetable.load(bods.scratch)
    assert len(saved.journeys) == journeys > 0
    assert saved.operators == ("BNML", "HIPK") and saved.stops == STOPS
    assert timetable.due(saved, STOPS, SUMMER_NOW, ["BNML", "HIPK"]) is None
    assert sorted(p.name for p in bods.scratch.iterdir()) == sorted([discover.LOCK_NAME, timetable.FILE_NAME])


def test_the_job_reports_its_progress(bods):
    gate = Gate(bods.fetch)
    job = _job(bods, fetch=gate)
    job.start()
    assert gate.entered.wait(5)
    status = job.status()
    assert (status["state"], status["step"], status["done"], status["total"]) == (
        "running", discover.STEP_DISCOVER, 0, 3)
    gate.release.set()
    assert _finish(job)["state"] == "done"


def test_the_job_builds_route_strips_when_asked(bods):
    job = _job(bods, routes=True, walk_minutes={"A": 3, "*": 2})
    job.start()
    status = _finish(job)
    assert status["state"] == "done", status["message"]
    routes = status["result"]["fragment"]["bus"]["routes"]
    assert set(routes) == {"61", "237", "341"}
    for route in routes.values():
        stop = STOPS[route["stop"]]
        assert 2 <= len(route["stops"]) <= 8
        assert route["stops"][-1] == {"name": f"Stop {stop}", "lat": 53.44, "lon": -1.95}  # yours last
    assert routes["61"]["stop"] == "B" and routes["341"]["stop"] == "A"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_by_default_the_job_s_timetable_keeps_each_stop_s_sign_letter(bods, monkeypatch):
    # NaPTAN is asked for real (here, its CSV from a fake network), as the weekly build does,
    # so the timetable the wizard saves says which side of the road a stop is from the start.
    from departureboard import http

    others = set(re.findall(rb"<StopPointRef>(\w+)</StopPointRef>", TXC_341 + TXC_237 + TXC_61))
    rows = [f"{atco.decode()},Somewhere,53.44,-1.95,\n" for atco in sorted(others)]
    rows += [f"{atco},Henry Street,53.44384,-1.948999,{letter}\n" for atco, letter in ((STOP_A, "Stop A"),
                                                                                        (STOP_B, "Stop B"))]
    csv = ("ATCOCode,CommonName,Latitude,Longitude,Indicator\n" + "".join(rows)).encode()

    def network(request, timeout):
        if urlsplit(request.full_url).hostname == "naptan.api.dft.gov.uk":
            return FakeResponse(csv)
        raise OSError("No network in tests")

    monkeypatch.setattr(http, "urlopen", network)
    job = SetupJob(bods.scratch, STOPS, KEY, routes=True, now=lambda: SUMMER_NOW, find=bods.find,
                   fetch=bods.fetch, find_datasets=_find_by_noc(bods))
    job.start()
    status = _finish(job)
    assert status["state"] == "done", status["message"]
    saved = timetable.load(bods.scratch)
    assert saved.positions[STOP_A].indicator == "Stop A" and saved.positions[STOP_B].indicator == "Stop B"
    # Route strips still read NaPTAN's name and position from the longer rows.
    routes = status["result"]["fragment"]["bus"]["routes"]
    assert routes["341"]["stops"][-1] == {"name": "Henry Street", "lat": 53.44384, "lon": -1.948999}


def test_route_strips_are_skipped_if_naptan_fails(bods):
    def failing(atcos):
        raise OSError(f"https://naptan.example/?api_key={KEY} down")

    job = _job(bods, routes=True, naptan=failing)
    job.start()
    status = _finish(job)
    assert status["state"] == "done"
    assert "routes" not in status["result"]["fragment"]["bus"]
    assert any("NaPTAN failed" in note for note in status["result"]["notes"])
    assert KEY not in json.dumps(status)


@pytest.mark.parametrize("place", [None, ("", 53.4, -1.9), ("X", float("nan"), -1.9), ("X", 40.0, -1.9),
                                   ("X", "north", -1.9), ("X", 53.4)])
def test_a_route_strip_stop_needs_a_name_and_a_uk_position(place):
    assert discover._strip_stop(place) is None


def test_a_route_strip_stop_name_is_cleaned():
    assert discover._strip_stop(("Henry\nStreet\x7f  (A)", "53.4438412", -1.949)) == {
        "name": "Henry Street (A)", "lat": 53.443841, "lon": -1.949}


def test_a_failed_search_fails_the_job_and_lets_go_of_the_folder(bods):
    def failing(api_key, area, at_most):
        raise DiscoveryError("BODS data set list failed: HTTP Error 403: Forbidden")

    job = SetupJob(bods.scratch, STOPS, KEY, find=failing)
    job.start()
    status = _finish(job)
    assert status["state"] == "failed"
    assert status["message"] == "BODS data set list failed: HTTP Error 403: Forbidden"
    assert status["result"] is None
    again = _job(bods)
    again.start()
    assert _finish(again)["state"] == "done"


def test_a_failed_timetable_fails_the_job_without_showing_the_key(bods):
    def failing(api_key, noc, line=None):
        raise OSError(f"failed https://data.bus-data.dft.gov.uk/api/v1/dataset/?api_key={api_key}")

    job = _job(bods, find_datasets=failing)
    job.start()
    status = _finish(job)
    assert status["state"] == "failed"
    assert status["message"].startswith("The bus timetable couldn't be built")
    assert KEY not in json.dumps(status)
    assert timetable.load(bods.scratch) is None


def test_cancelling_a_running_job(bods):
    gate = Gate(bods.fetch)
    job = _job(bods, fetch=gate)
    job.start()
    assert gate.entered.wait(5)
    job.cancel()
    gate.release.set()
    status = _finish(job)
    assert (status["state"], status["message"]) == ("failed", "Cancelled")
    assert [p.name for p in bods.scratch.iterdir()] == [discover.LOCK_NAME]  # no download left behind


def test_cancelling_while_the_timetable_builds(bods):
    calls = []

    def fetch(url, file):
        calls.append(url)
        if len(calls) == 4:  # the timetable's first download, after the search's three
            job.cancel()
        bods.fetch(url, file)

    job = _job(bods, fetch=fetch)
    job.start()
    assert _finish(job)["message"] == "Cancelled"
    assert timetable.load(bods.scratch) is None


def test_the_roads_show_their_progress_and_cancelling_stops_them_at_once(bods):
    seen = []

    def router(url, max_bytes):
        seen.append(job.status())
        if len(seen) == 2:
            job.cancel()
        raise ValueError("no road here")

    def naptan(atcos):  # every stop 111 m from the last, so each pair is asked about
        return {atco: (f"Stop {atco}", 53.44 + i * 0.001, -1.95) for i, atco in enumerate(sorted(atcos))}

    job = _job(bods, naptan=naptan, router=router)
    start = time.monotonic()
    job.start()
    status = _finish(job)
    assert (status["state"], status["message"]) == ("failed", "Cancelled")
    assert len(seen) == 2 and time.monotonic() - start < 3  # one request a second, then no more
    assert [(s["step"], s["done"]) for s in seen] == [(discover.STEP_ROADS, 0), (discover.STEP_ROADS, 1)]
    assert seen[0]["total"] > 2
    assert timetable.load(bods.scratch) is not None  # saved before its roads were asked for


def test_cancelling_before_starting(bods):
    job = _job(bods)
    job.cancel()
    assert job.status()["state"] == "failed"
    assert job.wait(0)
    with pytest.raises(RuntimeError):
        job.start()


def test_one_job_at_a_time_per_state_folder(bods, tmp_path):
    gate = Gate(bods.fetch)
    first = _job(bods, fetch=gate)
    first.start()
    assert gate.entered.wait(5)
    second = _job(bods)
    with pytest.raises(SetupBusy):
        second.start()
    assert second.status()["state"] == "queued"
    elsewhere = _job(bods, folder=tmp_path / "other")
    elsewhere.start()  # another board's folder is its own
    assert _finish(elsewhere)["state"] == "done"
    gate.release.set()
    assert _finish(first)["state"] == "done"
    second.start()  # free again once the first is done
    assert _finish(second)["state"] == "done"
    with pytest.raises(RuntimeError):
        second.start()


def test_the_lock_file_is_private(bods):
    job = _job(bods)
    job.start()
    _finish(job)
    assert stat.S_IMODE((bods.scratch / discover.LOCK_NAME).stat().st_mode) == 0o600


# The settings, as TOML.


def _result(routes=None):
    bus = {"source": "bods", "operators": ["BNML", "HIPK"]}
    if routes:
        bus["routes"] = routes
    return {"fragment": {"bus": bus}, "lines": {"BNML": ["237", "341"], "HIPK": ["61"]},
            "document_operators": {"BNML": ["BNGN"]}, "datasets_scanned": 3}


def test_the_fragment_is_toml_for_config(bods):
    routes = {
        "341": {"stop": "A", "stops": [{"name": 'Market "Hall"\u007f', "lat": 53.443239, "lon": -1.949828},
                                       {"name": "Henry Street", "lat": 53.44384, "lon": -1.948999}]},
        "Sky 1": {"stop": "B", "stops": [{"name": "A", "lat": 53.4, "lon": -1.9},
                                         {"name": "B", "lat": 53.5, "lon": -1.8}]},
    }
    text = discover.fragment_toml(_result(routes))
    assert "# BNML: lines 237, 341 (its timetables say BNGN)\n# HIPK: line 61\n" in text
    assert tomllib.loads(text) == {"bus": {"source": "bods", "operators": ["BNML", "HIPK"], "routes": routes}}


def test_the_fragment_round_trips_from_a_real_job(bods):
    job = _job(bods, routes=True)
    job.start()
    result = _finish(job)["result"]
    assert tomllib.loads(discover.fragment_toml(result)) == result["fragment"]


# The command line.


@pytest.fixture
def cli_config(tmp_path, monkeypatch, bods):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(f'state_file = "{state / "state.json"}"\ntimezone = "Europe/London"\n'
                      "[bus]\nwalk_minutes = 4\n")
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"BODS_API_KEY={KEY}\n")
    monkeypatch.delenv("BODS_API_KEY", raising=False)
    made = []
    real = SetupJob

    def job(folder, stops, api_key, **options):
        made.append((folder, stops, api_key, options))
        return real(folder, stops, api_key, find=bods.find, fetch=bods.fetch,
                    find_datasets=_find_by_noc(bods), naptan=_naptan,
                    **{**options, "now": lambda: SUMMER_NOW})

    monkeypatch.setattr(discover, "SetupJob", job)
    return ["--config", str(config), "--secrets", str(secrets)], state, made


def test_cli_discover_prints_the_fragment(cli_config, capsys):
    from departureboard.cli import main

    args, state, made = cli_config
    code = main(["setup", "discover", "--stop", f"A={STOP_A}", "--stop", f"B={STOP_B}", "--routes", *args])
    out, err = capsys.readouterr()
    assert code == 0, err
    parsed = tomllib.loads(out)
    assert parsed["bus"]["source"] == "bods"
    assert parsed["bus"]["operators"] == ["BNML", "HIPK"]
    assert set(parsed["bus"]["routes"]) == {"61", "237", "341"}
    assert discover.STEP_DISCOVER in err and "Found 2 bus companies" in err
    folder, stops, key, options = made[0]
    assert (folder, stops, key) == (state, {"A": STOP_A, "B": STOP_B}, KEY)
    assert options["routes"] is True and options["walk_minutes"] == {"*": 4}
    assert KEY not in out + err


def test_cli_discover_shows_the_roads_every_25_and_the_last(cli_config, capsys, monkeypatch):
    from departureboard.cli import main

    class Job:
        """Moves one step on each poll: two data sets, then 60 roads, then fails (no timetable needed)."""

        def __init__(self, *args, **options):
            self.steps = [(discover.STEP_DISCOVER, done, 2) for done in range(3)]
            self.steps += [(discover.STEP_ROADS, done, 60) for done in range(61)]
            self.at = 0

        def start(self):
            pass

        def status(self):
            if self.at < len(self.steps):
                step, done, total = self.steps[self.at]
                return {"state": "running", "step": step, "done": done, "total": total, "message": "", "result": None}
            return {"state": "failed", "step": "", "done": 0, "total": 0, "message": "Stopped", "result": None}

        def wait(self, timeout=None):
            self.at += 1
            return self.at >= len(self.steps)

    monkeypatch.setattr(discover, "SetupJob", Job)
    args, _, _ = cli_config
    assert main(["setup", "discover", "--stop", f"A={STOP_A}", *args]) == 1
    shown = capsys.readouterr().err.splitlines()
    # A road a second, up to 300 of them: a line for each would bury the rest.
    assert shown == [
        *(f"{discover.STEP_DISCOVER}: {done} of 2" for done in range(3)),
        *(f"{discover.STEP_ROADS}: {done} of 60" for done in (0, 25, 50, 60)),
        "Setup failed: Stopped",
    ]


def test_cli_discover_does_not_write_config(cli_config, capsys):
    from departureboard.cli import main

    args, state, _ = cli_config
    config = Path(args[1])
    before = config.read_text()
    assert main(["setup", "discover", "--stop", f"A={STOP_A}", *args]) == 0
    assert config.read_text() == before
    assert not (state / "settings.toml").exists()


def test_cli_discover_reports_failure(cli_config, capsys):
    from departureboard.cli import main

    args, _, _ = cli_config
    assert main(["setup", "discover", "--stop", "A=1000NOWHERE1", *args]) == 1
    assert "Setup failed: No timetabled bus calls at your stops" in capsys.readouterr().err


def test_cli_discover_needs_a_key_and_a_state_folder(cli_config, tmp_path, capsys):
    from departureboard.cli import main

    args, _, _ = cli_config
    assert main(["setup", "discover", "--stop", f"A={STOP_A}", "--config", args[1],
                 "--secrets", str(tmp_path / "none.env")]) == 2
    assert "BODS_API_KEY" in capsys.readouterr().err
    bare = tmp_path / "bare.toml"
    bare.write_text("")
    assert main(["setup", "discover", "--stop", f"A={STOP_A}", "--config", str(bare), "--secrets", args[3]]) == 2
    assert "state_file" in capsys.readouterr().err
    assert main(["setup", "discover", "--stop", "nolabel", *args]) == 2
