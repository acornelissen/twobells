"""London's metro, live from TfL (sources/tfl.py), against synthetic answers in TfL's shape."""

import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from tests.paths import FIXTURES
from twobells.core.model import Status
from twobells.sources import tfl

ZONE = ZoneInfo("Europe/London")
NOW = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)  # 08:00 in London
STOPS = {"T": "940GZZLUBNK", "U": "940GZZDLCAN"}
LINES = {"940GZZLUBNK": ("central", "northern", "waterloo-city"), "940GZZDLCAN": ("dlr",)}
KEY = "0123456789abcdef0123456789abcdef"


def answer(name):
    return json.loads((FIXTURES / name).read_text())


ANSWERS = {
    "StopPoint/940GZZLUBNK/Arrivals": "tfl_arrivals_bank_synthetic.json",
    "StopPoint/940GZZDLCAN/Arrivals": "tfl_arrivals_canary_wharf_dlr_synthetic.json",
    "Line/central,northern,waterloo-city,dlr/Status": "tfl_line_status_synthetic.json",
    "StopPoint/940GZZLUBNK,940GZZDLCAN/Disruption?getFamily=false&includeRouteBlockedStops=false":
        "tfl_stop_disruption_synthetic.json",
}


class Http:
    def __init__(self, answers=ANSWERS, fail=None):
        self.asked = []
        self._answers = answers
        self._fail = fail

    def __call__(self, url, headers):
        self.asked.append((url, dict(headers)))
        path = url.removeprefix(f"{tfl.BASE_URL}/")
        if self._fail is not None and self._fail in path:
            raise HTTPError(url, 429, "Too Many Requests", {}, None)
        return answer(self._answers[path])


def source(http=None, stops=STOPS, **options):
    options.setdefault("lines", LINES)
    return tfl.TflSource(stops, ZONE, http_get=http or Http(), clock=lambda: NOW, **options)


def test_predictions_are_read_soonest_first_and_live():
    found = tfl.parse_arrivals(answer("tfl_arrivals_bank_synthetic.json"), "T", "940GZZLUBNK", NOW)
    assert [(a.line, a.destination, a.platform, a.bound, a.expected.strftime("%H:%M:%S")) for a in found] == [
        ("Waterloo & City", "Check Front of Train", "7", "Westbound", "07:00:30"),
        ("Northern", "Edgware", "4", "Northbound", "07:01:10"),
        ("Central", "Epping", "6", "Eastbound", "07:03:10"),
        ("Central", "West Ruislip", "5", "Westbound", "07:06:40"),
    ]
    # Left out: the train ending here, the Elizabeth line's (Darwin has it) and one a day off.
    northern = found[1]
    assert northern.via == "via Bank" and northern.where == "At London Bridge Platform 1"
    assert northern.line_words == "Northern line" and northern.vehicle == "066"


def test_a_train_listed_at_two_platforms_is_one_row_with_no_platform():
    found = tfl.parse_arrivals(answer("tfl_arrivals_canary_wharf_dlr_synthetic.json"), "U", "940GZZDLCAN", NOW)
    assert [(a.line_words, a.destination, a.platform) for a in found] == [("DLR", "Bank", None),
                                                                        ("DLR", "Lewisham", "1")]


def good(**changes):
    item = {"lineId": "central", "lineName": "Central", "modeName": "tube", "naptanId": "940GZZLUBNK",
            "destinationName": "Epping Underground Station", "towards": "Epping",
            "expectedArrival": "2026-10-19T07:03:00Z", "timeToStation": 180, "platformName": "Eastbound - Platform 6",
            "currentLocation": "At Liverpool Street", "vehicleId": "003"}
    item.update(changes)
    return [{key: value for key, value in item.items() if value is not None}]


@pytest.mark.parametrize("changes", [
    {"lineId": "Central Line"},  # not an id
    {"lineId": None},
    {"modeName": "bus"},  # the bus data's
    {"modeName": "overground"},  # Darwin's
    {"naptanId": "940GZZLUWLO"},  # another stop's
    {"destinationName": None, "towards": None},
    {"destinationName": "x" * 500, "towards": None},
    {"expectedArrival": "soon", "timeToStation": "180"},
    {"expectedArrival": "2026-10-19T03:00:00Z"},  # long gone
    {"expectedArrival": None, "timeToStation": -5},
    {"expectedArrival": None, "timeToStation": True},
])
def test_a_prediction_with_a_field_that_is_wrong_is_left_out(changes):
    assert tfl.parse_arrivals(good(**changes), "T", "940GZZLUBNK", NOW) == []


def test_odd_text_is_cleaned_or_left_out_and_odd_platforms_are_none():
    [arrival] = tfl.parse_arrivals(good(currentLocation="Between\nA and\tB", platformName="outbound",
                                        lineName="<b>Central</b>"), "T", "940GZZLUBNK", NOW)
    assert arrival.where == "Between A and B" and arrival.platform is None and arrival.bound is None
    assert arrival.line == "<b>Central</b>"  # text, never markup: the views escape it
    [arrival] = tfl.parse_arrivals(good(expectedArrival=None), "T", "940GZZLUBNK", NOW)
    assert arrival.expected == NOW + timedelta(seconds=180)
    with pytest.raises(ValueError, match="aren't a list"):
        tfl.parse_arrivals({"message": "nope"}, "T", "940GZZLUBNK", NOW)
    assert tfl.parse_arrivals(["not a prediction", 5, None], "T", "940GZZLUBNK", NOW) == []


def test_departures_are_live_at_their_expected_time_on_the_board_s_clock():
    found = source().fetch()
    assert [(d.mode, d.scheduled, d.service, d.destination, d.platform, d.stop, d.status, d.expected)
            for d in found] == [
        ("metro", "08:00", "Waterloo & City", "Check Front of Train", "7", "T", Status.ON_TIME, None),
        ("metro", "08:01", "Northern", "Edgware", "4", "T", Status.ON_TIME, None),
        ("metro", "08:01", "DLR", "Bank", None, "U", Status.ON_TIME, None),
    ]
    assert found[1].via == "via Bank"


def test_rows_and_directions_choose_which():
    assert len(source(rows=10).fetch()) == 6
    assert [d.destination for d in source(rows=10, directions=["epping", "Bank"]).fetch()] == ["Bank", "Epping"]


def test_one_call_a_stop_then_the_lines_status_with_the_key_in_a_header_only():
    http = Http()
    found = source(http, app_key=KEY)
    found.fetch()
    assert [url for url, _ in http.asked] == [
        "https://api.tfl.gov.uk/StopPoint/940GZZLUBNK/Arrivals",
        "https://api.tfl.gov.uk/StopPoint/940GZZDLCAN/Arrivals",
        "https://api.tfl.gov.uk/Line/central,northern,waterloo-city,dlr/Status",
    ]
    assert all(headers == {"app_key": KEY} for _, headers in http.asked)
    assert all(KEY not in url for url, _ in http.asked)
    http = Http()
    source(http).fetch()
    assert all(headers == {} for _, headers in http.asked)  # no key: anonymous


def test_the_lines_not_running_well_are_the_board_s_messages():
    found = source()
    found.fetch()
    assert found.messages == [
        "Central Line: Minor delays between White City and Liverpool Street while we fix a signal failure at "
        "Bank. Tickets are valid on buses.",
        "Waterloo & City: Service Closed.",
    ]
    statuses = tfl.parse_statuses(answer("tfl_line_status_synthetic.json"), ["central", "waterloo-city"])
    assert [(m.severity, m.kind) for m in statuses] == [("minor", "service"), ("severe", "service")]


def test_without_the_lines_status_the_times_still_come_and_the_board_keeps_its_notice():
    found = source(Http(fail="Status"))
    assert len(found.fetch()) == 3
    assert found.messages is None  # board/app.py keeps the last ones


def test_a_failure_says_so_without_the_key():
    with pytest.raises(RuntimeError, match="TfL: HTTP Error 429") as error:
        source(Http(fail="Arrivals"), app_key=KEY).fetch()
    assert KEY not in str(error.value)


def test_station_notes_are_lifts_escalators_and_step_free_access_in_force_now():
    notes = source().station_notes()
    assert [(n.text, n.kind) for n in notes] == [
        ("Bank: The lift between the street and the Northern line is not working today. Use Monument for "
         "step-free access.", "station")]


@pytest.mark.parametrize(("codes", "label", "empty"), [
    (["940GZZLUBNK"], "Underground", "No trains"),
    (["940GZZDLCAN"], "DLR", "No trains"),
    (["940GZZCRECR", "940GZZCRWMB"], "Trams", "No trams"),
    (["940GZZALGWP"], "Cable car", "No times for the cable car"),
    (["930GWMR"], "River buses", "No river buses"),
    (["940GZZLUBNK", "940GZZDLBNK"], "Metro", "Nothing due"),
])
def test_the_section_is_named_for_the_stops_chosen(codes, label, empty):
    assert tfl.label_for(codes) == label and tfl.empty_for(codes) == empty


def test_the_lines_asked_about_are_those_at_the_stops_each_once():
    found = source(stops={"T": "940GZZLUBNK", "U": "940GZZLUWLO"},
                   lines={"940GZZLUBNK": ("central", "northern"), "940GZZLUWLO": ("northern", "jubilee")})
    assert found.lines == ["central", "northern", "jubilee"]
    assert tfl.TflSource({"T": "930GWMR"}, ZONE, http_get=Http({})).line_statuses() == []  # none known: none asked
