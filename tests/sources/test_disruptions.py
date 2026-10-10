from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from departureboard.core import net
from departureboard.core.safexml import DoctypeError
from departureboard.dashboard.snapshot import row_id
from departureboard.sources import disruptions
from departureboard.sources.disruptions import (
    BusArea,
    BusDisruptionSource,
    RailArea,
    TrainIncidentSource,
    bus_disruptions,
    disruptions_part,
    kb_url_is_placeholder,
    parse_kb_incidents,
    parse_siri_sx,
    plain_text,
    safe_link,
    train_disruptions,
)
from departureboard.timetable.model import StopPosition
from tests.paths import FIXTURES
from tests.sources.test_livebus import EAST, WEST, schedule

SX = FIXTURES / "bods_siri_sx_glossop.xml"
KB = FIXTURES / "kb_incidents_synthetic.xml"
LONDON = ZoneInfo("Europe/London")
UTC = UTC
CAPTURED = datetime(2026, 10, 9, 22, 14, tzinfo=UTC)  # when the SIRI-SX capture was taken

HADFIELD_ROADWORKS = "f6811095-2129-4149-b41c-5d0f62841166"  # 237 (BNML), from 12 Oct
STOCKPORT_STOP = "f467f0b9-9628-45de-8b00-ee0b35289992"  # 394 (HIPK) among others, from tonight
REMEMBRANCE = "0f577fbb-d340-4522-bedf-8a0e1de2c221"  # 237 (BNML) and 341 (BNGN) at our stops, 8 Nov
KIRKLEES_393 = "2f05a1c3-3441-49f2-8a68-2132c938f29b"  # a 393, but ESTS's in Huddersfield
LIFT = "fafdf849-95b0-4a1c-9c85-24b4f2511162"  # all lines, all operators, at Heaton Park tram stop


def situations():
    return parse_siri_sx(SX.read_bytes())


def our_schedule():
    """Our lines as the timetable has them: 341 and 237 (BNML), and High
    Peak's 394, 393 and 61 (HIPK)."""
    hipk = [replace(EAST, line=line, operator="HIPK") for line in ("394", "393", "61")]
    return schedule(EAST, WEST, *hipk)


def area(**positions):
    plan = our_schedule()
    return BusArea.from_schedule(replace(plan, positions={**plan.positions, **positions}))


# A positioned stop west of Glossop, as a fuller timetable has one: it puts
# the Hadfield roadworks' stops at Brookfield inside our area.
DINTING_LANE = {"1000DGDL0001": StopPosition("Dinting Lane", 53.452, -1.975)}


def keys(found):
    return [d.key for d in found]


# The trimmed real capture


def test_the_fixture_is_a_small_real_capture():
    body = SX.read_bytes()
    assert len(body) <= 80_000
    assert b"<!DOCTYPE" not in body
    assert len(situations()) == 9


def test_situations_are_read_whole():
    found = {s.number: s for s in situations()}
    hadfield = found[HADFIELD_ROADWORKS]
    assert hadfield.summary == "Station Road, Hadfield - Roadworks"
    assert hadfield.reason == "Roadworks" and hadfield.planned is True
    assert hadfield.periods == ((datetime(2026, 10, 12, 7, 0, tzinfo=UTC), datetime(2026, 10, 30, 16, 0, tzinfo=UTC)),)
    assert ("237", "BNML") in hadfield.consequences[0].lines
    assert hadfield.consequences[0].severity == "normal"
    lift = found[LIFT]
    assert lift.consequences[0].all_lines and lift.consequences[0].lines == frozenset()
    assert lift.link == "https://tfgm.com/lift-replacement"
    assert found[KIRKLEES_393].link is None  # wymetro.com isn't one of ours
    assert found["d4f83d44-29d7-4349-bc80-0b6d5554969e"].reason == "Roadworks"


def test_a_notice_on_our_line_far_from_our_stops_is_left_out():
    # The Stockport Road closure names our 394, but its stops are in
    # Stockport; the Hadfield roadworks' stops are outside this small area.
    assert bus_disruptions(situations(), area(), CAPTURED) == []


def test_a_notice_on_our_line_with_a_stop_in_our_area_is_kept_as_line():
    found = bus_disruptions(situations(), area(**DINTING_LANE), CAPTURED)
    assert keys(found) == [HADFIELD_ROADWORKS]  # Stockport is still too far
    hadfield = found[0]
    assert hadfield.relevance == "line" and hadfield.stops == ()
    assert hadfield.lines == ("237",) and hadfield.mode == "bus" and hadfield.severity == "normal"
    assert "Brookfield" in hadfield.places
    assert hadfield.summary.startswith("Due to") and len(hadfield.summary) <= 500


def test_a_notice_on_our_line_at_a_stop_on_our_routes_is_nearby():
    brookfield = StopPosition("Brookfield", 53.452107, -1.979437)
    found = bus_disruptions(situations(), area(**{"1000DDSL4374": brookfield}), CAPTURED)
    assert keys(found) == [HADFIELD_ROADWORKS]
    assert found[0].relevance == "nearby"


def test_a_line_must_be_run_by_our_operator_for_it():
    found = {d.key: d for d in bus_disruptions(situations(), area(), CAPTURED)}
    assert KIRKLEES_393 not in found  # line 393, but ESTS's
    assert "57d733c9-ca95-40a4-a9f4-2f329a87d830" not in found  # line 61, but Arriva's in Liverpool
    assert "9585bf47-ecfc-4555-a13a-4dff27a96de2" not in found  # High Peak, but the 58


def test_a_situation_at_our_stop_counts_whatever_its_lines():
    near_remembrance = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    found = {d.key: d for d in bus_disruptions(situations(), area(), near_remembrance)}
    remembrance = found[REMEMBRANCE]
    assert remembrance.relevance == "your_stop"
    assert remembrance.stops == ("A", "B")  # Henry Street and St Marys Road are our stops
    assert remembrance.lines == ("237",)  # its 341 is BNGN's; ours is BNML's
    assert "Henry Street" in remembrance.places and "George Street" in remembrance.places
    assert remembrance.periods[0][0] == datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def test_a_stop_on_our_routes_counts_only_when_its_consequence_names_no_lines():
    heaton = StopPosition("Heaton Park", 53.53, -2.25)
    assert LIFT not in keys(bus_disruptions(situations(), area(), CAPTURED))
    found = bus_disruptions(situations(), area(**{"9400ZZMAHEA": heaton}), CAPTURED)
    lift = {d.key: d for d in found}[LIFT]
    assert lift.relevance == "nearby"
    # The Stockport closure lists its own lines, so its stops alone wouldn't do.
    stockport = next(s for s in situations() if s.number == STOCKPORT_STOP)
    others = replace(stockport, consequences=tuple(
        replace(c, lines=frozenset(p for p in c.lines if p[0] != "394")) for c in stockport.consequences))
    stops = {s.atco: StopPosition(s.name, 53.4, -2.15) for c in stockport.consequences for s in c.stops}
    assert bus_disruptions([others], area(**stops), CAPTURED) == []


SIRI = "http://www.siri.org.uk/siri"


def sx(*situations_xml, root="Siri"):
    return (f'<?xml version="1.0"?><{root} xmlns="{SIRI}"><ServiceDelivery><SituationExchangeDelivery>'
            f'<Situations>{"".join(situations_xml)}</Situations></SituationExchangeDelivery>'
            f'</ServiceDelivery></{root}>').encode()


def situation(number="S1", start="2026-10-09T20:00:00Z", end="2026-10-10T20:00:00Z", affects="",
              extra="", summary="Something", planned="true", severity="normal"):
    end_xml = f"<EndTime>{end}</EndTime>" if end else ""
    return (f"<PtSituationElement><SituationNumber>{number}</SituationNumber><Progress>open</Progress>"
            f"<ValidityPeriod><StartTime>{start}</StartTime>{end_xml}</ValidityPeriod>"
            f"<MiscellaneousReason>roadClosed</MiscellaneousReason><Planned>{planned}</Planned>"
            f"<Summary>{summary}</Summary><Description>Details.</Description>{extra}"
            f"<Consequences><Consequence><Severity>{severity}</Severity><Affects>{affects}</Affects>"
            f"</Consequence></Consequences></PtSituationElement>")


def all_lines(operator, stops):
    points = "".join(
        f"<AffectedStopPoint><StopPointRef>{atco}</StopPointRef><StopPointName>{atco}</StopPointName>"
        f"<Location><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></Location></AffectedStopPoint>"
        for atco, lat, lon in stops)
    return (f"<Operators><AffectedOperator><OperatorRef>{operator}</OperatorRef></AffectedOperator></Operators>"
            f"<Networks><AffectedNetwork><VehicleMode>bus</VehicleMode><AllLines/></AffectedNetwork></Networks>"
            f"<StopPoints>{points}</StopPoints>")


def test_all_lines_of_our_operator_count_only_within_our_area():
    glossop = ("1000DGXX0001", 53.4435, -1.9550)  # in the box around our positioned stops
    stockport = ("0600XX0001", 53.4100, -2.1600)
    inside = situation("IN", affects=all_lines("BNML", [glossop]))
    outside = situation("OUT", affects=all_lines("BNML", [stockport]))
    nowhere = situation("NONE", affects=all_lines("BNML", []))
    others = situation("OTHER", affects=all_lines("SCMN", [glossop]))
    found = bus_disruptions(parse_siri_sx(sx(inside, outside, nowhere, others)), area(), CAPTURED)
    assert keys(found) == ["IN"]
    assert found[0].lines == ("237", "341") and found[0].stops == ()
    assert found[0].reason == "Road closed" and found[0].relevance == "line"


def our_line(line, stops=(("1000DGXX0001", 53.4435, -1.9550),)):
    """A consequence on our line (BNML's), at stops given as (ATCO, lat, lon);
    by default one in our area but not on our routes."""
    points = "".join(
        f"<AffectedStopPoint><StopPointRef>{atco}</StopPointRef><StopPointName>{atco}</StopPointName>"
        f"<Location><Longitude>{lon}</Longitude><Latitude>{lat}</Latitude></Location></AffectedStopPoint>"
        for atco, lat, lon in stops)
    return ("<Networks><AffectedNetwork><AffectedLine><AffectedOperator><OperatorRef>BNML</OperatorRef>"
            f"</AffectedOperator><LineRef>{line}</LineRef></AffectedLine></AffectedNetwork></Networks>"
            f"<StopPoints>{points}</StopPoints>")


def test_our_line_counts_only_with_an_affected_stop_in_our_area():
    # Like the live Stockport Road (394) and Mayfield Depot (219) notices:
    # our line, but nowhere near our stops.
    mayfield = ("1800NE43431", 53.4740, -2.2260)
    body = sx(
        situation("FAR", affects=our_line("237", [mayfield])),
        situation("NO_STOPS", affects=our_line("237", [])),
        situation("NEAR", affects=our_line("237", [mayfield, ("1000DGXX0001", 53.4435, -1.9550)])),
    )
    found = bus_disruptions(parse_siri_sx(body), area(), CAPTURED)
    assert keys(found) == ["NEAR"]
    assert found[0].relevance == "line" and found[0].places == ("1000DGXX0001",)


def test_lines_come_only_from_the_parts_of_a_notice_in_our_area():
    far = ("<Consequence><Severity>severe</Severity><Affects>"
           + our_line("341", [("1800NE43431", 53.4740, -2.2260)]) + "</Affects></Consequence>")
    body = sx(situation("TWO", affects=our_line("237")).replace("</Consequences>", far + "</Consequences>"))
    found = bus_disruptions(parse_siri_sx(body), area(), CAPTURED)
    assert found[0].lines == ("237",)


def test_the_most_relevant_come_first_then_those_in_force_now():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    henry_street = ("1000DGHS0900", 53.44384, -1.948999)  # our stop A
    st_marys = ("1000DGHS3406", 53.443511, -1.955759)  # on our routes
    body = sx(
        situation("LINE_NOW", start="2026-10-09T08:00:00Z", affects=our_line("341")),
        situation("NEARBY_SOON", start="2026-10-10T08:00:00Z", affects=our_line("341", [st_marys])),
        situation("NEARBY_NOW", start="2026-10-09T09:00:00Z", affects=our_line("341", [st_marys])),
        situation("STOP_LATER", start="2026-10-14T08:00:00Z", end="2026-10-15T08:00:00Z",
                  affects=our_line("341", [henry_street])),
    )
    found = bus_disruptions(parse_siri_sx(body), area(), now)
    assert keys(found) == ["STOP_LATER", "NEARBY_NOW", "NEARBY_SOON", "LINE_NOW"]
    assert [d.relevance for d in found] == ["your_stop", "nearby", "nearby", "line"]


def test_validity_windows():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    line = our_line("341")
    body = sx(
        situation("ENDED", start="2026-10-01T00:00:00Z", end="2026-10-09T11:59:00Z", affects=line),
        situation("NOW", start="2026-10-09T11:00:00+01:00", end=None, affects=line),
        situation("WEEK", start="2026-10-16T11:59:00Z", end="2026-10-16T13:00:00Z", affects=line),
        situation("LATER", start="2026-10-16T12:01:00Z", end="2026-10-17T00:00:00Z", affects=line),
        situation("OLD_OPEN", start="2025-01-01T00:00:00Z", end=None, affects=line),
    )
    found = bus_disruptions(parse_siri_sx(body), area(), now)
    assert keys(found) == ["OLD_OPEN", "NOW", "WEEK"]
    assert found[0].periods == ((datetime(2025, 1, 1, tzinfo=UTC), None),)


def test_only_the_periods_in_the_next_seven_days_are_kept():
    nightly = "".join(
        f"<ValidityPeriod><StartTime>2026-10-{d:02d}T19:00:00Z</StartTime>"
        f"<EndTime>2026-10-{d + 1:02d}T04:00:00Z</EndTime></ValidityPeriod>" for d in range(1, 28))
    line = our_line("237")
    later = situation("NIGHTS", start="2026-10-27T19:00:00Z", end="2026-10-28T04:00:00Z", affects=line)
    body = sx(later.replace("<Progress>open</Progress>", f"<Progress>open</Progress>{nightly}"))
    found = bus_disruptions(parse_siri_sx(body), area(), datetime(2026, 10, 9, 12, 0, tzinfo=UTC))
    periods = found[0].periods
    # Last night's ended at 04:00; the next seven nights start by the 15th.
    assert periods[0] == (datetime(2026, 10, 9, 19, 0, tzinfo=UTC), datetime(2026, 10, 10, 4, 0, tzinfo=UTC))
    assert [start.day for start, _ in periods] == [9, 10, 11, 12, 13, 14, 15]


def test_wrong_types_are_read_as_nothing_rather_than_trusted():
    line = ("<Networks><AffectedNetwork><AffectedLine><AffectedOperator><OperatorRef>BNML</OperatorRef>"
            "</AffectedOperator><LineRef>341</LineRef></AffectedLine></AffectedNetwork></Networks>"
            "<StopPoints><AffectedStopPoint><StopPointRef>1000DGHS0900</StopPointRef><StopPointName>Henry Street"
            "</StopPointName><Location><Longitude>west</Longitude><Latitude>NaN</Latitude></Location>"
            "</AffectedStopPoint></StopPoints>")
    body = sx(
        situation("ODD", planned="yes", severity="catastrophic", affects=line),
        situation("NO_TIME", start="tomorrow", affects=line),
        situation("BARE_TIME", start="2026-10-09T20:00:00", affects=line),
        situation("BACKWARDS", start="2026-10-10T20:00:00Z", end="2026-10-09T20:00:00Z", affects=line),
        situation("", affects=line),
        situation("EMPTY", summary="", affects=line).replace("<Description>Details.</Description>", ""),
        situation("CLOSED", affects=line).replace("<Progress>open</Progress>", "<Progress>closed</Progress>"),
    )
    found = parse_siri_sx(body)
    assert [s.number for s in found] == ["ODD"]
    odd = found[0]
    assert odd.planned is False and odd.consequences[0].severity == "unknown"
    stop = odd.consequences[0].stops[0]
    assert (stop.lat, stop.lon) == (None, None)
    item = bus_disruptions(found, area(), CAPTURED)[0]
    assert item.stops == ("A",) and item.severity == "unknown"


def test_text_is_plain_and_cut():
    assert plain_text("<p>Buses <b>divert</b> &amp; wait.</p><p>Next</p>") == "Buses divert & wait. Next"
    assert plain_text("<script>alert(1)</script>Safe") == "Safe"
    assert plain_text("a\x00b\nc") == "a b c"
    long = plain_text("word " * 200)
    assert len(long) == 500 and long.endswith("…")
    assert plain_text(None) == ""


@pytest.mark.parametrize("link, kept", [
    ("https://tfgm.com/lift-replacement", True),
    ("https://www.nationalrail.co.uk/x", True),
    ("https://wymetro.com/plan-a-journey/", False),
    ("http://tfgm.com/x", False),
    ("https://tfgm.com.evil.example/x", False),
    ("https://user:pw@tfgm.com/x", False),
    ("https://tfgm.com:8443/x", False),
    ("javascript:alert(1)", False),
    ("https://tfgm.com/a b", False),
    ("https://tfgm.com/" + "x" * 400, False),
    ("", False),
])
def test_links_only_to_known_operators_over_https(link, kept):
    assert (safe_link(link) == link) is kept


def test_a_doctype_is_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE Siri [<!ENTITY x "boom">]><Siri xmlns="' + SIRI.encode() + b'">&x;</Siri>'
    with pytest.raises(DoctypeError):
        parse_siri_sx(evil)
    with pytest.raises(DoctypeError):
        parse_kb_incidents(evil)


def test_a_document_is_read_in_chunks():
    body = SX.read_bytes()
    chunked = parse_siri_sx(body[i:i + 777] for i in range(0, len(body), 777))
    assert [s.number for s in chunked] == [s.number for s in situations()]


# Fetching


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.at = 0

    def read(self, n):
        chunk = self.body[self.at:self.at + n]
        self.at += n
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_response_bigger_than_the_cap_is_refused(monkeypatch):
    sent = []
    monkeypatch.setattr(net, "urlopen", lambda request, timeout: sent.append(request) or FakeResponse(b"x" * 300_000))
    with pytest.raises(net.TooLarge):
        list(net.stream("https://example.test/feed", {"x-apikey": "k"}, 200_000))
    assert sent[0].get_header("X-apikey") == "k"
    with pytest.raises(ValueError, match="https"):
        list(net.stream("http://example.test/feed", {}, 10))


def test_the_bus_source_asks_siri_sx_with_the_key_and_never_repeats_it():
    asked = []

    def opener(url, headers, cap):
        asked.append((url, headers, cap))
        return [SX.read_bytes()]

    found = BusDisruptionSource("s3cret", open_stream=opener).fetch()
    assert len(found) == 9
    assert asked == [("https://data.bus-data.dft.gov.uk/api/v1/siri-sx/?api_key=s3cret", {}, 12_000_000)]

    def failing(url, headers, cap):
        raise OSError(f"could not reach {url}")

    with pytest.raises(RuntimeError) as caught:
        BusDisruptionSource("s3cret", open_stream=failing).fetch()
    assert "s3cret" not in str(caught.value)

    def too_big(url, headers, cap):
        raise net.TooLarge(f"Response larger than {cap} bytes")

    with pytest.raises(RuntimeError, match="larger than 12000000"):
        BusDisruptionSource("s3cret", open_stream=too_big).fetch()
    with pytest.raises(RuntimeError, match="DOCTYPE"):
        BusDisruptionSource("k", open_stream=lambda *a: [b'<!DOCTYPE x><x/>']).fetch()
    with pytest.raises(RuntimeError, match="isn't XML"):
        BusDisruptionSource("k", open_stream=lambda *a: [b"<Siri>"]).fetch()


# Rail incidents (synthetic Knowledgebase fixture)


STATIONS = {"GLO": "Glossop", "MAN": "Manchester Piccadilly", "DTG": "Dinting", "HDF": "Hadfield",
            "GUI": "Guide Bridge", "BDB": "Broadbottom"}
RAIL = RailArea(frozenset({"NT"}), STATIONS, "GLO")
TONIGHT = datetime(2026, 10, 9, 22, 0, tzinfo=LONDON)


def test_kb_incidents_are_read():
    found = {i.number: i for i in parse_kb_incidents(KB.read_bytes())}
    assert set(found) == {f"A1B2C3D4E5F6000{n}" for n in (1, 2, 3, 4, 6)}  # 5 is cleared
    works = found["A1B2C3D4E5F60001"]
    assert works.planned is True and works.operators == frozenset({"NT"}) and works.severity == "normal"
    assert works.description == "Buses replace trains between Guide Bridge and Hadfield. Allow an extra 30 minutes."
    assert works.routes == "between Guide Bridge and Glossop / Hadfield"
    assert works.link == "https://www.nationalrail.co.uk/service-disruptions/glossop-20261015/"
    signals = found["A1B2C3D4E5F60002"]
    assert signals.severity == "severe" and signals.link is None  # plain http is dropped
    assert "alert" not in signals.description
    assert signals.periods == ((datetime(2026, 10, 9, 21, 40, tzinfo=timezone(timedelta(hours=1))), None),)


def test_only_our_operators_at_our_stations_soon_are_kept():
    found = train_disruptions(parse_kb_incidents(KB.read_bytes()), RAIL, TONIGHT)
    # The works name Glossop, our own station, so they come first.
    assert keys(found) == ["A1B2C3D4E5F60001", "A1B2C3D4E5F60002"]
    works, signals = found
    assert signals.mode == "train" and signals.planned is False and signals.places == ("Dinting",)
    assert signals.relevance == "line"
    assert works.planned is True and works.title.startswith("Engineering works")
    assert set(works.places) == {"Glossop", "Guide Bridge", "Hadfield", "Manchester Piccadilly"}
    assert works.lines == () and works.stops == () and works.relevance == "your_stop"


def test_without_our_own_station_rail_incidents_are_in_force_first():
    found = train_disruptions(parse_kb_incidents(KB.read_bytes()), replace(RAIL, home=""), TONIGHT)
    assert keys(found) == ["A1B2C3D4E5F60002", "A1B2C3D4E5F60001"]  # the signals now, the works next week
    assert {d.relevance for d in found} == {"line"}


def test_a_station_code_counts_as_a_mention():
    found = train_disruptions(parse_kb_incidents(KB.read_bytes()), RailArea(frozenset({"NT"}), {"DTG": ""}), TONIGHT)
    assert keys(found) == ["A1B2C3D4E5F60002"]


def test_without_our_operators_nothing_is_kept():
    incidents = parse_kb_incidents(KB.read_bytes())
    assert train_disruptions(incidents, None, TONIGHT) == []
    assert train_disruptions(incidents, RailArea(frozenset(), STATIONS), TONIGHT) == []


def test_the_train_source_sends_its_key_in_a_header_and_never_repeats_it():
    asked = []

    def opener(url, headers, cap):
        asked.append((url, dict(headers), cap))
        return [KB.read_bytes()]

    found = TrainIncidentSource("kbkey", "https://api.example.test/incidents", open_stream=opener).fetch()
    assert len(found) == 5
    assert asked == [("https://api.example.test/incidents", {"x-apikey": "kbkey"}, disruptions.MAX_KB_BYTES)]

    def failing(url, headers, cap):
        raise OSError(f"refused key kbkey at {url}")

    with pytest.raises(RuntimeError) as caught:
        TrainIncidentSource("kbkey", "https://api.example.test/incidents", open_stream=failing).fetch()
    assert "kbkey" not in str(caught.value)


def test_the_placeholder_address_is_known():
    assert kb_url_is_placeholder(disruptions.KB_URL_PLACEHOLDER)
    assert not kb_url_is_placeholder("https://api1.raildata.org.uk/incidents")


# The snapshot's section


def test_the_section_has_both_sources_and_their_items():
    bus = bus_disruptions(situations(), area(**DINTING_LANE), CAPTURED)
    train = train_disruptions(parse_kb_incidents(KB.read_bytes()), RAIL, TONIGHT)
    part = disruptions_part(bus, {"last_success": None, "stale": False, "error": None, "note": None}, train, None,
                            LONDON)
    assert part["status"]["train"] is None
    items = part["items"]
    assert [i["mode"] for i in items] == ["bus", "train", "train"]
    hadfield = items[0]
    assert hadfield["id"] == row_id("disruption", f"bus|{HADFIELD_ROADWORKS}")
    assert hadfield["from_at"] == "2026-10-12T08:00:00+01:00" and hadfield["to_at"] == "2026-10-30T16:00:00+00:00"
    assert hadfield["periods"] == [{"from_at": hadfield["from_at"], "to_at": hadfield["to_at"]}]
    assert hadfield["lines"] == ["237"] and hadfield["severity"] == "normal" and hadfield["link"] is None
    assert hadfield["relevance"] == "line"
    assert [i["relevance"] for i in items[1:]] == ["your_stop", "line"]
    assert items[2]["to_at"] is None and items[2]["severity"] == "severe"
    assert HADFIELD_ROADWORKS not in str(part)  # feed numbers stay on the server
