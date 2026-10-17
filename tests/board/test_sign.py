import dataclasses
import itertools

import pytest

from twobells.board.dotfont import DotFont
from twobells.board.panel import HDMI_1280X400, HDMI_1920X440, WAVESHARE_10IN85G
from twobells.board.render import HEIGHT, WIDTH, Palette
from twobells.board.sign import MARGIN, item_size, render_sign, sign_layout
from twobells.board.view import BoardView, Row, Section, Style

PALETTE = Palette(white=0, black=1, accent=2)
FONT = DotFont()


def board(trains=(), buses=(), leave=None, leave_for=0, stale_trains=False, stale_buses=False):
    return BoardView(
        title="Glossop > Manchester Picc",
        sections=(
            Section("Trains", tuple(trains), "No trains", stale_trains, leave, leave_for if leave else None),
            Section("Buses", tuple(buses), "No buses", stale_buses, mode="bus"),
        ),
        updated="07:03",
    )


TRAINS = [
    Row("07:12", "Manchester Piccadilly", "On time"),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED),
]
BUSES = [
    Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave by 07:17", "A"),
    Row("394", "Stepping Hill Hospital via Marple and Romiley", "07:35 sch", Style.NORMAL, "Leave by 07:28", "B"),
]


def texts(items):
    return [i.text for i in items]


def find(items, text):
    [item] = [i for i in items if i.text == text]
    return item


def test_trains_are_numbered_like_a_station_board():
    items = sign_layout(board(TRAINS)).items
    assert {"1st", "2nd", "3rd"} <= set(texts(items))


def test_ordinal_time_destination_and_status_share_a_line():
    items = sign_layout(board(TRAINS)).items
    first = find(items, "1st")
    line = [i for i in items if i.y == first.y]
    assert texts(line) == ["1st", "07:12", "Manchester Piccadilly", "On time"]


def test_status_is_right_aligned():
    items = sign_layout(board(TRAINS)).items
    status = find(items, "On time")
    assert status.x + FONT.getlength("On time") == WIDTH - MARGIN


def test_train_times_line_up():
    items = sign_layout(board(TRAINS)).items
    assert len({find(items, t).x for t in ("07:12", "07:42", "08:12")}) == 1


def test_leave_line_sits_under_its_train_aligned_with_the_times():
    items = sign_layout(board(TRAINS, leave="Leave by 07:05")).items
    leave = find(items, "Leave by 07:05")
    assert find(items, "1st").y < leave.y < find(items, "2nd").y
    assert leave.x == find(items, "07:12").x


def test_leave_line_follows_a_later_train_when_the_first_is_out_of_reach():
    items = sign_layout(board(TRAINS, leave="Leave by 07:35", leave_for=1)).items
    leave = find(items, "Leave by 07:35")
    assert find(items, "2nd").y < leave.y < find(items, "3rd").y


def test_cancelled_row_is_inverted():
    items = sign_layout(board(TRAINS)).items
    assert find(items, "Cancelled").inverted
    assert not find(items, "On time").inverted


def test_bus_rows_show_route_stop_destination_leave_and_time():
    items = sign_layout(board(buses=BUSES)).items
    route = find(items, "341")
    line = [i for i in items if i.y == route.y]
    assert texts(line) == ["341", "A", "Hyde", "Leave by 07:17", "Exp 07:24"]


def test_bus_leave_times_line_up_and_long_destinations_give_way():
    items = sign_layout(board(buses=BUSES)).items
    assert find(items, "Leave by 07:17").x == find(items, "Leave by 07:28").x
    dest = next(i for i in items if i.text.startswith("Stepping"))
    assert dest.text.endswith("…")
    assert dest.x + FONT.getlength(dest.text) < find(items, "Leave by 07:28").x


def test_empty_sections_say_so():
    assert {"No trains", "No buses"} <= set(texts(sign_layout(board()).items))


def test_footer_shows_last_update_not_a_ticking_clock():
    footer = find(sign_layout(board()).items, "Updated 07:03")
    assert footer.y + item_size(footer)[1] <= HEIGHT


def test_footer_is_small_print():
    items = sign_layout(board(stale_buses=True)).items
    for text in ("Updated 07:03", "Not updating: buses"):
        footer = find(items, text)
        assert footer.small
        assert item_size(footer)[1] == 5


def test_footer_names_feeds_that_have_stopped():
    items = texts(sign_layout(board(stale_trains=True, stale_buses=True)).items)
    assert "Not updating: trains, buses" in items
    assert "Not updating: buses" in texts(sign_layout(board(stale_buses=True)).items)


def test_everything_fits_on_the_panel():
    items = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05", stale_buses=True)).items
    for item in items:
        width, height = item_size(item)
        assert item.x >= 0 and item.x + width <= WIDTH
        assert item.y >= 0 and item.y + height <= HEIGHT


def test_rendered_board_is_amber_on_black():
    image = render_sign(board(TRAINS, BUSES), PALETTE)
    counts = {c: n for n, c in image.getcolors()}
    assert image.size == (WIDTH, HEIGHT)
    assert counts[PALETTE.black] > 0.6 * WIDTH * HEIGHT
    assert PALETTE.accent in counts
    assert PALETTE.white not in counts


def test_mono_panel_falls_back_to_white_text():
    mono = Palette(white=0, black=1, accent=1)
    counts = {c: n for n, c in render_sign(board(TRAINS), mono).getcolors()}
    assert mono.white in counts


def test_trains_and_buses_have_breathing_room_around_the_divider():
    layout = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05"))
    last_train_bottom = find(layout.items, "3rd").y + 9
    first_bus_top = find(layout.items, "341").y
    rule = layout.rules[0]  # the divider; a second rule may sit over the footer
    assert rule - last_train_bottom >= 8
    assert first_bus_top - rule >= 10


def test_buses_still_clear_the_footer_with_a_full_board():
    rows = [*BUSES, Row("237", "Ashton", "07:41", leave="Leave by 07:34", stop="A")]
    items = sign_layout(board(TRAINS, rows, leave="Leave by 07:05")).items
    last_bus_bottom = find(items, "237").y + 9
    assert last_bus_bottom < find(items, "Updated 07:03").y - 2


def test_not_updating_warning_is_small_but_reversed_out():
    layout = sign_layout(board(stale_buses=True))
    warning = find(layout.items, "Not updating: buses")
    assert warning.small and warning.inverted
    assert not find(layout.items, "Updated 07:03").inverted


def test_reversed_small_print_only_highlights_its_own_text():
    image = render_sign(board(stale_buses=True), PALETTE)
    # The far left of the footer line stays black: no full-width band.
    assert image.getpixel((0, HEIGHT - 3)) == PALETTE.black
    # Just left of the warning text, inside its badge, is amber.
    warning = find(sign_layout(board(stale_buses=True)).items, "Not updating: buses")
    assert image.getpixel((int(warning.x) - 1, warning.y + 2)) == PALETTE.accent


def test_board_has_padding_at_the_top():
    items = sign_layout(board(TRAINS)).items
    assert find(items, "1st").y >= 6


def test_cancelled_first_train_band_keeps_the_top_padding():
    rows = [Row("07:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED)]
    image = render_sign(board(rows), PALETTE)
    assert all(image.getpixel((x, y)) == PALETTE.black for x in range(WIDTH) for y in range(4))


BIG_PANELS = [WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440]


def dots(panel):
    return panel.width // panel.scale, panel.height // panel.scale


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_bigger_panels_get_an_image_their_size(panel):
    image = render_sign(board(TRAINS, BUSES), PALETTE, panel)
    assert image.size == panel.size
    assert image.mode == "P"
    assert PALETTE.accent in {c for _, c in image.getcolors()}


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_everything_fits_on_a_bigger_board(panel):
    width, height = dots(panel)
    items = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05", stale_buses=True), width, height).items
    for item in items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_status_is_right_aligned_on_a_bigger_board(panel):
    width, height = dots(panel)
    status = find(sign_layout(board(TRAINS), width, height).items, "On time")
    assert status.x + FONT.getlength("On time") == width - MARGIN


def test_wider_board_shows_a_long_destination_whole():
    long = "Stepping Hill Hospital via Marple and Romiley"
    phat = sign_layout(board(buses=BUSES)).items
    assert any(i.text.startswith("Stepping") and i.text.endswith("…") for i in phat)
    assert long in texts(sign_layout(board(buses=BUSES), *dots(HDMI_1280X400)).items)


def _origin(panel):
    """Panel pixel of the top-left dot; the dot grid is centred on the panel."""
    width, height = dots(panel)
    return (panel.width - width * panel.scale) // 2, (panel.height - height * panel.scale) // 2


def test_big_dots_have_a_dark_gap_between_leds():
    panel = HDMI_1280X400  # 3 pixels per dot
    image = render_sign(board(), PALETTE, panel)
    x0, y0 = _origin(panel)
    # The divider is every other dot from the margin.
    rule = sign_layout(board(), *dots(panel)).rules[0]
    x, y = x0 + MARGIN * 3, y0 + rule * 3
    assert image.getpixel((x, y)) == image.getpixel((x + 1, y + 1)) == PALETTE.accent
    assert image.getpixel((x + 2, y)) == PALETTE.black  # gap column
    assert image.getpixel((x, y + 2)) == PALETTE.black  # gap row


def test_reversed_out_band_stays_solid_so_its_text_reads():
    panel = HDMI_1280X400
    rows = [Row("07:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED)]
    image = render_sign(board(rows), PALETTE, panel)
    x0, y0 = _origin(panel)
    band_top = find(sign_layout(board(rows), *dots(panel)).items, "1st").y - 1
    y = y0 + band_top * 3
    assert all(image.getpixel((x, y + dy)) == PALETTE.accent for x in range(x0, x0 + 6) for dy in range(3))


def test_screen_board_shows_a_running_clock_centred_at_the_bottom():
    width, height = dots(HDMI_1280X400)
    view = dataclasses.replace(board(TRAINS, BUSES), clock="07:03:15")
    items = sign_layout(view, width, height).items
    clock = find(items, "07:03:15")
    assert not clock.small
    assert abs((clock.x + FONT.getlength("07:03:15") / 2) - width / 2) <= 1
    assert clock.y + item_size(clock)[1] <= height
    assert "Updated 07:03" not in texts(items)


def test_buses_clear_the_clock():
    width, height = dots(HDMI_1280X400)
    rows = BUSES * 4
    view = dataclasses.replace(board(TRAINS, rows, leave="Leave in 2 min"), clock="07:03:15")
    items = sign_layout(view, width, height).items
    last_bus = max(i.y for i in items if i.text in ("341", "394"))
    assert last_bus + 9 < find(items, "07:03:15").y


from twobells.board.route import RouteView  # noqa: E402
from twobells.board.sign import _route_layout, render_route_sign  # noqa: E402

NAMES = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")


def route(bus_at=2.45, where="3 stops away", clock="07:12:40"):
    return RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", NAMES, bus_at, where, clock)


def test_route_page_is_the_screen_size():
    image = render_route_sign(route(), PALETTE, HDMI_1280X400)
    assert image.size == HDMI_1280X400.size
    assert PALETTE.white not in {c for _, c in image.getcolors()}


def test_route_page_names_every_stop_and_fits():
    width, height = dots(HDMI_1280X400)
    layout, road, xs = _route_layout(route(), width, height)
    for name in NAMES:
        assert name in texts(layout.items)
    for item in layout.items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height
    assert xs == sorted(xs) and len(xs) == len(NAMES)


def test_route_page_marks_your_stop():
    width, height = dots(HDMI_1280X400)
    layout, _, _ = _route_layout(route(), width, height)
    assert find(layout.items, "Henry Street").inverted
    assert not find(layout.items, "The Junction").inverted


def test_names_alternate_above_and_below_the_road():
    width, height = dots(HDMI_1280X400)
    layout, road, _ = _route_layout(route(), width, height)
    assert find(layout.items, "The Junction").y < road < find(layout.items, "Grapes Inn").y


def _lit_columns(image, panel, rows):
    """Panel x positions with amber in the given panel rows."""
    return {x for x in range(panel.width) for y in rows if image.getpixel((x, y)) == PALETTE.accent}


def test_bus_is_drawn_where_it_is_and_not_when_unknown():
    panel = HDMI_1280X400
    width, height = dots(panel)
    _, road, xs = _route_layout(route(), width, height)
    x0, y0 = _origin(panel)
    above_road = range(y0 + (road - 6) * 3, y0 + (road - 3) * 3)  # the bus body, clear of names
    with_bus = _lit_columns(render_route_sign(route(2.45), PALETTE, panel), panel, above_road)
    expected = x0 + (xs[0] + 2.45 * (xs[1] - xs[0])) * 3
    assert with_bus and min(with_bus) < expected < max(with_bus)
    assert not _lit_columns(render_route_sign(route(None, "Not yet past The Junction"), PALETTE, panel), panel, above_road)


def test_route_page_says_where_the_bus_is():
    width, height = dots(HDMI_1280X400)
    layout, _, _ = _route_layout(route(), width, height)
    assert {"341 to Hyde", "Exp 07:24", "Leave in 6 min", "3 stops away", "07:12:40"} <= set(texts(layout.items))


def test_no_leave_line_for_a_train_that_is_not_drawn():
    fourth = [*TRAINS, Row("08:42", "Manchester Piccadilly", "On time")]
    items = sign_layout(board(fourth, leave="Leave by 08:35", leave_for=3)).items
    assert "Leave by 08:35" not in texts(items)


def test_longer_stop_labels_push_the_destinations_along():
    rows = [Row("341", "Hyde", "07:24", stop="NB2"), Row("394", "Marple", "07:35", stop="A")]
    items = sign_layout(board(buses=rows)).items
    label = find(items, "NB2")
    for dest in ("Hyde", "Marple"):
        assert find(items, dest).x > label.x + FONT.getlength("NB2")
    assert find(items, "Hyde").x == find(items, "Marple").x


WITH_PLATFORMS = [
    dataclasses.replace(TRAINS[0], platform="Plat 1"),
    TRAINS[1],  # Darwin doesn't always know the platform
    dataclasses.replace(TRAINS[2], platform="Plat 2"),
]


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_wide_boards_show_the_platform_between_destination_and_status(panel):
    width, height = dots(panel)
    items = sign_layout(board(WITH_PLATFORMS), width, height).items
    first = find(items, "1st")
    assert texts([i for i in items if i.y == first.y]) == ["1st", "07:12", "Manchester Piccadilly", "Plat 1", "On time"]
    assert find(items, "Plat 1").x == find(items, "Plat 2").x
    second = find(items, "2nd")
    assert not any(i.text.startswith("Plat") for i in items if i.y == second.y)
    for status in ("On time", "Exp 07:49", "Cancelled"):
        assert find(items, "Plat 1").x + FONT.getlength("Plat 1") + 5 <= find(items, status).x
    assert find(items, "Plat 2").inverted  # part of the cancelled row's band


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_destination_gives_way_to_the_platform_column(panel):
    width, height = dots(panel)
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley, Hyde Central, Woodley, Romiley, Bredbury and Guide Bridge"
    rows = [Row("07:12", long, "On time", platform="Plat 1")]
    items = sign_layout(board(rows), width, height).items
    dest = next(i for i in items if i.text.startswith("Manchester"))
    assert dest.text.endswith("…")
    assert dest.x + FONT.getlength(dest.text) + 5 <= find(items, "Plat 1").x


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_no_platform_column_when_no_train_has_one(panel):
    width, height = dots(panel)
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley, Hyde Central, Woodley, Romiley, Bredbury and Guide Bridge"
    with_column = sign_layout(board([Row("07:12", long, "On time", platform="Plat 1")]), width, height).items
    without = sign_layout(board([Row("07:12", long, "On time")]), width, height).items

    def dest(items):
        return next(i.text for i in items if i.text.startswith("Manchester"))

    assert len(dest(without)) > len(dest(with_column))


def test_the_phat_has_no_room_for_platforms_and_is_unchanged():
    assert not any(i.text.startswith("Plat") for i in sign_layout(board(WITH_PLATFORMS)).items)
    with_platforms = render_sign(board(WITH_PLATFORMS, BUSES), PALETTE)
    without = render_sign(board(TRAINS, BUSES), PALETTE)
    assert with_platforms.tobytes() == without.tobytes()


REPLACEMENT_BUS = Row("07:12", "Manchester Piccadilly", "On time", platform="Bus", replacement_bus=True)


def test_without_a_platform_column_a_replacement_bus_says_so_before_its_destination():
    items = sign_layout(board([REPLACEMENT_BUS])).items
    assert texts([i for i in items if i.y == find(items, "1st").y])[2] == "Bus: Manchester Piccadilly"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_with_a_platform_column_a_replacement_bus_says_bus_there(panel):
    width, height = dots(panel)
    items = sign_layout(board([REPLACEMENT_BUS]), width, height).items
    first = find(items, "1st")
    assert texts([i for i in items if i.y == first.y]) == ["1st", "07:12", "Manchester Piccadilly", "Bus", "On time"]


FERRY = Row("07:12", "Ryde Pier Head", "On time", platform="Ferry", ferry=True)


def test_without_a_platform_column_a_ferry_says_so_before_its_destination():
    items = sign_layout(board([FERRY])).items
    assert texts([i for i in items if i.y == find(items, "1st").y])[2] == "Ferry: Ryde Pier Head"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_with_a_platform_column_a_ferry_says_ferry_there(panel):
    width, height = dots(panel)
    items = sign_layout(board([FERRY]), width, height).items
    first = find(items, "1st")
    assert texts([i for i in items if i.y == first.y]) == ["1st", "07:12", "Ryde Pier Head", "Ferry", "On time"]


VIA = Row("07:12", "Hadfield", "On time", platform="Plat 1", via="via Dinting")


def test_the_phat_leaves_the_via_words_off():
    items = sign_layout(board([VIA])).items
    assert texts([i for i in items if i.y == find(items, "1st").y]) == ["1st", "07:12", "Hadfield", "On time"]


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_a_wide_board_says_the_via_words_after_the_destination(panel):
    width, height = dots(panel)
    items = sign_layout(board([VIA]), width, height).items
    first = find(items, "1st")
    assert texts([i for i in items if i.y == first.y]) == ["1st", "07:12", "Hadfield via Dinting", "Plat 1", "On time"]


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_a_changed_platform_says_so_in_words(panel):
    width, height = dots(panel)
    changed = Row("07:12", "Hadfield", "On time", platform="Plat 2 (changed)", platform_changed=True)
    items = sign_layout(board([changed, TRAINS[1]]), width, height).items
    assert "Plat 2 (changed)" in texts(items)


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_platforms_fit_on_a_bigger_board(panel):
    width, height = dots(panel)
    items = sign_layout(board(WITH_PLATFORMS, BUSES, leave="Leave by 07:05"), width, height).items
    for item in items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width


NOTICE = (
    "Disruption between Glossop and Manchester Piccadilly until the end of the day. "
    "Trains may be cancelled or delayed by up to 20 minutes."
)
def noticed(view, notice=NOTICE, clock=None):
    return dataclasses.replace(view, notice=notice, clock=clock)


def test_notice_keeps_updated_on_the_phat_and_is_cut_to_fit_beside_it():
    # A frozen e-paper panel keeps its last image: the stamp is how to tell.
    items = sign_layout(noticed(board(TRAINS, BUSES))).items
    updated = find(items, "Updated 07:03")
    [notice] = [i for i in items if i.text.startswith("! ")]
    assert notice.small and updated.small and notice.y == updated.y
    assert notice.x == MARGIN
    assert updated.x + item_size(updated)[0] == WIDTH - MARGIN
    assert notice.text.endswith("…")
    assert notice.x + item_size(notice)[0] + 5 <= updated.x
    assert NOTICE.startswith(notice.text[2:-1].rstrip())


def test_notice_updated_and_badge_share_the_phat_footer_without_overlapping():
    items = sign_layout(noticed(board(TRAINS, BUSES, stale_trains=True, stale_buses=True))).items
    updated, badge = find(items, "Updated 07:03"), find(items, "Not updating: trains, buses")
    [notice] = [i for i in items if i.text.startswith("! ")]
    assert notice.x + item_size(notice)[0] + 5 <= updated.x
    assert updated.x + item_size(updated)[0] + 5 <= badge.x - 2


def test_short_notice_is_shown_whole():
    items = sign_layout(noticed(board(), "Lifts out of use")).items
    assert "! Lifts out of use" in texts(items)


def test_notice_stops_short_of_the_not_updating_badge():
    items = sign_layout(noticed(board(stale_buses=True))).items
    [notice] = [i for i in items if i.text.startswith("! ")]
    badge = find(items, "Not updating: buses")
    assert notice.x + item_size(notice)[0] + 5 <= badge.x - 2


def test_wide_epaper_gives_the_notice_the_full_width():
    width, height = dots(WAVESHARE_10IN85G)
    items = sign_layout(noticed(board()), width, height).items
    [notice] = [i for i in items if i.text.startswith("! ")]
    [phat] = [i for i in sign_layout(noticed(board())).items if i.text.startswith("! ")]
    assert len(notice.text) > len(phat.text)
    assert notice.x + item_size(notice)[0] <= width - MARGIN


def screen_footer(view):
    width, height = dots(HDMI_1280X400)
    items = sign_layout(view, width, height).items
    return [i for i in items if i.text.startswith("! ")], find(items, view.clock), width, height


def test_screen_keeps_its_clock_on_the_bottom_line_beside_the_notice():
    [notice], clock, width, height = screen_footer(noticed(board(TRAINS, BUSES), clock="07:03:15"))
    assert clock.y == notice.y == height - 10
    assert not notice.small and not clock.small
    assert clock.x + FONT.getlength(clock.text) == width - MARGIN
    assert notice.x == MARGIN and notice.x + item_size(notice)[0] + 5 <= clock.x


def test_screen_clock_and_notice_clear_the_not_updating_badge():
    view = noticed(board(stale_trains=True, stale_buses=True), clock="07:03:15")
    width, height = dots(HDMI_1280X400)
    items = sign_layout(view, width, height).items
    badge = find(items, "Not updating: trains, buses")
    assert find(items, "07:03:15").x + FONT.getlength("07:03:15") + 5 <= badge.x - 2


def test_screen_scrolls_a_long_notice_whole_in_its_space():
    for second in range(0, 60, 5):
        [notice], clock, _, _ = screen_footer(noticed(board(), clock=f"07:03:{second:02d}"))
        assert notice.text == "! " + NOTICE  # whole, not cut short or in parts
        assert notice.box is not None and notice.x + notice.box + 5 <= clock.x


def test_screen_shows_a_short_notice_whole_and_steady():
    [notice], _, _, _ = screen_footer(noticed(board(), "Lifts out of use", clock="07:03:15"))
    assert notice.text == "! Lifts out of use"


def test_without_a_notice_the_phat_footer_is_unchanged():
    assert render_sign(board(TRAINS, BUSES), PALETTE).tobytes() == render_sign(
        noticed(board(TRAINS, BUSES), None), PALETTE
    ).tobytes()


RAIN = "Rain from 07:10 (70%)"


def test_rain_line_shares_the_footer_with_updated():
    items = sign_layout(dataclasses.replace(board(TRAINS, BUSES), rain=RAIN)).items
    rain, updated = find(items, RAIN), find(items, "Updated 07:03")
    assert rain.small and updated.small and rain.y == updated.y
    assert rain.x == MARGIN
    assert updated.x + item_size(updated)[0] == WIDTH - MARGIN
    assert rain.x + item_size(rain)[0] + 5 <= updated.x


def test_rain_line_keeps_updated_beside_the_badge_on_the_phat():
    items = sign_layout(dataclasses.replace(board(stale_trains=True, stale_buses=True), rain=RAIN)).items
    updated, badge = find(items, "Updated 07:03"), find(items, "Not updating: trains, buses")
    assert find(items, RAIN).x + item_size(find(items, RAIN))[0] + 5 <= updated.x
    assert updated.x + item_size(updated)[0] + 5 <= badge.x - 2


def test_rain_line_is_cut_short_to_keep_updated_when_both_do_not_fit():
    view = dataclasses.replace(board(stale_buses=True), rain=RAIN)
    items = sign_layout(view, 200, HEIGHT).items  # a narrower board than any panel
    updated, badge = find(items, "Updated 07:03"), find(items, "Not updating: buses")
    [rain] = [i for i in items if i.text.startswith("Rain")]
    assert rain.text.endswith("…")
    assert rain.x + item_size(rain)[0] + 5 <= updated.x
    assert updated.x + item_size(updated)[0] + 5 <= badge.x - 2


def test_rain_line_keeps_updated_beside_a_badge_when_there_is_room():
    width, height = dots(WAVESHARE_10IN85G)
    items = sign_layout(dataclasses.replace(board(stale_buses=True), rain=RAIN), width, height).items
    rain, updated, badge = find(items, RAIN), find(items, "Updated 07:03"), find(items, "Not updating: buses")
    assert rain.x + item_size(rain)[0] < updated.x
    assert updated.x + item_size(updated)[0] + 5 <= badge.x - 2


def test_a_disruption_notice_beats_the_rain_line():
    items = texts(sign_layout(dataclasses.replace(board(), rain=RAIN, notice="Lifts out of use")).items)
    assert "! Lifts out of use" in items
    assert RAIN not in items


def test_screen_shows_the_rain_line_left_of_the_clock():
    width, height = dots(HDMI_1280X400)
    view = dataclasses.replace(board(TRAINS, BUSES), rain=RAIN, clock="07:10:15")
    items = sign_layout(view, width, height).items
    rain, clock = find(items, RAIN), find(items, "07:10:15")
    assert not rain.small and rain.y == clock.y
    assert clock.x + FONT.getlength("07:10:15") == width - MARGIN
    assert rain.x + FONT.getlength(RAIN) + 5 <= clock.x


def test_screen_shows_the_last_bus_left_of_the_clock():
    width, height = dots(HDMI_1280X400)
    view = dataclasses.replace(board(TRAINS, BUSES), last_bus="Last bus 23:05", clock="22:10:15")
    items = sign_layout(view, width, height).items
    last, clock = find(items, "Last bus 23:05"), find(items, "22:10:15")
    assert last.x == MARGIN and last.y == clock.y == height - 10
    assert clock.x + FONT.getlength("22:10:15") == width - MARGIN
    assert last.x + FONT.getlength(last.text) + 5 <= clock.x


def test_a_disruption_notice_and_the_rain_line_beat_the_last_bus():
    width, height = dots(HDMI_1280X400)
    view = dataclasses.replace(board(), last_bus="Last bus 23:05", rain=RAIN, clock="22:10:15")
    items = texts(sign_layout(view, width, height).items)
    assert RAIN in items and "Last bus 23:05" not in items
    items = texts(sign_layout(noticed(view, "Lifts out of use", clock="22:10:15"), width, height).items)
    assert "Last bus 23:05" not in items


def test_footer_glyphs_cover_notices_and_the_rain_line():
    from twobells.board.dotfont import GLYPHS, TINY

    for char in "!()%/'&+;…":
        assert char in TINY, char
    for char in "!()%;…":
        assert char in GLYPHS, char


@pytest.mark.parametrize("panel", [None, *BIG_PANELS], ids=lambda p: p.name if p else "inky-phat")
@pytest.mark.parametrize("clock", [None, "07:03:15"])
def test_everything_fits_with_a_notice(panel, clock):
    width, height = dots(panel) if panel else (WIDTH, HEIGHT)
    view = noticed(board(TRAINS, BUSES, leave="Leave by 07:05", stale_buses=True), clock=clock)
    for item in sign_layout(view, width, height).items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height


def test_everything_fits_on_the_original_phat():
    from twobells.board.panel import PHAT_V1

    items = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05", stale_buses=True), *PHAT_V1.size).items
    for item in items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= PHAT_V1.width
        assert item.y >= 0 and item.y + h <= PHAT_V1.height
    assert {"1st", "2nd", "3rd"} <= set(texts(items))


# The bar screen: four pixels a dot, one line a row, and where each train or bus is.

from twobells.board.panel import (  # noqa: E402
    LEAVE_COLUMN_MIN_WIDTH,
    PLATFORM_COLUMN_MIN_WIDTH,
    STRIP_COLUMN_MIN_WIDTH,
)
from twobells.board.sign import STRIP_WIDTH, StripMark, strip_dots, where_width  # noqa: E402
from twobells.core.model import Strip  # noqa: E402

CLOCK = "07:03:15"
# The screens wide enough for strips.
STRIP_PANELS = [HDMI_1280X400, HDMI_1920X440]
SCREEN_TRAINS = [
    dataclasses.replace(TRAINS[0], platform="Plat 1", strip=Strip("At Dinting", 4, 2, 1.0)),
    dataclasses.replace(TRAINS[1], platform="Plat 1", strip=Strip("Not yet left Hadfield sidings", 4, 2, None)),
    dataclasses.replace(TRAINS[2], platform="Plat 2"),  # cancelled: nowhere to be
]
SCREEN_BUSES = [
    Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave in 12 min", "A", strip=Strip("3 stops away", 7, 5, 2.4)),
    Row("61", "Buxton", "07:35 sch", Style.NORMAL, "Leave in 23 min", "B"),
    Row("237", "Ashton", "07:41", Style.NORMAL, "Leave in 29 min", "A", strip=Strip("1 stop away", 7, 5, 4.0)),
]


def screen(trains=SCREEN_TRAINS, buses=SCREEN_BUSES, leave="Leave in 3 min", leave_for=0, stale_buses=False, **changes):
    view = board(trains, buses, leave=leave, leave_for=leave_for, stale_buses=stale_buses)
    return dataclasses.replace(view, clock=CLOCK, **changes)


def line_of(items, text):
    y = find(items, text).y
    return sorted((i for i in items if i.y == y), key=lambda i: i.x)


def test_the_bar_screen_draws_four_pixels_a_dot():
    assert HDMI_1920X440.scale == 4
    assert dots(HDMI_1920X440) == (480, 110)


def test_only_the_bar_screen_puts_a_trains_leave_time_on_its_row():
    assert dots(HDMI_1920X440)[0] >= LEAVE_COLUMN_MIN_WIDTH
    for panel in (WAVESHARE_10IN85G, HDMI_1280X400):
        assert dots(panel)[0] < LEAVE_COLUMN_MIN_WIDTH


def test_on_the_bar_screen_each_train_is_one_line_with_its_leave_time_in_a_column():
    items = sign_layout(screen(), *dots(HDMI_1920X440)).items
    first = texts(line_of(items, "1st"))
    # The destination stays whole beside eighteen letters of where words.
    assert first == ["1st", "07:12", "Manchester Piccadilly", "At Dinting", "Plat 1", "Leave in 3 min", "On time"]
    assert all(i.box is None for i in line_of(items, "1st"))  # nothing on the row scrolls
    # Nothing between the 1st and the 2nd: the rows are a line apart.
    assert find(items, "2nd").y - find(items, "1st").y == 11


def test_the_trains_leave_column_follows_a_later_train():
    items = sign_layout(screen(leave="Leave in 33 min", leave_for=1), *dots(HDMI_1920X440)).items
    assert find(items, "Leave in 33 min").y == find(items, "2nd").y


def test_no_leave_column_for_a_train_that_is_not_drawn():
    fourth = [*SCREEN_TRAINS, Row("08:42", "Manchester Piccadilly", "On time")]
    items = sign_layout(screen(fourth, leave="Leave in 95 min", leave_for=3), *dots(HDMI_1920X440)).items
    assert "Leave in 95 min" not in texts(items)


def test_the_trains_leave_column_sits_between_platform_and_status():
    items = sign_layout(screen(), *dots(HDMI_1920X440)).items
    leave = find(items, "Leave in 3 min")
    platforms = [i for i in items if i.text.startswith("Plat")]
    assert len({i.x for i in platforms}) == 1
    assert platforms[0].x + FONT.getlength("Plat 1") + 5 <= leave.x
    for status in ("On time", "Exp 07:49", "Cancelled"):
        assert leave.x + FONT.getlength(leave.text) + 5 <= find(items, status).x


def test_the_7_9_inch_screen_keeps_the_leave_line_under_its_train():
    items = sign_layout(screen(), *dots(HDMI_1280X400)).items
    leave = find(items, "Leave in 3 min")
    assert find(items, "1st").y < leave.y < find(items, "2nd").y


def test_the_bar_screen_fits_three_trains_three_buses_and_the_clock():
    width, height = dots(HDMI_1920X440)
    items = sign_layout(screen(), width, height).items
    assert {"1st", "2nd", "3rd", "341", "61", "237", CLOCK} <= set(texts(items))
    clock = find(items, CLOCK)
    assert find(items, "237").y + 9 < clock.y
    assert clock.y + 9 <= height


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_wide_screens_say_where_each_train_and_bus_is(panel):
    layout = sign_layout(screen(), *dots(panel))
    items = layout.items
    assert texts(line_of(items, "341"))[3] == "3 stops away"
    assert "1 stop away" in texts(line_of(items, "237"))
    assert "At Dinting" in texts(line_of(items, "1st"))
    assert {(m.y, m.strip.where, m.train) for m in layout.strips} == {
        (find(items, "1st").y, "At Dinting", True),
        (find(items, "2nd").y, "Not yet left Hadfield sidings", True),
        (find(items, "341").y, "3 stops away", False),
        (find(items, "237").y, "1 stop away", False),
    }


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_a_row_without_a_strip_leaves_the_space_blank(panel):
    layout = sign_layout(screen(), *dots(panel))
    for first in ("3rd", "61"):
        y = find(layout.items, first).y
        assert not any(m.y == y for m in layout.strips)
    assert texts(line_of(layout.items, "61")) == ["61", "B", "Buxton", "Leave in 23 min", "07:35 sch"]


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_where_words_and_little_lines_line_up_down_the_board(panel):
    layout = sign_layout(screen(), *dots(panel))
    assert len({m.x for m in layout.strips}) == 1
    assert len({find(layout.items, w).x for w in ("At Dinting", "3 stops away", "1 stop away")}) == 1
    assert all(m.width == STRIP_WIDTH for m in layout.strips)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("leave_for", [0, 1])
def test_a_rows_columns_never_overlap(panel, leave_for):
    layout = sign_layout(screen(leave_for=leave_for), *dots(panel))
    for mark in layout.strips:
        line = [i for i in layout.items if i.y == mark.y]
        boxes = sorted([(i.x, i.x + item_size(i)[0]) for i in line] + [(mark.x, mark.x + mark.width)])
        for (_, end), (start, _) in itertools.pairwise(boxes):
            assert end + 4 <= start, (mark, line)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_long_where_words_scroll_in_their_column(panel):
    items = sign_layout(screen(), *dots(panel)).items
    where = next(i for i in items if i.text.startswith("Not yet left"))
    assert where.text == "Not yet left Hadfield sidings"
    assert where.box == where_width(dots(panel)[0])


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_destination_gives_way_to_the_strip(panel):
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley and Hyde Central"
    rows = [dataclasses.replace(SCREEN_TRAINS[0], middle=long)]
    items = sign_layout(screen(rows), *dots(panel)).items
    dest = next(i for i in items if i.text.startswith("Manchester"))
    assert dest.text == long and dest.box is not None  # scrolling in its space
    assert dest.x + item_size(dest)[0] + 5 <= find(items, "At Dinting").x


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_every_destination_keeps_room_beside_the_strip(panel):
    # A long destination keeps about 16 letters, giving way to the where words.
    long = "Stepping Hill Hospital via Marple and Romiley"
    trains = [dataclasses.replace(r, middle=long) for r in SCREEN_TRAINS]
    buses = [dataclasses.replace(r, middle=long) for r in SCREEN_BUSES]
    items = sign_layout(screen(trains, buses), *dots(panel)).items
    shown = [i for i in items if i.text.startswith("Stepping")]
    assert len(shown) == 6
    assert all(FONT.getlength(i.text) >= 85 for i in shown)


def test_the_strip_threshold_lets_in_both_screens_and_not_the_e_paper():
    assert dots(HDMI_1280X400)[0] >= STRIP_COLUMN_MIN_WIDTH > dots(WAVESHARE_10IN85G)[0]
    assert STRIP_COLUMN_MIN_WIDTH > PLATFORM_COLUMN_MIN_WIDTH


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_without_a_clock_no_strips_however_wide(panel):
    # A strip changes every few seconds: only a screen, which redraws, shows one.
    layout = sign_layout(dataclasses.replace(screen(), clock=None), *dots(panel))
    assert not layout.strips
    assert not any(i.text in ("At Dinting", "3 stops away") for i in layout.items)


@pytest.mark.parametrize("panel", [None, WAVESHARE_10IN85G], ids=lambda p: p.name if p else "inky-phat")
def test_the_e_paper_boards_are_unchanged_by_strips(panel):
    plain_trains = [dataclasses.replace(r, strip=None) for r in SCREEN_TRAINS]
    plain_buses = [dataclasses.replace(r, strip=None) for r in SCREEN_BUSES]
    kwargs = {"panel": panel} if panel else {}
    with_strips = render_sign(board(SCREEN_TRAINS, SCREEN_BUSES, leave="Leave by 07:07"), PALETTE, **kwargs)
    without = render_sign(board(plain_trains, plain_buses, leave="Leave by 07:07"), PALETTE, **kwargs)
    assert with_strips.tobytes() == without.tobytes()


# A strip stays inside its row's reversed-out band: a dot above the text to its bottom.
STRIP_TOP, STRIP_BOTTOM = -1, 8


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("notice", [None, NOTICE])
def test_everything_fits_on_a_wide_screen_with_strips(panel, notice):
    width, height = dots(panel)
    layout = sign_layout(screen(stale_buses=True, notice=notice), width, height)
    for item in layout.items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height
    footer_top = min(i.y for i in layout.items if i.text == CLOCK)
    for mark in layout.strips:
        for x, y in strip_dots(mark):
            assert mark.x <= x < mark.x + mark.width
            assert mark.y + STRIP_TOP <= y <= mark.y + STRIP_BOTTOM < footer_top


def mark(strip, train=False):
    return StripMark(100, 20, strip, train)


def test_the_little_line_marks_your_stop_bigger_and_the_stretch_after_it_dotted():
    lit = strip_dots(mark(Strip("3 stops away", 7, 5, None)))
    bottom = max(y for _, y in lit)
    line_y = bottom - 2  # your stop reaches two dots either side of the line
    top_xs = sorted(x for x, y in lit if y == line_y - 2)
    assert top_xs and top_xs[-1] - top_xs[0] == 4  # only yours, five dots across
    yours_x = top_xs[0] + 2
    line = sorted(x for x, y in lit if y == line_y)
    assert all((x, line_y) in lit for x in range(line[0], yours_x))  # solid up to yours
    assert any((x, line_y) not in lit for x in range(yours_x + 3, line[-1]))  # gaps after it


def test_the_little_line_draws_the_vehicle_where_it_is_and_none_when_unknown():
    def vehicle(strip, train=False):
        return {(x, y) for x, y in strip_dots(mark(strip, train)) if y < 20 + 4}

    near = vehicle(Strip("At the first", 7, 5, 0.0))
    far = vehicle(Strip("At your stop", 7, 5, 5.0))
    assert near and far
    assert max(x for x, _ in near) < min(x for x, _ in far)
    assert not vehicle(Strip("No live position", 7, 5, None))
    # A train and a bus differ by shape too, not only by the section they are in.
    assert vehicle(Strip("At Dinting", 4, 2, 1.0), train=True) != vehicle(Strip("At Dinting", 4, 2, 1.0))


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_little_line_is_lit_on_the_screen(panel):
    layout = sign_layout(screen(), *dots(panel))
    [strip] = [m for m in layout.strips if m.strip.where == "3 stops away"]
    image = render_sign(screen(), PALETTE, panel)
    x0, y0 = _origin(panel)
    for x, y in strip_dots(strip):
        assert image.getpixel((x0 + x * panel.scale, y0 + y * panel.scale)) == PALETTE.accent


def test_a_cancelled_rows_strip_is_dark_on_its_band():
    rows = [dataclasses.replace(SCREEN_TRAINS[0], style=Style.INVERTED, right="Cancelled")]
    panel = HDMI_1920X440
    [strip] = [m for m in sign_layout(screen(rows), *dots(panel)).strips if m.train]
    assert strip.inverted
    image = render_sign(screen(rows), PALETTE, panel)
    x0, y0 = _origin(panel)
    for x, y in strip_dots(strip):
        assert image.getpixel((x0 + x * panel.scale, y0 + y * panel.scale)) == PALETTE.black


@pytest.mark.parametrize("bus_at", [2.45, None])
def test_the_route_page_fits_the_bar_screen(bus_at):
    width, height = dots(HDMI_1920X440)
    layout, road, _ = _route_layout(route(bus_at), width, height)
    for item in layout.items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height
    # The bus, six dots tall on the road, clears the second line and the names above the road.
    assert find(layout.items, "Leave in 6 min").y + 9 < road - 8
    assert all(find(layout.items, name).y + 9 <= road - 2 for name in NAMES[::2])
    assert render_route_sign(route(bus_at), PALETTE, HDMI_1920X440).size == HDMI_1920X440.size


EIGHTEEN = "Left Glossop 07:08"  # as long as the where words are meant to be


def test_eighteen_letters_of_where_fit_uncut_on_the_bar_screen():
    assert len(EIGHTEEN) == 18
    rows = [dataclasses.replace(SCREEN_TRAINS[0], strip=Strip(EIGHTEEN, 4, 2, 1.0)), *SCREEN_TRAINS[1:]]
    buses = [dataclasses.replace(SCREEN_BUSES[0], strip=Strip(EIGHTEEN, 7, 5, 2.4)), *SCREEN_BUSES[1:]]
    items = sign_layout(screen(rows, buses), *dots(HDMI_1920X440)).items
    assert texts(items).count(EIGHTEEN) == 2


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_where_column_is_as_wide_whatever_the_words(panel):
    width, height = dots(panel)
    short = [dataclasses.replace(r, strip=Strip("At Dinting", 4, 2, 1.0)) for r in SCREEN_TRAINS]
    for view in (screen(), screen(short)):
        layout = sign_layout(view, width, height)
        for mark in layout.strips:
            where = max((i for i in layout.items if i.y == mark.y and i.x < mark.x), key=lambda i: i.x)
            assert mark.x - where.x == where_width(width) + 5


def test_the_where_column_takes_the_spare_width_on_wider_screens():
    narrow, wide = where_width(dots(HDMI_1280X400)[0]), where_width(dots(HDMI_1920X440)[0])
    assert FONT.getlength("3 stops away") <= narrow < wide
    assert FONT.getlength(EIGHTEEN) <= wide < FONT.getlength("Leaving Hadfield now")  # 18 letters, not 20


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_manchester_piccadilly_stays_whole_on_either_screen_with_strips(panel):
    items = sign_layout(screen(), *dots(panel)).items
    shown = [i for i in items if i.text.startswith("Manchester")]
    assert [i.text for i in shown] == ["Manchester Piccadilly"] * 3
    assert all(i.box is None for i in shown)  # drawn whole, never scrolling



# The bottom line on a screen: the notice and the rain warning side by side.

from twobells.board.panel import NOTES_SIDE_BY_SIDE_MIN_WIDTH  # noqa: E402


def both(view, clock=CLOCK):
    return dataclasses.replace(view, notice=NOTICE, rain=RAIN, clock=clock)


def footer_line(items, height):
    return sorted((i for i in items if i.y == height - 10), key=lambda i: i.x)


def test_both_screens_are_wide_enough_for_notice_and_rain_side_by_side():
    for panel in STRIP_PANELS:
        assert dots(panel)[0] >= NOTES_SIDE_BY_SIDE_MIN_WIDTH


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("stale", [False, True])
def test_a_screen_shows_the_notice_then_the_rain_warning_then_the_clock(panel, stale):
    width, height = dots(panel)
    items = sign_layout(both(screen(stale_buses=stale)), width, height).items
    notice, rain, clock = footer_line(items, height)
    assert notice.text.startswith("! ") and notice.x == MARGIN
    assert rain.text == RAIN and clock.text == CLOCK
    assert notice.x + item_size(notice)[0] + 10 <= rain.x
    assert rain.x + FONT.getlength(RAIN) + 5 <= clock.x
    if stale:
        badge = find(items, "Not updating: buses")
        assert clock.x + FONT.getlength(CLOCK) + 5 <= badge.x - 2
        assert badge.x + item_size(badge)[0] <= width - MARGIN
    else:
        assert clock.x + FONT.getlength(CLOCK) == width - MARGIN


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_beside_the_rain_warning_the_notice_scrolls_in_its_own_space(panel):
    width, height = dots(panel)
    view = both(screen())
    notice, rain, _ = footer_line(sign_layout(view, width, height).items, height)
    assert notice.text == "! " + NOTICE and notice.box is not None
    assert notice.x + notice.box + 10 <= rain.x


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_rain_warning_stays_put_as_the_clock_runs(panel):
    width, height = dots(panel)
    xs = {find(sign_layout(both(screen(), clock=f"07:03:{s:02d}"), width, height).items, RAIN).x
          for s in range(0, 60, 5)}
    assert len(xs) == 1


def test_e_paper_still_shows_only_the_notice():
    for width, height in ((WIDTH, HEIGHT), dots(WAVESHARE_10IN85G)):
        items = texts(sign_layout(both(board(), clock=None), width, height).items)
        assert RAIN not in items and any(t.startswith("! ") for t in items)


# A rule over the bottom line, like the divider between trains and buses.


def footer_rule(layout):
    return layout.rules[1] if len(layout.rules) > 1 else None


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("notice", [None, NOTICE])
def test_a_screen_has_a_rule_over_its_bottom_line_with_even_gaps(panel, notice):
    width, height = dots(panel)
    layout = sign_layout(screen(notice=notice), width, height)
    rule = footer_rule(layout)
    assert rule is not None
    last_bus_bottom = find(layout.items, "237").y + 8
    footer_top = height - 10
    above, below = rule - last_bus_bottom - 1, footer_top - rule - 1
    assert above >= 3 and below >= 3 and abs(above - below) <= 1


def test_the_bar_screen_keeps_three_buses_under_its_footer_rule():
    width, height = dots(HDMI_1920X440)
    layout = sign_layout(screen(buses=SCREEN_BUSES * 2), width, height)
    assert sum(i.text in ("341", "61", "237") for i in layout.items) == 3
    assert max(i.y for i in layout.items if i.text in ("341", "61", "237")) + 9 < footer_rule(layout)


def test_e_paper_has_the_footer_rule_where_it_costs_no_row():
    layout = sign_layout(board(TRAINS, BUSES, leave="Leave by 07:05"))
    rule = footer_rule(layout)
    assert rule is not None
    last_bus_bottom = max(i.y for i in layout.items if i.text in ("341", "394")) + 8
    footer_top = find(layout.items, "Updated 07:03").y
    above, below = rule - last_bus_bottom - 1, footer_top - rule - 1
    assert above >= 3 and below >= 3 and abs(above - below) <= 1


def test_e_paper_leaves_the_footer_rule_off_rather_than_lose_a_row():
    width, height = dots(WAVESHARE_10IN85G)
    buses = [*BUSES, Row("237", "Ashton", "07:41", leave="Leave by 07:34", stop="A"),
             Row("61", "Buxton", "07:50", leave="Leave by 07:43", stop="B")]
    with_rule_room = sign_layout(board(TRAINS, buses, leave="Leave by 07:05"), width, height)
    assert footer_rule(with_rule_room) is None
    assert "61" in texts(with_rule_room.items)  # the fourth bus is still there


def test_the_footer_rule_is_drawn_dotted():
    panel = HDMI_1920X440
    layout = sign_layout(screen(), *dots(panel))
    image = render_sign(screen(), PALETTE, panel)
    x0, y0 = _origin(panel)
    y = y0 + footer_rule(layout) * panel.scale
    assert image.getpixel((x0 + MARGIN * panel.scale, y)) == PALETTE.accent
    assert image.getpixel((x0 + (MARGIN + 1) * panel.scale, y)) == PALETTE.black


# On a screen, text too long for its space scrolls, a dot at a time, inside its own box.

from twobells.board.panel import PHAT  # noqa: E402
from twobells.board.scroll import SCROLL_PAUSE_S, SCROLL_STEPS_PER_S  # noqa: E402
from twobells.board.sign import route_sign_frame, sign_frame  # noqa: E402

LONG = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley and Hyde Central"


def long_screen():
    trains = [dataclasses.replace(r, middle=LONG) for r in SCREEN_TRAINS]
    return screen(trains, notice=NOTICE)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_each_scrolling_field_sits_in_its_own_column_in_whole_dots(panel):
    layout = sign_layout(long_screen(), *dots(panel))
    frame = sign_frame(long_screen(), PALETTE, panel)
    scrolling = [i for i in layout.items if i.box is not None]
    assert {i.text for i in scrolling} >= {LONG, "! " + NOTICE, "Not yet left Hadfield sidings"}
    assert [s.text for s in frame.scrolls] == [i.text for i in scrolling]
    x0, y0 = _origin(panel)
    s = panel.scale
    for item, scroll in zip(scrolling, frame.scrolls, strict=True):
        assert scroll.box == (x0 + int(item.x) * s, y0 + item.y * s, x0 + (int(item.x) + item.box) * s,
                              y0 + (item.y + 9) * s)
        assert scroll.step == s  # a dot at a time
        assert scroll.strip.width == FONT.getlength(item.text) * s


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_text_that_fits_on_a_screen_does_not_scroll(panel):
    scrolling = {s.text for s in sign_frame(screen(), PALETTE, panel).scrolls}
    assert not scrolling & {"At Dinting", "3 stops away", "Hyde", "Buxton"}
    short = sign_frame(screen(notice="Lifts out of use"), PALETTE, panel)
    assert "! Lifts out of use" not in {s.text for s in short.scrolls}


def test_scrolling_text_moves_only_inside_its_box():
    panel = HDMI_1920X440
    frame = sign_frame(long_screen(), PALETTE, panel)
    still, later = frame.at(0), frame.at(SCROLL_PAUSE_S + 1)
    changed = [(x, y) for y in range(panel.height) for x in range(panel.width)
               if still.getpixel((x, y)) != later.getpixel((x, y))]
    assert changed
    boxes = [s.box for s in frame.scrolls]
    assert all(any(left <= x < right and top <= y < bottom for left, top, right, bottom in boxes)
               for x, y in changed)


def test_scrolling_text_holds_at_its_start_then_moves_a_dot_a_step():
    panel = HDMI_1920X440
    frame = sign_frame(long_screen(), PALETTE, panel)
    scroll = next(s for s in frame.scrolls if s.text == LONG)
    assert scroll.offset(0) == scroll.offset(SCROLL_PAUSE_S - 0.1) == 0
    assert scroll.offset(SCROLL_PAUSE_S + 1) == SCROLL_STEPS_PER_S * panel.scale
    assert frame.image.tobytes() == frame.at(0).tobytes()  # the still board shows each from its start


def test_a_reversed_out_row_scrolls_on_its_lit_band():
    frame = sign_frame(long_screen(), PALETTE, HDMI_1920X440)
    normal, _, cancelled = (s for s in frame.scrolls if s.text == LONG)
    # Below the last letter, an "l" with no descender: lit on the cancelled train's band, dark on a normal row.
    assert cancelled.strip.getpixel((cancelled.strip.width - 2, 33)) == PALETTE.accent
    assert normal.strip.getpixel((normal.strip.width - 2, 33)) == PALETTE.black


def test_e_paper_still_cuts_long_text_short_and_nothing_scrolls():
    trains = [dataclasses.replace(r, middle=LONG) for r in TRAINS]
    view = dataclasses.replace(board(trains, BUSES), notice=NOTICE)
    for panel in (PHAT, WAVESHARE_10IN85G):
        assert sign_frame(view, PALETTE, panel).scrolls == ()
        items = sign_layout(view, *dots(panel)).items
        assert all(i.box is None for i in items)
        assert any(i.text.startswith("Manchester") and i.text.endswith("…") for i in items)
        assert any(i.text.startswith("! ") and i.text.endswith("…") for i in items)


def test_the_route_page_scrolls_a_long_title_on_a_screen():
    view = RouteView("341 to " + LONG, "Exp 07:24", "Leave in 6 min", NAMES, 1.0, "2 stops away", CLOCK)
    assert [s.text for s in route_sign_frame(view, PALETTE, HDMI_1920X440).scrolls] == ["341 to " + LONG]
    e_paper = dataclasses.replace(view, clock=None)
    assert route_sign_frame(e_paper, PALETTE, WAVESHARE_10IN85G).scrolls == ()


# A screen wide enough names each bus row's stop where e-paper shows its label.

from twobells.board.panel import STOP_NAME_MIN_WIDTH  # noqa: E402

GLOSSOP_STOPS = ("Henry Street A", "Henry Street B")  # both of Glossop's stops are on Henry Street
NAMED_BUSES = [
    dataclasses.replace(SCREEN_BUSES[0], stop_name="Henry Street A"),
    dataclasses.replace(SCREEN_BUSES[1], stop_name="Henry Street B"),
    dataclasses.replace(SCREEN_BUSES[2], stop_name="Henry Street A"),
]


def named(buses=NAMED_BUSES, stop_names=GLOSSOP_STOPS, clock=CLOCK, **changes):
    view = screen(buses=buses, **changes)
    return dataclasses.replace(view.with_section(dataclasses.replace(view.section("bus"), stop_names=stop_names)), clock=clock)


def test_only_the_bar_screen_is_wide_enough_for_stop_names():
    assert dots(HDMI_1920X440)[0] >= STOP_NAME_MIN_WIDTH > dots(HDMI_1280X400)[0]


def test_the_bar_screen_names_each_bus_row_s_stop_where_the_label_was():
    items = sign_layout(named(), *dots(HDMI_1920X440)).items
    assert texts(line_of(items, "341")) == ["341", "Henry Street A", "Hyde", "3 stops away", "Leave in 12 min",
                                           "Exp 07:24"]
    assert texts(line_of(items, "61"))[:3] == ["61", "Henry Street B", "Buxton"]
    assert "A" not in texts(items) and "B" not in texts(items)


def test_stop_names_and_destinations_line_up_down_the_bus_rows():
    items = sign_layout(named(), *dots(HDMI_1920X440)).items
    assert len({line_of(items, first)[1].x for first in ("341", "61", "237")}) == 1
    destinations = [find(items, d) for d in ("Hyde", "Buxton", "Ashton")]
    assert len({d.x for d in destinations}) == 1
    # Twice the usual gap after the name, so it and the destination don't read as one.
    assert destinations[0].x == find(items, "Henry Street B").x + FONT.getlength("Henry Street B") + 10


def test_the_stop_column_is_as_wide_whatever_buses_are_shown():
    # From all your stops' names, not the rows': B's longer name keeps its room with no bus from B.
    width, height = dots(HDMI_1920X440)
    names = ("Henry Street", "Norfolk Square")
    from_a = [dataclasses.replace(r, stop="A", stop_name="Henry Street") for r in SCREEN_BUSES]
    from_b = dataclasses.replace(SCREEN_BUSES[1], stop_name="Norfolk Square")
    only_a = sign_layout(named(from_a, names), width, height).items
    both_stops = sign_layout(named([from_a[0], from_b], names), width, height).items
    assert find(only_a, "Hyde").x == find(both_stops, "Hyde").x
    assert find(only_a, "Hyde").x == line_of(only_a, "341")[1].x + FONT.getlength("Norfolk Square") + 10


def test_a_long_stop_name_scrolls_in_a_column_about_sixteen_letters_wide():
    long = "Glossop Norfolk Street Bus Station Stand 3"
    buses = [dataclasses.replace(NAMED_BUSES[0], stop_name=long), *NAMED_BUSES[1:]]
    items = sign_layout(named(buses, (long, "Henry Street B")), *dots(HDMI_1920X440)).items
    stop = find(items, long)
    assert stop.box is not None  # scrolling in its space
    assert FONT.getlength("Henry Street B") < stop.box <= FONT.getlength("Norfolk Square W")
    assert stop.x + stop.box + 10 <= find(items, "Hyde").x
    assert find(items, "Henry Street B").box is None  # a name that fits stays still


def test_a_stop_whose_name_isn_t_known_shows_its_label_in_the_column():
    buses = [NAMED_BUSES[0], dataclasses.replace(NAMED_BUSES[1], stop_name=None), NAMED_BUSES[2]]
    items = sign_layout(named(buses, ("Henry Street A",)), *dots(HDMI_1920X440)).items
    assert texts(line_of(items, "61"))[:3] == ["61", "B", "Buxton"]
    assert find(items, "B").x == line_of(items, "341")[1].x == line_of(items, "237")[1].x
    assert find(items, "Buxton").x == find(items, "Hyde").x


@pytest.mark.parametrize("leave_for", [0, 1])
def test_a_named_bus_row_s_columns_never_overlap(leave_for):
    layout = sign_layout(named(leave_for=leave_for), *dots(HDMI_1920X440))
    for first in ("341", "61", "237"):
        line = line_of(layout.items, first)
        marks = [(m.x, m.x + m.width) for m in layout.strips if m.y == line[0].y]
        boxes = sorted([(i.x, i.x + item_size(i)[0]) for i in line] + marks)
        for (_, end), (start, _) in itertools.pairwise(boxes):
            assert end + 4 <= start, line


def test_bus_destinations_keep_room_beside_stop_names():
    # A long destination scrolls in what the name leaves it: eight letters at least.
    long = "Stepping Hill Hospital via Marple and Romiley"
    buses = [dataclasses.replace(r, middle=long) for r in NAMED_BUSES]
    items = sign_layout(named(buses), *dots(HDMI_1920X440)).items
    shown = [i for i in items if i.text == long]
    assert len(shown) == 3
    assert all(i.box is not None and i.box >= FONT.getlength("Stepping") for i in shown)


def test_stop_names_leave_the_train_rows_as_they_were():
    width, height = dots(HDMI_1920X440)
    before, after = sign_layout(screen(), width, height).items, sign_layout(named(), width, height).items
    first_bus = find(before, "341").y
    assert [i for i in before if i.y < first_bus] == [i for i in after if i.y < first_bus]
    assert [i.text for i in after if i.text.startswith("Manchester")] == ["Manchester Piccadilly"] * 3


@pytest.mark.parametrize("panel", [HDMI_1280X400, WAVESHARE_10IN85G], ids=lambda p: p.name)
def test_narrower_boards_keep_the_label(panel):
    view = named(clock=None if panel.epaper else CLOCK)
    items = sign_layout(view, *dots(panel)).items
    assert texts(line_of(items, "341"))[:3] == ["341", "A", "Hyde"]
    assert not any(i.text.startswith("Henry Street") for i in items)


def test_without_a_clock_the_bar_screen_s_width_keeps_the_label():
    items = sign_layout(named(clock=None), *dots(HDMI_1920X440)).items
    assert texts(line_of(items, "341"))[:3] == ["341", "A", "Hyde"]


@pytest.mark.parametrize("panel", [None, WAVESHARE_10IN85G, HDMI_1280X400], ids=lambda p: p.name if p else "inky-phat")
def test_e_paper_and_the_7_9_inch_screen_are_unchanged_by_stop_names(panel):
    kwargs = {"panel": panel} if panel else {}
    clock = CLOCK if panel is HDMI_1280X400 else None
    labels = render_sign(dataclasses.replace(screen(), clock=clock), PALETTE, **kwargs)
    assert render_sign(named(clock=clock), PALETTE, **kwargs).tobytes() == labels.tobytes()


def test_everything_fits_on_the_bar_screen_with_stop_names():
    width, height = dots(HDMI_1920X440)
    layout = sign_layout(named(stale_buses=True, notice=NOTICE), width, height)
    for item in layout.items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width
        assert item.y >= 0 and item.y + h <= height


# The first train's "Calling at" line, scrolling under its row on a screen.

CALLING = ("Calling at: Dinting, Broadbottom, Hattersley, Godley and Manchester Piccadilly. "
           "This train is formed of 3 coaches.")


def calling(view=None, text=CALLING):
    view = view or screen()
    first = dataclasses.replace(view.section("train").rows[0], calling=text)
    return view.with_section(dataclasses.replace(view.section("train"), rows=(first, *view.section("train").rows[1:])))


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_a_screen_scrolls_the_calling_at_line_under_the_first_train(panel):
    items = sign_layout(calling(), *dots(panel)).items
    line = find(items, CALLING)
    assert find(items, "1st").y < line.y < find(items, "2nd").y
    assert line.x == find(items, "07:12").x  # under the times, as the leave line is
    assert line.box is not None and line.x + line.box == dots(panel)[0] - MARGIN  # scrolling to the edge


def test_the_calling_at_line_comes_before_the_leave_line():
    items = sign_layout(calling(), *dots(HDMI_1280X400)).items
    assert find(items, "1st").y < find(items, CALLING).y < find(items, "Leave in 3 min").y < find(items, "2nd").y


def test_a_short_calling_at_line_stands_still():
    items = sign_layout(calling(text="Calling at: Dinting only."), *dots(HDMI_1280X400)).items
    assert find(items, "Calling at: Dinting only.").box is None


def test_only_the_first_train_has_a_calling_at_line():
    view = screen()
    second = dataclasses.replace(view.section("train").rows[1], calling=CALLING)
    trains = view.section("train")
    view = view.with_section(dataclasses.replace(trains, rows=(trains.rows[0], second, *trains.rows[2:])))
    assert CALLING not in texts(sign_layout(view, *dots(HDMI_1280X400)).items)


def test_e_paper_has_no_calling_at_line():
    view = calling(dataclasses.replace(screen(), clock=None))
    assert CALLING not in texts(sign_layout(view, *dots(WAVESHARE_10IN85G)).items)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_with_the_calling_at_line_a_screen_keeps_its_three_buses(panel):
    width, height = dots(panel)
    layout = sign_layout(calling(), width, height)
    assert {"341", "61", "237"} <= set(texts(layout.items))
    for item in layout.items:
        w, h = item_size(item)
        assert item.x >= 0 and item.x + w <= width and item.y >= 0 and item.y + h <= height


def test_the_bar_screen_gives_up_its_footer_rule_for_the_calling_at_line_not_a_bus():
    width, height = dots(HDMI_1920X440)
    assert footer_rule(sign_layout(screen(), width, height)) is not None
    assert footer_rule(sign_layout(calling(), width, height)) is None
    # The 7.9" screen has room for both.
    assert footer_rule(sign_layout(calling(), *dots(HDMI_1280X400))) is not None


# With scrolling off (the scrolling setting), a screen cuts long text short, as e-paper does.

@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_with_scrolling_off_a_screen_cuts_long_text_short_and_nothing_scrolls(panel):
    view = dataclasses.replace(calling(long_screen()), scroll=False)
    assert sign_frame(view, PALETTE, panel).scrolls == ()
    items = sign_layout(view, *dots(panel)).items
    assert all(i.box is None for i in items)
    assert any(i.text.startswith("Manchester") and i.text.endswith("…") for i in items)
    assert any(i.text.startswith("! ") and i.text.endswith("…") for i in items)
    assert any(i.text.startswith("Calling at: ") and i.text.endswith("…") for i in items)  # still under the train


def test_with_scrolling_off_the_route_page_cuts_a_long_title_short():
    view = RouteView("341 to " + LONG, "Exp 07:24", "Leave in 6 min", NAMES, 1.0, "2 stops away", CLOCK, scroll=False)
    assert route_sign_frame(view, PALETTE, HDMI_1920X440).scrolls == ()
    layout, _, _ = _route_layout(view, *dots(HDMI_1920X440))
    assert any(i.text.startswith("341 to ") and i.text.endswith("…") for i in layout.items)
