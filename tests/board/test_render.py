import dataclasses
import hashlib
import string
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from departureboard.board.panel import HDMI_1280X400, PHAT, PHAT_V1, WAVESHARE_10IN85G
from departureboard.board.render import (
    HEIGHT,
    WIDTH,
    Palette,
    drawn_length,
    fit_text,
    header_text,
    heights,
    in_turns,
    left_column_width,
    load_fonts,
    plain_scale,
    platform_column,
    render,
    render_code,
    render_setup,
    row_layout,
    wrap_text,
)
from departureboard.board.view import BoardView, CodeView, Row, Section, SetupView, Style
from tests.paths import ROOT

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
    image = render(board([Row("07:12", "Manchester Piccadilly", "+7", Style.ALERT)]), PALETTE)
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

BOARD_FONTS = ROOT / "src" / "departureboard" / "board" / "fonts"


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


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_the_plain_look_draws_in_the_packaged_inter(scale):
    fonts = load_fonts(scale)
    assert fonts.regular.getname() == ("Inter", "Regular")
    assert fonts.bold.getname() == fonts.small.getname() == ("Inter", "ExtraBold")
    for font in (fonts.regular, fonts.bold, fonts.small):
        assert Path(str(font.path)).parent == BOARD_FONTS


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


@pytest.mark.parametrize("panel", [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400])
def test_row_text_stays_inside_its_row(panel):
    # Drawn a scale step below the row's top, as _Painter.row draws it.
    s = plain_scale(panel)
    fonts = load_fonts(s)
    row_h = heights()[2] * s
    for font in (fonts.regular, fonts.bold):
        _, top, _, bottom = font.getbbox("ÉÅHdgjpqy", mode="1")
        assert s + top >= 0 and s + bottom <= row_h


def test_preview_palette_gives_rgb_png_colours():
    image = render(board([Row("07:12", "Manchester", "+7", Style.ALERT)]), PALETTE.with_preview("red"))
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
    with_leave = render(board(train_leave="Leave 07:05 (07:12)"), PALETTE)
    assert ink_in_right_half_of_train_label_row(plain) == 0
    assert ink_in_right_half_of_train_label_row(with_leave) > 30


def test_leave_text_is_not_in_accent_colour():
    image = render(board(train_leave="Leave 07:05 (07:12)"), PALETTE)
    assert PALETTE.accent not in colours(image)


def test_row_layout_without_leave_has_no_leave_column():
    layout = row_layout(Row("341", "A: Hyde", "07:20"), load_fonts())
    assert layout.leave_x is None


def test_row_layout_places_leave_between_destination_and_time():
    fonts = load_fonts()
    row = Row("341", "A: Hyde", "07:24 +4", Style.ALERT, "leave 07:17")
    layout = row_layout(row, fonts)
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < layout.leave_x
    assert layout.leave_x + fonts.small.getlength("leave 07:17") < layout.right_x


def test_long_destination_is_truncated_to_keep_leave_time_whole():
    fonts = load_fonts()
    row = Row("394", "B: Stepping Hill Hospital", "07:35 sch", leave="leave 07:28")
    layout = row_layout(row, fonts)
    assert layout.middle.endswith("…")
    assert layout.middle_x + fonts.regular.getlength(layout.middle) < layout.leave_x


def test_row_with_leave_draws_more_ink():
    def ink(image):
        return sum(n for n, c in image.getcolors() if c != PALETTE.white)

    plain = render(board(bus_rows=[Row("341", "A: Hyde", "07:20")]), PALETTE)
    with_leave = render(board(bus_rows=[Row("341", "A: Hyde", "07:20", leave="leave 07:14")]), PALETTE)
    assert ink(with_leave) > ink(plain)


def test_plain_layout_prefixes_destination_with_stop_label():
    layout = row_layout(Row("341", "Hyde", "07:20", stop="A"), load_fonts())
    assert layout.middle == "A: Hyde"


BIG_PANELS = [WAVESHARE_10IN85G, HDMI_1280X400]


@pytest.mark.parametrize("panel", BIG_PANELS, ids=lambda p: p.name)
def test_bigger_panels_get_an_image_their_size(panel):
    image = render(board([Row("07:12", "Manchester", "+7", Style.ALERT)]), PALETTE, panel)
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
    row = Row("394", "B: Stepping Hill Hospital", "07:35 sch", leave="leave 07:28")
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
    from departureboard.board.render import render_route
    from departureboard.board.route import RouteView

    names = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")

    def ink(view):
        image = render_route(view, PALETTE, HDMI_1280X400)
        assert image.size == HDMI_1280X400.size
        return sum(n for n, c in image.getcolors() if c == PALETTE.black)

    with_bus = RouteView("341 to Hyde", "07:24 +4", "leave in 6 min", names, 2.5, "3 stops away", "07:12:40")
    without = RouteView("341 to Hyde", "07:24 +4", "leave in 6 min", names, None, "3 stops away", "07:12:40")
    assert ink(with_bus) > ink(without) + 500  # the bus is a solid block


def test_themes_draw_the_route_page():
    from departureboard.board.render import render_route
    from departureboard.board.sign import render_route_sign
    from departureboard.board.themes import THEMES

    assert THEMES["plain"].render_route is render_route
    assert THEMES["sign"].render_route is render_route_sign


def test_no_leave_text_for_a_train_that_is_not_drawn():
    # Only three trains fit; a leave time for the fourth would point at nothing.
    rows = [Row(f"07:{m}", "Manchester", "On time") for m in (12, 20, 31, 42)]
    view = board(rows)
    for_fourth = dataclasses.replace(view, trains=dataclasses.replace(view.trains, leave="Leave 07:35 (07:42)", leave_for=3))
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
    image = render(board(rows, stale_trains=True, train_leave="Leave 07:05 (07:12)"), PALETTE)
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
    image = render(board(trains, rows, train_leave="Leave 07:05 (07:12)"), PALETTE, WAVESHARE_10IN85G)
    bottom = [image.getpixel((x, y)) for x in range(WAVESHARE_10IN85G.width) for y in range(476, 480)]
    assert PALETTE.black not in bottom


ALL_PANELS = [PHAT, WAVESHARE_10IN85G, HDMI_1280X400]


def ink_columns(image, top, bottom, background):
    """x positions with anything but `background` between rows top and bottom."""
    return {x for x in range(image.width) for y in range(top, bottom) if image.getpixel((x, y)) != background}


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("status", ["Cancelled", "On time", "Exp 07:49", "+7"])
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
    Row("07:42", "Manchester Piccadilly", "+7", Style.ALERT),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 2"),
]


def test_the_phat_has_no_platform_column_and_is_unchanged():
    assert platform_column(TRAINS_WITH_PLATFORMS, load_fonts(), WIDTH, 1) is None
    plain = [dataclasses.replace(r, platform=None) for r in TRAINS_WITH_PLATFORMS]
    assert render(board(TRAINS_WITH_PLATFORMS), PALETTE).tobytes() == render(board(plain), PALETTE).tobytes()


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
    long = "Manchester Piccadilly via Hadfield, Dinting, Broadbottom and Guide Bridge"
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
def test_on_a_screen_a_long_notice_takes_turns_in_the_header(panel):
    def shown(clock):
        view = dataclasses.replace(board(), notice=NOTICE * 2, clock=clock)
        title, stamp, stamp_x, fonts, s = header(view, panel)
        assert stamp == clock
        assert 3 * s + drawn_length(fonts.bold, title) <= stamp_x - 6 * s
        return title

    parts = {shown(f"07:03:{second:02d}") for second in range(0, 60, 5)}
    assert len(parts) > 1
    assert all(p.startswith("! ") for p in parts)
    assert shown("07:03:20") == shown("07:03:24") != shown("07:03:25")


def test_notice_changes_what_the_header_draws():
    plain = render(board(), PALETTE)
    noticed = render(dataclasses.replace(board(), notice="Lifts out of use"), PALETTE)
    assert plain.crop((0, 0, WIDTH, 15)).tobytes() != noticed.crop((0, 0, WIDTH, 15)).tobytes()
    assert plain.crop((0, 15, WIDTH, HEIGHT)).tobytes() == noticed.crop((0, 15, WIDTH, HEIGHT)).tobytes()


@pytest.mark.parametrize("panel", ALL_PANELS, ids=lambda p: p.name)
@pytest.mark.parametrize("clock", [None, "07:10:15"])
def test_rain_line_takes_the_titles_place(panel, clock):
    view = dataclasses.replace(board(), rain="Rain from 07:10 (70%)", clock=clock)
    assert header(view, panel)[0] == "Rain from 07:10 (70%)"


def test_a_disruption_notice_beats_the_rain_line_in_the_header():
    view = dataclasses.replace(board(), rain="Rain from 07:10 (70%)", notice="Lifts out of use")
    assert header(view)[0] == "! Lifts out of use"


def test_wrap_text_keeps_words_whole_and_splits_only_overlong_ones():
    measure = len
    assert wrap_text("one two three four", 9, measure) == ["one two", "three", "four"]
    assert wrap_text("abcdefghijkl xy", 5, measure) == ["abcde", "fghij", "kl xy"]
    assert wrap_text("", 5, measure) == []


def test_in_turns_follows_the_board_clock():
    text = "one two three four five six"
    assert in_turns(text, 100, len, "07:00:00") == text
    first, second = in_turns(text, 12, len, "07:00:00", "! "), in_turns(text, 12, len, "07:00:05", "! ")
    assert first == "! one two …" and second == "! three …"
    assert all(len(in_turns(text, 12, len, f"07:00:{s:02d}", "! ")) <= 12 for s in range(60))


def test_plain_look_shows_fewer_rows_rather_than_cut_them_off_on_the_original_phat():
    from departureboard.board.panel import PHAT_V1

    trains = [Row(f"07:{m}", "Manchester Piccadilly", "On time") for m in (12, 42, 52)]
    buses = [Row("341", "A: Hyde", "07:20"), Row("61", "B: Buxton", "07:35 sch")]
    image = render(board(trains, buses), PALETTE, PHAT_V1)
    assert image.size == PHAT_V1.size
    bottom = [image.getpixel((x, y)) for x in range(PHAT_V1.width) for y in range(PHAT_V1.height - 2, PHAT_V1.height)]
    assert PALETTE.black not in bottom  # nothing drawn into the last rows, so nothing cut off


def _full_board():
    trains = [Row(f"07:{m}", "Manchester Piccadilly", "On time") for m in (12, 42, 52)]
    buses = [Row("341", "A: Hyde", "07:20"), Row("61", "B: Buxton", "07:35 sch")]
    return board(trains, buses, train_leave="Leave 07:05 (07:12)")


def test_default_text_size_draws_exactly_as_before():
    assert render(_full_board(), PALETTE).tobytes() == render(_full_board(), PALETTE, text_size=11).tobytes()


def test_slightly_bigger_text_still_fits_the_original_phat():
    from departureboard.board.panel import PHAT_V1
    from departureboard.board.render import row_limits

    assert row_limits(PHAT_V1, text_size=12) == {"Trains": 2, "Buses": 1}
    image = render(_full_board(), PALETTE, PHAT_V1, text_size=12)
    bottom = [image.getpixel((x, y)) for x in range(PHAT_V1.width) for y in range(PHAT_V1.height - 2, PHAT_V1.height)]
    assert PALETTE.black not in bottom


def test_bigger_text_draws_more_ink():
    from departureboard.board.panel import PHAT_V1

    def ink(size):
        image = render(_full_board(), PALETTE, PHAT_V1, text_size=size)
        return sum(n for n, c in image.getcolors() if c == PALETTE.black)

    assert ink(12) > ink(11)


@pytest.mark.parametrize("text_size", [11, 12, 13])
def test_a_shortened_destination_leaves_a_gap_before_the_status(text_size):
    from departureboard.board.panel import PHAT_V1
    from departureboard.board.render import drawn_length

    fonts = load_fonts(1, text_size)
    layout = row_layout(Row("20:22", "Manchester Piccadilly", "On time"), fonts, PHAT_V1.width, 1)
    # Whole or cut short, the destination ends clear of the status.
    assert layout.middle_x + drawn_length(fonts.regular, layout.middle) <= layout.right_x - 3


# --- The settings reset code page ---

CODE_PAGE = CodeView("123 456", "23:52", 1.0)


def ink_box(image, background):
    """The bounding box of everything not in the background colour."""
    return image.point(lambda v: 0 if v == background else 255, mode="L").getbbox()


@pytest.mark.parametrize("panel", [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400])
@pytest.mark.parametrize("dark", [False, True])
def test_the_code_page_fits_every_panel(panel, dark):
    image = render_code(CODE_PAGE, PALETTE, panel, dark=dark)
    assert image.size == panel.size and image.mode == "P"
    left, top, right, bottom = ink_box(image, PALETTE.black if dark else PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height  # nothing cut off


@pytest.mark.parametrize("panel", [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400])
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
PANELS = [PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400]


@pytest.mark.parametrize("panel", PANELS)
@pytest.mark.parametrize("dark", [False, True])
def test_the_setup_page_fits_every_panel(panel, dark):
    image = render_setup(SETUP_PAGE, PALETTE, panel, dark=dark)
    assert image.size == panel.size and image.mode == "P"
    left, top, right, bottom = ink_box(image, PALETTE.black if dark else PALETTE.white)
    assert left > 0 and top > 0 and right < panel.width and bottom < panel.height  # nothing cut off


@pytest.mark.parametrize("panel", PANELS)
def test_the_address_is_drawn_across_the_panel(panel):
    # The bottom band is the address, nearly as wide as the panel.
    image = render_setup(SETUP_PAGE, PALETTE, panel)
    band = image.crop((0, panel.height * 3 // 4, panel.width, panel.height))
    left, _, right, _ = ink_box(band, PALETTE.white)
    assert right - left > panel.width * 0.8


@pytest.mark.parametrize("panel", PANELS)
def test_a_long_name_still_fits(panel):
    image = render_setup(SetupView("http://my-very-long-departure-board-name.local:8090/settings"), PALETTE, panel)
    left, top, right, bottom = ink_box(image, PALETTE.white)
    assert left > 0 and right < panel.width


def test_an_address_too_long_to_read_on_one_line_breaks_before_its_port():
    from departureboard.board.render import MIN_ADDRESS_PX, _biggest_bold, address_lines

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
    from departureboard.cli.common import setup_url
    from departureboard.cli.run import OFF_HEADING, OFF_NOTE

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


@pytest.mark.parametrize("panel", PANELS)
def test_the_board_shows_the_address_without_http(panel):
    # Browsers take name.local:8090/settings as typed; leaving out http://
    # keeps a long Pi name readable on the small panels.
    view = SetupView("http://kitchen-board.local:8090/settings")
    assert view.shown == "kitchen-board.local:8090/settings"
    bare = SetupView("kitchen-board.local:8090/settings")
    assert render_setup(view, PALETTE, panel).tobytes() == render_setup(bare, PALETTE, panel).tobytes()


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
