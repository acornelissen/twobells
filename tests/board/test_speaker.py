"""Station announcements through a screen's speaker: when, what, and how espeak-ng is run."""

import logging
import subprocess
import threading
from datetime import datetime, timedelta

from twobells.board.speaker import (
    ARGS,
    MOST_CHARS,
    TIME_LIMIT_S,
    Speaker,
    due,
    forget,
    spoken,
)
from twobells.core.model import Departure, Status

NOW = datetime(2026, 10, 8, 7, 0, 30)


def train(scheduled="07:04", expected=None, status=Status.ON_TIME, platform="1", changed=False,
          destination="Manchester Piccadilly", mode="train"):
    if status is Status.ON_TIME and expected is None:
        expected = scheduled
    return Departure(mode, "Northern", destination, scheduled, expected, status, platform=platform,
                     platform_changed=changed)


NEXT_0704 = ("The next train to depart from platform 1 will be the 07:04 Northern service to "
             "Manchester Piccadilly.")


# --- When: the wall tablet's rules.

def test_the_next_train_is_announced_4_minutes_before_it_goes_and_once():
    said = set()
    assert due([train("07:05")], NOW, said) == []  # 4.5 minutes off
    assert due([train("07:04")], NOW, said) == [NEXT_0704]  # 3.5 minutes off
    assert due([train("07:04")], NOW + timedelta(minutes=1), said) == []  # once


def test_only_the_next_train_is_announced_not_a_later_one():
    said = set()
    assert due([train("07:02"), train("07:03")], NOW, said) == [
        "The next train to depart from platform 1 will be the 07:02 Northern service to Manchester Piccadilly."]


def test_a_late_train_is_announced_by_when_it_goes_with_its_delay_first():
    late = train("06:55", "07:03", Status.LATE)
    assert due([late], NOW, set()) == [
        "We are sorry to announce that the 06:55 Northern service to Manchester Piccadilly is delayed by "
        "approximately 8 minutes. The next train to depart from platform 1 will be the delayed 06:55 Northern "
        "service to Manchester Piccadilly."]
    assert due([train("07:00", "07:20", Status.LATE)], NOW, set()) == []  # not yet


def test_a_cancellation_is_announced_within_the_hour_once_and_the_next_train_is_the_one_after():
    said = set()
    gone = train("07:02", None, Status.CANCELLED)
    assert due([gone, train("07:03")], NOW, said) == [
        "We are sorry to announce that the 07:02 Northern service to Manchester Piccadilly has been cancelled.",
        "The next train to depart from platform 1 will be the 07:03 Northern service to Manchester Piccadilly."]
    assert due([gone, train("07:03")], NOW, said) == []
    assert due([train("08:10", None, Status.CANCELLED)], NOW, set()) == []  # more than an hour off


def test_a_platform_alteration_is_announced_once_and_again_if_it_changes_again():
    said = set()
    moved = train("07:40", platform="2", changed=True)
    words = "Platform alteration: the 07:40 Northern service to Manchester Piccadilly will now depart from platform 2."
    assert due([moved], NOW, said) == [words]
    assert due([moved], NOW, said) == []
    again = train("07:40", platform="3", changed=True)
    assert due([again], NOW, said) == [words.replace("platform 2", "platform 3")]


def test_a_late_train_with_no_time_stays_but_is_never_the_next():
    said = set()
    untimed = train("06:50", None, Status.LATE, platform="2", changed=True)
    found = due([untimed, train("07:03")], NOW, said)
    assert found[0].startswith("Platform alteration: the 06:50")  # still to go, so its alteration is said
    # Its delay is said by itself, as the wall tablet does; the next train is the 07:03, worded as next.
    assert found[1:] == ["We are sorry to announce that the 06:50 Northern service to Manchester Piccadilly "
                         "is delayed.",
                         "The next train to depart from platform 1 will be the 07:03 Northern service to "
                         "Manchester Piccadilly."]
    assert due([untimed, train("07:03")], NOW, said) == []  # once


def test_an_untimed_delay_is_said_a_few_minutes_before_it_was_due_not_sooner():
    untimed = train("07:10", None, Status.LATE)
    assert due([untimed], NOW, set()) == []  # 9.5 minutes off
    assert due([untimed], NOW + timedelta(minutes=6), set()) == [
        "We are sorry to announce that the 07:10 Northern service to Manchester Piccadilly is delayed."]


def test_a_train_gone_but_still_listed_is_not_the_next():
    # The 06:59 has gone by 07:00:30 though the board still lists it: the 07:03 is the next.
    assert due([train("06:59"), train("07:03")], NOW, set()) == [
        "The next train to depart from platform 1 will be the 07:03 Northern service to Manchester Piccadilly."]


def test_gone_trains_and_buses_are_never_announced():
    assert due([train("06:59")], NOW, set()) == []
    assert due([train("07:02", mode="bus")], NOW, set()) == []


def test_nothing_is_said_in_a_quiet_window_nor_marked_said():
    said = set()
    assert due([train("07:04")], NOW, said, quiet=True) == []
    assert said == set()
    assert due([train("07:04")], NOW, said) == [NEXT_0704]


def test_trains_no_longer_on_the_board_are_forgotten():
    said = set()
    due([train("07:04"), train("07:30", platform="2", changed=True)], NOW, said)
    forget(said, [train("07:30", platform="2", changed=True)])
    assert {key[1][0] for key in said} == {"07:30"}


# --- What: the words espeak-ng is given.

def test_the_words_are_printable_on_one_line_and_capped():
    assert spoken("Platform\n2\x00 \x1b[31mnow‮ ") == "Platform 2 [31mnow"
    long = spoken("word " * 1000)
    assert len(long) <= MOST_CHARS and long.endswith("word")
    assert spoken("\n\t") == ""


# --- How: espeak-ng from an argument list, never a shell.

class FakeRun:
    def __init__(self, result=0, error=None):
        self.calls = []
        self.result, self.error = result, error

    def __call__(self, args, **options):
        self.calls.append((args, options))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(args, self.result)


def test_espeak_ng_is_run_from_a_fixed_argument_list_with_the_words_on_its_input():
    run = FakeRun()
    assert Speaker(run).speak("Mind the gap; rm -rf /")
    [(args, options)] = run.calls
    assert args == list(ARGS) == ["espeak-ng", "-v", "en-gb", "-s", "150", "--stdin"]
    assert options["input"] == "Mind the gap; rm -rf /" and options["text"] is True
    assert options["timeout"] == TIME_LIMIT_S and options["check"] is False
    assert "shell" not in options and "Mind" not in " ".join(args)


def test_a_failure_logs_one_line_until_it_works_again(caplog):
    caplog.set_level(logging.WARNING, logger="twobells.board.speaker")
    run = FakeRun(error=FileNotFoundError("espeak-ng"))
    speaker = Speaker(run)
    assert not speaker.speak("one") and not speaker.speak("two")
    assert [r.getMessage() for r in caplog.records] == [
        "Can't make station announcements: espeak-ng isn't installed"]
    run.error = None
    assert speaker.speak("three")
    run.error = subprocess.TimeoutExpired(list(ARGS), TIME_LIMIT_S)
    assert not speaker.speak("four") and not speaker.speak("five")
    run.error, run.result = None, 1
    assert not speaker.speak("six")  # still failing: no new line
    assert len(caplog.records) == 2 and "took longer than 60 s" in caplog.records[1].getMessage()


def test_saying_never_waits_for_espeak_ng():
    started, release = threading.Event(), threading.Event()

    def slow(args, **options):
        started.set()
        release.wait(5)
        return subprocess.CompletedProcess(args, 0)

    speaker = Speaker(slow)
    assert speaker.tick([train("07:04")], NOW, quiet=False) == [NEXT_0704]  # returns while it is said
    assert started.wait(5)
    release.set()
    speaker.wait()


def test_a_tick_says_each_announcement_once_and_nothing_when_quiet():
    run = FakeRun()
    speaker = Speaker(run)
    assert speaker.tick([train("07:04")], NOW, quiet=True) == []
    speaker.tick([train("07:04")], NOW, quiet=False)
    speaker.tick([train("07:04")], NOW + timedelta(seconds=1), quiet=False)
    speaker.wait()
    assert [options["input"] for _, options in run.calls] == [NEXT_0704]
