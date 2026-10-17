from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from twobells.dashboard import performance
from twobells.dashboard.history import History, Observed, Watched
from twobells.dashboard.performance import Performance, Setup, route_parts

LONDON = ZoneInfo("Europe/London")
FRIDAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 18, 30, tzinfo=LONDON)
MANCHESTER = "GLO>MAN|Manchester Piccadilly"
HADFIELD = "GLO>MAN|Hadfield"
BUS = "A|341|Hyde Bus Station"


@pytest.fixture
def history(tmp_path):
    history = History(tmp_path)
    yield history
    history.close()


def _record(history, mode, route, day, status="on_time", delay=0, scheduled="07:42"):
    """A departure seen just before it left, then gone: a final outcome."""
    booked = datetime.combine(day, datetime.min.time(), tzinfo=LONDON) + timedelta(hours=int(scheduled[:2]),
                                                                                minutes=int(scheduled[3:]))
    leaves = booked + timedelta(minutes=delay or 0)
    row = Observed(route, booked, leaves, status, delay, delay is not None, "Northern", "x", None, None, None)
    history.observe(mode, [row], leaves - timedelta(minutes=1))
    history.observe(mode, [], leaves + timedelta(minutes=1))


def _figures(history, late=None):
    return Performance(history, lambda: NOW, lambda: Setup(late or {"train": 3, "bus": 2}))


@pytest.mark.parametrize("mode, route, parts", [
    ("train", "GLO>MAN|Manchester Piccadilly", ("Manchester Piccadilly", None)),
    ("train", "GLO|Hadfield", ("Hadfield", None)),
    ("train", "GLO>MAN bus|Manchester Piccadilly", ("Manchester Piccadilly (replacement bus)", None)),
    ("train", "PMH ferry|Ryde Pier Head", ("Ryde Pier Head (ferry)", None)),
    ("train", "GLO", ("GLO", None)),
    ("bus", "A|341|Hyde Bus Station", ("341 to Hyde Bus Station", "A")),
    ("bus", "|341|Hyde Bus Station", ("341 to Hyde Bus Station", None)),  # a stop with no label
    ("bus", "A|341|Hyde | Ashton", ("341 to Hyde | Ashton", "A")),
    ("bus", "A|341", ("A|341", None)),
])
def test_route_parts(mode, route, parts):
    assert route_parts(mode, route) == parts


def test_each_route_and_all_of_them_over_7_and_28_days(history):
    # Manchester: the last 5 days, late on Thursday, cancelled on Friday;
    # Hadfield: three days a fortnight ago.
    for back in range(5):
        _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=back))
    _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=1), "late", 4, scheduled="08:42")
    _record(history, "train", MANCHESTER, FRIDAY, "cancelled", None, scheduled="08:42")
    _record(history, "train", MANCHESTER, FRIDAY, "late", None, scheduled="09:42")  # "Delayed": not known
    for back in (14, 15, 16):
        _record(history, "train", HADFIELD, FRIDAY - timedelta(days=back), "late", 20)
    _record(history, "train", HADFIELD, FRIDAY - timedelta(days=40))  # before the 28 days

    report = _figures(history).report()
    assert report["generated_at"] == "2026-10-09T18:30:00+01:00"
    trains = report["trains"]
    assert trains["late_minutes"] == 3
    week, month = trains["all"]["periods"]
    assert week == {"span": 7, "since": "2026-10-03", "enough": True, "days": 5, "counted": 7, "right_time": 5,
                    "within_3": 5, "within_15": 6, "on_time": 5, "cancelled": 1, "unknown": 1}
    assert (month["span"], month["since"], month["days"], month["counted"]) == (28, "2026-09-12", 8, 10)
    assert month["within_15"] == 6  # Hadfield's were 20 minutes late
    assert [(r["label"], r["stop"]) for r in trains["routes"]] == [("Hadfield", None), ("Manchester Piccadilly", None)]
    hadfield = trains["routes"][0]["periods"]
    # Nothing in the last 7 days: not enough to say anything. Three days in the 28: enough.
    assert (hadfield[0]["enough"], hadfield[0]["days"], hadfield[0]["counted"]) == (False, 0, 0)
    assert (hadfield[1]["enough"], hadfield[1]["days"], hadfield[1]["right_time"]) == (True, 3, 0)
    assert report["buses"] == {"late_minutes": 2, "routes": [], "all": {"periods": [
        {**week, "enough": False, "days": 0, "counted": 0, "right_time": 0, "within_3": 0, "within_15": 0,
         "on_time": 0, "cancelled": 0, "unknown": 0},
        {**month, "enough": False, "days": 0, "counted": 0, "right_time": 0, "within_3": 0, "within_15": 0,
         "on_time": 0, "cancelled": 0, "unknown": 0},
    ]}}


def test_all_trains_are_trains_alone(history):
    # Rail replacement buses and ferries have route rows of their own, but aren't trains.
    for back in range(3):
        _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=back))
        _record(history, "train", "GLO>MAN bus|Manchester Piccadilly", FRIDAY - timedelta(days=back), "late", 20)
        _record(history, "train", "PMH ferry|Ryde Pier Head", FRIDAY - timedelta(days=back), "cancelled", None)
    trains = _figures(history).report()["trains"]
    week = trains["all"]["periods"][0]
    assert (week["counted"], week["right_time"], week["cancelled"]) == (3, 3, 0)
    assert [r["label"] for r in trains["routes"]] == [
        "Manchester Piccadilly", "Manchester Piccadilly (replacement bus)", "Ryde Pier Head (ferry)"]


def test_too_few_days_are_not_enough(history):
    _record(history, "train", MANCHESTER, FRIDAY)
    _record(history, "train", MANCHESTER, FRIDAY, scheduled="08:42")
    _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=1))
    week, month = _figures(history).report()["trains"]["all"]["periods"]
    assert (week["enough"], week["days"], week["counted"]) == (False, 2, 3)
    assert not month["enough"]
    _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=6))  # the 7 days include today
    assert _figures(history).report()["trains"]["all"]["periods"][0]["enough"]


def test_buses_are_on_time_by_the_bus_late_threshold(history):
    for back, delay in enumerate([0, 1, 2, -1, None]):
        _record(history, "bus", BUS, FRIDAY - timedelta(days=back), "unknown" if delay is None else "on_time", delay)
    buses = _figures(history, {"train": 3, "bus": 2}).report()["buses"]
    [route] = buses["routes"]
    assert (route["label"], route["stop"], buses["late_minutes"]) == ("341 to Hyde Bus Station", "A", 2)
    week = route["periods"][0]
    assert (week["counted"], week["on_time"], week["right_time"], week["unknown"]) == (4, 3, 2, 1)
    assert _figures(history, {"train": 3, "bus": 3}).report()["buses"]["all"]["periods"][0]["on_time"] == 4


def test_one_route_for_its_sheet(history):
    for back in range(3):
        _record(history, "train", MANCHESTER, FRIDAY - timedelta(days=back), "late", 5)
        _record(history, "train", HADFIELD, FRIDAY - timedelta(days=back))
    found = _figures(history).route("train", MANCHESTER)
    assert (found["label"], found["stop"], found["late_minutes"]) == ("Manchester Piccadilly", None, 3)
    week = found["periods"][0]
    assert (week["enough"], week["counted"], week["right_time"], week["within_15"]) == (True, 3, 0, 3)
    assert _figures(history).route("bus", BUS)["periods"][0]["counted"] == 0


def test_only_the_busiest_routes_are_listed(history, monkeypatch):
    monkeypatch.setattr(performance, "MAX_ROUTES", 2)
    for n, destination in enumerate(["Zeta", "Alpha", "Beta"]):
        for k in range(n + 1):
            _record(history, "train", f"GLO|{destination}", FRIDAY, scheduled=f"0{k + 6}:00")
    trains = _figures(history).report()["trains"]
    # Beta (3) and Alpha (2) are the busiest, listed by name; all of them count in the total.
    assert [r["label"] for r in trains["routes"]] == ["Alpha", "Beta"]
    assert trains["all"]["periods"][0]["counted"] == 6


# Delay Repay


def _arrived(history, day, scheduled, booked, arrived, reported=True, operator="Northern", station="MAN"):
    """A train that reached `station` at `arrived`, as reported (or, if not, by its estimate)."""
    def moment(hhmm):
        at = datetime.combine(day, datetime.min.time(), tzinfo=LONDON) + timedelta(hours=int(hhmm[:2]),
                                                                                minutes=int(hhmm[3:]))
        return at if hhmm >= scheduled else at + timedelta(days=1)  # there after midnight

    row = Watched(MANCHESTER, moment(scheduled), station, "Manchester Piccadilly", None, operator,
                  "Manchester Piccadilly", moment(booked), moment(arrived), moment(arrived) if reported else None,
                  False)
    history.watch([row], moment(arrived))
    history.settle(moment(arrived) + timedelta(minutes=2))


def _repay(history, **options):
    return Performance(history, lambda: NOW, lambda: Setup({"train": 3, "bus": 2}, **options)).report()["delay_repay"]


def test_delay_repay_lists_trains_15_or_more_minutes_late_there(history):
    _arrived(history, FRIDAY, "07:42", "08:05", "08:19")  # 14 minutes: not listed
    _arrived(history, FRIDAY, "08:42", "09:05", "09:20", reported=False)  # 15, by its estimate
    _arrived(history, FRIDAY - timedelta(days=2), "07:42", "08:05", "08:40", operator="TransPennine Express")
    _arrived(history, FRIDAY - timedelta(days=30), "07:42", "08:05", "09:05")  # before the 28 days
    _arrived(history, FRIDAY, "09:42", "10:05", "11:00", station="HDF")  # not the filter station
    assert _repay(history, station="MAN", trains=True, details=True) == {
        "station": "MAN", "station_name": "Manchester Piccadilly", "since": "2026-09-12", "least_minutes": 15,
        "off": None,
        "trains": [
            {"day": "2026-10-09", "scheduled": "08:42", "destination": "Manchester Piccadilly", "operator": "Northern",
             "booked": "09:05", "arrived": "09:20", "minutes_late": 15, "band": 15, "reported": False},
            {"day": "2026-10-07", "scheduled": "07:42", "destination": "Manchester Piccadilly",
             "operator": "TransPennine Express", "booked": "08:05", "arrived": "08:40", "minutes_late": 35,
             "band": 30, "reported": True},
        ],
    }


def test_delay_repay_after_midnight_and_at_most_so_many(history, monkeypatch):
    _arrived(history, FRIDAY - timedelta(days=1), "23:30", "23:55", "00:20")  # there the next day
    [train] = _repay(history, station="MAN", trains=True, details=True)["trains"]
    assert (train["day"], train["booked"], train["arrived"], train["minutes_late"]) == ("2026-10-08", "23:55",
                                                                                         "00:20", 25)
    monkeypatch.setattr(performance, "MAX_LISTED", 1)
    _arrived(history, FRIDAY, "07:42", "08:05", "08:40")
    assert len(_repay(history, station="MAN", trains=True, details=True)["trains"]) == 1  # the latest


def test_delay_repay_says_why_it_is_off(history):
    _arrived(history, FRIDAY, "08:42", "09:05", "09:40")
    no_filter = _repay(history, trains=True, details=True)
    assert (no_filter["off"], no_filter["station"], no_filter["trains"]) == ("no_filter", None, [])
    assert _repay(history)["off"] == "no_trains"
    # Only Service Details can say when a train that has left got there.
    no_key = _repay(history, station="MAN", trains=True)
    assert (no_key["off"], no_key["station"], no_key["trains"]) == ("no_details_key", "MAN", [])
    nothing = _repay(history, station="SHF", trains=True, details=True)
    assert (nothing["off"], nothing["station_name"], nothing["trains"]) == (None, None, [])
