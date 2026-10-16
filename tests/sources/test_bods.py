from datetime import UTC
from urllib.parse import parse_qs, urlparse

import pytest

from tests.paths import FIXTURES
from twobells.sources.bods import (
    MAX_RESPONSE_BYTES,
    BodsVehicleSource,
    fleet_and_registration,
    parse_siri_vm,
    redact_key,
)

FIXTURE = (FIXTURES / "bods_glossop_synthetic.xml").read_bytes()
BOX = (-1.99, 53.43, -1.94, 53.46)


def test_parses_vehicles_with_a_position():
    vehicles = parse_siri_vm(FIXTURE)
    assert len(vehicles) == 4  # the one without a position is skipped
    first = vehicles[0]
    assert (first.line, first.destination, first.direction) == ("341", "Hyde", "inbound")
    assert (first.lat, first.lon, first.bearing) == (53.443502, -1.957392, 90.0)
    assert first.recorded_at == "2026-10-08T07:12:31+01:00"
    assert first.vehicle_ref == "BNML-1001"


@pytest.mark.parametrize(
    ("ref", "fleet", "registration"),
    [
        ("3017_-_AB25_CDE", "3017", "AB25 CDE"),  # High Peak: fleet number and plate
        ("290_-_AB51_BUS", "290", "AB51 BUS"),
        ("CD25EFG", "", "CD25 EFG"),  # Bee Network: the plate, unspaced
        ("CD25_EFH", "", "CD25 EFH"),  # or with an underscore for the space
        ("cd25efg", "", "CD25 EFG"),
        ("4021", "4021", ""),  # a fleet number alone
        ("10234", "10234", ""),
        ("R123_ABC", "", "R123 ABC"),  # an older plate, spaced
        ("12_-_ABC_123D", "12", "ABC 123D"),
        ("12_-_NOT A PLATE", "12", ""),  # the fleet number is still worth having
    ],
)
def test_reads_a_fleet_number_and_registration_from_the_vehicle_ref(ref, fleet, registration):
    assert fleet_and_registration(ref) == (fleet, registration)


@pytest.mark.parametrize(
    "ref",
    [
        "", "TSDUMMY", "BNML-1001", "1234567", "R123ABC",  # no fleet number, no plate it can be sure of
        "DUMMY_BUS", "1234_5678", "AB25_CDE_X", "AB25  CDE", "<b>AB25CDE</b>", "AB25CDE\n", "AB25\u00a0CDE",
        "\uff21\uff2225CDE",  # full-width letters
        "3017_-_" + "A" * 40,  # too long to be either
    ],
)
def test_an_odd_vehicle_ref_gives_neither(ref):
    assert fleet_and_registration(ref) == ("", "")


def test_parses_the_fleet_number_and_registration_of_each_vehicle():
    body = FIXTURE.replace(b"<VehicleRef>BNML-1001</VehicleRef>", b"<VehicleRef>3017_-_AB25_CDE</VehicleRef>")
    body = body.replace(b"<VehicleRef>BNML-1002</VehicleRef>", b"<VehicleRef>TSDUMMY</VehicleRef>")
    vehicles = parse_siri_vm(body)
    assert (vehicles[0].vehicle_ref, vehicles[0].fleet_number, vehicles[0].registration) == (
        "3017_-_AB25_CDE", "3017", "AB25 CDE",
    )
    assert (vehicles[1].fleet_number, vehicles[1].registration) == ("", "")


@pytest.mark.parametrize("bearing", [b"north", b"nan", b"inf", b"-inf", b"1e400"])
def test_an_unreadable_bearing_loses_only_that_bearing(bearing):
    body = FIXTURE.replace(b"<Bearing>90.0</Bearing>", b"<Bearing>" + bearing + b"</Bearing>")
    vehicles = parse_siri_vm(body)
    assert len(vehicles) == 4  # the others are kept
    assert vehicles[0].bearing is None
    assert [v.bearing for v in vehicles[1:]] == [120.0, 270.0, 95.0]


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        (b"Latitude", b"nan"), (b"Latitude", b"inf"), (b"Latitude", b"-inf"), (b"Latitude", b"1e400"),
        (b"Latitude", b"90.5"), (b"Latitude", b"-91"),
        (b"Longitude", b"nan"), (b"Longitude", b"-inf"), (b"Longitude", b"180.5"), (b"Longitude", b"-181"),
    ],
)
def test_a_position_that_isnt_on_earth_drops_only_that_vehicle(tag, value):
    old = {b"Latitude": b"<Latitude>53.443502</Latitude>", b"Longitude": b"<Longitude>-1.957392</Longitude>"}[tag]
    body = FIXTURE.replace(old, b"<" + tag + b">" + value + b"</" + tag + b">")
    vehicles = parse_siri_vm(body)
    assert len(vehicles) == 3  # the others are kept
    assert "BNML-1001" not in {v.vehicle_ref for v in vehicles}


def test_a_position_at_the_edge_of_the_map_is_kept():
    body = FIXTURE.replace(b"<Latitude>53.443502</Latitude>", b"<Latitude>-90</Latitude>")
    body = body.replace(b"<Longitude>-1.957392</Longitude>", b"<Longitude>180</Longitude>")
    first = parse_siri_vm(body)[0]
    assert (first.lat, first.lon) == (-90.0, 180.0)


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

    from twobells.core import net

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(net, "urlopen", lambda request, timeout: FakeResponse(b"x" * 11))
    with pytest.raises(ValueError, match="larger"):
        net.get_bytes("https://example.invalid/", max_bytes=10)
    monkeypatch.setattr(net, "urlopen", lambda request, timeout: FakeResponse(b"x" * 10))
    assert net.get_bytes("https://example.invalid/", max_bytes=10) == b"x" * 10


def test_positions_not_updated_for_ten_minutes_are_dropped():
    from datetime import datetime

    # The fixture's vehicles report between 07:12:20 and 07:12:35 BST (06:12 UTC).
    fresh = datetime(2026, 10, 8, 6, 22, 0, tzinfo=UTC)
    later = datetime(2026, 10, 8, 6, 22, 32, tzinfo=UTC)
    source = BodsVehicleSource("k", BOX, {"341", "237"}, http_get=lambda u, m: FIXTURE, now=lambda: fresh)
    assert len(source.fetch()) == 4
    source = BodsVehicleSource("k", BOX, {"341", "237"}, http_get=lambda u, m: FIXTURE, now=lambda: later)
    assert [v.vehicle_ref for v in source.fetch()] == ["BNML-1003"]  # 07:12:35, the only one under 10 min old


def test_lines_are_kept_whatever_their_case():
    from datetime import datetime

    # Operators write a line's letters either way: "X1" in the timetable, "x1" on the bus.
    body = FIXTURE.replace(b"<PublishedLineName>237</PublishedLineName>", b"<PublishedLineName>x1</PublishedLineName>")
    now = datetime(2026, 10, 8, 6, 13, tzinfo=UTC)
    source = BodsVehicleSource("k", BOX, {"X1"}, http_get=lambda u, m: body, now=lambda: now)
    assert [(v.line, v.vehicle_ref) for v in source.fetch()] == [("x1", "BNML-2001")]


def test_positions_with_an_unreadable_time_are_dropped():
    body = FIXTURE.replace(b"2026-10-08T07:12:31+01:00", b"yesterday-ish")
    from datetime import datetime

    now = datetime(2026, 10, 8, 6, 13, tzinfo=UTC)
    source = BodsVehicleSource("k", BOX, {"341"}, http_get=lambda u, m: body, now=lambda: now)
    assert "BNML-1001" not in {v.vehicle_ref for v in source.fetch()}


LIVE = (FIXTURES / "bods_glossop_live.xml").read_bytes()


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
