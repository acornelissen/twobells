"""Station announcements through an HDMI screen's speaker, with espeak-ng.

The words are core/announce.py's, as the dashboard's; the rules for when are
its wall tablet's (dashboard_static/pa.js, due()): the next train to depart
(by the dashboard's rule, Departure.can_be_next), NEXT_AHEAD before it goes,
the delay of a train late with no time to go by, NEXT_AHEAD before it was
due, and a cancellation or a platform alteration of a train due within
CHANGE_AHEAD, each said once, and nothing in a quiet window.
The board's departures carry no stops, coaches or reasons, so it says less
than the tablet: "The next train to depart from platform 1 will be the 08:12
Northern service to Manchester Piccadilly."

espeak-ng runs from an argument list, never a shell, with the words on its
standard input (printable only, and capped), and a time limit. It runs on a
thread of its own, so the screen's frame loop never waits for it. A failure
logs one line, and no more until it works again. E-paper boards don't speak.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from ..core import announce
from ..core.errors import safe_message
from ..core.model import Departure, Status, clock_at, real

log = logging.getLogger(__name__)

# How long before it goes the next train is announced, and how far ahead a
# cancellation or a platform alteration is: the wall tablet's.
NEXT_AHEAD = timedelta(minutes=4)
CHANGE_AHEAD = timedelta(minutes=60)

# What the next-train announcement says: the delay first, when there is one.
_LEAVING = (announce.DELAYED, announce.NEXT)

# espeak-ng, in a British English voice a little slower than its default,
# reading the words from its standard input: nothing said is ever an argument.
PROGRAM = "espeak-ng"
ARGS = (PROGRAM, "-v", "en-gb", "-s", "150", "--stdin")
# Far beyond any announcement; anything longer is cut.
MOST_CHARS = 1000
# The longest announcement takes under a minute to say.
TIME_LIMIT_S = 60
# Announcements waiting to be said; more than this are dropped.
MOST_WAITING = 8

# Which train a said announcement was for: (scheduled, operator, destination).
TrainId = tuple[str, str, str]
# What was said: (kind, train, platform), the platform for an alteration only.
Said = tuple[str, TrainId, str]
Run = Callable[..., subprocess.CompletedProcess]


def _train_id(dep: Departure) -> TrainId:
    return dep.scheduled, dep.service, dep.destination


def _train(dep: Departure) -> announce.Train:
    """What an announcement says of a departure: no stops, coaches or reasons,
    which the board's departures don't carry."""
    return announce.Train(time=dep.scheduled, destination=dep.destination, operator=dep.service, via=dep.via,
                          platform=dep.platform, replacement_bus=dep.replacement_bus, ferry=dep.ferry)


def _ahead(now: datetime, clock: str) -> timedelta:
    """How long from `now` until `clock` (HH:MM), within 12 hours either way,
    in the time that passes."""
    return real(clock_at(clock, now)) - real(now)


def _said_of(items: Sequence[tuple[str, str]], kinds: Sequence[str]) -> str:
    """The words of a train's announcements of `kinds`, as one text; "" for none."""
    return " ".join(words for kind, words in items if kind in kinds)


def due(trains: Sequence[Departure], now: datetime, said: set[Said], quiet: bool = False) -> list[str]:
    """What to say now of the board's departures (in its order), as the wall
    tablet's due() decides it. `said` holds what has been said, and is added
    to here, so each is said once: a cancellation, a platform alteration
    (again if the platform changes again), the next train's announcement,
    and the delay of a train late with no time to go by. Nothing when
    `quiet`."""
    if quiet:
        return []
    found = []

    def add(key: Said, text: str) -> None:
        if text and key not in said:
            said.add(key)
            found.append(text)

    next_found = False
    for dep in trains:
        if dep.mode != "train":
            continue
        cancelled = dep.status is Status.CANCELLED
        # As the dashboard decides it: the first that can be the next, still to go.
        is_next = not next_found and dep.can_be_next(now, now)
        next_found = next_found or is_next
        items = announce.announcements(
            _train(dep), is_cancelled=cancelled, late=dep.status is Status.LATE,
            minutes_late=dep.minutes_late_at(now) if dep.status is Status.LATE else None,
            platform_changed=dep.platform_changed, is_next=is_next)
        # Late with no time to go by: it could go any moment, so it stays.
        untimed = not cancelled and dep.status is Status.LATE and dep.expected is None
        ahead = _ahead(now, dep.scheduled if cancelled else dep.departs_at)
        if ahead < timedelta(0) and not untimed:
            continue  # gone
        train = _train_id(dep)
        if ahead <= CHANGE_AHEAD:
            add((announce.CANCELLED, train, ""), _said_of(items, [announce.CANCELLED]))
            add((announce.PLATFORM, train, dep.platform or ""), _said_of(items, [announce.PLATFORM]))
        if cancelled or ahead > NEXT_AHEAD:
            continue
        if untimed:  # never the next: it has no time to go by
            add((announce.DELAYED, train, ""), _said_of(items, [announce.DELAYED]))
        elif is_next:
            add((announce.NEXT, train, ""), _said_of(items, _LEAVING))
    return found


def forget(said: set[Said], trains: Sequence[Departure]) -> None:
    """Drops what was said of trains no longer on the board, so `said` stays small."""
    listed = {_train_id(dep) for dep in trains}
    for key in [key for key in said if key[1] not in listed]:
        said.discard(key)


def spoken(text: str) -> str:
    """The words as espeak-ng is given them: printable characters only, one
    line, at most MOST_CHARS (cut at a word)."""
    words = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if len(words) > MOST_CHARS:
        words = words[:MOST_CHARS].rsplit(" ", 1)[0]
    return words


class Speaker:
    """Says announcements through espeak-ng, one at a time, on a thread of
    its own started on the first one. `run` is subprocess.run, or a stand-in."""

    def __init__(self, run: Run = subprocess.run) -> None:
        self._run = run
        self._said: set[Said] = set()
        self._waiting: queue.Queue[str] = queue.Queue(MOST_WAITING)
        self._thread: threading.Thread | None = None
        self._failing = False

    def tick(self, trains: Sequence[Departure], now: datetime, quiet: bool) -> list[str]:
        """Says what is due now of the board's departures; returns it. Never
        raises: a fault here mustn't stop the board."""
        try:
            forget(self._said, trains)
            texts = due(trains, now, self._said, quiet)
        except Exception as error:  # noqa: BLE001 - the board goes on without announcements
            self._failed(f"Can't work out station announcements: {type(error).__name__}")
            return []
        for text in texts:
            self.say(text)
        return texts

    def say(self, text: str) -> None:
        """Queues `text` to be said, without waiting; dropped when too many wait."""
        words = spoken(text)
        if not words:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="twobells-speaker", daemon=True)
            self._thread.start()
        try:
            self._waiting.put_nowait(words)
        except queue.Full:
            log.debug("Too many station announcements waiting; one dropped")

    def _work(self) -> None:
        while True:
            words = self._waiting.get()
            try:
                self.speak(words)
            finally:
                self._waiting.task_done()

    def wait(self) -> None:
        """Until everything queued has been said: for tests."""
        self._waiting.join()

    def speak(self, words: str) -> bool:
        """Says `words` now, waiting until they have been said; whether they were."""
        try:
            done = self._run(list(ARGS), input=words, text=True, timeout=TIME_LIMIT_S, check=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            return self._failed(f"Can't make station announcements: {PROGRAM} isn't installed")
        except subprocess.TimeoutExpired:
            return self._failed(f"{PROGRAM} took longer than {TIME_LIMIT_S} s to make an announcement; stopped it")
        except OSError as error:
            return self._failed(f"Can't run {PROGRAM}: {safe_message(error)}")
        if done.returncode != 0:
            return self._failed(f"{PROGRAM} failed to make an announcement (exit {done.returncode}); "
                                "is there a speaker?")
        self._failing = False
        return True

    def _failed(self, message: str) -> bool:
        """Logs `message`, unless a failure has been logged since the last success."""
        if not self._failing:
            log.warning(message)
        self._failing = True
        return False
