from departureboard.board.view import SIGN, Style, build_view
from departureboard.core.model import Departure, Status
from departureboard.core.walk import WalkTimes


def train(scheduled="07:12", expected=None, status=Status.ON_TIME, dest="Manchester Piccadilly"):
    return Departure("train", "Northern", dest, scheduled, expected, status)


def bus(line="341", scheduled="07:20", expected=None, status=Status.ON_TIME, dest="Hyde", stop=None):
    return Departure("bus", line, dest, scheduled, expected, status, stop=stop)


def view(trains=(), buses=(), now="07:00", stale=frozenset(), walk=None, **kwargs):
    return build_view(
        title="GLO > MAN",
        trains=list(trains),
        buses=list(buses),
        now=now,
        stale=set(stale),
        walk=walk,
        **kwargs,
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
    assert row.right == "Cancelled"
    assert row.style is Style.INVERTED


def test_unknown_train_status():
    [row] = view(trains=[train(status=Status.UNKNOWN)]).trains.rows
    assert row.right == "No info"


def test_sign_wording_for_unknown_train_status():
    [row] = view(trains=[train(status=Status.UNKNOWN)], wording=SIGN).trains.rows
    assert row.right == "No info"


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
    assert (row.stop, row.middle) == ("A", "Hyde")


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


WALK = WalkTimes(train=7, bus={"A": 6})


def test_no_leave_text_without_walk_times():
    v = view(trains=[train()], buses=[bus(stop="A")])
    assert v.trains.leave is None and v.buses.leave is None


def test_train_leave_text_names_the_departure_time():
    v = view(trains=[train("07:12"), train("07:42")], now="07:00", walk=WALK)
    assert v.trains.leave == "Leave 07:05 (07:12)"


def test_leave_text_moves_on_once_a_departure_is_out_of_reach():
    trains = [train("07:12"), train("07:42")]
    assert view(trains=trains, now="07:06", walk=WALK).trains.leave == "Leave 07:35 (07:42)"


def test_signature_changes_when_leave_time_changes():
    trains = [train("07:12"), train("07:42")]
    early = view(trains=trains, now="07:00", walk=WALK)
    later = view(trains=trains, now="07:06", walk=WALK)
    assert early.signature() != later.signature()


def test_each_bus_row_has_its_own_leave_time():
    rows = view(
        buses=[bus("341", "07:20", stop="A"), bus("61", "07:35", "07:38", Status.LATE, stop="A")],
        now="07:00",
        walk=WALK,
    ).buses.rows
    assert [r.leave for r in rows] == ["leave 07:14", "leave 07:32"]


def test_bus_row_leave_time_stays_after_it_has_passed():
    [row] = view(buses=[bus("341", "07:20", stop="A")], now="07:16", walk=WALK).buses.rows
    assert row.leave == "leave 07:14"


def test_cancelled_bus_row_has_no_leave_time():
    [row] = view(buses=[bus("341", "07:20", status=Status.CANCELLED, stop="A")], walk=WALK).buses.rows
    assert row.leave is None


def test_bus_header_drops_leave_text_when_rows_carry_it():
    v = view(trains=[train("07:12")], buses=[bus("341", "07:20", stop="A")], now="07:00", walk=WALK)
    assert v.buses.leave is None
    assert v.trains.leave == "Leave 07:05 (07:12)"


def test_train_rows_have_no_per_row_leave_time():
    [row] = view(trains=[train("07:12")], now="07:00", walk=WALK).trains.rows
    assert row.leave is None


def test_sign_wording_for_train_statuses():
    rows = view(
        trains=[
            train("07:12"),
            train("07:42", "07:49", Status.LATE),
            train("08:12", status=Status.LATE),
            train("08:42", status=Status.CANCELLED),
        ],
        wording=SIGN,
    ).trains.rows
    assert [r.right for r in rows] == ["On time", "Exp 07:49", "Delayed", "Cancelled"]


def test_sign_wording_for_cancelled_bus():
    [row] = view(buses=[bus(status=Status.CANCELLED)], wording=SIGN).buses.rows
    assert row.right == "Cancelled"


def test_sign_wording_for_leave_line():
    v = view(trains=[train("07:12")], now="07:00", walk=WALK, wording=SIGN)
    assert v.trains.leave == "Leave by 07:05"


def test_plain_leave_text_names_the_actual_departure_time_when_late():
    v = view(trains=[train("07:12", "07:20", Status.LATE)], now="07:00", walk=WALK)
    assert v.trains.leave == "Leave 07:13 (07:20)"


def test_section_records_which_row_the_leave_time_is_for():
    trains = [train("07:12"), train("07:42")]
    assert view(trains=trains, now="07:00", walk=WALK).trains.leave_for == 0
    assert view(trains=trains, now="07:06", walk=WALK).trains.leave_for == 1
    assert view(trains=trains, now="07:00").trains.leave_for is None


def test_no_clock_by_default_so_epaper_is_not_redrawn_every_minute():
    v = view(now="07:03")
    assert v.clock is None
    assert v.signature() == view(now="07:04").signature()


def test_running_clock_changes_the_signature_so_a_screen_redraws():
    a = view(now="07:03", clock="07:03:15")
    b = view(now="07:03", clock="07:03:16")
    assert a.clock == "07:03:15"
    assert a.signature() != b.signature()


def test_with_a_clock_train_leave_time_counts_down():
    walk = WalkTimes(train=7)
    v = view(trains=[train("07:12")], now="07:03", clock="07:03:00", walk=walk, wording=SIGN)
    assert v.trains.leave == "Leave in 2 min"


def test_countdown_rounds_down_so_it_never_promises_more_time():
    walk = WalkTimes(train=7)  # leave by 07:05
    v = view(trains=[train("07:12")], now="07:03", clock="07:03:40", walk=walk, wording=SIGN)
    assert v.trains.leave == "Leave in 1 min"


def test_countdown_says_now_in_the_last_minute():
    walk = WalkTimes(train=7)
    for clock in ("07:04:01", "07:05:00", "07:05:59"):
        v = view(trains=[train("07:12")], now=clock[:5], clock=clock, walk=walk, wording=SIGN)
        assert v.trains.leave == "Leave now", clock


def test_plain_countdown_names_the_train():
    walk = WalkTimes(train=7)
    v = view(trains=[train("07:12")], now="07:00", clock="07:00:00", walk=walk)
    assert v.trains.leave == "Leave in 5 min (07:12)"


def test_with_a_clock_bus_leave_times_count_down():
    walk = WalkTimes(bus={"*": 7})
    [row] = view(buses=[bus(scheduled="07:20")], now="07:03", clock="07:03:30", walk=walk).buses.rows
    assert row.leave == "leave in 9 min"


def test_without_a_clock_leave_times_stay_clock_times():
    walk = WalkTimes(train=7, bus={"*": 7})
    v = view(trains=[train("07:12")], buses=[bus(scheduled="07:20")], now="07:03", walk=walk, wording=SIGN)
    assert v.trains.leave == "Leave by 07:05"
    assert v.buses.rows[0].leave == "leave 07:13"


def test_countdown_drops_the_leave_hint_once_a_bus_is_out_of_reach():
    walk = WalkTimes(bus={"*": 7})  # 07:08 bus: leave by 07:01
    [row] = view(buses=[bus(scheduled="07:08")], now="07:02", clock="07:02:00", walk=walk).buses.rows
    assert row.leave is None


def test_countdown_still_says_now_during_the_leave_minute():
    walk = WalkTimes(bus={"*": 7})
    [row] = view(buses=[bus(scheduled="07:08")], now="07:01", clock="07:01:40", walk=walk).buses.rows
    assert row.leave == "leave now"


def test_train_row_names_its_platform():
    with_platform = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "2A")
    [row] = view(trains=[with_platform]).trains.rows
    assert row.platform == "Plat 2A"
    [row] = view(trains=[train()]).trains.rows
    assert row.platform is None


def test_signature_changes_when_the_platform_changes():
    one = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "1")
    two = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "2")
    assert view(trains=[one]).signature() != view(trains=[two]).signature()


def test_no_notice_without_messages():
    assert view().notice is None
    assert view(notices=[]).notice is None


def test_messages_become_one_notice():
    assert view(notices=["Lifts out of use."]).notice == "Lifts out of use."
    assert view(notices=["Lifts out of use.", "Buses replace trains."]).notice == (
        "Lifts out of use. / Buses replace trains."
    )


def test_notice_is_capped():
    from departureboard.board.view import NOTICE_LIMIT

    notice = view(notices=["x" * 300, "y" * 300]).notice
    assert len(notice) == NOTICE_LIMIT and notice.endswith("…")


def test_signature_changes_with_the_notice():
    assert view().signature() != view(notices=["Lifts out of use."]).signature()
