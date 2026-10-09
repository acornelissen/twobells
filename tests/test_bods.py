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


def test_empty_delivery_has_no_vehicles():
    assert parse_siri_vm(b'<Siri xmlns="http://www.siri.org.uk/siri"><ServiceDelivery/></Siri>') == []


def test_source_asks_for_the_box_in_bods_order_and_keeps_only_its_lines():
    calls = []

    def fake_get(url, max_bytes):
        calls.append((url, max_bytes))
        return FIXTURE

    source = BodsVehicleSource("secret", BOX, {"341"}, http_get=fake_get)
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
