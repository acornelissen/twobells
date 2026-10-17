import dataclasses

import pytest

from twobells.board.view import PLAIN, SIGN, Style, build_view, destination_text
from twobells.core.model import Departure, Status
from twobells.core.walk import WalkTimes


def train(scheduled="07:12", expected=None, status=Status.ON_TIME, dest="Manchester Piccadilly"):
    return Departure("train", "Northern", dest, scheduled, expected, status)


def bus(line="341", scheduled="07:20", expected=None, status=Status.ON_TIME, dest="Hyde", stop=None):
    return Departure("bus", line, dest, scheduled, expected, status, stop=stop)


def view(trains=(), buses=(), now="07:00", stale=frozenset(), walk=None, **kwargs):
    return build_view(
        title="GLO > MAN",
        departures={"train": list(trains), "bus": list(buses)},
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
    [row] = view(trains=[train()]).section("train").rows
    assert (row.left, row.middle, row.right, row.style) == (
        "07:12",
        "Manchester Piccadilly",
        "On time",
        Style.NORMAL,
    )


def test_late_train_shows_expected_time_as_text_and_alert_style():
    [row] = view(trains=[train(expected="07:19", status=Status.LATE)]).section("train").rows
    assert row.right == "Exp 07:19"
    assert row.style is Style.ALERT


def test_delayed_train_without_estimate():
    [row] = view(trains=[train(status=Status.LATE)]).section("train").rows
    assert row.right == "Delayed"
    assert row.style is Style.ALERT


def test_cancelled_train_is_inverted_and_labelled():
    [row] = view(trains=[train(status=Status.CANCELLED)]).section("train").rows
    assert row.right == "Cancelled"
    assert row.style is Style.INVERTED


def test_unknown_train_status():
    [row] = view(trains=[train(status=Status.UNKNOWN)]).section("train").rows
    assert row.right == "No report"


def test_sign_wording_for_unknown_train_status():
    [row] = view(trains=[train(status=Status.UNKNOWN)], wording=SIGN).section("train").rows
    assert row.right == "No report"


@pytest.mark.parametrize("wording", [PLAIN, SIGN])
def test_both_looks_use_the_same_status_words(wording):
    rows = view(
        trains=[
            train("07:12"),
            train("07:42", "07:49", Status.LATE),
            train("08:12", status=Status.LATE),
            train("08:42", status=Status.CANCELLED),
            train("09:12", status=Status.UNKNOWN),
        ],
        wording=wording,
    ).section("train").rows
    assert [r.right for r in rows] == ["On time", "Exp 07:49", "Delayed", "Cancelled", "No report"]


def test_departed_services_are_dropped():
    v = view(trains=[train("06:58"), train("07:12")], now="07:00")
    assert [r.left for r in v.section("train").rows] == ["07:12"]


def test_bus_rows_use_clock_times_not_countdowns():
    [on_time, late, timetabled] = view(
        buses=[
            bus("341", "07:20"),
            bus("61", "07:25", "07:29", Status.LATE),
            bus("62", "07:40", status=Status.UNKNOWN),
        ]
    ).section("bus").rows
    assert (on_time.left, on_time.middle, on_time.right) == ("341", "Hyde", "07:20")
    assert (late.right, late.style) == ("Exp 07:29", Style.ALERT)
    assert (timetabled.right, timetabled.style) == ("07:40 sch", Style.NORMAL)


def test_bus_row_shows_stop_label_when_known():
    [row] = view(buses=[bus(stop="A")]).section("bus").rows
    assert (row.stop, row.middle) == ("A", "Hyde")


def test_empty_sections_say_so():
    v = view()
    assert v.section("train").rows == ()
    assert v.section("train").empty_text == "No trains"
    assert v.section("bus").empty_text == "No buses"


def test_stale_sources_are_flagged_per_section():
    v = view(stale={"bus"})
    assert v.section("bus").stale
    assert not v.section("train").stale


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
    assert v.section("train").leave is None and v.section("bus").leave is None


def test_train_leave_text_names_the_departure_time():
    v = view(trains=[train("07:12"), train("07:42")], now="07:00", walk=WALK)
    assert v.section("train").leave == "Leave by 07:05 (07:12)"


def test_leave_text_moves_on_once_a_departure_is_out_of_reach():
    trains = [train("07:12"), train("07:42")]
    assert view(trains=trains, now="07:06", walk=WALK).section("train").leave == "Leave by 07:35 (07:42)"


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
    ).section("bus").rows
    assert [r.leave for r in rows] == ["Leave by 07:14", "Leave by 07:32"]


def test_e_paper_bus_rows_also_give_the_leave_time_alone():
    # For the plain look, which says "Leave by" once, over the column.
    rows = view(buses=[bus("341", "07:20", stop="A")], now="07:00", walk=WALK).section("bus").rows
    assert [r.leave_at for r in rows] == ["07:14"]


def test_screen_bus_rows_have_no_leave_time_alone():
    # A countdown only reads whole, so each row keeps its own words.
    walk = WalkTimes(bus={"*": 7})
    [row] = view(buses=[bus(scheduled="07:20")], now="07:03", clock="07:03:30", walk=walk).section("bus").rows
    assert row.leave == "Leave in 9 min"
    assert row.leave_at is None


def test_bus_row_leave_time_stays_after_it_has_passed():
    [row] = view(buses=[bus("341", "07:20", stop="A")], now="07:16", walk=WALK).section("bus").rows
    assert row.leave == "Leave by 07:14"


def test_cancelled_bus_row_has_no_leave_time():
    [row] = view(buses=[bus("341", "07:20", status=Status.CANCELLED, stop="A")], walk=WALK).section("bus").rows
    assert row.leave is None and row.leave_at is None


def test_bus_header_drops_leave_text_when_rows_carry_it():
    v = view(trains=[train("07:12")], buses=[bus("341", "07:20", stop="A")], now="07:00", walk=WALK)
    assert v.section("bus").leave is None
    assert v.section("train").leave == "Leave by 07:05 (07:12)"


def test_train_rows_have_no_per_row_leave_time():
    [row] = view(trains=[train("07:12")], now="07:00", walk=WALK).section("train").rows
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
    ).section("train").rows
    assert [r.right for r in rows] == ["On time", "Exp 07:49", "Delayed", "Cancelled"]


def test_sign_wording_for_cancelled_bus():
    [row] = view(buses=[bus(status=Status.CANCELLED)], wording=SIGN).section("bus").rows
    assert row.right == "Cancelled"


def test_sign_wording_for_leave_line():
    v = view(trains=[train("07:12")], now="07:00", walk=WALK, wording=SIGN)
    assert v.section("train").leave == "Leave by 07:05"


def test_plain_leave_text_names_the_actual_departure_time_when_late():
    v = view(trains=[train("07:12", "07:20", Status.LATE)], now="07:00", walk=WALK)
    assert v.section("train").leave == "Leave by 07:13 (07:20)"


def test_section_records_which_row_the_leave_time_is_for():
    trains = [train("07:12"), train("07:42")]
    assert view(trains=trains, now="07:00", walk=WALK).section("train").leave_for == 0
    assert view(trains=trains, now="07:06", walk=WALK).section("train").leave_for == 1
    assert view(trains=trains, now="07:00").section("train").leave_for is None


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
    assert v.section("train").leave == "Leave in 2 min"


def test_countdown_rounds_down_so_it_never_promises_more_time():
    walk = WalkTimes(train=7)  # leave by 07:05
    v = view(trains=[train("07:12")], now="07:03", clock="07:03:40", walk=walk, wording=SIGN)
    assert v.section("train").leave == "Leave in 1 min"


def test_countdown_says_now_in_the_last_minute():
    walk = WalkTimes(train=7)
    for clock in ("07:04:01", "07:05:00", "07:05:59"):
        v = view(trains=[train("07:12")], now=clock[:5], clock=clock, walk=walk, wording=SIGN)
        assert v.section("train").leave == "Leave now", clock


def test_plain_countdown_names_the_train():
    walk = WalkTimes(train=7)
    v = view(trains=[train("07:12")], now="07:00", clock="07:00:00", walk=walk)
    assert v.section("train").leave == "Leave in 5 min (07:12)"


def test_with_a_clock_bus_leave_times_count_down():
    walk = WalkTimes(bus={"*": 7})
    [row] = view(buses=[bus(scheduled="07:20")], now="07:03", clock="07:03:30", walk=walk).section("bus").rows
    assert row.leave == "Leave in 9 min"


def test_without_a_clock_leave_times_stay_clock_times():
    walk = WalkTimes(train=7, bus={"*": 7})
    v = view(trains=[train("07:12")], buses=[bus(scheduled="07:20")], now="07:03", walk=walk, wording=SIGN)
    assert v.section("train").leave == "Leave by 07:05"
    assert v.section("bus").rows[0].leave == "Leave by 07:13"


def test_countdown_drops_the_leave_hint_once_a_bus_is_out_of_reach():
    walk = WalkTimes(bus={"*": 7})  # 07:08 bus: leave by 07:01
    [row] = view(buses=[bus(scheduled="07:08")], now="07:02", clock="07:02:00", walk=walk).section("bus").rows
    assert row.leave is None


def test_countdown_still_says_now_during_the_leave_minute():
    walk = WalkTimes(bus={"*": 7})
    [row] = view(buses=[bus(scheduled="07:08")], now="07:01", clock="07:01:40", walk=walk).section("bus").rows
    assert row.leave == "Leave now"


def test_train_row_names_its_platform():
    with_platform = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "2A")
    [row] = view(trains=[with_platform]).section("train").rows
    assert row.platform == "Plat 2A"
    [row] = view(trains=[train()]).section("train").rows
    assert row.platform is None


def test_a_replacement_bus_says_bus_in_the_platform_column():
    replacement = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME,
                            replacement_bus=True)
    [row] = view(trains=[replacement]).section("train").rows
    assert row.platform == "Bus"
    assert row.replacement_bus is True
    assert row.middle == "Manchester Piccadilly"
    [row] = view(trains=[train()]).section("train").rows
    assert row.replacement_bus is False


def test_a_ferry_says_ferry_in_the_platform_column():
    ferry = Departure("train", "Wightlink", "Ryde Pier Head", "07:12", None, Status.ON_TIME, ferry=True,
                      platform_changed=True)
    [row] = view(trains=[ferry]).section("train").rows
    assert (row.platform, row.platform_changed) == ("Ferry", False)
    assert row.ferry is True and row.replacement_bus is False
    assert row.middle == "Ryde Pier Head"
    assert destination_text(row, platform_column=False) == "Ferry: Ryde Pier Head"
    assert destination_text(row, platform_column=True) == "Ryde Pier Head"
    [row] = view(trains=[train()]).section("train").rows
    assert row.ferry is False


def test_a_changed_platform_says_so_in_words():
    changed = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "2",
                        platform_changed=True)
    [row] = view(trains=[changed]).section("train").rows
    assert row.platform == "Plat 2 (changed)"
    assert row.platform_changed is True
    [row] = view(trains=[dataclasses.replace(changed, platform_changed=False)]).section("train").rows
    assert (row.platform, row.platform_changed) == ("Plat 2", False)


def test_a_replacement_bus_is_never_shown_as_a_platform_change():
    bus = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME,
                    replacement_bus=True, platform_changed=True)
    [row] = view(trains=[bus]).section("train").rows
    assert (row.platform, row.platform_changed) == ("Bus", False)


def test_a_train_row_carries_its_via_words():
    via = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME,
                    via="via Guide Bridge")
    [row] = view(trains=[via]).section("train").rows
    assert row.via == "via Guide Bridge"
    assert row.middle == "Manchester Piccadilly"
    # Only a board wide enough for a platform column says it.
    assert destination_text(row, platform_column=False) == "Manchester Piccadilly"
    assert destination_text(row, platform_column=False, wide=True) == "Manchester Piccadilly via Guide Bridge"
    bus = dataclasses.replace(row, replacement_bus=True)
    assert destination_text(bus, platform_column=False, wide=True) == "Bus: Manchester Piccadilly via Guide Bridge"


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
    from twobells.board.view import NOTICE_LIMIT

    notice = view(notices=["x" * 300, "y" * 300]).notice
    assert len(notice) == NOTICE_LIMIT and notice.endswith("…")


def test_signature_changes_with_the_notice():
    assert view().signature() != view(notices=["Lifts out of use."]).signature()


CALLING = "Calling at: Dinting and Manchester Piccadilly. This train is formed of 3 coaches."


def test_a_screen_s_trains_carry_their_calling_at_lines():
    first = dataclasses.replace(train(), calling_at=CALLING)
    rows = view(trains=[first, train("07:42")], clock="07:00:10").section("train").rows
    assert [row.calling for row in rows] == [CALLING, None]


def test_e_paper_has_no_calling_at_line_as_it_cannot_scroll_one():
    first = dataclasses.replace(train(), calling_at=CALLING)
    assert view(trains=[first]).section("train").rows[0].calling is None
    # So a change to it never costs e-paper a redraw.
    assert view(trains=[first]).signature() == view(trains=[train()]).signature()


def test_a_calling_at_line_too_long_to_be_real_is_cut():
    from twobells.board.view import CALLING_LIMIT

    first = dataclasses.replace(train(), calling_at="Calling at: " + "Dinting, " * 500)
    [row] = view(trains=[first], clock="07:00:10").section("train").rows
    assert row.calling is not None and len(row.calling) == CALLING_LIMIT and row.calling.endswith("…")


def test_each_row_carries_where_its_train_or_bus_is():
    from twobells.core.model import Strip, strip_key

    train = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, platform="1")
    bus = Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A")
    other = Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B")
    at_dinting = Strip("At Dinting", stops=7, yours=5, at=4.0)
    three_away = Strip("3 stops away", stops=6, yours=5, at=2.0)
    view = build_view("Glossop", {"train": [train], "bus": [bus, other]}, "07:03", set(),
                      strips={strip_key(train): at_dinting, strip_key(bus): three_away})
    assert view.section("train").rows[0].strip == at_dinting
    assert [row.strip for row in view.section("bus").rows] == [three_away, None]


def test_where_a_train_is_changes_the_view():
    from twobells.core.model import Strip, strip_key

    train = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME)
    before = build_view("Glossop", {"train": [train], "bus": []}, "07:03", set(), strips={strip_key(train): Strip("At Hadfield", 3, 2)})
    after = build_view("Glossop", {"train": [train], "bus": []}, "07:03", set(), strips={strip_key(train): Strip("At Dinting", 3, 2)})
    assert before.signature() != after.signature()


# Wide screens name each bus row's stop: the rule for what they call it.

from twobells.board.view import STOP_NAME_LIMIT, stop_names_shown  # noqa: E402


def test_a_stop_whose_name_no_other_of_your_stops_shares_is_called_by_its_name():
    assert stop_names_shown({"A": "Henry Street", "B": "Howard Arms"}) == {"A": "Henry Street", "B": "Howard Arms"}


def test_stops_sharing_a_name_are_told_apart_by_their_labels():
    names = {"A": "Henry Street", "B": "Henry Street", "C": "Howard Arms"}
    assert stop_names_shown(names) == {"A": "Henry Street A", "B": "Henry Street B", "C": "Howard Arms"}


def test_names_differing_only_in_case_or_spacing_are_the_same_name():
    assert stop_names_shown({"A": "Henry Street", "B": " henry  STREET "}) == {
        "A": "Henry Street A", "B": "henry STREET B",
    }


def test_a_stop_without_a_name_is_left_out_and_shares_none():
    # Its rows show the label, as today; and with one name left, that name alone says which.
    names = {"A": "Henry Street", "B": None, "C": "", "D": "\x00\x07", "E": 5}
    assert stop_names_shown(names) == {"A": "Henry Street"}


def test_a_stop_name_is_feed_data_kept_to_one_short_printable_line():
    shown = stop_names_shown({"A": "Henry\x1b Street\n\tWest", "B": "x" * 500})
    assert shown["A"] == "Henry Street West"
    assert len(shown["B"]) == STOP_NAME_LIMIT


def test_no_names_none_shown():
    assert stop_names_shown({}) == {}


def test_bus_rows_carry_their_stop_s_name_beside_its_label():
    v = view(trains=[train()], buses=[bus(stop="A"), bus("61", stop="B"), bus("237", stop="C"), bus("6")],
             stop_names={"A": "Henry Street A", "B": "Henry Street B"})
    assert [(r.stop, r.stop_name) for r in v.section("bus").rows] == [
        ("A", "Henry Street A"), ("B", "Henry Street B"), ("C", None), (None, None),
    ]
    assert v.section("train").rows[0].stop_name is None


def test_the_bus_section_has_every_stop_s_name_not_only_the_shown_rows():
    # So the column a screen draws them in is as wide whatever buses are shown.
    v = view(buses=[bus(stop="A")], stop_names={"A": "Henry Street", "B": "Norfolk Square"})
    assert v.section("bus").stop_names == ("Henry Street", "Norfolk Square")
    assert v.section("train").stop_names == ()
    assert view(buses=[bus(stop="A")]).section("bus").stop_names == ()


def test_stop_names_change_the_view():
    named = view(buses=[bus(stop="A")], stop_names={"A": "Henry Street"})
    assert view(buses=[bus(stop="A")]).signature() != named.signature()


# What the board shows ([board] show, settings/show.py): an item left out is
# left out of the view, as if the feeds had never given it, so no look draws
# it or keeps room for it.


def _full_trains(plain=False):
    first = Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "2",
                      platform_changed=True, via="via Guide Bridge", calling_at=CALLING)
    second = Departure("train", "Northern", "Hadfield", "07:42", None, Status.ON_TIME, "1")
    if plain:
        first = dataclasses.replace(first, platform=None, platform_changed=False, via=None)
        second = dataclasses.replace(second, platform=None)
    return [first, second]


def _full(show=None, **without):
    from twobells.core.model import Strip, strip_key

    trains = without.pop("trains", _full_trains())
    buses = [bus(stop="A")]
    strips = {strip_key(trains[0]): Strip("At Dinting", 4, 2, 1.0), strip_key(buses[0]): Strip("3 stops away", 6, 5, 2.0)}
    given = {"notices": ["Lifts out of use."], "rain": "Rain from 07:10 (70%)", "strips": strips,
             "last_bus": "Last bus 23:05", "clock": "07:00:10"}
    return view(trains=trains, buses=buses, walk=WalkTimes(train=5, bus={"*": 8}), show=show, **(given | without))


SHOWN = ("platforms", "where", "disruption", "rain", "last_bus", "bus_page", "calling_at", "clock_page")
# Each item as if never given.
NEVER_GIVEN = {
    "platforms": {"trains": _full_trains(plain=True)},
    "where": {"strips": {}},
    "disruption": {"notices": None},
    "rain": {"rain": None},
    "last_bus": {"last_bus": None},
    "calling_at": {"trains": [dataclasses.replace(t, calling_at=None) for t in _full_trains()]},
}


def test_without_a_show_list_the_view_has_everything():
    full = _full()
    assert full == _full(show=SHOWN)
    first = full.section("train").rows[0]
    assert (first.platform, first.via, first.calling) == ("Plat 2 (changed)", "via Guide Bridge", CALLING)
    assert first.strip is not None and full.section("bus").rows[0].strip is not None
    assert (full.notice, full.rain, full.last_bus) == ("Lifts out of use.", "Rain from 07:10 (70%)", "Last bus 23:05")


@pytest.mark.parametrize("item", sorted(NEVER_GIVEN))
def test_an_item_left_out_is_as_if_it_was_never_given(item):
    left_out = _full(show=set(SHOWN) - {item})
    assert left_out == _full(**NEVER_GIVEN[item])
    assert left_out != _full()


def test_without_platforms_a_replacement_bus_says_so_before_its_destination():
    replacement = Departure("train", "Northern", "Hadfield", "07:42", None, Status.ON_TIME, replacement_bus=True)
    rows = _full(show=set(SHOWN) - {"platforms"}, trains=[*_full_trains(), replacement]).section("train").rows
    assert [row.platform for row in rows] == [None, None, None]
    assert rows[0].platform_changed is False
    assert destination_text(rows[2], platform_column=False, wide=True) == "Bus: Hadfield"


def test_with_nothing_shown_the_rows_keep_their_times_and_words():
    bare = _full(show=())
    first = bare.section("train").rows[0]
    assert (first.left, first.middle, first.right) == ("07:12", "Manchester Piccadilly", "On time")
    assert bare.section("bus").rows[0].leave is not None  # when to leave is part of the times
    assert (bare.notice, bare.rain, bare.last_bus) == (None, None, None)


def _pages():
    from twobells.board.panel import PANELS
    from twobells.board.render import SCREEN_PALETTE, Palette
    from twobells.board.themes import THEMES, theme_named

    for look in sorted(THEMES):
        for panel in PANELS.values():
            screen = not panel.epaper
            if THEMES[look].screen_only and not screen:
                continue
            palette = SCREEN_PALETTE if screen else Palette(white=0, black=1, accent=2)
            yield f"{look}/{panel.name}", theme_named(look), palette, panel


@pytest.mark.parametrize("look, theme, palette, panel", list(_pages()), ids=[name for name, *_ in _pages()])
def test_every_look_draws_the_board_without_each_item(look, theme, palette, panel):
    clock = None if panel.epaper else "07:00:10"
    for item in (*NEVER_GIVEN, None):
        shown = set(SHOWN) if item is None else set(SHOWN) - {item}
        for show in (shown, ()):
            frame = theme.frame(_full(show=show, clock=clock), palette, panel)
            assert frame.image.size == (panel.width, panel.height)
    # Left out, it is drawn exactly as if never given: no gap where it was.
    for item, given in NEVER_GIVEN.items():
        left_out = theme.frame(_full(show=set(SHOWN) - {item}, clock=clock), palette, panel).image
        never = theme.frame(_full(clock=clock, **given), palette, panel).image
        assert left_out.tobytes() == never.tobytes(), item


# Trams, from the timetable alone (sources/trams.py).


def tram(scheduled="07:14", dest="Bury", stop="T", bus=False):
    return Departure("metro", "Blue Line", dest, scheduled, None, Status.UNKNOWN, stop=stop, replacement_bus=bus)


def tram_view(trams, now="07:00", walk=None, **kwargs):
    return build_view(title="Home", departures={"metro": list(trams)}, now=now, stale=set(), walk=walk, **kwargs)


def test_a_tram_row_is_drawn_as_a_train_s_with_the_timetable_s_mark():
    section = tram_view([tram()]).section("metro")
    [row] = section.rows
    assert (section.label, section.empty_text) == ("Trams", "No trams")
    assert (row.left, row.middle, row.right, row.style, row.platform) == ("07:14", "Bury", "sch", Style.NORMAL, None)


def test_a_replacement_bus_in_the_trams_says_so():
    [row] = tram_view([tram(bus=True, dest="Altrincham")]).section("metro").rows
    assert row.platform == "Bus" and row.replacement_bus
    assert destination_text(row, platform_column=False) == "Bus: Altrincham"


def test_with_several_tram_stops_each_row_names_its_stop():
    rows = tram_view([tram(), tram("07:16", "Altrincham", stop="U")]).section("metro").rows
    assert [row.middle for row in rows] == ["T: Bury", "U: Altrincham"]


def test_trams_say_when_to_leave_for_the_next_in_reach_by_their_own_walks():
    walk = WalkTimes(train=30, bus={"T": 1}, metro={"T": 6})
    section = tram_view([tram("07:04"), tram("07:14")], walk=walk, wording=SIGN).section("metro")
    assert (section.leave, section.leave_for) == ("Leave by 07:08", 1)
    assert tram_view([tram("07:04")], walk=walk, wording=SIGN).section("metro").leave is None
