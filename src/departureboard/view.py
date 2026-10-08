"""Turns departures into display rows. Meaning is always carried by text;
colour and inversion only reinforce it, so the board works for colour-blind
viewers and on a monochrome panel."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum

from .model import Departure, Status


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
    return Row(dep.service, dep.destination, right, style)


def _section(label, departures, to_row, now, empty_text, stale) -> Section:
    rows = tuple(to_row(d) for d in departures if not d.is_gone(now))
    return Section(label, rows, empty_text, stale)


def build_view(
    title: str,
    trains: list[Departure],
    buses: list[Departure],
    now: str,
    stale: set[str],
) -> BoardView:
    return BoardView(
        title=title,
        trains=_section("Trains", trains, _train_row, now, "No trains", "train" in stale),
        buses=_section("Buses", buses, _bus_row, now, "No buses", "bus" in stale),
        updated=now,
    )
