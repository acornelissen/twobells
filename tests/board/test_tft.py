import dataclasses

import pytest

from tests.board.painted import assert_in_order, painted_rows
from twobells.board.panel import HDMI_1280X400, HDMI_1920X440
from twobells.board.render import (
    SCREEN_PALETTE,
    Palette,
    drawn_length,
    leave_column,
    load_fonts,
    plain_scale,
    platform_column,
    row_layout,
)
from twobells.board.route import RouteView
from twobells.board.tft import PILL_PAD, pill_colours, render_route_tft, render_tft, tft_frame
from twobells.board.view import BoardView, Row, Section, Style
from twobells.core.model import Strip

SCREENS = [HDMI_1280X400, HDMI_1920X440]
P = SCREEN_PALETTE
CLOCK = "07:03:15"
TRAINS = (
    Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 1", strip=Strip("At Dinting", 4, 2, 1.0)),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT, platform="Plat 1"),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 2"),
)
BUSES = (
    Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave in 12 min", "A", strip=Strip("3 stops away", 7, 5, 2.4)),
    Row("237", "Ashton", "07:41", leave="Leave in 29 min", stop="A"),
)


def board(trains=TRAINS, buses=BUSES, clock=CLOCK, **extra):
    return BoardView("GLO > MAN", (Section("Trains", tuple(trains), "No trains", leave="Leave in 3 min (07:12)",
                                           leave_for=0),
                                   Section("Buses", tuple(buses), "No buses", mode="bus")), "07:03", clock, **extra)


def fonts_for(panel):
    s = plain_scale(panel)
    return dataclasses.replace(load_fonts(s), pill=PILL_PAD * s), s


def row_box(panel, section, index):
    """(top, bottom) of a row as the board draws it: under the header, a gap
    and the section's label; buses after the trains, a rule and a gap."""
    s = plain_scale(panel)
    top = (15 + 6 + 12) * s
    if section == "buses":
        top += len(TRAINS) * 14 * s + 7 * s + 12 * s
    top += index * 14 * s
    return top, top + 14 * s


def colours_in(image, box):
    return {colour for _, colour in image.crop(box).getcolors(maxcolors=256)}


def status_box(panel, row, rows, section, index):
    """Where a row's status and its pill are drawn."""
    fonts, s = fonts_for(panel)
    leave_x = leave_column(rows, fonts, panel.width, s)
    layout = row_layout(row, fonts, panel.width, s, platform_column(rows, fonts, panel.width, s), leave_x)
    top, bottom = row_box(panel, section, index)
    return (int(layout.right_x - fonts.pill), top, panel.width, bottom), layout


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_tft_look_is_drawn_light_on_black_at_the_screens_size(panel):
    image = render_tft(board(), P, panel)
    assert image.size == panel.size and image.mode == "P"
    counts = {colour: count for count, colour in image.getcolors(maxcolors=256)}
    assert max(counts, key=lambda c: counts[c]) == P.black
    assert {P.white, P.accent, P.green, P.red} <= set(counts)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
@pytest.mark.parametrize(("index", "fill", "words"), [(0, "green", "black"), (1, "accent", "black"),
                                                     (2, "red", "white")])
def test_each_trains_status_is_in_a_pill_of_its_colour_with_its_words(panel, index, fill, words):
    image = render_tft(board(), P, panel)
    box, _ = status_box(panel, TRAINS[index], TRAINS, "trains", index)
    assert colours_in(image, box) == {P.black, getattr(P, fill), getattr(P, words)}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_bus_on_time_shows_its_time_in_white_without_a_pill(panel):
    image = render_tft(board(), P, panel)
    box, _ = status_box(panel, BUSES[1], BUSES, "buses", 1)
    assert colours_in(image, box) == {P.black, P.white}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_times_are_in_the_accent_and_a_cancelled_row_is_not_reversed_out(panel):
    image = render_tft(board(), P, panel)
    fonts, s = fonts_for(panel)
    for index in range(len(TRAINS)):
        top, bottom = row_box(panel, "trains", index)
        time = (3 * s, top, int(3 * s + drawn_length(fonts.bold, "00:00")), bottom)
        assert colours_in(image, time) == {P.black, P.accent}
        destination = (int(3 * s + drawn_length(fonts.bold, "00:00") + 5 * s), top, panel.width // 3, bottom)
        assert colours_in(image, destination) == {P.black, P.white}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
@pytest.mark.parametrize("index", range(len(TRAINS)))
def test_a_status_and_its_pill_keep_the_right_margin(panel, index):
    image = render_tft(board(), P, panel)
    s = plain_scale(panel)
    top, bottom = row_box(panel, "trains", index)
    lit = [x for x in range(panel.width - 6 * s, panel.width)
           for y in range(top, bottom) if image.getpixel((x, y)) != P.black]
    assert lit and max(lit) < panel.width - 3 * s


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_rows_columns_run_in_order_and_never_into_its_pill(panel):
    fonts, s = fonts_for(panel)
    layouts = painted_rows(tft_frame, board(), panel)
    assert {row.left for row, layout in layouts if layout.strip_x is not None} == {"07:12", "341"}
    for row, layout in layouts:
        assert_in_order(row, layout, fonts, s)
        if layout.where_x is not None:
            # The where words end twice the gap clear of the next column: here, the pill.
            assert layout.where_x + layout.where_room + 12 * s <= layout.right_x - fonts.pill


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_tft_look_lines_its_strips_up_on_the_bar_screen_only(panel):
    layouts = painted_rows(tft_frame, board(), panel)
    xs = {layout.strip_x for _, layout in layouts if layout.strip_x is not None}
    assert len(xs) == (1 if panel is HDMI_1920X440 else 2)
    # Either way the trains keep "Manchester Piccadilly" whole.
    assert [layout.middle for row, layout in layouts if row in TRAINS] == ["Manchester Piccadilly"] * 3


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_header_has_no_bar_the_clock_in_the_accent_and_a_line_under_it(panel):
    image = render_tft(board(), P, panel)
    s = plain_scale(panel)
    assert colours_in(image, (0, 0, panel.width // 2, 15 * s)) == {P.black, P.white}  # the title
    assert colours_in(image, (panel.width - 60 * s, 0, panel.width, 15 * s)) == {P.black, P.accent}  # the clock
    assert colours_in(image, (0, 15 * s, panel.width, 16 * s)) == {P.accent}  # the line


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_long_notice_and_destination_scroll_in_the_tft_look(panel):
    notice = "Disruption between Glossop and Manchester Piccadilly: trains may be cancelled. " * 2
    long = Row("07:12", "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Godley, Newton for Hyde, "
               "Flowery Field, Guide Bridge, Ashburys and Ardwick", "On time")
    frame = tft_frame(board(trains=(long,), notice=notice), P, panel)
    assert {scroll.text for scroll in frame.scrolls} >= {"! " + notice, long.middle}
    assert frame.at(30.0).tobytes() != frame.image.tobytes()


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_route_page_is_drawn_light_on_black(panel):
    route = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", ("Norfolk St", "Henry St", "Your stop"),
                      0.5, "2 stops away", CLOCK)
    image = render_route_tft(route, P, panel)
    counts = {colour: count for count, colour in image.getcolors(maxcolors=256)}
    assert max(counts, key=lambda c: counts[c]) == P.black and P.white in counts and P.accent in counts


def test_without_a_screens_colours_on_time_has_no_pill_and_cancelled_takes_the_accent():
    epaper = Palette(white=0, black=1, accent=2)
    assert pill_colours(TRAINS[0], epaper) is None
    assert pill_colours(TRAINS[1], epaper) == (epaper.accent, epaper.black)
    assert pill_colours(TRAINS[2], epaper) == (epaper.accent, epaper.black)
    assert pill_colours(TRAINS[2], P) == (P.red, P.white)
    assert pill_colours(Row("61", "Buxton", "07:35 sch"), P) is None
    assert pill_colours(Row("07:12", "Hyde", "No report"), P) is None


# A changed platform and the via words, as the plain look has them.

CHANGED = (
    Row("07:12", "Hadfield", "On time", platform="Plat 2 (changed)", platform_changed=True, via="via Dinting"),
    Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT, platform="Plat 1"),
    Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 3 (changed)",
        platform_changed=True),
)


def platform_box(panel, index):
    fonts, s = fonts_for(panel)
    platform_x = platform_column(CHANGED, fonts, panel.width, s)
    top, bottom = row_box(panel, "trains", index)
    return (int(platform_x), top, int(platform_x + drawn_length(fonts.regular, CHANGED[index].platform)), bottom)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_changed_platform_is_in_the_accent_and_an_unchanged_one_in_white(panel):
    image = render_tft(board(trains=CHANGED), P, panel)
    assert colours_in(image, platform_box(panel, 0)) == {P.black, P.accent}
    assert colours_in(image, platform_box(panel, 1)) == {P.black, P.white}
    # A cancelled row isn't reversed out in this look, so it keeps the accent too.
    assert colours_in(image, platform_box(panel, 2)) == {P.black, P.accent}


def test_the_accent_on_black_is_legible():
    from tests.contrast import contrast
    from twobells.board.displays.screen import SCREEN_RGB

    # The TFT look's own accent, amber, behind a changed platform's words.
    assert contrast("#%02x%02x%02x" % SCREEN_RGB["yellow"], "#000000") >= 4.5


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_via_words_follow_the_destination(panel):
    fonts, s = fonts_for(panel)
    layout = row_layout(CHANGED[0], fonts, panel.width, s, platform_column(CHANGED, fonts, panel.width, s))
    assert layout.middle == "Hadfield via Dinting"
    texts = [scroll.text for scroll in tft_frame(board(trains=(dataclasses.replace(
        CHANGED[0], middle="Manchester Piccadilly", via="via Guide Bridge, Hyde Central and Woodley " * 3),)),
        P, panel).scrolls]
    # Too long for its space: the whole of it scrolls, via words and all.
    assert any(text.startswith("Manchester Piccadilly via Guide Bridge") for text in texts)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_calling_at_line_scrolls_under_the_first_train_in_the_tft_look(panel):
    calling = ("Calling at: Dinting, Broadbottom, Hattersley, Godley, Newton for Hyde, Flowery Field, Guide Bridge, "
               "Ashburys and Manchester Piccadilly. This train is formed of 3 coaches.")
    trains = (dataclasses.replace(TRAINS[0], calling=calling), *TRAINS[1:])
    frame = tft_frame(board(trains=trains), P, panel)
    [scroll] = [s for s in frame.scrolls if s.text == calling]
    top, bottom = row_box(panel, "trains", 0)
    assert scroll.box[1] == bottom  # under the first row
    # White on black, as the destinations are.
    assert colours_in(scroll.strip, (0, 0, scroll.strip.width, scroll.strip.height)) == {P.black, P.white}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_with_scrolling_off_nothing_scrolls_in_the_tft_look(panel):
    notice = "Disruption between Glossop and Manchester Piccadilly: trains may be cancelled. " * 2
    long = Row("07:12", "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Godley, Newton for Hyde, "
               "Flowery Field, Guide Bridge, Ashburys and Ardwick", "On time")
    frame = tft_frame(board(trains=(long,), notice=notice, scroll=False), P, panel)
    assert frame.scrolls == ()
    assert frame.at(30.0).tobytes() == frame.image.tobytes()
