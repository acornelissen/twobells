"""Turns departures into display rows. Meaning is always carried by text;
colour and inversion only reinforce it, so the board works for colour-blind
viewers and on a monochrome panel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable

from .model import Departure, Status, minutes_between
from .walk import WalkTimes, leave_by, leave_time


class Style(str, Enum):
    NORMAL = "normal"
    ALERT = "alert"
    INVERTED = "inverted"


@dataclass(frozen=True)
class Wording:
    """Status words for a theme."""

    on_time: str
    cancelled: str
    delayed: str
    unknown: str
    late: Callable[[Departure], str]
    leave: Callable[[str, str], str]  # (leave time, departure it is for)
    leave_soon: Callable[[str, str], str]  # ("now" or "in N min", departure it is for)


def _target(dep: Departure) -> str:
    # The time it actually leaves, so a late train never reads as leaving before you do.
    return dep.departs_at if dep.mode == "train" else dep.service


PLAIN = Wording(
    "On time", "Cancelled", "Delayed", "No info",
    late=lambda dep: f"+{dep.minutes_late}",
    leave=lambda leave, target: f"Leave {leave} ({target})",
    leave_soon=lambda when, target: f"Leave {when} ({target})",
)
SIGN = Wording(
    "On time", "Cancelled", "Delayed", "No info",
    late=lambda dep: f"Exp {dep.expected}",
    # Drawn under the train it refers to, so the time alone is enough.
    leave=lambda leave, target: f"Leave by {leave}",
    leave_soon=lambda when, target: f"Leave {when}",
)


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

    def signature(self) -> str:
        content = asdict(self)
        content.pop("updated")
        encoded = json.dumps(content, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()


def _until(clock: str, leave: str) -> str:
    """'now' or 'in N min' to a leave time, rounded down so it never promises more time than there is."""
    seconds = minutes_between(clock[:5], leave) * 60 - int(clock[6:8])
    minutes = max(0, seconds // 60)
    return "now" if minutes == 0 else f"in {minutes} min"


def _train_row(dep: Departure, walk: WalkTimes | None, words: Wording, clock: str | None) -> Row:
    if dep.status is Status.CANCELLED:
        right, style = words.cancelled, Style.INVERTED
    elif dep.status is Status.LATE:
        right, style = (words.late(dep) if dep.expected else words.delayed), Style.ALERT
    elif dep.status is Status.ON_TIME:
        right, style = words.on_time, Style.NORMAL
    else:
        right, style = words.unknown, Style.NORMAL
    platform = f"Plat {dep.platform}" if dep.platform else None
    return Row(dep.scheduled, dep.destination, right, style, platform=platform)


def _bus_row(dep: Departure, walk: WalkTimes | None, words: Wording, clock: str | None) -> Row:
    if dep.status is Status.CANCELLED:
        right, style = words.cancelled, Style.INVERTED
    elif dep.status is Status.LATE:
        right, style = f"{dep.departs_at} +{dep.minutes_late}", Style.ALERT
    elif dep.status is Status.UNKNOWN:
        right, style = f"{dep.scheduled} sch", Style.NORMAL
    else:
        right, style = dep.scheduled, Style.NORMAL
    leave = leave_time(dep, walk) if walk else None
    if leave and clock:
        # Past its leave minute the bus can't be reached on foot: say nothing rather than "now".
        leave = _until(clock, leave) if minutes_between(clock[:5], leave) >= 0 else None
    return Row(dep.service, dep.destination, right, style, f"leave {leave}" if leave else None, dep.stop)


def _leave(departures, walk: WalkTimes | None, now: str, words: Wording, clock: str | None):
    """(text, row index) for the next departure in reach, or (None, None)."""
    found = leave_by(departures, walk, now) if walk else None
    if found is None:
        return None, None
    leave, dep = found
    if clock:
        return words.leave_soon(_until(clock, leave), _target(dep)), departures.index(dep)
    return words.leave(leave, _target(dep)), departures.index(dep)


def _section(label, departures, to_row, now, empty_text, stale, walk, words, clock) -> Section:
    upcoming = [d for d in departures if not d.is_gone(now)]
    rows = tuple(to_row(d, walk, words, clock) for d in upcoming)
    # Rows that carry their own leave time make a header summary redundant.
    if any(r.leave for r in rows):
        text, index = None, None
    else:
        text, index = _leave(upcoming, walk, now, words, clock)
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
) -> BoardView:
    """`clock` (HH:MM:SS, matching `now`) is for screens: it is shown, and
    leave times count down instead of giving a clock time. `notices` are the
    station's disruption messages, shown one after another as one notice."""
    return BoardView(
        title=title,
        trains=_section("Trains", trains, _train_row, now, "No trains", "train" in stale, walk, wording, clock),
        buses=_section("Buses", buses, _bus_row, now, "No buses", "bus" in stale, walk, wording, clock),
        updated=now,
        clock=clock,
        notice=_notice(notices or []),
    )


# Long enough for a message or two; a screen shows it in turns, e-paper cuts it short.
NOTICE_LIMIT = 300


def _notice(messages: list[str]) -> str | None:
    text = " / ".join(m for m in messages if m)
    if len(text) <= NOTICE_LIMIT:
        return text or None
    return text[: NOTICE_LIMIT - 1].rstrip() + "…"
