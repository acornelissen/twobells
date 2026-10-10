"""Turns departures into display rows. Meaning is always carried by text;
colour and inversion only reinforce it, so the board works for colour-blind
viewers and on a monochrome panel."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum

from ..core import words
from ..core.model import Departure, Status, minutes_between
from ..core.walk import WalkTimes, leave_by, leave_time


class Style(str, Enum):
    NORMAL = "normal"
    ALERT = "alert"
    INVERTED = "inverted"


@dataclass(frozen=True)
class Wording:
    """How a look says when to leave for a train. The words themselves are
    core.words, the same in every look and on the dashboard."""

    leave: Callable[[str, str], str]  # ("Leave by 07:05", departure time it is for)


def _target(dep: Departure) -> str:
    # The time it actually leaves, so a late train never reads as leaving before you do.
    return dep.departs_at if dep.mode == "train" else dep.service


# Drawn beside the section's label, so it names the train it is for.
PLAIN = Wording(leave=lambda leave, target: f"{leave} ({target})")
# Drawn under the train it refers to, so the leave words alone are enough.
SIGN = Wording(leave=lambda leave, target: leave)


@dataclass(frozen=True)
class Row:
    left: str
    middle: str
    right: str
    style: Style = Style.NORMAL
    leave: str | None = None
    stop: str | None = None
    # "Plat 1"; only boards wide enough for a platform column draw it.
    platform: str | None = None


@dataclass(frozen=True)
class Section:
    label: str
    rows: tuple[Row, ...]
    empty_text: str
    stale: bool = False
    leave: str | None = None
    leave_for: int | None = None  # index of the row the leave time is for


@dataclass(frozen=True)
class BoardView:
    title: str
    trains: Section
    buses: Section
    updated: str = field(compare=False)
    # A running HH:MM:SS clock, for screens that can redraw every second.
    clock: str | None = None
    # A disruption message for the station, in plain text, while there is one.
    notice: str | None = None
    # "Rain from 07:10 (70%)" when rain is likely before you would reach your train or bus.
    rain: str | None = None

    def signature(self) -> str:
        content = asdict(self)
        content.pop("updated")
        encoded = json.dumps(content, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class CodeView:
    """The page shown instead of departures while a settings reset code waits
    to be read off the board."""

    code: str  # "123 456"
    until: str  # "23:52", local time
    expires: float  # when, as a Unix time: tells one code from the next

    def signature(self) -> str:
        # Not the code: the signature is saved in the state file, and a hash of
        # six digits would give it away. Each code has its own expiry.
        return hashlib.sha256(json.dumps(["code page", self.until, self.expires]).encode()).hexdigest()


SETUP_HEADING = "Set me up"
SETUP_NOTE = "On your home network, open"


@dataclass(frozen=True)
class SetupView:
    """The page shown instead of departures until the board is set up: the
    settings site's address, where the setup wizard is. With another
    heading and note, the same page says the board view is off."""

    url: str  # "http://raspberrypi.local:8090/settings"
    heading: str = SETUP_HEADING
    note: str = SETUP_NOTE

    @property
    def shown(self) -> str:
        """The address as the board draws it: without http://, which browsers
        add themselves, so a long name stays readable on a small panel."""
        return self.url.removeprefix("http://")

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(["setup page", self.url, self.heading, self.note]).encode()).hexdigest()


def _until(clock: str, leave: str) -> int:
    """Whole minutes to a leave time, rounded down so it never promises more time than there is."""
    seconds = minutes_between(clock[:5], leave) * 60 - int(clock[6:8])
    return max(0, seconds // 60)


def _leave_words(leave: str, clock: str | None) -> str:
    """When to leave: counting down on a screen ("Leave in 4 min"), a clock time on e-paper ("Leave by 07:05")."""
    return words.leave_in(_until(clock, leave)) if clock else words.leave_by(leave)


def _style(dep: Departure) -> Style:
    if dep.status is Status.CANCELLED:
        return Style.INVERTED
    return Style.ALERT if dep.status is Status.LATE else Style.NORMAL


def _train_row(dep: Departure, walk: WalkTimes | None, clock: str | None) -> Row:
    platform = f"Plat {dep.platform}" if dep.platform else None
    return Row(dep.scheduled, dep.destination, words.status(dep), _style(dep), platform=platform)


def _bus_row(dep: Departure, walk: WalkTimes | None, clock: str | None) -> Row:
    # A bus running to time shows just its time.
    if dep.status is Status.ON_TIME:
        right = dep.scheduled
    elif dep.status is Status.UNKNOWN:
        right = f"{dep.scheduled} {words.SCHEDULED}"
    else:
        right = words.status(dep)
    leave = leave_time(dep, walk) if walk else None
    # Past its leave minute the bus can't be reached on foot: on a screen, say
    # nothing rather than "Leave now". E-paper keeps the clock time.
    if leave and clock and minutes_between(clock[:5], leave) < 0:
        leave = None
    leave_text = _leave_words(leave, clock) if leave else None
    return Row(dep.service, dep.destination, right, _style(dep), leave_text, dep.stop)


def _leave(departures, walk: WalkTimes | None, now: str, wording: Wording, clock: str | None):
    """(text, row index) for the next departure in reach, or (None, None)."""
    found = leave_by(departures, walk, now) if walk else None
    if found is None:
        return None, None
    leave, dep = found
    return wording.leave(_leave_words(leave, clock), _target(dep)), departures.index(dep)


def _section(label, departures, to_row, now, empty_text, stale, walk, wording, clock) -> Section:
    upcoming = [d for d in departures if not d.is_gone(now)]
    rows = tuple(to_row(d, walk, clock) for d in upcoming)
    # Rows that carry their own leave time make a header summary redundant.
    if any(r.leave for r in rows):
        text, index = None, None
    else:
        text, index = _leave(upcoming, walk, now, wording, clock)
    return Section(label, rows, empty_text, stale, text, index)


def build_view(
    title: str,
    trains: list[Departure],
    buses: list[Departure],
    now: str,
    stale: set[str],
    walk: WalkTimes | None = None,
    wording: Wording = PLAIN,
    clock: str | None = None,
    notices: list[str] | None = None,
    rain: str | None = None,
) -> BoardView:
    """`clock` (HH:MM:SS, matching `now`) is for screens: it is shown, and
    leave times count down instead of giving a clock time. `notices` are the
    station's disruption messages, shown one after another as one notice;
    `rain` is the rain warning, if there is one."""
    return BoardView(
        title=title,
        trains=_section("Trains", trains, _train_row, now, "No trains", "train" in stale, walk, wording, clock),
        buses=_section("Buses", buses, _bus_row, now, "No buses", "bus" in stale, walk, wording, clock),
        updated=now,
        clock=clock,
        notice=_notice(notices or []),
        rain=rain,
    )


# Long enough for a message or two; a screen shows it in turns, e-paper cuts it short.
NOTICE_LIMIT = 300


def _notice(messages: list[str]) -> str | None:
    text = " / ".join(m for m in messages if m)
    if len(text) <= NOTICE_LIMIT:
        return text or None
    return text[: NOTICE_LIMIT - 1].rstrip() + "…"
