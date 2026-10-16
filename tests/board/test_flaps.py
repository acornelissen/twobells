from itertools import pairwise

import pytest
from PIL import Image

from twobells.board.flaps import DRUM, FLAP_FRAME_S, FLAP_STEPS, Flap, Flapper, flap_frames
from twobells.board.scroll import SCROLL_FRAME_S

# --- What the tiles show as they turn ---


def test_the_same_text_doesnt_turn():
    assert flap_frames("07:12", "07:12") == []


@pytest.mark.parametrize(("old", "new"), [
    ("07:12", "07:42"), ("On time", "Exp 07:49"), ("Exp 07:49", "Cancelled"), ("Hyde", "Manchester Piccadilly"),
    ("Manchester Piccadilly", "Hyde"), ("", "Glossop"), ("Glossop", "Glössop"), ("12 min", "11 min"),
])
def test_a_change_ends_on_the_new_text_within_the_most_steps(old, new):
    frames = flap_frames(old, new)
    assert 1 <= len(frames) <= FLAP_STEPS
    assert frames[-1] == new
    assert all(len(frame) == len(new) for frame in frames)


def test_only_the_letters_that_change_turn():
    frames = flap_frames("07:12", "07:42")
    assert all(frame[:3] == "07:" and frame[4] == "2" for frame in frames)
    assert len({frame[3] for frame in frames}) == len(frames)  # a new letter every frame


def test_a_tile_turns_forward_round_the_drum_and_then_stays():
    frames = flap_frames("A", "D")
    assert frames == ["B", "C", "D"]  # one letter a frame when it is that near
    far = [DRUM.index(frame) for frame in ["1", *flap_frames("1", "0")]]  # nearly all the way round
    assert len(far) == FLAP_STEPS + 1
    assert all(0 < (b - a) % len(DRUM) < len(DRUM) / 2 for a, b in pairwise(far))  # never back
    settled = flap_frames("07:12", "08:13")
    for position in range(5):
        column = [frame[position] for frame in settled]
        first = column.index("08:13"[position])
        assert set(column[first:]) == {"08:13"[position]}  # once a tile reaches its letter it stays


def test_lower_case_letters_turn_through_lower_case():
    frames = flap_frames("Hyde", "Hope")
    assert all(frame[0] == "H" for frame in frames)
    assert all(frame[1:].islower() for frame in frames[:-1] if frame[1:].strip())


def test_a_letter_off_the_drum_turns_every_step_then_lands():
    frames = flap_frames("o", "ö")
    assert len(frames) == FLAP_STEPS and frames[-1] == "ö"


def test_a_status_lines_up_from_its_end_and_a_destination_from_its_start():
    # "On time" right aligned under "Exp 07:49": the "me" of "time" turns into ":49".
    right = flap_frames("On time", "Exp 07:49", right=True)
    assert all(frame[-1] != "e" for frame in right[1:])
    left = flap_frames("Hyde", "Hyde Bus Station")
    assert all(frame.startswith("Hyde") for frame in left)  # the same start stays put


def test_a_change_takes_under_a_second_at_the_screens_frame_rate():
    assert FLAP_FRAME_S == SCROLL_FRAME_S  # one step a frame of the screen loop
    assert FLAP_STEPS * FLAP_FRAME_S < 1


# --- A screen's fields turning between redraws ---


def painted(text):
    """A patch that says which frame it shows: its text, one letter a pixel."""
    image = Image.new("P", (len(text), 1))
    image.putdata([ord(letter) % 256 for letter in text])
    return image


def flap(text, key="time", box=(10, 20, 15, 30), right=False):
    return Flap(key, box, text, painted, right)


def shows(patches):
    return ["".join(chr(v) for v in patch.tobytes()) for _, patch in patches]


def test_a_first_redraw_shows_the_text_as_it_is():
    flapper = Flapper()
    assert flapper.show((flap("07:12"),), 100) == []
    assert not flapper.flapping


def test_a_changed_field_turns_a_frame_at_a_time_then_stops():
    flapper = Flapper()
    flapper.show((flap("07:12"),), 100)
    expected = flap_frames("07:12", "07:42")
    first = flapper.show((flap("07:42"),), 101)
    assert flapper.flapping and shows(first) == expected[:1]
    seen = []
    for k in range(1, len(expected)):
        patches = flapper.moved(101 + k * FLAP_FRAME_S)
        assert [box for box, _ in patches] == [(10, 20, 15, 30)]
        seen += shows(patches)
    assert seen == expected[1:] and seen[-1] == "07:42"
    assert not flapper.flapping
    assert flapper.moved(102) == []


def test_frames_follow_the_boards_clock_skipping_any_missed():
    flapper = Flapper()
    flapper.show((flap("A"),), 0)
    flapper.show((flap("H"),), 10)
    assert flapper.moved(10) == []  # the first frame is already drawn
    assert shows(flapper.moved(10 + 3 * FLAP_FRAME_S)) == ["E"]  # C and D went by unseen
    assert shows(flapper.moved(10 + 20 * FLAP_FRAME_S)) == ["H"]  # late: straight to the end
    assert not flapper.flapping


def test_a_redraw_mid_turn_carries_on_where_it_was():
    flapper = Flapper()
    flapper.show((flap("A"),), 0)
    flapper.show((flap("H"),), 10)
    flapper.moved(10 + 2 * FLAP_FRAME_S)
    # The clock ticks: the whole board is drawn again, with the field's new text.
    assert shows(flapper.show((flap("H"),), 10 + 3 * FLAP_FRAME_S)) == ["E"]
    assert flapper.flapping


def test_only_the_changed_field_turns():
    flapper = Flapper()
    flapper.show((flap("07:12", "time"), flap("Hyde", "destination", (40, 20, 70, 30))), 0)
    patches = flapper.show((flap("07:12", "time"), flap("Hope", "destination", (40, 20, 70, 30))), 1)
    assert [box for box, _ in patches] == [(40, 20, 70, 30)]


def test_a_field_that_comes_or_goes_doesnt_turn():
    flapper = Flapper()
    flapper.show((flap("07:12", "time"),), 0)
    assert flapper.show((flap("Hyde", "destination"),), 1) == []
    assert flapper.show((), 2) == [] and not flapper.flapping


def test_a_clock_that_goes_back_starts_the_turn_again():
    flapper = Flapper()
    flapper.show((flap("A"),), 0)
    flapper.show((flap("H"),), 86399)
    assert flapper.moved(0) == []  # midnight: the first frame again, which is showing
    assert shows(flapper.moved(FLAP_FRAME_S)) == ["C"] and flapper.flapping
