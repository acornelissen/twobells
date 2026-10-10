import string

import pytest
from PIL import Image, ImageDraw

from departureboard.board.dotfont import GLYPHS, HEIGHT, DotFont

FONT = DotFont()
NEEDED = string.ascii_letters + string.digits + " :.,-+'&()/!?*>…"


def test_every_character_the_board_uses_has_a_glyph():
    assert set(NEEDED) <= set(GLYPHS)


@pytest.mark.parametrize("char", sorted(GLYPHS))
def test_glyphs_are_rectangular_and_fit_the_cell(char):
    rows = GLYPHS[char]
    assert len(rows) in (7, HEIGHT)
    assert len({len(r) for r in rows}) == 1
    assert set("".join(rows)) <= {"#", "."}


def test_digits_share_one_width_so_times_line_up():
    assert len({FONT.getlength(d) for d in string.digits}) == 1


def test_length_is_glyph_widths_plus_one_dot_gaps():
    assert FONT.getlength("") == 0
    assert FONT.getlength("1") == 5
    assert FONT.getlength("11") == 11
    assert FONT.getlength(" ") == 3


def test_unknown_characters_fall_back_to_question_mark():
    assert FONT.getlength("é") == FONT.getlength("?")


def test_draw_lights_one_pixel_per_dot():
    image = Image.new("P", (20, HEIGHT), 0)
    FONT.draw(ImageDraw.Draw(image), (0, 0), "1", fill=1)
    lit = "".join(GLYPHS["1"]).count("#")
    assert {c: n for n, c in image.getcolors()}[1] == lit


def test_descenders_draw_below_the_baseline():
    image = Image.new("P", (10, HEIGHT), 0)
    FONT.draw(ImageDraw.Draw(image), (0, 0), "g", fill=1)
    assert any(image.getpixel((x, HEIGHT - 1)) == 1 for x in range(10))


def test_works_with_fit_text():
    from departureboard.board.render import fit_text

    fitted = fit_text("Manchester Piccadilly", FONT, 60)
    assert fitted.endswith("…") and FONT.getlength(fitted) <= 60


def test_tiny_font_covers_the_footer_text():
    from departureboard.board.dotfont import TINY, TINY_HEIGHT

    tiny = DotFont(TINY)
    for text in ("Updated 07:03", "Not updating: trains, buses"):
        assert tiny.getlength(text) < FONT.getlength(text)
    assert all(len(rows) == TINY_HEIGHT for rows in TINY.values())


def test_tiny_font_draws_lowercase_as_small_capitals():
    from departureboard.board.dotfont import TINY

    tiny = DotFont(TINY)
    assert tiny.getlength("updated") == tiny.getlength("UPDATED")


def test_tiny_glyphs_are_rectangular():
    from departureboard.board.dotfont import TINY

    for char, rows in TINY.items():
        assert len({len(r) for r in rows}) == 1, char
        assert set("".join(rows)) <= {"#", "."}, char
