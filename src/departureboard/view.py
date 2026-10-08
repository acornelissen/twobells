"""Turns departures into display rows. Meaning is always carried by text;
colour and inversion only reinforce it, so the board works for colour-blind
viewers and on a monochrome panel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum

from .model import Departure, Status
from .walk import WalkTimes, leave_by


class Style(str, Enum):
    NORMAL = "normal"
    ALERT = "alert"
    INVERTED = "inverted"


@dataclass(frozen=True)
class Row:
    left: str
    middle: str
    right: str
    style: Style = Style.NORMAL


@dataclass(frozen=True)
class Section:
    label: str
    rows: tuple[Row, ...]
    empty_text: str
    stale: bool = False
    leave: str | None = None


@dataclass(frozen=True)
class BoardView:
    title: str
    trains: Section
    buses: Section
    updated: str = field(compare=False)

    def signature(self) -> str:
        content = asdict(self)
        content.pop("updated")
        encoded = json.dumps(content, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()


def _train_row(dep: Departure) -> Row:
    if dep.status is Status.CANCELLED:
        right, style = "CANC", Style.INVERTED
    elif dep.status is Status.LATE:
        late = dep.minutes_late
        right, style = (f"+{late}" if late else "Delayed"), Style.ALERT
    elif dep.status is Status.ON_TIME:
        right, style = "On time", Style.NORMAL
    else:
        right, style = "?", Style.NORMAL
    return Row(dep.scheduled, dep.destination, right, style)


def _bus_row(dep: Departure) -> Row:
    if dep.status is Status.CANCELLED:
        right, style = "CANC", Style.INVERTED
    elif dep.status is Status.LATE:
        right, style = f"{dep.departs_at} +{dep.minutes_late}", Style.ALERT
    elif dep.status is Status.UNKNOWN:
        right, style = f"{dep.scheduled} sch", Style.NORMAL
    else:
        right, style = dep.scheduled, Style.NORMAL
    middle = f"{dep.stop}: {dep.destination}" if dep.stop else dep.destination
    return Row(dep.service, middle, right, style)


def _leave_text(departures: list[Departure], walk: WalkTimes | None, now: str) -> str | None:
    found = leave_by(departures, walk, now) if walk else None
    if found is None:
        return None
    leave, dep = found
    target = dep.scheduled if dep.mode == "train" else dep.service
    return f"Leave {leave} ({target})"


def _section(label, departures, to_row, now, empty_text, stale, walk) -> Section:
    upcoming = [d for d in departures if not d.is_gone(now)]
    rows = tuple(to_row(d) for d in upcoming)
    return Section(label, rows, empty_text, stale, _leave_text(upcoming, walk, now))


def build_view(
    title: str,
    trains: list[Departure],
    buses: list[Departure],
    now: str,
    stale: set[str],
    walk: WalkTimes | None = None,
) -> BoardView:
    return BoardView(
        title=title,
        trains=_section("Trains", trains, _train_row, now, "No trains", "train" in stale, walk),
        buses=_section("Buses", buses, _bus_row, now, "No buses", "bus" in stale, walk),
        updated=now,
    )
