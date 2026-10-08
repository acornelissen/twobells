from departureboard.model import Departure, Status
from departureboard.view import Style, build_view


def train(scheduled="07:12", expected=None, status=Status.ON_TIME, dest="Manchester Piccadilly"):
    return Departure("train", "Northern", dest, scheduled, expected, status)


def bus(line="341", scheduled="07:20", expected=None, status=Status.ON_TIME, dest="Hyde", stop=None):
    return Departure("bus", line, dest, scheduled, expected, status, stop=stop)


def view(trains=(), buses=(), now="07:00", stale=frozenset()):
    return build_view(
        title="GLO > MAN",
        trains=list(trains),
        buses=list(buses),
        now=now,
        stale=set(stale),
    )


def test_title_and_update_time():
    v = view(now="07:03")
    assert v.title == "GLO > MAN"
    assert v.updated == "07:03"


def test_on_time_train_row():
    [row] = view(trains=[train()]).trains.rows
    assert (row.left, row.middle, row.right, row.style) == (
        "07:12",
        "Manchester Piccadilly",
        "On time",
        Style.NORMAL,
    )


def test_late_train_shows_delay_as_text_and_alert_style():
    [row] = view(trains=[train(expected="07:19", status=Status.LATE)]).trains.rows
    assert row.right == "+7"
    assert row.style is Style.ALERT


def test_delayed_train_without_estimate():
    [row] = view(trains=[train(status=Status.LATE)]).trains.rows
    assert row.right == "Delayed"
    assert row.style is Style.ALERT


def test_cancelled_train_is_inverted_and_labelled():
    [row] = view(trains=[train(status=Status.CANCELLED)]).trains.rows
    assert row.right == "CANC"
    assert row.style is Style.INVERTED


def test_unknown_train_status():
    [row] = view(trains=[train(status=Status.UNKNOWN)]).trains.rows
    assert row.right == "?"


def test_departed_services_are_dropped():
    v = view(trains=[train("06:58"), train("07:12")], now="07:00")
    assert [r.left for r in v.trains.rows] == ["07:12"]


def test_bus_rows_use_clock_times_not_countdowns():
    [on_time, late, timetabled] = view(
        buses=[
            bus("341", "07:20"),
            bus("61", "07:25", "07:29", Status.LATE),
            bus("62", "07:40", status=Status.UNKNOWN),
        ]
    ).buses.rows
    assert (on_time.left, on_time.middle, on_time.right) == ("341", "Hyde", "07:20")
    assert (late.right, late.style) == ("07:29 +4", Style.ALERT)
    assert (timetabled.right, timetabled.style) == ("07:40 sch", Style.NORMAL)


def test_bus_row_shows_stop_label_when_known():
    [row] = view(buses=[bus(stop="A")]).buses.rows
    assert row.middle == "A: Hyde"


def test_empty_sections_say_so():
    v = view()
    assert v.trains.rows == ()
    assert v.trains.empty_text == "No trains"
    assert v.buses.empty_text == "No buses"


def test_stale_sources_are_flagged_per_section():
    v = view(stale={"bus"})
    assert v.buses.stale
    assert not v.trains.stale


def test_signature_ignores_update_time():
    assert view(now="07:00").signature() == view(now="07:01").signature()


def test_signature_changes_with_content():
    on_time = view(trains=[train()])
    late = view(trains=[train(expected="07:19", status=Status.LATE)])
    assert on_time.signature() != late.signature()


def test_signature_changes_when_staleness_changes():
    assert view().signature() != view(stale={"train"}).signature()
