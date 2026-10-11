import pytest

from twobells.core import words
from twobells.core.model import Departure, Status


@pytest.mark.parametrize("status, expected, said", [
    (Status.ON_TIME, None, "On time"),
    (Status.LATE, "17:54", "Exp 17:54"),
    (Status.LATE, None, "Delayed"),
    (Status.CANCELLED, None, "Cancelled"),
    (Status.UNKNOWN, None, "No report"),
])
def test_status_words_are_the_station_board_s(status, expected, said):
    assert words.status(Departure("train", "Northern", "Manchester Piccadilly", "17:47", expected, status)) == said


def test_expected_in_full_for_screen_readers():
    assert words.expected_spoken("17:54") == "Expected 17:54"


def test_leave_words():
    assert words.leave_by("17:40") == "Leave by 17:40"
    assert words.leave_in(4) == "Leave in 4 min"
    assert words.leave_in(0) == "Leave now"
