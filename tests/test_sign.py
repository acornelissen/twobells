import dataclasses

import pytest

from departureboard.dotfont import DotFont
from departureboard.panel import HDMI_1280X400, WAVESHARE_10IN85G
from departureboard.render import HEIGHT, WIDTH, Palette
from departureboard.sign import MARGIN, item_size, render_sign, sign_layout
from departureboard.view import BoardView, Row, Section, Style

PALETTE = Palette(white=0, black=1, accent=2)
FONT = DotFont()


def board(trains=(), buses=(), leave=None, leave_for=0, stale_trains=False, stale_buses=False):
    return BoardView(
        title="Glossop > Manchester Picc",
        trains=Section("Trains", tuple(trains), "No trains", stale_trains, leave, leave_for if leave else None),
        buses=Section("Buses", tuple(buses), "No buses", stale_buses),
        updated="07:03",
    )


TRAINS = [
    Row("07:12", "Manchester Piccadilly", "On time"),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED),
]
BUSES = [
    Row("341", "Hyde", "07:24 +4", Style.ALERT, "leave 07:17", "A"),
    Row("394", "Stepping Hill Hospital via Marple and Romiley", "07:35 sch", Style.NORMAL, "leave 07:28", "B"),
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
    assert texts(line) == ["341", "A", "Hyde", "leave 07:17", "07:24 +4"]


def test_bus_leave_times_line_up_and_long_destinations_give_way():
    items = sign_layout(board(buses=BUSES)).items
    assert find(items, "leave 07:17").x == find(items, "leave 07:28").x
    dest = next(i for i in items if i.text.startswith("Stepping"))
    assert dest.text.endswith("…")
    assert dest.x + FONT.getlength(dest.text) < find(items, "leave 07:28").x


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
        assert 0 <= item.x and item.x + width <= WIDTH
        assert 0 <= item.y and item.y + height <= HEIGHT


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
    [rule] = layout.rules
    assert rule - last_train_bottom >= 8
    assert first_bus_top - rule >= 10


def test_buses_still_clear_the_footer_with_a_full_board():
    rows = BUSES + [Row("237", "Ashton", "07:41", leave="leave 07:34", stop="A")]
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


BIG_PANELS = [WAVESHARE_10IN85G, HDMI_1280X400]


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
        assert 0 <= item.x and item.x + w <= width
        assert 0 <= item.y and item.y + h <= height


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
    [rule] = sign_layout(board(), *dots(panel)).rules
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


from departureboard.route import RouteView  # noqa: E402
from departureboard.sign import _route_layout, render_route_sign  # noqa: E402

NAMES = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")


def route(bus_at=2.45, where="3 stops away", clock="07:12:40"):
    return RouteView("341 to Hyde", "07:24 +4", "leave in 6 min", NAMES, bus_at, where, clock)


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
        assert 0 <= item.x and item.x + w <= width
        assert 0 <= item.y and item.y + h <= height
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
    assert {"341 to Hyde", "07:24 +4", "leave in 6 min", "3 stops away", "07:12:40"} <= set(texts(layout.items))


def test_no_leave_line_for_a_train_that_is_not_drawn():
    fourth = TRAINS + [Row("08:42", "Manchester Piccadilly", "On time")]
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
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom and Guide Bridge"
    rows = [Row("07:12", long, "On time", platform="Plat 1")]
    items = sign_layout(board(rows), width, height).items
    dest = next(i for i in items if i.text.startswith("Manchester"))
    assert dest.text.endswith("…")
    assert dest.x + FONT.getlength(dest.text) + 5 <= find(items, "Plat 1").x


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_no_platform_column_when_no_train_has_one(panel):
    width, height = dots(panel)
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom and Guide Bridge"
    with_column = sign_layout(board([Row("07:12", long, "On time", platform="Plat 1")]), width, height).items
    without = sign_layout(board([Row("07:12", long, "On time")]), width, height).items
    dest = lambda items: next(i.text for i in items if i.text.startswith("Manchester"))
    assert len(dest(without)) > len(dest(with_column))


def test_the_phat_has_no_room_for_platforms_and_is_unchanged():
    assert not any(i.text.startswith("Plat") for i in sign_layout(board(WITH_PLATFORMS)).items)
    with_platforms = render_sign(board(WITH_PLATFORMS, BUSES), PALETTE)
    without = render_sign(board(TRAINS, BUSES), PALETTE)
    assert with_platforms.tobytes() == without.tobytes()


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_platforms_fit_on_a_bigger_board(panel):
    width, height = dots(panel)
    items = sign_layout(board(WITH_PLATFORMS, BUSES, leave="Leave by 07:05"), width, height).items
    for item in items:
        w, h = item_size(item)
        assert 0 <= item.x and item.x + w <= width


NOTICE = (
    "Disruption between Glossop and Manchester Piccadilly until the end of the day. "
    "Trains may be cancelled or delayed by up to 20 minutes."
)
def noticed(view, notice=NOTICE, clock=None):
    return dataclasses.replace(view, notice=notice, clock=clock)


def test_notice_replaces_updated_on_the_phat_cut_to_fit():
    items = sign_layout(noticed(board(TRAINS, BUSES))).items
    assert "Updated 07:03" not in texts(items)
    [notice] = [i for i in items if i.text.startswith("! ")]
    assert notice.small and notice.x == MARGIN
    assert notice.text.endswith("…")
    assert notice.x + item_size(notice)[0] <= WIDTH - MARGIN
    assert NOTICE.startswith(notice.text[2:-1].rstrip())


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
    assert notice.x == MARGIN and notice.x + FONT.getlength(notice.text) + 5 <= clock.x


def test_screen_clock_and_notice_clear_the_not_updating_badge():
    view = noticed(board(stale_trains=True, stale_buses=True), clock="07:03:15")
    width, height = dots(HDMI_1280X400)
    items = sign_layout(view, width, height).items
    badge = find(items, "Not updating: trains, buses")
    assert find(items, "07:03:15").x + FONT.getlength("07:03:15") + 5 <= badge.x - 2


def test_screen_shows_a_long_notice_in_parts_every_five_seconds():
    def shown(clock):
        [notice], _, _, _ = screen_footer(noticed(board(), clock=clock))
        return notice.text

    parts = []
    for second in range(0, 60, 5):
        text = shown(f"07:03:{second:02d}")
        assert shown(f"07:03:{second + 4:02d}") == text  # steady within its five seconds
        if text not in parts:
            parts.append(text)
    assert len(parts) > 1
    assert all(p.startswith("! ") for p in parts)
    assert sum(p.endswith(" …") for p in parts) == len(parts) - 1
    words = " ".join(p[2:].removesuffix(" …") for p in parts).split()
    assert sorted(words) == sorted(NOTICE.split())


def test_screen_shows_a_short_notice_whole_and_steady():
    [notice], _, _, _ = screen_footer(noticed(board(), "Lifts out of use", clock="07:03:15"))
    assert notice.text == "! Lifts out of use"


def test_without_a_notice_the_phat_footer_is_unchanged():
    assert render_sign(board(TRAINS, BUSES), PALETTE).tobytes() == render_sign(
        noticed(board(TRAINS, BUSES), None), PALETTE
    ).tobytes()


def test_footer_glyphs_cover_notices_and_the_rain_line():
    from departureboard.dotfont import GLYPHS, TINY

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
        assert 0 <= item.x and item.x + w <= width
        assert 0 <= item.y and item.y + h <= height
