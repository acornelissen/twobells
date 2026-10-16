import json
from urllib.error import HTTPError

import pytest

from tests.paths import FIXTURES
from twobells.sources.arrivalboard import Arrival, ArrivalBoard, ArrivalsRefused, parse_arrivals


def board():
    return json.loads((FIXTURES / "ldbws_manchester_arrivals.json").read_text())


def test_reads_the_trains_from_your_station():
    assert parse_arrivals(board(), "GLO") == [
        Arrival("9000002GLSP____", "22:25", "22:57", "23:14", None, False),
        Arrival("9000004GLSP____", "22:55", "23:27", "On time", None, False),
        Arrival("9000005GLSP____", "23:10", "23:42", "Cancelled", None, True),
    ]  # one with no clock time to arrive at, and one that isn't a train, are left out


def test_when_it_left_is_read_from_the_station_it_came_from_only():
    assert [a.departs for a in parse_arrivals(board(), "HDF")] == ["22:19", "22:49", None]


@pytest.mark.parametrize("change, field, value", [
    ({"eta": "soon"}, "eta", None),
    ({"eta": 2257}, "eta", None),
    ({"ata": "23:15"}, "ata", "23:15"),
    ({"ata": "<b>23:15</b>"}, "ata", None),
    ({"serviceID": "../../etc"}, "service_id", None),
    ({"isCancelled": "true"}, "cancelled", False),
    ({"previousCallingPoints": "GLO 22:25"}, "departs", None),
    ({"previousCallingPoints": [{"callingPoint": [{"locationName": "Glossop", "crs": "GLO", "st": "9am"}]}]},
     "departs", None),
])
def test_odd_values_are_left_out(change, field, value):
    raw = board()
    raw["trainServices"][0].update(change)
    assert getattr(parse_arrivals(raw, "GLO")[0], field) == value


def test_not_a_board():
    with pytest.raises(ValueError):
        parse_arrivals(["not", "a", "board"], "GLO")
    assert parse_arrivals({"trainServices": None}, "GLO") == []


class Get:
    def __init__(self, error=None):
        self.asked = []
        self.error = error

    def __call__(self, url, headers):
        self.asked.append((url, headers))
        if self.error:
            raise self.error
        return board()


def test_asks_for_the_arrival_board_of_trains_from_your_station():
    get = Get()
    found = ArrivalBoard("k3y", "https://example.org/LDBWS/", http_get=get).arrivals("MAN", "GLO")
    assert len(found) == 3
    assert get.asked == [("https://example.org/LDBWS/GetArrBoardWithDetails/MAN?numRows=10&filterCrs=GLO"
                          "&filterType=from", {"x-apikey": "k3y"})]


@pytest.mark.parametrize("crs, from_crs", [("man", "GLO"), ("MAN", "GLO&x=1"), ("../MAN", "GLO"), ("MAN", "")])
def test_refuses_a_code_that_isnt_one(crs, from_crs):
    get = Get()
    with pytest.raises(ValueError):
        ArrivalBoard("k3y", http_get=get).arrivals(crs, from_crs)
    assert get.asked == []


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_says_so_without_the_key(code):
    get = Get(HTTPError("https://example.org/?key=k3y", code, "No", {}, None))  # type: ignore[arg-type]
    with pytest.raises(ArrivalsRefused, match=f"HTTP {code}") as raised:
        ArrivalBoard("k3y", http_get=get).arrivals("MAN", "GLO")
    assert "k3y" not in str(raised.value)


def test_other_failures_are_raised_as_they_are():
    get = Get(HTTPError("https://example.org/", 500, "Down", {}, None))  # type: ignore[arg-type]
    with pytest.raises(HTTPError):
        ArrivalBoard("k3y", http_get=get).arrivals("MAN", "GLO")
