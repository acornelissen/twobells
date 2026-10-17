import pytest

from twobells.board import sign
from twobells.board.panel import (
    HDMI_1280X400,
    HDMI_1920X440,
    MOST_MODES,
    PANELS,
    PHAT,
    WAVESHARE_10IN85G,
    Panel,
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
    rows = tuple(Row(f"07:1{i}", "Manchester Piccadilly", "On time", platform="Plat 1",
                     calling=_CALLING if screen and i == 0 else None) for i in range(3))
    return Section(mode, rows, "None", leave="Leave by 07:05", leave_for=0, mode=mode)


def _sign_rows(panel, modes):
    """The rows the sign look draws of each mode's section."""
    screen = not panel.epaper
    view = BoardView("Glossop > Manchester", tuple(_section(m, screen) for m in modes), "07:03",
                     "07:03:15" if screen else None)
    layout = sign.sign_layout(view, panel.width // panel.scale, panel.height // panel.scale)
    return [len({item.y for item in layout.items if item.text in {r.left for r in s.rows}}) for s in view.sections]


def _fits(panel, count):
    """Whether `count` modes each keep a whole row in every look the panel has."""
    for look in themes_for(panel.epaper):
        if look == "sign":
            if min(_sign_rows(panel, KEYS[:count])) < 1:
                return False
            continue
        for size in _TEXT_SIZES if look == "plain" else (11,):
            if rows_room(panel, count, size, calling=not panel.epaper) < count:
                return False
    return True


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
