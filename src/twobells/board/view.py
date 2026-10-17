"""Turns departures into display rows. Meaning is always carried by text;
colour and inversion only reinforce it, so the board works for colour-blind
viewers and on a monochrome panel."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from functools import partial

from ..core import words
from ..core.model import Departure, Status, Strip, minutes_between, strip_key
from ..core.walk import WalkTimes, leave_by, leave_time
from ..settings import show as show_items


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
    # What a screen wide enough calls the stop, in place of its label
    # ("Henry Street", or "Henry Street A": see stop_names_shown); None when
    # the name isn't known, and then it shows the label.
    stop_name: str | None = None
    # "Plat 1", or "Plat 2 (changed)"; only boards wide enough for a platform column draw it.
    platform: str | None = None
    # The platform isn't the one the train was given before: the words say
    # so, and a look with an accent colour draws them in it as well.
    platform_changed: bool = False
    # "via Guide Bridge", after the destination on a board wide enough.
    via: str | None = None
    # The leave time alone ("07:16") when `leave` is a clock time, for a look
    # that says words.LEAVE_BY once, over the column, rather than on every row.
    leave_at: str | None = None
    # Where its train or bus is; only screens wide enough draw it.
    strip: Strip | None = None
    # A rail replacement bus in the trains: its platform is words.BUS, and a
    # board with no platform column says so before the destination.
    replacement_bus: bool = False
    # A ferry in the trains: the same, with words.FERRY.
    ferry: bool = False
    # The train's "Calling at" line (core/calling.py), on a screen only: it
    # scrolls the first train's under its row. E-paper can't scroll it.
    calling: str | None = None


def destination_text(row: Row, platform_column: bool, wide: bool = False) -> str:
    """A train row's destination as drawn: after "Bus: " for a replacement bus
    (or "Ferry: " for a ferry) on a board with no platform column to say so
    in, and followed by its via words on a board `wide` enough for a
    platform column."""
    text = row.middle
    if wide and row.via:
        text = f"{text} {row.via}"
    if row.replacement_bus and not platform_column:
        return f"{words.BUS}: {text}"
    if row.ferry and not platform_column:
        return f"{words.FERRY}: {text}"
    return text


@dataclass(frozen=True)
class Section:
    label: str
    rows: tuple[Row, ...]
    empty_text: str
    stale: bool = False
    leave: str | None = None
    leave_for: int | None = None  # index of the row the leave time is for
    # Every name a screen wide enough calls your stops by, shown or not, so
    # the column they are drawn in is as wide whatever buses are shown.
    stop_names: tuple[str, ...] = ()


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
    # "Last bus 23:05" on a screen in the hour before the last bus of all goes.
    last_bus: str | None = None
    # On a screen, whether text too long for its space scrolls (the
    # scrolling setting); off, it is cut short with "…", as on e-paper.
    scroll: bool = True

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


def _train_row(dep: Departure, walk: WalkTimes | None, clock: str | None, platforms: bool = True,
               calling: bool = True) -> Row:
    """A train's row. Without `platforms` it has no platform, via words or
    changed mark (a replacement bus or a ferry still says what it is, before
    its destination); without `calling`, no "Calling at" line."""
    changed = platforms and dep.platform_changed and dep.is_train and dep.platform is not None
    if not platforms:
        platform = None
    elif dep.replacement_bus:
        platform = words.BUS
    elif dep.ferry:
        platform = words.FERRY
    else:
        platform = words.platform(dep.platform, changed) if dep.platform else None
    return Row(dep.scheduled, dep.destination, words.status(dep), _style(dep), platform=platform,
               platform_changed=changed, via=dep.via if platforms else None, replacement_bus=dep.replacement_bus,
               ferry=dep.ferry,
               calling=_cut(dep.calling_at, CALLING_LIMIT) if calling and clock and dep.calling_at else None)


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
    # A countdown ("Leave in 4 min", "Leave now") only reads whole, so a screen has no leave_at.
    leave_at = leave if leave and not clock else None
    return Row(dep.service, dep.destination, right, _style(dep), leave_text, dep.stop, leave_at=leave_at)


# A stop's name from the bus timetable is feed data: kept to one line of
# printable text, and this long at most (a screen scrolls a long one).
STOP_NAME_LIMIT = 60


def _stop_name(name: object) -> str | None:
    if not isinstance(name, str):
        return None
    # Whitespace first, so a tab or newline still parts two words.
    text = "".join(c for c in " ".join(name.split()) if c.isprintable())
    return text[:STOP_NAME_LIMIT].rstrip() or None


def stop_names_shown(names: Mapping[str, object]) -> dict[str, str]:
    """What a wide screen's bus rows call each of your stops, by label, from
    the bus timetable's names: the name alone ("Henry Street"), or the name
    and the label ("Henry Street A") when two or more of your stops share it,
    so each row still says which. A stop whose name isn't known is left out:
    its rows show the label."""
    cleaned = {label: name for label, raw in names.items() if isinstance(label, str) and (name := _stop_name(raw))}
    counts = Counter(name.casefold() for name in cleaned.values())
    return {label: f"{name} {label}" if counts[name.casefold()] > 1 else name for label, name in cleaned.items()}


def _leave(departures, walk: WalkTimes | None, now: str, wording: Wording, clock: str | None):
    """(text, row index) for the next departure in reach, or (None, None)."""
    found = leave_by(departures, walk, now) if walk else None
    if found is None:
        return None, None
    leave, dep = found
    return wording.leave(_leave_words(leave, clock), _target(dep)), departures.index(dep)


def _section(label, departures, to_row, now, empty_text, stale, walk, wording, clock, strips,
             stop_names: Mapping[str, str]) -> Section:
    upcoming = [d for d in departures if not d.is_gone(now)]
    rows = tuple(
        replace(to_row(d, walk, clock), strip=strips.get(strip_key(d)), stop_name=stop_names.get(d.stop or ""))
        for d in upcoming
    )
    # Rows that carry their own leave time make a header summary redundant.
    if any(r.leave for r in rows):
        text, index = None, None
    else:
        text, index = _leave(upcoming, walk, now, wording, clock)
    return Section(label, rows, empty_text, stale, text, index, tuple(sorted(set(stop_names.values()))))


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
    strips: Mapping[tuple[str, str, str, str], Strip] | None = None,
    stop_names: Mapping[str, str] | None = None,
    last_bus: str | None = None,
    show: Collection[str] | None = None,
    scroll: bool = True,
) -> BoardView:
    """`clock` (HH:MM:SS, matching `now`) is for screens: it is shown, leave
    times count down instead of giving a clock time, and trains have their
    "Calling at" lines. `notices` are the
    station's disruption messages, shown one after another as one notice;
    `rain` is the rain warning, if there is one. `strips` say where each
    departure's train or bus is, by strip_key. `stop_names` are what a wide
    screen calls your bus stops, by label (see stop_names_shown). `last_bus`
    is when the last bus goes, for a screen in its last hour. Without
    `scroll`, a screen cuts text too long for its space short, as e-paper does.

    `show` is what the board shows of what it can leave out (settings/show.py),
    None for all of it. What it leaves out is left out of the view, so no look
    draws it or keeps room for it: platforms (with their via words and
    changed marks), where each train and bus is, the "Calling at" line, the
    disruption notice, the rain warning and the last bus."""
    shown = set(show_items.keys("board") if show is None else show)
    strips = (strips or {}) if "where" in shown else {}
    to_train = partial(_train_row, platforms="platforms" in shown, calling="calling_at" in shown)
    return BoardView(
        title=title,
        trains=_section("Trains", trains, to_train, now, "No trains", "train" in stale, walk, wording, clock,
                        strips, {}),
        buses=_section("Buses", buses, _bus_row, now, "No buses", "bus" in stale, walk, wording, clock, strips,
                       stop_names or {}),
        updated=now,
        clock=clock,
        notice=_notice(notices or []) if "disruption" in shown else None,
        rain=rain if "rain" in shown else None,
        last_bus=last_bus if "last_bus" in shown else None,
        scroll=scroll,
    )


# Long enough for a message or two; a screen scrolls it, e-paper cuts it short.
NOTICE_LIMIT = 300


# A long journey's "Calling at" line is several hundred letters; a hand-edited
# saved state could hold far more, and its scrolling strip would be huge.
CALLING_LIMIT = 1000


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _notice(messages: list[str]) -> str | None:
    return _cut(" / ".join(m for m in messages if m), NOTICE_LIMIT) or None
