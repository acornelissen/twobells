import dataclasses
import functools
import hashlib
import string
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from tests.paths import ROOT
from twobells.board.panel import HDMI_1280X400, HDMI_1920X440, PHAT, PHAT_V1, WAVESHARE_10IN85G
from twobells.board.render import (
    HEIGHT,
    WIDTH,
    Palette,
    drawn_length,
    fit_text,
    header_text,
    heights,
    leave_column,
    leave_heading,
    left_column_width,
    load_fonts,
    middle_text,
    plain_scale,
    platform_column,
    render,
    render_code,
    render_setup,
    row_layout,
)
from twobells.board.view import BoardView, CodeView, Row, Section, SetupView, Style
from twobells.core import words

PALETTE = Palette(white=0, black=1, accent=2)


def board(train_rows=(), bus_rows=(), stale_trains=False, train_leave=None):
    return BoardView(
        title="GLO > MAN",
        trains=Section("Trains", tuple(train_rows), "No trains", stale_trains, train_leave),
        buses=Section("Buses", tuple(bus_rows), "No buses"),
        updated="07:03",
    )


def colours(image):
    return {count_colour[1] for count_colour in image.getcolors()}


def test_image_matches_panel_size_and_is_paletted():
    image = render(board(), PALETTE)
    assert image.size == (WIDTH, HEIGHT) == (250, 122)
    assert image.mode == "P"


def test_normal_board_uses_no_accent_colour():
    image = render(board([Row("07:12", "Manchester Piccadilly", "On time")]), PALETTE)
    assert PALETTE.accent not in colours(image)


def test_alert_row_uses_accent_colour():
    image = render(board([Row("07:12", "Manchester Piccadilly", "Exp 07:19", Style.ALERT)]), PALETTE)
    assert PALETTE.accent in colours(image)


def test_stale_section_uses_accent_colour():
    image = render(board(stale_trains=True), PALETTE)
    assert PALETTE.accent in colours(image)


def test_inverted_row_is_mostly_black():
    plain = render(board([Row("07:12", "Manchester", "On time")]), PALETTE)
    inverted = render(board([Row("07:12", "Manchester", "Cancelled", Style.INVERTED)]), PALETTE)

    def black(img):
        return {c: n for n, c in img.getcolors()}[PALETTE.black]

    assert black(inverted) > black(plain) + WIDTH * 8


def test_extra_rows_beyond_capacity_are_not_drawn():
    many = [Row(f"07:{i:02d}", "Manchester", "On time") for i in range(10)]
    # Must not raise or draw outside the panel.
    image = render(board(many, many), PALETTE)
    assert image.size == (WIDTH, HEIGHT)


def test_fit_text_leaves_short_text_alone():
    font = load_fonts().regular
    assert fit_text("Hyde", font, 100) == "Hyde"


def test_fit_text_truncates_with_ellipsis():
    font = load_fonts().regular
    fitted = fit_text("Manchester Piccadilly via Hadfield and Dinting", font, 80)
    assert fitted.endswith("…")
    assert font.getlength(fitted) <= 80


def test_left_column_clears_a_bold_clock_time():
    fonts = load_fonts()
    assert left_column_width(fonts) >= fonts.bold.getlength("00:00") + 4


# --- Fonts: Inter, as on the dashboard, from the package ---

BOARD_FONTS = ROOT / "src" / "twobells" / "board" / "fonts"


def test_board_fonts_are_pinned_and_licensed():
    listed = {}
    for line in (BOARD_FONTS / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name] = digest
    assert set(listed) == {"Inter-Regular.ttf", "Inter-ExtraBold.ttf", "LICENSE"}
    for name, digest in listed.items():
        assert hashlib.sha256((BOARD_FONTS / name).read_bytes()).hexdigest() == digest, name
    assert {p.name for p in BOARD_FONTS.iterdir()} == set(listed) | {"SHA256SUMS"}, "a font file is not pinned"
    assert "SIL Open Font License, Version 1.1" in (BOARD_FONTS / "LICENSE").read_text()


@pytest.mark.parametrize("scale", [1, 3])
def test_the_plain_look_draws_in_the_packaged_inter(scale):
    fonts = load_fonts(scale)
    assert fonts.regular.getname() == ("Inter", "Regular")
    assert fonts.bold.getname() == fonts.small.getname() == ("Inter", "ExtraBold")
    for font in (fonts.regular, fonts.bold, fonts.small):
        assert Path(str(font.path)).parent == BOARD_FONTS


def test_the_plain_look_lays_text_out_the_same_on_every_computer(monkeypatch):
    # Left to choose, Pillow lays text out with raqm where it can load FriBiDi
    # (Ubuntu, and Debian's Pillow on a Pi) and with its own basic layout
    # elsewhere (a Mac). The two measure text differently, so what fits a row,
    # and every recorded page, would differ from one computer to the next.
    from PIL import ImageFont

    from twobells.board import render as board_render

    asked = []
    truetype = ImageFont.truetype

    def recording(*args, **kwargs):
        asked.append(kwargs.get("layout_engine"))
        return truetype(*args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", recording)
    board_render._font.cache_clear()
    try:
        fonts = load_fonts(3, 13)
    finally:
        board_render._font.cache_clear()  # no font loaded under the stand-in stays behind
    assert asked and set(asked) == {ImageFont.Layout.BASIC}
    assert {f.layout_engine for f in (fonts.regular, fonts.bold, fonts.small)} == {ImageFont.Layout.BASIC}


@pytest.mark.parametrize("name", ["Inter-Regular.ttf", "Inter-ExtraBold.ttf"])
def test_board_fonts_keep_their_hints_and_the_text_the_board_draws(name):
    font = TTFont(BOARD_FONTS / name)
    # The hints are what keep one-bit text crisp at the pHAT's 9 and 11 px.
    assert {"fpgm", "prep", "cvt "} <= set(font.keys())
    cmap = font.getBestCmap()
    missing = [c for c in string.printable.strip() + " °·£é…–’“”→×" if ord(c) not in cmap]
    assert not missing


def test_times_line_up_in_tabular_figures():
    for font in load_fonts(1).regular, load_fonts(1).bold, load_fonts(3).bold:
        assert len({font.getlength(t) for t in ("07:12", "11:11", "08:48", "20:00")}) == 1


@pytest.mark.parametrize("panel", [PHAT, HDMI_1920X440])  # one per plain scale: 1 and 3
def test_row_text_stays_inside_its_row(panel):
    # Drawn a scale step below the row's top, as _Painter.row draws it.
    s = plain_scale(panel)
    fonts = load_fonts(s)
    row_h = heights()[2] * s
    for font in (fonts.regular, fonts.bold):
        _, top, _, bottom = font.getbbox("ÉÅHdgjpqy", mode="1")
        assert s + top >= 0 and s + bottom <= row_h


def test_preview_palette_gives_rgb_png_colours():
    image = render(board([Row("07:12", "Manchester", "Exp 07:19", Style.ALERT)]), PALETTE.with_preview("red"))
    rgb = image.convert("RGB")
    assert (255, 255, 255) in {c for _, c in rgb.getcolors()}


def ink_in_right_half_of_train_label_row(image):
    # Train section label sits just under the 15 px header.
    return sum(
        1
        for x in range(WIDTH // 2, WIDTH)
        for y in range(17, 28)
        if image.getpixel((x, y)) != PALETTE.white
    )


def test_leave_text_is_drawn_right_of_the_section_label():
    plain = render(board(), PALETTE)
    with_leave = render(board(train_leave="Leave by 07:05 (07:12)"), PALETTE)
    assert ink_in_right_half_of_train_label_row(plain) == 0
    assert ink_in_right_half_of_train_label_row(with_leave) > 30


def test_leave_text_is_not_in_accent_colour():
    image = render(board(train_leave="Leave by 07:05 (07:12)"), PALETTE)
    assert PALETTE.accent not in colours(image)


def test_row_layout_without_leave_has_no_leave_column():
    layout = row_layout(Row("341", "A: Hyde", "07:20"), load_fonts())
    assert layout.leave_x is None


def test_row_layout_places_leave_between_destination_and_time():
    fonts = load_fonts()
    row = Row("341", "A: Hyde", "Exp 07:24", Style.ALERT, "Leave by 07:17")
    layout = row_layout(row, fonts)
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < layout.leave_x
    assert layout.leave_x + fonts.small.getlength("Leave by 07:17") < layout.right_x


def test_long_destination_is_truncated_to_keep_leave_time_whole():
    fonts = load_fonts()
    row = Row("394", "B: Stepping Hill Hospital", "07:35 sch", leave="Leave by 07:28")
    layout = row_layout(row, fonts)
    assert layout.middle.endswith("…")
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < layout.leave_x


def test_row_with_leave_draws_more_ink():
    def ink(image):
        return sum(n for n, c in image.getcolors() if c != PALETTE.white)

    plain = render(board(bus_rows=[Row("341", "A: Hyde", "07:20")]), PALETTE)
    with_leave = render(board(bus_rows=[Row("341", "A: Hyde", "07:20", leave="Leave by 07:14")]), PALETTE)
    assert ink(with_leave) > ink(plain)


def test_plain_layout_prefixes_destination_with_stop_label():
    layout = row_layout(Row("341", "Hyde", "07:20", stop="A"), load_fonts())
    assert layout.middle == "A: Hyde"


EVERY_PANEL = [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440]


def e_paper_bus_rows(destination="Hyde"):
    """Bus rows as build_view gives them on e-paper: the leave words, and the time alone."""
    return (
        Row("341", destination, "Exp 07:24", Style.ALERT, "Leave by 07:16", "A", leave_at="07:16"),
        Row("61", "Buxton", "07:35 sch", leave="Leave by 07:27", stop="B", leave_at="07:27"),
    )


def test_leave_by_is_said_once_over_the_bus_leave_column():
    fonts = load_fonts()
    rows = e_paper_bus_rows()
    leave_x = leave_column(rows, fonts, WIDTH, 1)
    assert leave_heading(rows) == words.LEAVE_BY == "Leave by"
    layouts = [row_layout(row, fonts, leave_x=leave_x) for row in rows]
    assert [layout.leave for layout in layouts] == ["07:16", "07:27"]
    assert {layout.leave_x for layout in layouts} == {leave_x}  # one column, under the heading


def test_countdown_rows_keep_their_own_words_and_have_no_heading():
    rows = (Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave in 12 min", "A"),)
    assert leave_heading(rows) is None
    assert row_layout(rows[0], load_fonts()).leave == "Leave in 12 min"


@pytest.mark.parametrize("panel", EVERY_PANEL, ids=lambda p: p.name)
@pytest.mark.parametrize("destination", ["Buxton", "Stepping Hill Hospital"])
def test_bus_destinations_keep_at_least_the_room_they_had_before_leave_by(panel, destination):
    # Before the board said "Leave by" on every row, each said "leave 07:16",
    # just left of its own status. The destination keeps at least that many letters.
    s = plain_scale(panel)
    fonts = load_fonts(s)
    rows = e_paper_bus_rows(destination)
    shown = row_layout(rows[0], fonts, panel.width, s, leave_x=leave_column(rows, fonts, panel.width, s)).middle
    before = dataclasses.replace(rows[0], leave="leave 07:16", leave_at=None)
    earlier = row_layout(before, fonts, panel.width, s).middle
    assert len(shown.removesuffix("…")) >= len(earlier.removesuffix("…"))


def test_short_bus_destinations_are_whole_on_every_e_paper_panel():
    for panel in (PHAT, PHAT_V1, WAVESHARE_10IN85G):
        s = plain_scale(panel)
        fonts = load_fonts(s)
        for destination in ("Hyde", "Buxton"):
            rows = e_paper_bus_rows(destination)
            layout = row_layout(rows[0], fonts, panel.width, s, leave_x=leave_column(rows, fonts, panel.width, s))
            assert layout.middle == f"A: {destination}", panel.name


def buses_only(stale=False):
    rows = tuple(dataclasses.replace(r, style=Style.NORMAL, right="07:20") for r in e_paper_bus_rows())
    return BoardView("GLO > MAN", Section("Trains", (), "No trains"), Section("Buses", rows, "No buses", stale), "07:03")


def bus_label_band(image):
    """(top, bottom, right) of the accent "! NOT UPDATING" beside BUSES."""
    accent = [(x, y) for x in range(image.width) for y in range(image.height) if image.getpixel((x, y)) == PALETTE.accent]
    ys = [y for _, y in accent]
    return min(ys), max(ys), max(x for x, _ in accent)


def black_right_of(image, top, bottom, x0):
    return [x for x in range(x0 + 1, image.width) for y in range(top, bottom + 1) if image.getpixel((x, y)) == PALETTE.black]


@pytest.mark.parametrize("panel", EVERY_PANEL, ids=lambda p: p.name)
@pytest.mark.parametrize("text_size", range(9, 17))
def test_the_leave_heading_never_touches_not_updating(panel, text_size):
    stale = render(buses_only(stale=True), PALETTE, panel, text_size)
    top, bottom, right = bus_label_band(stale)
    heading = black_right_of(stale, top, bottom, right)
    if heading:
        assert min(heading) - right >= 6 * plain_scale(panel)
    else:
        # No room for it: each row says "Leave by" itself, so a time is never left unexplained.
        plain = render(buses_only(), PALETTE, panel, text_size)
        rows = bottom + 1, panel.height - 1
        assert len(black_right_of(stale, *rows, -1)) > len(black_right_of(plain, *rows, -1))


@pytest.mark.parametrize("panel", EVERY_PANEL, ids=lambda p: p.name)
def test_the_leave_heading_is_drawn_beside_buses(panel):
    top, bottom, _ = bus_label_band(render(buses_only(stale=True), PALETTE, panel))
    s = plain_scale(panel)
    leave_x = leave_column(buses_only().buses.rows, load_fonts(s), panel.width, s)
    assert black_right_of(render(buses_only(), PALETTE, panel), top, bottom, int(leave_x) - 1)


BIG_PANELS = [WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440]


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_bigger_panels_get_an_image_their_size(panel):
    image = render(board([Row("07:12", "Manchester", "Exp 07:19", Style.ALERT)]), PALETTE, panel)
    assert image.size == panel.size
    assert image.mode == "P"
    assert PALETTE.accent in colours(image)


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_type_grows_with_the_panel(panel):
    def ink(image):
        return sum(n for n, c in image.getcolors() if c != PALETTE.white)

    rows = [Row("07:12", "Manchester", "On time")]
    small = ink(render(board(rows), PALETTE))
    big = ink(render(board(rows), PALETTE, panel))
    assert big > small * panel.scale  # area grows with the square of the scale


def test_wider_panel_keeps_a_destination_whole_that_the_phat_truncates():
    row = Row("394", "B: Stepping Hill Hospital", "07:35 sch", leave="Leave by 07:28")
    assert row_layout(row, load_fonts()).middle.endswith("…")
    panel = HDMI_1280X400
    wide = row_layout(row, load_fonts(panel.scale), panel.width, panel.scale)
    assert wide.middle == "B: Stepping Hill Hospital"


def test_header_shows_the_running_clock_on_a_screen():
    import dataclasses

    def header_ink(image):
        return sum(1 for x in range(WIDTH // 2, WIDTH) for y in range(15) if image.getpixel((x, y)) == PALETTE.white)

    stamp = header_ink(render(board(), PALETTE))
    clock = header_ink(render(dataclasses.replace(board(), clock="07:03:15"), PALETTE))
    assert clock != stamp


def test_route_page_in_the_plain_look():
    from twobells.board.render import render_route
    from twobells.board.route import RouteView

    names = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")

    def ink(view):
        image = render_route(view, PALETTE, HDMI_1280X400)
        assert image.size == HDMI_1280X400.size
        return sum(n for n, c in image.getcolors() if c == PALETTE.black)

    with_bus = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", names, 2.5, "3 stops away", "07:12:40")
    without = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", names, None, "3 stops away", "07:12:40")
    assert ink(with_bus) > ink(without) + 500  # the bus is a solid block


def test_themes_draw_the_route_page():
    from twobells.board.render import render_route
    from twobells.board.sign import render_route_sign
    from twobells.board.themes import THEMES

    assert THEMES["plain"].render_route is render_route
    assert THEMES["sign"].render_route is render_route_sign


def test_no_leave_text_for_a_train_that_is_not_drawn():
    # Only three trains fit; a leave time for the fourth would point at nothing.
    rows = [Row(f"07:{m}", "Manchester", "On time") for m in (12, 20, 31, 42)]
    view = board(rows)
    for_fourth = dataclasses.replace(view, trains=dataclasses.replace(view.trains, leave="Leave by 07:35 (07:42)", leave_for=3))
    for_third = dataclasses.replace(for_fourth, trains=dataclasses.replace(for_fourth.trains, leave_for=2))
    assert ink_in_right_half_of_train_label_row(render(for_fourth, PALETTE)) == 0
    assert ink_in_right_half_of_train_label_row(render(for_third, PALETTE)) > 30


def label_row_inks(image, y0=17, y1=28):
    """x positions of accent and of black ink along the train section's label row."""
    accent, black = [], []
    for x in range(WIDTH):
        column = {image.getpixel((x, y)) for y in range(y0, y1)}
        if PALETTE.accent in column:
            accent.append(x)
        if PALETTE.black in column:
            black.append(x)
    return accent, black


def test_not_updating_and_leave_text_share_the_label_row_without_touching():
    rows = [Row("07:12", "Manchester Piccadilly", "On time")]
    image = render(board(rows, stale_trains=True, train_leave="Leave by 07:05 (07:12)"), PALETTE)
    accent, black = label_row_inks(image)
    leave = [x for x in black if x > max(accent)]
    assert leave, "leave text drawn"
    assert min(leave) - max(accent) >= 6


def test_cancelled_row_keeps_its_status_whole_on_the_phat():
    fonts = load_fonts()
    row = Row("07:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED)
    layout = row_layout(row, fonts)
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < layout.right_x
    assert layout.right_x + fonts.bold.getlength("Cancelled") <= WIDTH


def test_plain_look_fits_the_waveshare_panel_height():
    # 122 design pixels at 4x is 488, taller than the 480 panel: the last bus row was cut off.
    trains = [Row(f"07:{m}", "Manchester Piccadilly", "On time") for m in (12, 42, 52)]
    rows = [Row("341", "A: Hyde", "07:20"), Row("61", "B: Buxton", "07:35 sch")]
    image = render(board(trains, rows, train_leave="Leave by 07:05 (07:12)"), PALETTE, WAVESHARE_10IN85G)
    bottom = [image.getpixel((x, y)) for x in range(WAVESHARE_10IN85G.width) for y in range(476, 480)]
    assert PALETTE.black not in bottom


ALL_PANELS = [PHAT, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440]


def ink_columns(image, top, bottom, background):
    """x positions with anything but `background` between rows top and bottom."""
    return {x for x in range(image.width) for y in range(top, bottom) if image.getpixel((x, y)) != background}


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("status", ["Cancelled", "On time", "Exp 07:49", "Delayed", "No report"])
def test_right_aligned_status_keeps_the_right_margin(panel, status):
    # Drawn on a paletted image, glyphs are hinted for one-bit output and can come
    # out a few pixels wider than getlength says: measure what is drawn.
    s = plain_scale(panel)
    image = render(board([Row("07:12", "Manchester Piccadilly", status, Style.ALERT)]), PALETTE, panel)
    top = (15 + 6 + 12) * s  # header, gap, section label
    ink = ink_columns(image, top, top + 14 * s, PALETTE.white)
    assert ink and max(ink) < panel.width - 3 * s


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_reversed_out_cancelled_keeps_the_right_margin(panel):
    s = plain_scale(panel)
    image = render(board([Row("07:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED)]), PALETTE, panel)
    top = (15 + 6 + 12) * s
    ink = ink_columns(image, top + s, top + 13 * s, PALETTE.black)
    assert ink and max(ink) < panel.width - 3 * s


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_header_stamp_keeps_the_right_margin(panel):
    s = plain_scale(panel)
    for view in (board(), dataclasses.replace(board(), clock="07:03:15")):
        ink = ink_columns(render(view, PALETTE, panel), 0, 15 * s, PALETTE.black)
        assert max(ink) < panel.width - 3 * s


TRAINS_WITH_PLATFORMS = [
    Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 1"),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 2"),
]


def test_the_phat_has_no_platform_column_and_is_unchanged():
    assert platform_column(TRAINS_WITH_PLATFORMS, load_fonts(), WIDTH, 1) is None
    plain = [dataclasses.replace(r, platform=None) for r in TRAINS_WITH_PLATFORMS]
    assert render(board(TRAINS_WITH_PLATFORMS), PALETTE).tobytes() == render(board(plain), PALETTE).tobytes()


REPLACEMENT_BUS = Row("07:12", "Manchester Piccadilly", "On time", platform="Bus", replacement_bus=True)


def test_without_a_platform_column_a_replacement_bus_says_so_before_its_destination():
    fonts = load_fonts()
    assert platform_column([REPLACEMENT_BUS], fonts, WIDTH, 1) is None
    layout = row_layout(REPLACEMENT_BUS, fonts, WIDTH, 1)
    assert layout.middle.startswith("Bus: Manc")
    assert middle_text(REPLACEMENT_BUS, layout) == "Bus: Manchester Piccadilly"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_with_a_platform_column_a_replacement_bus_says_bus_there(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    platform_x = platform_column([REPLACEMENT_BUS], fonts, panel.width, s)
    assert platform_x is not None
    layout = row_layout(REPLACEMENT_BUS, fonts, panel.width, s, platform_x)
    assert layout.platform_x == platform_x
    assert middle_text(REPLACEMENT_BUS, layout) == "Manchester Piccadilly"


FERRY = Row("07:12", "Ryde Pier Head", "On time", platform="Ferry", ferry=True)


def test_without_a_platform_column_a_ferry_says_so_before_its_destination():
    fonts = load_fonts()
    assert platform_column([FERRY], fonts, WIDTH, 1) is None
    layout = row_layout(FERRY, fonts, WIDTH, 1)
    assert layout.middle.startswith("Ferry: Ryde")
    assert middle_text(FERRY, layout) == "Ferry: Ryde Pier Head"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_with_a_platform_column_a_ferry_says_ferry_there(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    platform_x = platform_column([FERRY], fonts, panel.width, s)
    assert platform_x is not None
    layout = row_layout(FERRY, fonts, panel.width, s, platform_x)
    assert layout.platform_x == platform_x
    assert middle_text(FERRY, layout) == "Ryde Pier Head"


VIA = Row("07:12", "Hadfield", "On time", platform="Plat 1", via="via Dinting")


def test_the_phat_leaves_the_via_words_off():
    layout = row_layout(VIA, load_fonts(), WIDTH, 1)
    assert middle_text(VIA, layout) == "Hadfield"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_a_wide_board_says_the_via_words_after_the_destination(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    layout = row_layout(VIA, fonts, panel.width, s, platform_column([VIA], fonts, panel.width, s))
    assert middle_text(VIA, layout) == "Hadfield via Dinting"
    assert layout.middle == "Hadfield via Dinting"  # it fits whole


CHANGED = Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 2 (changed)", platform_changed=True)


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_a_changed_platform_is_drawn_in_the_accent(panel):
    unchanged = dataclasses.replace(CHANGED, platform_changed=False)
    assert PALETTE.accent in colours(render(board([CHANGED]), PALETTE, panel))
    assert PALETTE.accent not in colours(render(board([unchanged]), PALETTE, panel))
    # On a cancelled train's black bar it stays white, as the rest of the row.
    cancelled = dataclasses.replace(CHANGED, right="Cancelled", style=Style.INVERTED)
    assert PALETTE.accent not in colours(render(board([cancelled]), PALETTE, panel))


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_wide_panels_put_the_platform_between_destination_and_status(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    platform_x = platform_column(TRAINS_WITH_PLATFORMS, fonts, panel.width, s)
    assert platform_x is not None
    for row in TRAINS_WITH_PLATFORMS:
        layout = row_layout(row, fonts, panel.width, s, platform_x)
        assert layout.middle_x + fonts.regular.getlength(layout.middle) < platform_x
        if row.platform:
            assert layout.platform_x == platform_x
            assert platform_x + drawn_length(fonts.regular, row.platform) < layout.right_x
        else:
            assert layout.platform_x is None  # left blank
    plain = [dataclasses.replace(r, platform=None) for r in TRAINS_WITH_PLATFORMS]
    assert platform_column(plain, fonts, panel.width, s) is None

    def ink(image):
        return sum(n for n, c in image.getcolors() if c != PALETTE.white)

    # The first row only: the cancelled row's text is reversed out, so it lowers the count.
    assert ink(render(board(TRAINS_WITH_PLATFORMS[:1]), PALETTE, panel)) > ink(render(board(plain[:1]), PALETTE, panel))


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_destination_gives_way_to_the_platform_on_wide_panels(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley, Hyde Central, Woodley, Romiley, Bredbury and Guide Bridge"
    rows = [Row("07:12", long, "On time", platform="Plat 1")]
    platform_x = platform_column(rows, fonts, panel.width, s)
    layout = row_layout(rows[0], fonts, panel.width, s, platform_x)
    assert layout.middle.endswith("…")
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < platform_x


NOTICE = (
    "Disruption between Glossop and Manchester Piccadilly until the end of the day. "
    "Trains may be cancelled or delayed by up to 20 minutes."
)


def header(view, panel=PHAT):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    title, stamp, stamp_x = header_text(view, fonts, panel.width, s)
    return title, stamp, stamp_x, fonts, s


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_notice_takes_the_titles_place_on_epaper_cut_to_fit(panel):
    title, stamp, stamp_x, fonts, s = header(dataclasses.replace(board(), notice=NOTICE), panel)
    assert title.startswith("! Disruption") and title.endswith("…")
    assert stamp == "Upd 07:03"
    assert 3 * s + drawn_length(fonts.bold, title) <= stamp_x - 6 * s


def test_without_a_notice_the_header_shows_the_title():
    assert header(board())[0] == "GLO > MAN"


def test_short_notice_is_shown_whole_in_the_header():
    assert header(dataclasses.replace(board(), notice="Lifts out of use"))[0] == "! Lifts out of use"


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_on_a_screen_a_long_notice_scrolls_whole_in_the_header(panel):
    from twobells.board.render import board_frame

    view = dataclasses.replace(board(), notice=NOTICE * 2, clock="07:03:20")
    _, stamp, stamp_x, fonts, s = header(view, panel)
    [scroll] = board_frame(view, PALETTE, panel).scrolls
    assert scroll.text == "! " + NOTICE * 2
    left, top, right, bottom = scroll.box
    assert (left, top) == (3 * s, 0) and bottom == heights()[0] * s  # in the header bar
    assert right <= stamp_x - 6 * s  # clear of the clock
    assert scroll.strip.width > right - left


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_on_e_paper_nothing_scrolls(panel):
    from twobells.board.render import board_frame

    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley and Hyde Central"
    view = dataclasses.replace(board([Row("07:12", long, "On time")]), notice=NOTICE * 2)
    frame = board_frame(view, PALETTE, panel)
    assert frame.scrolls == ()
    assert frame.image.tobytes() == render(view, PALETTE, panel).tobytes()


def test_notice_changes_what_the_header_draws():
    plain = render(board(), PALETTE)
    noticed = render(dataclasses.replace(board(), notice="Lifts out of use"), PALETTE)
    assert plain.crop((0, 0, WIDTH, 15)).tobytes() != noticed.crop((0, 0, WIDTH, 15)).tobytes()
    assert plain.crop((0, 15, WIDTH, HEIGHT)).tobytes() == noticed.crop((0, 15, WIDTH, HEIGHT)).tobytes()


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_rain_line_takes_the_titles_place(panel):
    view = dataclasses.replace(board(), rain="Rain from 07:10 (70%)")
    assert header(view, panel)[0] == "Rain from 07:10 (70%)"


def test_a_disruption_notice_beats_the_rain_line_in_the_header():
    view = dataclasses.replace(board(), rain="Rain from 07:10 (70%)", notice="Lifts out of use")
    assert header(view)[0] == "! Lifts out of use"


def _fit_text_one_at_a_time(text, font, width, measure=None):
    """fit_text as it was, taking off a character at a time: what it must still give."""
    measure = measure or font.getlength
    if measure(text) <= width:
        return text
    while text and measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


FIT_SAMPLES = (
    "",
    "Hyde",
    "Manchester Piccadilly via Hadfield and Dinting",
    "! Disruption between Glossop and Manchester Piccadilly until the end of the day. "
    "Trains may be cancelled or delayed by up to 20 minutes.",
    "Stepping Hill Hospital via Marple and Romiley    ",
    "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW iiiiiiiiiiiiiiiiiiiiii",
    "Llanfairpwllgwyngyllgogerychwyrndrobwllllantysiliogogogoch",
)


def test_fit_text_gives_what_it_gave_a_character_at_a_time():
    from twobells.board.dotfont import TINY, DotFont

    fonts = load_fonts()
    measures = [
        (fonts.regular, fonts.regular.getlength),
        (fonts.regular, lambda text: drawn_length(fonts.regular, text)),
        (fonts.bold, lambda text: drawn_length(fonts.bold, text)),
        (DotFont(), DotFont().getlength),
        (DotFont(TINY), DotFont(TINY).getlength),
    ]
    for font, uncached in measures:
        measure = functools.lru_cache(maxsize=None)(uncached)  # the same prefixes come up at every width
        for text in FIT_SAMPLES:
            for width in (0, 1, 5, 9, 17, 40, 80, 133, 250, 400, 1300):
                assert fit_text(text, font, width, measure) == _fit_text_one_at_a_time(text, font, width, measure)


def test_fit_text_measures_a_long_notice_a_few_times_not_once_a_character():
    calls = []

    def measure(text):
        calls.append(text)
        return len(text)

    notice = "x" * 240
    assert fit_text(notice, None, 60, measure) == "x" * 59 + "…"
    assert len(calls) <= 12  # about log2(240) halvings, not 180 one-character steps


def test_plain_look_shows_fewer_rows_rather_than_cut_them_off_on_the_original_phat():
    from twobells.board.panel import PHAT_V1

    trains = [Row(f"07:{m}", "Manchester Piccadilly", "On time") for m in (12, 42, 52)]
    buses = [Row("341", "A: Hyde", "07:20"), Row("61", "B: Buxton", "07:35 sch")]
    image = render(board(trains, buses), PALETTE, PHAT_V1)
    assert image.size == PHAT_V1.size
    bottom = [image.getpixel((x, y)) for x in range(PHAT_V1.width) for y in range(PHAT_V1.height - 2, PHAT_V1.height)]
    assert PALETTE.black not in bottom  # nothing drawn into the last rows, so nothing cut off


def _full_board():
    trains = [Row(f"07:{m}", "Manchester Piccadilly", "On time") for m in (12, 42, 52)]
    buses = [Row("341", "A: Hyde", "07:20"), Row("61", "B: Buxton", "07:35 sch")]
    return board(trains, buses, train_leave="Leave by 07:05 (07:12)")


def test_default_text_size_draws_exactly_as_before():
    assert render(_full_board(), PALETTE).tobytes() == render(_full_board(), PALETTE, text_size=11).tobytes()


def test_slightly_bigger_text_still_fits_the_original_phat():
    from twobells.board.panel import PHAT_V1
    from twobells.board.render import row_limits

    assert row_limits(PHAT_V1, text_size=12) == {"Trains": 2, "Buses": 1}
    image = render(_full_board(), PALETTE, PHAT_V1, text_size=12)
    bottom = [image.getpixel((x, y)) for x in range(PHAT_V1.width) for y in range(PHAT_V1.height - 2, PHAT_V1.height)]
    assert PALETTE.black not in bottom


def test_bigger_text_draws_more_ink():
    from twobells.board.panel import PHAT_V1

    def ink(size):
        image = render(_full_board(), PALETTE, PHAT_V1, text_size=size)
        return sum(n for n, c in image.getcolors() if c == PALETTE.black)

    assert ink(12) > ink(11)


@pytest.mark.parametrize("text_size", [11, 12, 13])
def test_a_shortened_destination_leaves_a_gap_before_the_status(text_size):
    from twobells.board.panel import PHAT_V1
    from twobells.board.render import drawn_length

    fonts = load_fonts(1, text_size)
    layout = row_layout(Row("20:22", "Manchester Piccadilly", "On time"), fonts, PHAT_V1.width, 1)
    # Whole or cut short, the destination ends clear of the status.
    assert layout.middle_x + drawn_length(fonts.regular, layout.middle) <= layout.right_x - 3


# --- The settings reset code page ---

CODE_PAGE = CodeView("123 456", "23:52", 1.0)


def ink_box(image, background):
    """The bounding box of everything not in the background colour."""
    return image.point(lambda v: 0 if v == background else 255, mode="L").getbbox()


@pytest.mark.parametrize("panel", [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440])
def test_the_code_page_fits_every_panel(panel):
    # Dark only changes the colours, not where anything goes.
    image = render_code(CODE_PAGE, PALETTE, panel)
    assert image.size == panel.size and image.mode == "P"
    left, top, right, bottom = ink_box(image, PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height  # nothing cut off


@pytest.mark.parametrize("panel", [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440])
def test_the_code_is_drawn_big(panel):
    # The middle band, between the two lines of small print, is mostly code.
    image = render_code(CODE_PAGE, PALETTE, panel)
    band = image.crop((0, panel.height // 3, panel.width, 2 * panel.height // 3))
    left, _, right, _ = ink_box(band, PALETTE.white)
    assert right - left > panel.width * 0.6


def test_different_codes_draw_differently_and_the_same_code_alike():
    page = render_code(CODE_PAGE, PALETTE, PHAT).tobytes()
    assert page == render_code(CODE_PAGE, PALETTE, PHAT).tobytes()
    assert page != render_code(dataclasses.replace(CODE_PAGE, code="654 321"), PALETTE, PHAT).tobytes()
    assert page != render_code(dataclasses.replace(CODE_PAGE, until="23:53"), PALETTE, PHAT).tobytes()


def test_the_code_page_is_ink_on_paper_or_light_on_black():
    assert colours(render_code(CODE_PAGE, PALETTE, PHAT)) == {PALETTE.white, PALETTE.black}
    assert colours(render_code(CODE_PAGE, PALETTE, PHAT, dark=True)) == {PALETTE.black, PALETTE.accent}
    mono = Palette(white=0, black=1, accent=1)
    assert colours(render_code(CODE_PAGE, mono, PHAT, dark=True)) == {0, 1}


def test_the_code_page_signature_never_carries_the_code():
    import hashlib

    signature = CODE_PAGE.signature()
    assert signature != hashlib.sha256(b"123 456").hexdigest()
    assert signature == dataclasses.replace(CODE_PAGE, code="654 321").signature()
    assert signature != dataclasses.replace(CODE_PAGE, expires=61.0).signature()


# --- The set-me-up page, until the board is set up ---

SETUP_PAGE = SetupView("http://raspberrypi.local:8090/settings")  # as setup_url gives it
PANELS = [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440]


@pytest.mark.parametrize("panel", PANELS)
def test_the_setup_page_fits_every_panel(panel):
    # Dark only changes the colours, not where anything goes.
    image = render_setup(SETUP_PAGE, PALETTE, panel)
    assert image.size == panel.size and image.mode == "P"
    left, top, right, bottom = ink_box(image, PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height  # nothing cut off


@pytest.mark.parametrize("panel", PANELS)
def test_the_address_is_drawn_across_the_panel(panel):
    # The bottom band is the address, nearly as wide as the panel.
    image = render_setup(SETUP_PAGE, PALETTE, panel)
    band = image.crop((0, panel.height * 3 // 4, panel.width, panel.height))
    left, _, right, _ = ink_box(band, PALETTE.white)
    assert right - left > panel.width * 0.8


def test_an_address_too_long_to_read_on_one_line_breaks_before_its_port():
    from twobells.board.render import MIN_ADDRESS_PX, _biggest_bold, address_lines

    long_url = "http://my-very-long-departure-board-name.local:8090/settings"
    one_line = _biggest_bold(long_url, 200, 40)
    assert one_line.size < MIN_ADDRESS_PX  # too small to read on one line
    lines, font = address_lines(long_url, 200, 40)
    assert lines == ["http://my-very-long-departure-board-name.local", ":8090/settings"]
    assert font.size > one_line.size  # bigger, broken in two
    for line in lines:
        left, _, right, _ = font.getbbox(line, mode="1")
        assert right - left <= 200
    # A usual name stays on one line.
    lines, _ = address_lines("http://pi.local:8090/settings", 200, 40)
    assert lines == ["http://pi.local:8090/settings"]


@pytest.mark.parametrize("panel", PANELS)
@pytest.mark.parametrize("off", [False, True])
def test_the_settings_address_the_board_shows_fits_with_a_long_name(panel, off):
    from twobells.cli.common import setup_url
    from twobells.cli.run import OFF_HEADING, OFF_NOTE

    url = setup_url("my-very-long-departure-board-name")
    assert url.endswith(":8090/settings")
    page = SetupView(url, heading=OFF_HEADING, note=OFF_NOTE) if off else SetupView(url)
    left, top, right, bottom = ink_box(render_setup(page, PALETTE, panel), PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height


def test_the_setup_page_shows_its_address_and_is_ink_on_paper_or_light_on_black():
    page = render_setup(SETUP_PAGE, PALETTE, PHAT)
    assert page.tobytes() != render_setup(SetupView("http://pi.local:8090/settings"), PALETTE, PHAT).tobytes()
    assert colours(page) == {PALETTE.white, PALETTE.black}
    assert colours(render_setup(SETUP_PAGE, PALETTE, PHAT, dark=True)) == {PALETTE.black, PALETTE.accent}
    mono = Palette(white=0, black=1, accent=1)
    assert colours(render_setup(SETUP_PAGE, mono, PHAT, dark=True)) == {0, 1}


def test_the_board_shows_the_address_without_http():
    # Browsers take name.local:8090/settings as typed; leaving out http://
    # keeps a long Pi name readable on the small panels.
    view = SetupView("http://kitchen-board.local:8090/settings")
    assert view.shown == "kitchen-board.local:8090/settings"
    bare = SetupView("kitchen-board.local:8090/settings")
    assert render_setup(view, PALETTE, PHAT).tobytes() == render_setup(bare, PALETTE, PHAT).tobytes()


def test_the_setup_page_signature_follows_its_address():
    assert SETUP_PAGE.signature() == SetupView(SETUP_PAGE.url).signature()
    assert SETUP_PAGE.signature() != SetupView("http://pi.local:8090/settings").signature()


OFF_PAGE = SetupView(SETUP_PAGE.url, heading="Board off", note="Turn it on in Settings:")


@pytest.mark.parametrize("panel", PANELS)
def test_the_setup_page_can_say_something_else(panel):
    # The board view switched off: the same page, with its own heading and note.
    image = render_setup(OFF_PAGE, PALETTE, panel)
    assert image.tobytes() != render_setup(SETUP_PAGE, PALETTE, panel).tobytes()
    left, top, right, bottom = ink_box(image, PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height
    # The address is drawn as before, across the bottom.
    band = image.crop((0, panel.height * 3 // 4, panel.width, panel.height))
    assert band.tobytes() == render_setup(SETUP_PAGE, PALETTE, panel).crop(
        (0, panel.height * 3 // 4, panel.width, panel.height)).tobytes()


def test_the_setup_page_signature_follows_its_words():
    assert OFF_PAGE.signature() != SETUP_PAGE.signature()
    assert SetupView(SETUP_PAGE.url, heading="Board off").signature() != SETUP_PAGE.signature()


# Where each train or bus is, on a screen wide enough; and the bar screen's third bus.

from twobells.board.panel import STRIP_COLUMN_MIN_WIDTH  # noqa: E402
from twobells.board.render import STRIP_WIDTH, row_limits, where_column, where_width  # noqa: E402
from twobells.core.model import Strip  # noqa: E402

CLOCK = "07:03:15"
STRIP_PANELS = [HDMI_1280X400, HDMI_1920X440]
STRIP_TRAINS = (
    Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 1", strip=Strip("At Dinting", 4, 2, 1.0)),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT, platform="Plat 1",
        strip=Strip("Not yet left Hadfield sidings", 4, 2, None)),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 2"),
)
STRIP_BUSES = (
    Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave in 12 min", "A", strip=Strip("3 stops away", 7, 5, 2.4)),
    Row("61", "Buxton", "07:35 sch", leave="Leave in 23 min", stop="B"),
    Row("237", "Ashton", "07:41", leave="Leave in 29 min", stop="A", strip=Strip("1 stop away", 7, 5, 4.0)),
)


def on_screen(trains=STRIP_TRAINS, buses=STRIP_BUSES, clock=CLOCK):
    return dataclasses.replace(board(trains, buses, train_leave="Leave in 3 min (07:12)"), clock=clock)


def strip_layouts(panel, trains=STRIP_TRAINS, buses=STRIP_BUSES):
    """Each row's layout, as the painter places it, and the where column."""
    s = plain_scale(panel)
    fonts = load_fonts(s)
    where_x = where_column([trains, buses], fonts, panel.width, s, live=True)
    layouts = []
    for rows in (trains, buses):
        platform_x = platform_column(rows, fonts, panel.width, s)
        leave_x = leave_column(rows, fonts, panel.width, s)
        layouts += [(row, row_layout(row, fonts, panel.width, s, platform_x, leave_x, where_x)) for row in rows]
    return layouts, where_x, fonts, s


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_wide_screens_say_where_each_train_and_bus_is_in_a_column(panel):
    layouts, where_x, fonts, s = strip_layouts(panel)
    assert where_x is not None
    shown = {row.left: layout.where for row, layout in layouts}
    assert shown["07:12"] == "At Dinting" and shown["341"] == "3 stops away" and shown["237"] == "1 stop away"
    assert shown["07:42"].startswith("Not yet left") and shown["07:42"].endswith("…")
    for row, layout in layouts:
        if row.strip:
            assert layout.where_x == where_x
            assert drawn_length(fonts.small, layout.where) <= where_width(fonts, panel.width, s)
            assert layout.strip_x == where_x + where_width(fonts, panel.width, s) + 6 * s
        else:
            # Left blank, but the destination still stops where the others' do.
            assert layout.where is None and layout.strip_x is None


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_a_rows_columns_never_overlap_in_the_plain_look(panel):
    layouts, where_x, fonts, s = strip_layouts(panel)
    for _, layout in layouts:
        assert layout.middle_x + drawn_length(fonts.regular, layout.middle) + 6 * s <= where_x
        strip_end = where_x + where_width(fonts, panel.width, s) + (6 + STRIP_WIDTH) * s
        for later in (layout.platform_x, layout.leave_x, layout.right_x):
            if later is not None:
                assert strip_end + 6 * s <= later


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_destinations_keep_their_room_beside_the_strip(panel):
    layouts, _, _, _ = strip_layouts(panel)
    for row, layout in layouts:
        assert layout.middle == (f"{row.stop}: {row.middle}" if row.stop else row.middle)


def test_the_plain_look_shows_strips_on_both_screens_only():
    fonts = load_fonts(3)
    for panel in STRIP_PANELS:
        assert panel.width / plain_scale(panel) >= STRIP_COLUMN_MIN_WIDTH
    # The 10.85" e-paper is wide enough in the plain look, but e-paper is never live.
    assert where_column([STRIP_TRAINS], fonts, WAVESHARE_10IN85G.width, 3, live=False) is None
    assert where_column([STRIP_TRAINS], load_fonts(), WIDTH, 1, live=True) is None
    no_strips = tuple(dataclasses.replace(r, strip=None) for r in STRIP_TRAINS)
    assert where_column([no_strips], fonts, HDMI_1920X440.width, 3, live=True) is None


@pytest.mark.parametrize("panel", [PHAT, WAVESHARE_10IN85G], ids=lambda p: p.name)
def test_e_paper_in_the_plain_look_is_unchanged_by_strips(panel):
    plain = (tuple(dataclasses.replace(r, strip=None) for r in STRIP_TRAINS),
             tuple(dataclasses.replace(r, strip=None) for r in STRIP_BUSES))
    with_strips = render(on_screen(clock=None), PALETTE, panel)
    assert with_strips.tobytes() == render(on_screen(*plain, clock=None), PALETTE, panel).tobytes()


def ink_in(image, box, colour=PALETTE.black):
    x0, y0, x1, y1 = (int(v) for v in box)
    return sum(1 for x in range(x0, x1) for y in range(y0, y1) if image.getpixel((x, y)) == colour)


def bus_rows_top(panel):
    """The first bus row's top and the row height, as render places them."""
    s = plain_scale(panel)
    header_h, label_h, row_h = heights()
    trains = (header_h + 6 + label_h) * s
    return trains + 3 * row_h * s + (6 + 1 + label_h) * s, row_h * s


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_little_line_is_drawn_with_the_vehicle_where_it_is(panel):
    _, where_x, fonts, s = strip_layouts(panel)
    top, row_h = bus_rows_top(panel)
    strip_x = where_x + where_width(fonts, panel.width, s) + 6 * s
    right = strip_x + STRIP_WIDTH * s
    drawn = render(on_screen(), PALETTE, panel)
    assert ink_in(drawn, (strip_x, top, right, top + row_h)) > 0
    above_the_line = (strip_x, top, right, top + 6 * s)
    assert ink_in(drawn, above_the_line) > 0
    unknown = (dataclasses.replace(STRIP_BUSES[0], strip=Strip("3 stops away", 7, 5, None)), *STRIP_BUSES[1:])
    assert ink_in(render(on_screen(buses=unknown), PALETTE, panel), above_the_line) == 0
    # The 61 has no strip: its space is blank.
    assert ink_in(drawn, (strip_x, top + row_h, right, top + 2 * row_h)) == 0


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_your_stop_is_in_the_accent_colour(panel):
    _, where_x, fonts, s = strip_layouts(panel)
    top, row_h = bus_rows_top(panel)
    strip_x = where_x + where_width(fonts, panel.width, s) + 6 * s
    drawn = render(on_screen(), PALETTE, panel)
    assert ink_in(drawn, (strip_x, top, strip_x + STRIP_WIDTH * s, top + row_h), PALETTE.accent) > 0


def test_the_bar_screen_shows_three_buses_in_the_plain_look():
    assert row_limits(HDMI_1920X440) == {"Trains": 3, "Buses": 3}
    # E-paper and the 7.9" screen keep what they had: there's no room for more there.
    for panel in (PHAT, WAVESHARE_10IN85G, HDMI_1280X400):
        assert row_limits(panel)["Buses"] == 2, panel.name
    image = render(on_screen(), PALETTE, HDMI_1920X440)
    top, row_h = bus_rows_top(HDMI_1920X440)
    assert top + 3 * row_h <= HDMI_1920X440.height
    assert ink_in(image, (0, top + 2 * row_h, 200, top + 3 * row_h)) > 0  # the 237



def test_eighteen_letters_of_where_fit_uncut_on_the_bar_screen_in_the_plain_look():
    eighteen = "Left Glossop 07:08"
    trains = (dataclasses.replace(STRIP_TRAINS[0], strip=Strip(eighteen, 4, 2, 1.0)), *STRIP_TRAINS[1:])
    buses = (dataclasses.replace(STRIP_BUSES[0], strip=Strip(eighteen, 7, 5, 2.4)), *STRIP_BUSES[1:])
    layouts, _, _, _ = strip_layouts(HDMI_1920X440, trains, buses)
    assert [layout.where for _, layout in layouts].count(eighteen) == 2
    fonts = load_fonts(3)
    assert where_width(fonts, HDMI_1920X440.width, 3) < drawn_length(fonts.small, "Leaving Hadfield now")


def test_the_plain_where_column_is_as_wide_whatever_the_words_and_wider_on_the_bar():
    fonts = load_fonts(3)
    narrow, wide = where_width(fonts, HDMI_1280X400.width, 3), where_width(fonts, HDMI_1920X440.width, 3)
    assert drawn_length(fonts.small, "3 stops away") <= narrow < wide
    short = tuple(dataclasses.replace(r, strip=Strip("At Dinting", 4, 2, 1.0)) for r in STRIP_TRAINS)
    for panel in STRIP_PANELS:
        a = strip_layouts(panel)[0][0][1]
        b = strip_layouts(panel, short)[0][0][1]
        assert a.strip_x - a.where_x == b.strip_x - b.where_x



# The header on a screen: the notice where the title goes, the rain warning beside it.

from twobells.board.render import header_rain  # noqa: E402

RAIN = "Rain from 07:10 (70%)"


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_a_screen_header_shows_the_notice_and_the_rain_warning_before_the_clock(panel):
    view = dataclasses.replace(board(), notice=NOTICE, rain=RAIN, clock=CLOCK)
    title, stamp, stamp_x, fonts, s = header(view, panel)
    rain, rain_x = header_rain(view, fonts, panel.width, s)
    assert rain == RAIN and stamp == CLOCK
    assert title.startswith("! ")
    assert 3 * s + drawn_length(fonts.bold, title) + 12 * s <= rain_x
    assert rain_x + drawn_length(fonts.bold, rain) + 6 * s <= stamp_x


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_header_rain_warning_stays_put_as_the_notice_turns(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    xs = {header_rain(dataclasses.replace(board(), notice=NOTICE * 2, rain=RAIN, clock=f"07:03:{sec:02d}"),
                      fonts, panel.width, s)[1] for sec in range(0, 60, 5)}
    assert len(xs) == 1


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
def test_e_paper_and_a_lone_warning_keep_the_header_as_it_was(panel):
    s = plain_scale(panel)
    fonts = load_fonts(s)
    e_paper = dataclasses.replace(board(), notice=NOTICE, rain=RAIN)
    assert header_rain(e_paper, fonts, panel.width, s) is None
    assert header(e_paper, panel)[0].startswith("! Disruption")
    rain_only = dataclasses.replace(board(), rain=RAIN, clock=CLOCK)
    assert header_rain(rain_only, fonts, panel.width, s) is None
    assert header(rain_only, panel)[0] == RAIN


def test_the_header_draws_the_rain_warning_beside_the_notice():
    panel = HDMI_1920X440
    notice_only = dataclasses.replace(board(), notice="Lifts out of use", clock=CLOCK)
    with_rain = render(dataclasses.replace(notice_only, rain=RAIN), PALETTE, panel)
    header_h = heights()[0] * plain_scale(panel)
    plain = render(notice_only, PALETTE, panel)
    assert with_rain.crop((0, 0, panel.width, header_h)).tobytes() != plain.crop((0, 0, panel.width, header_h)).tobytes()


# On a screen, text too long for its space scrolls, a pHAT pixel at a time, clipped to its column and row.

from twobells.board.render import board_frame  # noqa: E402
from twobells.board.scroll import SCROLL_PAUSE_S  # noqa: E402

LONG = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Hattersley, Godley and Hyde Central"


def long_on_screen():
    trains = tuple(dataclasses.replace(r, middle=LONG) for r in STRIP_TRAINS)
    return dataclasses.replace(on_screen(trains), notice=NOTICE)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_on_a_screen_long_text_scrolls_in_its_column_and_row(panel):
    layouts, where_x, fonts, s = strip_layouts(panel, tuple(dataclasses.replace(r, middle=LONG) for r in STRIP_TRAINS))
    frame = board_frame(long_on_screen(), PALETTE, panel)
    texts = [scroll.text for scroll in frame.scrolls]
    assert texts.count(LONG) == 3 and "Not yet left Hadfield sidings" in texts and "! " + NOTICE in texts
    _, label_h, row_h = heights()
    for scroll in frame.scrolls:
        assert scroll.step == s  # a pHAT pixel at a time
        if scroll.text == LONG:
            row, layout = next((r, lay) for r, lay in layouts if lay.middle_room == scroll.width and r.middle == LONG)
            left, top, right, bottom = scroll.box
            assert left == int(layout.middle_x) and right == left + int(layout.middle_room)
            assert bottom - top == row_h * s  # its row, no more
            assert right <= where_x  # clear of the where words
        if scroll.text.startswith("Not yet"):
            assert scroll.box[0] == int(where_x) and scroll.width == int(where_width(fonts, panel.width, s))


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_on_a_screen_text_that_fits_does_not_scroll(panel):
    scrolling = {s.text for s in board_frame(on_screen(), PALETTE, panel).scrolls}
    assert not scrolling & {"At Dinting", "3 stops away", "A: Hyde", "GLO > MAN"}


def test_on_a_screen_scrolling_text_moves_only_inside_its_box():
    panel = HDMI_1920X440
    frame = board_frame(long_on_screen(), PALETTE, panel)
    still, later = frame.at(0), frame.at(SCROLL_PAUSE_S + 1)
    assert frame.image.tobytes() == still.tobytes()  # the still board shows each from its start
    changed = [(x, y) for y in range(panel.height) for x in range(panel.width)
               if still.getpixel((x, y)) != later.getpixel((x, y))]
    assert changed
    boxes = [s.box for s in frame.scrolls]
    assert all(any(left <= x < right and top <= y < bottom for left, top, right, bottom in boxes)
               for x, y in changed)


def test_on_a_screen_a_reversed_out_row_scrolls_on_its_own_paper():
    frame = board_frame(long_on_screen(), PALETTE, HDMI_1920X440)
    normal, _, cancelled = (s for s in frame.scrolls if s.text == LONG)
    assert normal.strip.getpixel((0, 0)) == PALETTE.white
    assert cancelled.strip.getpixel((0, 0)) == PALETTE.black


# A screen wide enough names each bus row's stop where e-paper shows "A: Hyde".

from twobells.board.panel import STOP_NAME_MIN_WIDTH  # noqa: E402
from twobells.board.render import stop_column  # noqa: E402

GLOSSOP_STOPS = ("Henry Street A", "Henry Street B")  # both of Glossop's stops are on Henry Street
NAMED_BUSES = (
    dataclasses.replace(STRIP_BUSES[0], stop_name="Henry Street A"),
    dataclasses.replace(STRIP_BUSES[1], stop_name="Henry Street B"),
    dataclasses.replace(STRIP_BUSES[2], stop_name="Henry Street A"),
)


def named_on_screen(buses=NAMED_BUSES, stop_names=GLOSSOP_STOPS, clock=CLOCK):
    view = on_screen(buses=buses, clock=clock)
    return dataclasses.replace(view, buses=dataclasses.replace(view.buses, stop_names=stop_names))


def named_layouts(panel, buses=NAMED_BUSES, stop_names=GLOSSOP_STOPS):
    """Each bus row's layout, as the painter places it, and the stop column's width."""
    s = plain_scale(panel)
    fonts = load_fonts(s)
    where_x = where_column([STRIP_TRAINS, buses], fonts, panel.width, s, live=True)
    leave_x = leave_column(buses, fonts, panel.width, s)
    room = stop_column(buses, stop_names, fonts, panel.width, s, live=True)
    layouts = [(row, row_layout(row, fonts, panel.width, s, None, leave_x, where_x, room)) for row in buses]
    return layouts, room, where_x, fonts, s


def test_only_the_bar_screen_is_wide_enough_for_stop_names_in_the_plain_look():
    bar, small = HDMI_1920X440, HDMI_1280X400
    assert bar.width / plain_scale(bar) >= STOP_NAME_MIN_WIDTH > small.width / plain_scale(small)
    fonts = load_fonts(3)
    assert stop_column(NAMED_BUSES, GLOSSOP_STOPS, fonts, small.width, 3, live=True) is None
    assert stop_column(NAMED_BUSES, GLOSSOP_STOPS, fonts, bar.width, 3, live=False) is None  # e-paper never
    assert stop_column(STRIP_BUSES, (), fonts, bar.width, 3, live=True) is None  # no names known


def test_the_bar_screen_names_each_bus_row_s_stop_in_a_column_before_the_destination():
    layouts, room, where_x, fonts, s = named_layouts(HDMI_1920X440)
    assert room == drawn_length(fonts.regular, "Henry Street A")
    assert [(lay.stop, lay.middle) for _, lay in layouts] == [
        ("Henry Street A", "Hyde"), ("Henry Street B", "Buxton"), ("Henry Street A", "Ashton"),
    ]
    assert len({lay.stop_x for _, lay in layouts}) == 1 and len({lay.middle_x for _, lay in layouts}) == 1
    first = layouts[0][1]
    assert first.stop_x == 3 * s + left_column_width(fonts, s)  # where the destination starts without names
    # Twice the usual gap after the name, so it and the destination don't read as one.
    assert first.middle_x == first.stop_x + room + 12 * s
    assert first.middle_x + first.middle_room + 12 * s <= where_x


def test_the_plain_stop_column_is_as_wide_whatever_buses_are_shown():
    fonts = load_fonts(3)
    names = ("Henry Street", "Norfolk Square")
    only_a = tuple(dataclasses.replace(r, stop_name="Henry Street") for r in STRIP_BUSES)
    room = stop_column(only_a, names, fonts, HDMI_1920X440.width, 3, live=True)
    assert room == drawn_length(fonts.regular, "Norfolk Square")


def test_a_long_stop_name_scrolls_in_a_plain_column_about_sixteen_letters_wide():
    long = "Glossop Norfolk Street Bus Station Stand 3"
    buses = (dataclasses.replace(NAMED_BUSES[0], stop_name=long), *NAMED_BUSES[1:])
    layouts, room, _, fonts, s = named_layouts(HDMI_1920X440, buses, (long, "Henry Street B"))
    assert drawn_length(fonts.regular, "Henry Street B") < room <= drawn_length(fonts.regular, "Norfolk Square W")
    assert layouts[0][1].stop.endswith("…") and layouts[1][1].stop == "Henry Street B"
    frame = board_frame(named_on_screen(buses, (long, "Henry Street B")), PALETTE, HDMI_1920X440)
    [scroll] = [sc for sc in frame.scrolls if sc.text == long]
    assert scroll.box[0] == int(layouts[0][1].stop_x) and scroll.width == int(room)


def test_a_stop_whose_name_isn_t_known_shows_its_label_in_the_plain_column():
    buses = (NAMED_BUSES[0], dataclasses.replace(NAMED_BUSES[1], stop_name=None), NAMED_BUSES[2])
    layouts, _, _, _, _ = named_layouts(HDMI_1920X440, buses, ("Henry Street A",))
    assert [(lay.stop, lay.middle) for _, lay in layouts][1] == ("B", "Buxton")
    assert len({lay.middle_x for _, lay in layouts}) == 1


def test_the_plain_bar_screen_draws_the_names():
    labels = render(on_screen(), PALETTE, HDMI_1920X440)
    assert render(named_on_screen(), PALETTE, HDMI_1920X440).tobytes() != labels.tobytes()
    frame = board_frame(named_on_screen(), PALETTE, HDMI_1920X440)
    assert not {sc.text for sc in frame.scrolls} & {"Henry Street A", "Henry Street B", "Hyde"}


def test_stop_names_leave_the_plain_train_rows_whole():
    layouts, where_x, fonts, s = strip_layouts(HDMI_1920X440, buses=NAMED_BUSES)
    trains = [lay for row, lay in layouts if row in STRIP_TRAINS]
    assert [lay.middle for lay in trains] == ["Manchester Piccadilly"] * 3


@pytest.mark.parametrize("panel", [PHAT, WAVESHARE_10IN85G, HDMI_1280X400], ids=lambda p: p.name)
def test_e_paper_and_the_7_9_inch_screen_keep_the_label_in_the_plain_look(panel):
    clock = CLOCK if panel is HDMI_1280X400 else None
    labels = render(on_screen(clock=clock), PALETTE, panel)
    assert render(named_on_screen(clock=clock), PALETTE, panel).tobytes() == labels.tobytes()


# --- Text kept between redraws ---


@pytest.mark.parametrize("xy", [(3, 4), (12.3, 5.75), (0.984375, 0.5), (-7.6, -3.2), (240.4, 110.1), (-40, 2)])
def test_kept_glyphs_draw_as_pillow_draws_them(xy):
    from PIL import Image, ImageDraw

    from twobells.board.render import draw_text

    for font in load_fonts(1).regular, load_fonts(4).bold:
        for text in ("07:12", "Manchester Piccadilly", "Jy ! …", " leading space"):
            expected = Image.new("P", (250, 122), 0)
            ImageDraw.Draw(expected).text(xy, text, font=font, fill=2)
            for _ in range(2):  # drawn, then from what was kept
                image = Image.new("P", (250, 122), 0)
                draw_text(image, xy, text, font, 2)
                assert image.tobytes() == expected.tobytes(), (xy, text)


def test_kept_values_stay_within_their_bytes_least_recently_used_going_first():
    from twobells.board.render import _Kept

    kept = _Kept(100)
    for key in "abcd":
        assert kept.get(key, lambda key=key: (key, 25)) == key
    assert kept.get("a", lambda: ("new a", 25)) == "a"  # kept, and now the most recently used
    kept.get("e", lambda: ("e", 25))
    assert kept.taken == 100
    assert kept.get("b", lambda: ("new b", 25)) == "new b"  # the least recently used went
    assert kept.get("a", lambda: ("newer a", 25)) == "a"
    assert kept.get("big", lambda: ("big", 26)) == "big"  # over a quarter: given, not kept
    assert kept.get("big", lambda: ("big again", 26)) == "big again"
    assert kept.taken <= 100


def test_a_screen_redrawing_every_second_keeps_its_text_within_bounds():
    from twobells.board import render as plain

    view = dataclasses.replace(board([Row("07:12", "Manchester Piccadilly", "On time")]), notice="x " * 400)
    for second in range(400):
        view = dataclasses.replace(view, clock=f"07:{second // 60:02}:{second % 60:02}")
        plain.board_frame(view, PALETTE, HDMI_1920X440)
    assert plain._GLYPHS.taken <= plain._GLYPHS.most <= 8 * 1024 * 1024
    for cache in (plain._drawn_box, plain.text_length, plain._font):
        assert cache.cache_info().currsize <= cache.cache_info().maxsize <= 2048


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_on_a_screen_the_last_bus_takes_the_titles_place_after_any_notice_or_rain(panel):
    view = dataclasses.replace(board(), last_bus="Last bus 23:05", clock="22:10:15")
    assert header(view, panel)[0] == "Last bus 23:05"
    assert header(dataclasses.replace(view, rain="Rain from 22:30 (70%)"), panel)[0] == "Rain from 22:30 (70%)"
    assert header(dataclasses.replace(view, notice="Lifts out of use"), panel)[0] == "! Lifts out of use"


# The first train's "Calling at" line, scrolling under its row on a screen.

CALLING = ("Calling at: Dinting, Broadbottom, Hattersley, Godley, Newton for Hyde, Flowery Field, Guide Bridge, "
           "Ashburys and Manchester Piccadilly. This train is formed of 3 coaches.")


def calling_on_screen(clock=CLOCK, text=CALLING):
    trains = (dataclasses.replace(STRIP_TRAINS[0], calling=text), *STRIP_TRAINS[1:])
    return on_screen(trains, clock=clock)


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_a_screen_scrolls_the_calling_at_line_under_the_first_train(panel):
    s = plain_scale(panel)
    [scroll] = [sc for sc in board_frame(calling_on_screen(), PALETTE, panel).scrolls if sc.text == CALLING]
    header_h, label_h, row_h = heights()
    first_row_bottom = (header_h + 6 + label_h + row_h) * s
    # In the destinations' column, to the right edge, between the first and second rows.
    x = 3 * s + left_column_width(load_fonts(s), s)
    assert scroll.box == (int(x), first_row_bottom, int(x) + int(panel.width - 3 * s - x), first_row_bottom + 10 * s)
    assert scroll.strip.width > scroll.width


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_calling_at_line_costs_neither_hdmi_screen_a_row(panel):
    from twobells.board.render import row_limits

    assert row_limits(panel, calling=True) == row_limits(panel)


def test_with_bigger_text_the_calling_at_line_takes_a_bus_s_room():
    from twobells.board.render import row_limits

    assert row_limits(HDMI_1920X440, text_size=14) == {"Trains": 3, "Buses": 2}
    assert row_limits(HDMI_1920X440, text_size=14, calling=True) == {"Trains": 3, "Buses": 1}


@pytest.mark.parametrize("panel", STRIP_PANELS, ids=lambda p: p.name)
def test_the_rows_under_the_calling_at_line_move_down_by_its_height(panel):
    s = plain_scale(panel)
    header_h, label_h, row_h = heights()
    second_row = (header_h + 6 + label_h + row_h) * s
    with_line, without = render(calling_on_screen(), PALETTE, panel), render(on_screen(), PALETTE, panel)
    moved = second_row + 10 * s
    assert with_line.crop((0, moved, panel.width, moved + row_h * s)).tobytes() == (
        without.crop((0, second_row, panel.width, second_row + row_h * s)).tobytes())


def test_e_paper_has_no_calling_at_line():
    view = calling_on_screen(clock=None)
    assert render(view, PALETTE, WAVESHARE_10IN85G).tobytes() == render(on_screen(clock=None), PALETTE,
                                                                          WAVESHARE_10IN85G).tobytes()
