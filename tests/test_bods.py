from datetime import UTC
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from departureboard.bods import MAX_RESPONSE_BYTES, BodsVehicleSource, parse_siri_vm, redact_key

FIXTURE = (Path(__file__).parent / "fixtures" / "bods_glossop_synthetic.xml").read_bytes()
BOX = (-1.99, 53.43, -1.94, 53.46)


def test_parses_vehicles_with_a_position():
    vehicles = parse_siri_vm(FIXTURE)
    assert len(vehicles) == 4  # the one without a position is skipped
    first = vehicles[0]
    assert (first.line, first.destination, first.direction) == ("341", "Hyde", "inbound")
    assert (first.lat, first.lon, first.bearing) == (53.443502, -1.957392, 90.0)
    assert first.recorded_at == "2026-10-08T07:12:31+01:00"
    assert first.vehicle_ref == "BNML-1001"


def test_falls_back_to_line_ref_without_a_published_name():
    body = FIXTURE.replace(b"<PublishedLineName>237</PublishedLineName>", b"")
    assert "237" in {v.line for v in parse_siri_vm(body)}


def test_refuses_a_document_type_declaration():
    evil = b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><Siri/>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_siri_vm(evil)


def test_refuses_a_document_type_declaration_after_a_long_comment():
    evil = b"<!--" + b" " * 5000 + b'--><!DOCTYPE lol [<!ENTITY a "aaaa">]><Siri/>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_siri_vm(evil)


def test_refuses_a_document_type_declaration_in_utf16():
    evil = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><Siri/>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_siri_vm(evil.encode("utf-16"))


def test_reads_a_utf16_document():
    body = '<?xml version="1.0" encoding="UTF-16"?><Siri xmlns="http://www.siri.org.uk/siri"><ServiceDelivery/></Siri>'
    assert parse_siri_vm(body.encode("utf-16")) == []


def test_empty_delivery_has_no_vehicles():
    assert parse_siri_vm(b'<Siri xmlns="http://www.siri.org.uk/siri"><ServiceDelivery/></Siri>') == []


def test_source_asks_for_the_box_in_bods_order_and_keeps_only_its_lines():
    calls = []

    def fake_get(url, max_bytes):
        calls.append((url, max_bytes))
        return FIXTURE

    from datetime import datetime

    fixture_time = datetime(2026, 10, 8, 6, 13, tzinfo=UTC)
    source = BodsVehicleSource("secret", BOX, {"341"}, http_get=fake_get, now=lambda: fixture_time)
    vehicles = source.fetch()
    assert {v.line for v in vehicles} == {"341"}
    url, max_bytes = calls[0]
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/api/v1/datafeed/"
    # minLongitude, minLatitude, maxLongitude, maxLatitude
    assert query["boundingBox"] == ["-1.99000,53.43000,-1.94000,53.46000"]
    assert query["api_key"] == ["secret"]
    assert max_bytes == MAX_RESPONSE_BYTES


def test_errors_never_carry_the_api_key():
    def failing_get(url, max_bytes):
        raise OSError(f"could not reach {url}")

    source = BodsVehicleSource("secret", BOX, {"341"}, http_get=failing_get)
    with pytest.raises(RuntimeError) as error:
        source.fetch()
    assert "secret" not in str(error.value)
    assert "api_key=REDACTED" in str(error.value)
    assert error.value.__cause__ is None


def test_redact_key():
    assert redact_key("https://x/?boundingBox=1&api_key=abc123&y=2") == "https://x/?boundingBox=1&api_key=REDACTED&y=2"


def test_get_bytes_refuses_an_oversized_body(monkeypatch):
    import io

    from departureboard import http

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(http, "urlopen", lambda request, timeout: FakeResponse(b"x" * 11))
    with pytest.raises(ValueError, match="larger"):
        http.get_bytes("https://example.invalid/", max_bytes=10)
    monkeypatch.setattr(http, "urlopen", lambda request, timeout: FakeResponse(b"x" * 10))
    assert http.get_bytes("https://example.invalid/", max_bytes=10) == b"x" * 10


def test_positions_not_updated_for_five_minutes_are_dropped():
    from datetime import datetime

    # The fixture's vehicles report between 07:12:20 and 07:12:35 BST (06:12 UTC).
    fresh = datetime(2026, 10, 8, 6, 15, 0, tzinfo=UTC)
    later = datetime(2026, 10, 8, 6, 17, 32, tzinfo=UTC)
    source = BodsVehicleSource("k", BOX, {"341", "237"}, http_get=lambda u, m: FIXTURE, now=lambda: fresh)
    assert len(source.fetch()) == 4
    source = BodsVehicleSource("k", BOX, {"341", "237"}, http_get=lambda u, m: FIXTURE, now=lambda: later)
    assert [v.vehicle_ref for v in source.fetch()] == ["BNML-1003"]  # 07:12:35, the only one under 5 min old


def test_positions_with_an_unreadable_time_are_dropped():
    body = FIXTURE.replace(b"2026-10-08T07:12:31+01:00", b"yesterday-ish")
    from datetime import datetime

    now = datetime(2026, 10, 8, 6, 13, tzinfo=UTC)
    source = BodsVehicleSource("k", BOX, {"341"}, http_get=lambda u, m: body, now=lambda: now)
    assert "BNML-1001" not in {v.vehicle_ref for v in source.fetch()}


LIVE = (Path(__file__).parent / "fixtures" / "bods_glossop_live.xml").read_bytes()


def test_reads_the_journey_fields_for_matching_a_timetable():
    from datetime import datetime

    first = parse_siri_vm(LIVE)[0]
    assert (first.operator, first.line, first.direction) == ("BNML", "237", "outbound")
    assert first.origin_ref == "1800ANIN0K1"
    assert first.origin_aimed == datetime(2026, 10, 9, 16, 35, tzinfo=UTC)
    assert first.journey_ref == "1175"  # DatedVehicleJourneyRef, not the extension's JourneyCode
    assert first.block == "T145"
    assert first.bearing is None
    assert first.origin_name == "Ashton_under_Lyne_Interchange"  # as sent; enrich tidies it


def test_journey_fields_are_empty_when_not_sent():
    first = parse_siri_vm(FIXTURE)[0]  # the synthetic feed has no origin or journey refs
    assert (first.origin_ref, first.origin_aimed, first.journey_ref, first.block) == ("", None, "", "")
    assert first.origin_name == ""
    assert first.operator == "BNML"


def test_an_unreadable_or_zoneless_origin_time_is_dropped():
    garbled = LIVE.replace(b"2026-10-09T16:35:00+00:00", b"soon")
    assert parse_siri_vm(garbled)[0].origin_aimed is None
    zoneless = LIVE.replace(b"2026-10-09T16:35:00+00:00", b"2026-10-09T16:35:00")
    assert parse_siri_vm(zoneless)[0].origin_aimed is None  # local or UTC? Can't tell, so not used
