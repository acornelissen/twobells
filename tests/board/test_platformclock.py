from datetime import datetime

import pytest

from twobells.board.panel import HDMI_1280X400, HDMI_1920X440, PHAT
from twobells.board.platformclock import ClockView, clock_view, hand_angles, render_clock
from twobells.board.render import SCREEN_PALETTE, Palette


def test_the_hands_point_where_a_clock_s_do():
    assert hand_angles(12, 0, 0) == (0, 0, 0)
    assert hand_angles(3, 0, 0) == (90, 0, 0)
    assert hand_angles(15, 0, 45) == pytest.approx((90.375, 4.5, 270))
    # Half past six: the hour hand halfway to seven, the minute hand straight down.
    assert hand_angles(18, 30, 0) == (195, 180, 0)


def test_the_view_has_the_time_and_the_date_in_words():
    view = clock_view(datetime(2026, 10, 16, 7, 4, 9))
    assert (view.hour, view.minute, view.second) == (7, 4, 9)
    assert view.time == "07:04"
    assert view.date == "Friday 16 October"


def test_the_view_changes_every_second_so_a_screen_redraws_it():
    first = clock_view(datetime(2026, 10, 16, 7, 4, 9))
    assert first.signature() == clock_view(datetime(2026, 10, 16, 7, 4, 9)).signature()
    assert first.signature() != clock_view(datetime(2026, 10, 16, 7, 4, 10)).signature()


@pytest.mark.parametrize("panel", [HDMI_1280X400, HDMI_1920X440])
def test_the_face_is_white_with_black_batons_hands_and_a_red_seconds_hand(panel):
    # 03:00:30: the hour hand at 3, the minute hand at 12, the seconds hand at 6.
    image = render_clock(ClockView(3, 0, 30, "Friday 16 October"), SCREEN_PALETTE, panel)
    assert image.size == panel.size
    pixels = image.load()
    assert pixels is not None
    p = SCREEN_PALETTE
    # Find the face: its left edge on the middle row, and its top.
    middle = panel.height / 2
    left = [pixels[x, round(middle)] for x in range(panel.width)].index(p.white)
    top = next(y for y in range(panel.height) if p.white in {pixels[x, y] for x in range(left, left + panel.height)})
    radius = middle - top
    cx = left + radius
    assert radius > 0.45 * panel.height  # as big as the panel is high, near enough

    def at(fraction_x, fraction_y):
        return pixels[round(cx + fraction_x * radius), round(middle + fraction_y * radius)]

    assert pixels[0, 0] == p.black  # on black, as a station's would be
    assert at(-0.3, -0.5) == p.white  # the face, between the batons and the centre
    assert at(0, 0.77) == p.red  # the seconds hand, straight down
    assert at(0, -0.7) == p.black  # the minute hand, straight up
    assert at(0.4, 0) == p.black  # the hour hand, at 3
    assert at(-0.4, 0) == p.white  # nothing at 9
    assert at(-0.77, 0) == p.black  # the 9 o'clock baton
    # The time and date beside it, in white, to the right of the face.
    words = {pixels[x, y] for x in range(round(cx + radius) + 1, panel.width) for y in range(panel.height)}
    assert words == {p.black, p.white}


def test_without_a_red_the_seconds_hand_takes_the_accent():
    palette = Palette(white=0, black=1, accent=2)
    image = render_clock(ClockView(3, 0, 30, "Friday 16 October"), palette, HDMI_1280X400)
    assert palette.accent in set(image.get_flattened_data())


def test_a_panel_too_narrow_for_the_words_shows_the_face_alone():
    image = render_clock(ClockView(10, 10, 0, "Wednesday 30 September"), Palette(0, 1, 2), PHAT)
    # The face in the middle, and nothing to either side of it.
    pixels = image.load()
    assert pixels is not None
    assert pixels[PHAT.width // 2, PHAT.height * 3 // 4] == 0  # white, below the centre
    sides = {pixels[x, y] for x in (*range(0, 60), *range(PHAT.width - 60, PHAT.width)) for y in range(PHAT.height)}
    assert sides == {1}
