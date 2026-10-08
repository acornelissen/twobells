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
