import pytest

from twobells.board import sign
from twobells.board.dotfont import HEIGHT as GLYPH_H
from twobells.board.dotfont import TINY_HEIGHT
from twobells.board.panel import (
    HDMI_1280X400,
    HDMI_1920X440,
    MORE_MODES_UP_TO,
    MOST_MODES,
    PANELS,
    PHAT,
    WAVESHARE_10IN85G,
    Panel,
    biggest_text_for,
    most_modes,
    panel_name,
)
from twobells.board.render import rows_room
from twobells.board.themes import themes_for
from twobells.board.view import BoardView, Row, Section
from twobells.settings.modes import KEYS


def test_phat_is_drawn_at_one_pixel_per_dot():
    assert (PHAT.size, PHAT.scale, PHAT.epaper) == ((250, 122), 1, True)


def test_bigger_panels_scale_by_whole_pixels_from_their_height():
    assert (WAVESHARE_10IN85G.size, WAVESHARE_10IN85G.scale) == ((1360, 480), 4)
    assert (HDMI_1280X400.size, HDMI_1280X400.scale) == ((1280, 400), 3)
    assert Panel.sized("tiny", 200, 60).scale == 1


def test_the_bar_screen_draws_bigger_dots_than_its_height_alone_gives():
    # Four pixels a dot, 480x110 dots: a third bigger, with a line a row to make room.
    assert (HDMI_1920X440.size, HDMI_1920X440.scale) == ((1920, 440), 4)


def test_the_lcd_is_not_epaper():
    assert WAVESHARE_10IN85G.epaper
    assert not HDMI_1280X400.epaper
    assert not HDMI_1920X440.epaper


def test_registry_names_match():
    assert all(panel.name == name for name, panel in PANELS.items())
    assert set(PANELS) == {"inky-phat", "inky-phat-v1", "waveshare-10in85g", "hdmi-1280x400", "hdmi-1920x440"}


# How many modes each display has room for (MOST_MODES), measured: every
# mode keeps at least a whole row in every look the display has, at every
# text size, with a screen's "Calling at" line and the leave words under a
# train; one more mode would leave one without.

_TEXT_SIZES = range(9, 17)
_CALLING = "Calling at: Dinting, Broadbottom, Hattersley, Godley and Manchester Piccadilly"


def _section(mode, screen):
    if mode == "bus":
        rows = tuple(Row(f"34{i}", "Hyde", f"07:2{i}", stop="A", leave=f"Leave by 07:1{i}") for i in range(3))
        return Section("Buses", rows, "No buses", mode="bus")
    # Each mode its own times, so the rows drawn are counted a section at a time.
    hour = KEYS.index(mode) + 7
    rows = tuple(Row(f"{hour:02d}:1{i}", "Manchester Piccadilly", "On time", platform="Plat 1",
                     calling=_CALLING if screen and i == 0 else None) for i in range(3))
    return Section(mode, rows, "None", leave=f"Leave by {hour:02d}:05", leave_for=0, mode=mode)


def _sign_fits(panel, modes):
    """Whether the sign look draws a row of each mode's section, and a
    train's or tram's leave words with it, all clear of the bottom line and
    of the rule over it."""
    screen = not panel.epaper
    view = BoardView("Glossop > Manchester", tuple(_section(m, screen) for m in modes), "07:03",
                     "07:03:15" if screen else None)
    width, height = panel.width // panel.scale, panel.height // panel.scale
    layout = sign.sign_layout(view, width, height)
    drawn = [item for item in layout.items if item.text not in {view.clock, f"Updated {view.updated}"}]
    words = {item.text for item in drawn}
    rows = [len({item.y for item in drawn if item.text in {r.left for r in s.rows}}) for s in view.sections]
    leaves = [s.leave for s in view.sections if s.leave]
    bottom = height - (GLYPH_H if screen else TINY_HEIGHT) - 1
    if len(layout.rules) == len(view.sections):  # a rule over the bottom line
        bottom = layout.rules[-1]
    return min(rows) >= 1 and all(leave in words for leave in leaves) and all(
        item.y + sign.item_size(item)[1] <= bottom for item in drawn)


def _fits_in(panel, count, look, size):
    """Whether `count` modes each keep a whole row in `look` at text size
    `size` (the plain look's; the others draw at their own), with their
    leave words: the sign look draws a train's on a line of its own."""
    if look == "sign":
        return _sign_fits(panel, KEYS[:count])
    return rows_room(panel, count, size if look == "plain" else 11, calling=not panel.epaper) >= count


def _fits(panel, count):
    """Whether `count` modes each keep a whole row in every look the panel has."""
    return all(_fits_in(panel, count, look, size) for look in themes_for(panel.epaper)
               for size in (_TEXT_SIZES if look == "plain" else (11,)))


@pytest.mark.parametrize("panel", PANELS.values(), ids=lambda p: p.name)
def test_each_display_has_room_for_the_modes_that_keep_a_row_each(panel):
    most = most_modes(panel)
    assert most >= 2  # trains and buses have always shared every display
    if most > 2:
        assert _fits(panel, most)
    if most < len(KEYS):
        assert not _fits(panel, most + 1)


def test_only_the_10in85_e_paper_has_room_for_three_modes():
    assert MOST_MODES == {"inky-phat": 2, "inky-phat-v1": 2, "waveshare-10in85g": 3, "hdmi-1280x400": 2,
                          "hdmi-1920x440": 2}


def test_a_panel_of_another_size_has_room_for_two_and_is_this_display():
    other = Panel.sized("inky-400x300", 400, 300)
    assert most_modes(other) == 2
    assert panel_name(other) == "this display"
    assert panel_name(PHAT) == "the Inky pHAT"


@pytest.mark.parametrize(("panel", "size"), [(PHAT, 13), (HDMI_1280X400, 13), (HDMI_1920X440, 14)],
                         ids=("inky-phat", "hdmi-1280x400", "hdmi-1920x440"))
def test_a_short_display_has_no_row_for_a_third_mode_from_a_bigger_text_size(panel, size):
    # The plain look: a screen with its "Calling at" line.
    assert rows_room(panel, 3, size - 1, calling=not panel.epaper) >= 3
    assert rows_room(panel, 3, size, calling=not panel.epaper) < 3


# In a given look and text size (most_modes with a look): the HDMI screens
# have room for a third mode where it keeps a whole row too.


@pytest.mark.parametrize("panel", PANELS.values(), ids=lambda p: p.name)
def test_each_look_and_text_size_has_room_for_the_modes_that_keep_a_row_each(panel):
    for look in themes_for(panel.epaper):
        for size in _TEXT_SIZES:
            most = most_modes(panel, look, size)
            assert most >= most_modes(panel)  # never less than in any look
            if most > most_modes(panel):  # the room in any look is measured above
                assert _fits_in(panel, most, look, size), (look, size)
            if panel.name in MORE_MODES_UP_TO and most < len(KEYS):
                assert not _fits_in(panel, most + 1, look, size), (look, size)


def test_the_screens_have_room_for_three_modes_in_the_station_board_tft_and_split_flap_looks():
    for panel in (HDMI_1280X400, HDMI_1920X440):
        for look in ("sign", "tft", "flap"):
            assert [most_modes(panel, look, size) for size in _TEXT_SIZES] == [3] * len(_TEXT_SIZES)


def test_the_screens_have_room_for_three_in_the_plain_look_up_to_the_biggest_text_that_fits():
    assert MORE_MODES_UP_TO == {"hdmi-1280x400": 12, "hdmi-1920x440": 13}
    assert [most_modes(HDMI_1280X400, "plain", size) for size in _TEXT_SIZES] == [3, 3, 3, 3, 2, 2, 2, 2]
    assert [most_modes(HDMI_1920X440, "plain", size) for size in _TEXT_SIZES] == [3, 3, 3, 3, 3, 2, 2, 2]
    assert most_modes(HDMI_1280X400, "plain") == 3  # the standard size
    assert (biggest_text_for(HDMI_1280X400, 3), biggest_text_for(HDMI_1920X440, 3)) == (12, 13)
    assert biggest_text_for(HDMI_1280X400, 2) is None


def test_e_paper_has_the_same_room_in_every_look():
    for panel in (PHAT, WAVESHARE_10IN85G):
        for look in themes_for(True):
            assert {most_modes(panel, look, size) for size in _TEXT_SIZES} == {most_modes(panel)}
        assert biggest_text_for(panel, 3) is None
