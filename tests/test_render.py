import pytest

from departureboard.panel import HDMI_1280X400, WAVESHARE_10IN85G
from departureboard.render import (
    HEIGHT,
    WIDTH,
    Palette,
    fit_text,
    left_column_width,
    load_fonts,
    render,
    row_layout,
)
from departureboard.view import BoardView, Row, Section, Style

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
    inverted = render(board([Row("07:12", "Manchester", "CANC", Style.INVERTED)]), PALETTE)
    black = lambda img: dict((c, n) for n, c in img.getcolors())[PALETTE.black]
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
    from departureboard.render import render_route
    from departureboard.route import RouteView

    names = ("The Junction", "Grapes Inn", "Sunlaws Street", "St Marys Road", "Arundel Street", "Henry Street")

    def ink(view):
        image = render_route(view, PALETTE, HDMI_1280X400)
        assert image.size == HDMI_1280X400.size
        return sum(n for n, c in image.getcolors() if c == PALETTE.black)

    with_bus = RouteView("341 to Hyde", "07:24 +4", "leave in 6 min", names, 2.5, "3 stops away", "07:12:40")
    without = RouteView("341 to Hyde", "07:24 +4", "leave in 6 min", names, None, "3 stops away", "07:12:40")
    assert ink(with_bus) > ink(without) + 500  # the bus is a solid block


def test_themes_draw_the_route_page():
    from departureboard.render import render_route
    from departureboard.sign import render_route_sign
    from departureboard.themes import THEMES

    assert THEMES["plain"].render_route is render_route
    assert THEMES["sign"].render_route is render_route_sign
