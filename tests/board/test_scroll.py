import pytest
from PIL import Image

from twobells.board.scroll import (
    SCROLL_FRAME_S,
    SCROLL_PAUSE_S,
    SCROLL_STEPS_PER_S,
    Frame,
    Scroll,
    Scroller,
    frame_of,
    scroll_steps,
)


def test_text_that_fits_never_moves():
    assert all(scroll_steps(0, t / 4) == 0 for t in range(200))


def test_scroll_holds_at_its_start_then_moves_a_step_at_a_time():
    assert scroll_steps(40, 0) == 0
    assert scroll_steps(40, SCROLL_PAUSE_S - 0.01) == 0
    assert scroll_steps(40, SCROLL_PAUSE_S) == 0
    assert scroll_steps(40, SCROLL_PAUSE_S + 1) == SCROLL_STEPS_PER_S
    assert scroll_steps(40, SCROLL_PAUSE_S + 1.5) == 1.5 * SCROLL_STEPS_PER_S


def test_scroll_moves_steadily_one_step_a_frame():
    steps = [scroll_steps(40, SCROLL_PAUSE_S + k * SCROLL_FRAME_S) for k in range(41)]
    assert steps == list(range(41))


def test_scroll_holds_at_its_end_then_starts_again():
    travel = 40 / SCROLL_STEPS_PER_S
    end = SCROLL_PAUSE_S + travel
    assert scroll_steps(40, end) == 40
    assert scroll_steps(40, end + SCROLL_PAUSE_S - 0.01) == 40
    assert scroll_steps(40, end + SCROLL_PAUSE_S) == 0  # from the beginning again
    assert scroll_steps(40, end + SCROLL_PAUSE_S + SCROLL_PAUSE_S - 0.01) == 0
    assert scroll_steps(40, end + 2 * SCROLL_PAUSE_S + 1) == SCROLL_STEPS_PER_S


def test_about_three_characters_a_second():
    # A character is about 5 steps with its gap, in either look.
    assert 2.5 <= SCROLL_STEPS_PER_S / 5 <= 3.5
    assert SCROLL_FRAME_S * SCROLL_STEPS_PER_S == 1  # a step a frame
    assert (1 / SCROLL_FRAME_S).is_integer()  # frames divide a second evenly


def strip(width, height=4):
    """A strip whose pixel at x is x, so where it has got to can be read off."""
    image = Image.new("P", (width, height))
    image.putdata([x for _ in range(height) for x in range(width)])
    return image


def field(box=(10, 2, 30, 6), width=60, step=2, text="Long words"):
    return Scroll(box, strip(width), step, text)


def test_a_field_moves_in_whole_steps_and_stops_with_its_end_in_view():
    scroll = field()
    assert scroll.offset(0) == 0
    assert scroll.offset(SCROLL_PAUSE_S + 1) == 2 * SCROLL_STEPS_PER_S
    # 60 wide in a box of 20: 20 steps of 2, then it holds with its end in view.
    assert scroll.offset(SCROLL_PAUSE_S + 20 / SCROLL_STEPS_PER_S + 1) == 40
    assert all(scroll.offset(t / 16) % 2 == 0 for t in range(200))


def test_a_field_with_a_last_part_step_still_shows_its_end():
    scroll = field(width=61)
    assert scroll.offset(SCROLL_PAUSE_S + 21 / SCROLL_STEPS_PER_S + 1) == 41


def test_a_field_is_clipped_to_its_box():
    scroll = field()
    patch = scroll.patch(scroll.offset(SCROLL_PAUSE_S + 1))
    assert patch.size == (20, 4)
    assert patch.getpixel((0, 0)) == 32 and patch.getpixel((19, 0)) == 51


def test_a_frame_draws_each_field_only_inside_its_box():
    board = Image.new("P", (40, 8), 255)
    frame = frame_of(board, [field()])
    at = frame.at(SCROLL_PAUSE_S + 1)
    assert at.getpixel((10, 2)) == 32 and at.getpixel((29, 5)) == 51
    outside = [(9, 2), (30, 2), (10, 1), (10, 6), (0, 0), (39, 7)]
    assert all(at.getpixel(xy) == 255 for xy in outside)
    # The still image has each field at its start.
    assert frame.image.getpixel((10, 2)) == 0


def test_a_frame_without_fields_is_its_image():
    image = Image.new("P", (4, 4), 3)
    assert Frame(image).at(12.5).tobytes() == image.tobytes()


def test_the_scroller_keeps_a_field_going_across_redraws_and_starts_new_text_afresh():
    scroller = Scroller()
    board = Image.new("P", (40, 8), 255)
    scroller.show(Frame(board, (field(),)), 100)
    later = scroller.show(Frame(board, (field(),)), 100 + SCROLL_PAUSE_S + 1)
    assert later.getpixel((10, 2)) == 32  # carried on, not back to the start
    changed = scroller.show(Frame(board, (field(text="Other words"),)), 104)
    assert changed.getpixel((10, 2)) == 0  # new words from their beginning


def test_the_scroller_gives_only_fields_that_moved():
    scroller = Scroller()
    board = Image.new("P", (40, 8), 255)
    scroller.show(Frame(board, (field(), field(box=(0, 0, 8, 2), width=8, text="Fits"))), 0)
    assert scroller.moved(1) == []  # still holding at the start
    [(box, patch)] = scroller.moved(SCROLL_PAUSE_S + SCROLL_FRAME_S)
    assert box == (10, 2, 30, 6) and patch.getpixel((0, 0)) == 2
    assert scroller.moved(SCROLL_PAUSE_S + SCROLL_FRAME_S) == []


def test_the_scroller_starts_again_if_the_clock_goes_back():
    scroller = Scroller()
    board = Image.new("P", (40, 8), 255)
    scroller.show(Frame(board, (field(),)), 86399)
    image = scroller.show(Frame(board, (field(),)), 3)  # past midnight
    assert image.getpixel((10, 2)) == 0
    [(_, patch)] = scroller.moved(3 + SCROLL_PAUSE_S + 1)
    assert patch.getpixel((0, 0)) == 32


@pytest.mark.parametrize("scrolls, scrolling", [((), False), ((field(),), True)])
def test_the_scroller_says_whether_anything_scrolls(scrolls, scrolling):
    scroller = Scroller()
    scroller.show(Frame(Image.new("P", (40, 8)), scrolls), 0)
    assert scroller.scrolling is scrolling
