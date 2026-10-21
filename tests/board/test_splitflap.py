import pytest

from tests.board.painted import assert_in_order, painted_rows
from twobells.board.flaps import Flapper
from twobells.board.panel import HDMI_1280X400, HDMI_1920X440
from twobells.board.render import (
    SCREEN_PALETTE,
    drawn_length,
    plain_scale,
)
from twobells.board.route import RouteView
from twobells.board.splitflap import (
    TILE_GAP,
    TILE_HEIGHT,
    TILE_PITCH,
    SplitFlapPainter,
    render_route_split_flap,
    render_split_flap,
    split_flap_frame,
    status_colours,
    tile_font,
)
from twobells.board.view import BoardView, Row, Section, Style
from twobells.core.model import Strip

SCREENS = [HDMI_1280X400, HDMI_1920X440]
P = SCREEN_PALETTE
CLOCK = "07:03:15"
TRAINS = (
    Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 1", strip=Strip("At Dinting", 4, 2, 1.0)),
    Row("07:42", "Hadfield", "Exp 07:49", Style.ALERT, platform="Plat 1"),
    Row("08:12", "Hadfield", "Cancelled", Style.INVERTED, platform="Plat 2"),
)
BUSES = (
    Row("341", "Hyde", "Exp 07:24", Style.ALERT, "Leave in 12 min", "A", strip=Strip("3 stops away", 7, 5, 2.4)),
    Row("237", "Ashton", "07:41", leave="Leave in 29 min", stop="A"),
)


def board(trains=TRAINS, buses=BUSES, clock=CLOCK, **extra):
    return BoardView("GLO > MAN", (Section("Trains", tuple(trains), "No trains", leave="Leave in 3 min (07:12)",
                                           leave_for=0),
                                   Section("Buses", tuple(buses), "No buses", mode="bus")), "07:03", clock, **extra)


def row_top(panel, section, index):
    """A row's top as the board draws it: under the header, a gap and the
    section's label; buses after the trains, a rule and a gap."""
    s = plain_scale(panel)
    top = (15 + 6 + 12) * s
    if section == "buses":
        top += len(TRAINS) * 14 * s + 7 * s + 12 * s
    return top + index * 14 * s


def colours_in(image, box):
    return {colour for _, colour in image.crop(box).getcolors(maxcolors=256)}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_look_is_drawn_on_tiles_on_black_at_the_screens_size(panel):
    image = render_split_flap(board(), P, panel)
    assert image.size == panel.size and image.mode == "P"
    counts = {colour: count for count, colour in image.getcolors(maxcolors=256)}
    assert max(counts, key=lambda c: counts[c]) == P.black
    assert {P.white, P.accent, P.tile} <= set(counts)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_rows_time_is_a_tile_a_letter_with_black_between_and_across(panel):
    image = render_split_flap(board(), P, panel)
    s = plain_scale(panel)
    pitch, gap, height = tile_font(s).pitch, tile_font(s).gap, TILE_HEIGHT * s
    top, left = row_top(panel, "trains", 0) + s, 3 * s
    for i in range(len("07:12")):
        tile = (left + i * pitch, top, left + (i + 1) * pitch - gap, top + height)
        assert colours_in(image, tile) == {P.black, P.tile, P.white}  # the letter on its tile, rounded corners
        between = (tile[2], top, tile[2] + gap, top + height)
        assert colours_in(image, between) == {P.black}
        split = (tile[0] + s, top + height // 2, tile[2] - s, top + height // 2 + 1)
        assert colours_in(image, split) == {P.black}


def test_the_tile_font_measures_every_letter_one_tile_apart():
    font = tile_font(3)
    assert (font.pitch, font.gap) == (TILE_PITCH * 3, TILE_GAP * 3)
    assert drawn_length(font, "07:12") == 5 * font.pitch - font.gap
    assert font.getlength("Wil") == font.getlength("iii") == 3 * font.pitch
    assert drawn_length(font, "") == 0


@pytest.mark.parametrize(("index", "letters", "tiles"), [(0, "white", "tile"), (1, "accent", "tile"),
                                                         (2, "black", "accent")])
def test_each_status_is_coloured_by_what_it_says(index, letters, tiles):
    assert status_colours(TRAINS[index], P) == (getattr(P, letters), getattr(P, tiles))


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
@pytest.mark.parametrize(("index", "colours"), [(0, {"white", "tile"}), (1, {"accent", "tile"}),
                                                (2, {"black", "accent"})])
def test_each_trains_status_is_drawn_in_its_colours_against_the_right_margin(panel, index, colours):
    image = render_split_flap(board(), P, panel)
    s = plain_scale(panel)
    top = row_top(panel, "trains", index)
    width = drawn_length(tile_font(s), TRAINS[index].right)
    box = (int(panel.width - 3 * s - width), top + s, panel.width - 3 * s, top + s + TILE_HEIGHT * s)
    assert colours_in(image, box) == {P.black} | {getattr(P, name) for name in colours}
    assert colours_in(image, (panel.width - 3 * s, top, panel.width, top + 14 * s)) == {P.black}


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_rows_columns_run_in_order_and_never_into_each_other(panel):
    s = plain_scale(panel)
    fonts = SplitFlapPainter(P, panel, live=True).f
    layouts = painted_rows(split_flap_frame, board(), panel)
    assert any(layout.strip_x is not None for _, layout in layouts)
    for row, layout in layouts:
        assert_in_order(row, layout, fonts, s)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_split_flap_look_lines_its_strips_up_down_the_board(panel):
    layouts = painted_rows(split_flap_frame, board(), panel)
    assert len({layout.strip_x for _, layout in layouts if layout.strip_x is not None}) == 1


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_each_rows_time_destination_and_status_turn_over_and_nothing_else(panel):
    frame = split_flap_frame(board(), P, panel)
    texts = [flap.text for flap in frame.flaps]
    for row in (*TRAINS, *BUSES):
        assert row.left in texts and row.right in texts
    assert TRAINS[1].middle in texts and "A: Hyde" in texts
    assert "GLO > MAN" not in texts and CLOCK not in texts  # the header's letters change at once
    assert len({flap.key for flap in frame.flaps}) == len(frame.flaps)
    assert [flap.right for flap in frame.flaps].count(True) == len(TRAINS) + len(BUSES)  # the statuses


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_turning_field_is_drawn_just_as_the_board_draws_it(panel):
    frame = split_flap_frame(board(), P, panel)
    for flap in frame.flaps:
        assert flap.paint(flap.text).tobytes() == frame.image.crop(flap.box).tobytes(), flap.text


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_same_field_keeps_its_key_when_its_text_changes(panel):
    before = split_flap_frame(board(), P, panel)
    later = (Row("07:12", "Manchester Piccadilly", "Exp 07:20", Style.ALERT, platform="Plat 1"), *TRAINS[1:])
    after = split_flap_frame(board(trains=later), P, panel)
    status = {flap.key: flap.text for flap in after.flaps}
    changed = [flap.key for flap in before.flaps if status.get(flap.key) != flap.text]
    assert len(changed) == 1 and status[changed[0]] == "Exp 07:20"


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_rows_under_the_calling_at_line_turn_over_when_it_goes(panel):
    # The first train leaves and the next has no "Calling at" line: every row
    # moves up by the line's height, yet each still turns over to its new
    # letters, as a Solari board's rows do, rather than changing at once.
    calling = "Calling at: Dinting and Manchester Piccadilly."
    before = (Row("07:12", "Glossop", "On time", platform="Plat 1", calling=calling), *TRAINS[1:])
    after = (*TRAINS[1:], Row("08:42", "Glossop", "On time", platform="Plat 2"))
    assert before[0].calling and after[0].calling is None
    flapper = Flapper()
    flapper.show(split_flap_frame(board(trains=before), P, panel).flaps, 0.0)
    later = split_flap_frame(board(trains=after), P, panel).flaps
    boxes = {box for box, _ in flapper.show(later, 1.0)}
    turning = [flap.text for flap in later if flap.box in boxes]
    # Each changed field of the three trains (Plat 1 to Plat 2 is one step of
    # the drum, a single frame); the buses moved up too, but haven't changed.
    assert turning == ["07:42", "Hadfield", "Exp 07:49",
                       "08:12", "Cancelled",
                       "08:42", "Glossop", "On time"]


def test_text_that_scrolls_doesnt_turn_and_e_paper_never_does():
    long = Row("07:12", "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Godley, Newton for Hyde", "On time")
    frame = split_flap_frame(board(trains=(long,)), P, HDMI_1280X400)
    assert long.middle in {scroll.text for scroll in frame.scrolls}
    assert long.middle not in {flap.text for flap in frame.flaps}
    assert split_flap_frame(board(clock=None), P, HDMI_1280X400).flaps == ()


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_a_long_notice_and_destination_scroll_whole_tiles_wide(panel):
    notice = "Disruption between Glossop and Manchester Piccadilly: trains may be cancelled. " * 2
    long = Row("07:12", "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Godley, Newton for Hyde, "
               "Flowery Field, Guide Bridge, Ashburys and Ardwick", "On time")
    frame = split_flap_frame(board(trains=(long,), notice=notice), P, panel)
    pitch, gap = tile_font(plain_scale(panel)).pitch, tile_font(plain_scale(panel)).gap
    assert {scroll.text for scroll in frame.scrolls} >= {"! " + notice, long.middle}
    assert all((scroll.width + gap) % pitch == 0 for scroll in frame.scrolls)
    assert frame.at(30.0).tobytes() != frame.image.tobytes()


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_the_route_page_is_drawn_on_tiles(panel):
    route = RouteView("341 to Hyde", "Exp 07:24", "Leave in 6 min", ("Norfolk St", "Henry St", "Your stop"),
                      0.5, "2 stops away", CLOCK)
    image = render_route_split_flap(route, P, panel)
    counts = {colour: count for count, colour in image.getcolors(maxcolors=256)}
    assert max(counts, key=lambda c: counts[c]) == P.black and {P.white, P.tile} <= set(counts)


@pytest.mark.parametrize("panel", SCREENS, ids=lambda p: p.name)
def test_with_scrolling_off_nothing_scrolls_but_the_tiles_still_turn(panel):
    notice = "Disruption between Glossop and Manchester Piccadilly: trains may be cancelled. " * 2
    long = Row("07:12", "Manchester Piccadilly via Hadfield, Dinting, Broadbottom, Godley, Newton for Hyde, "
               "Flowery Field, Guide Bridge, Ashburys and Ardwick", "On time")
    frame = split_flap_frame(board(trains=(long,), notice=notice, scroll=False), P, panel)
    assert frame.scrolls == ()
    assert frame.at(30.0).tobytes() == frame.image.tobytes()
    turning = {flap.text for flap in frame.flaps}
    assert "07:12" in turning and any(text.startswith("Manchester") and text.endswith("…") for text in turning)
