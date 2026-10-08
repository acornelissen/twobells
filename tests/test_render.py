from departureboard.render import (
    HEIGHT,
    WIDTH,
    Palette,
    fit_text,
    left_column_width,
    load_fonts,
    render,
)
from departureboard.view import BoardView, Row, Section, Style

PALETTE = Palette(white=0, black=1, accent=2)


def board(train_rows=(), bus_rows=(), stale_trains=False):
    return BoardView(
        title="GLO > MAN",
        trains=Section("Trains", tuple(train_rows), "No trains", stale_trains),
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
