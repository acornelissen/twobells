"""The modes of transport, and which of them each view shows.

A mode is a kind of departure with a section of its own on the board and
the dashboard: trains, buses, and trams (the metro mode, for trams, metro
and underground; Manchester's Metrolink trams so far). Each is one entry in
MODES: the table its settings are in ([train]), which is also its
source's name and its entry in the board's state; the words the views
say it in; how the board draws its rows (like a train's, or like a bus's,
with its stop and its own leave time); and how many rows it takes at most
while it shares the board. A mode without a source yet (`ready` false) is
never set up, so no view offers it.

A mode is set up while its table is there and not switched off
([bus] enabled = false). Each view has a modes list, [board] modes and
[dashboard] modes, saved by the Board and Dashboard settings pages (and
setup) like any other setting: the modes it shows, in MODES' order. Without
one, a view shows every mode set up. A mode named but not set up is left
out, and a list that leaves none is as if there were none, so switching a
mode off never leaves a view empty.

The dashboard lists every mode it shows, one after another. The board has
the room its display gives it in its look and text size (board/panel.py:
most_modes): a list naming more than that is refused, and without a list
it shows the first of those set up that fit.

This module imports nothing of the app's, so the board, the dashboard and
the settings can all use it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

VIEWS = ("board", "dashboard")


@dataclass(frozen=True)
class Mode:
    key: str  # its table, [train]; its source's name and its board state entry
    label: str  # its section's heading, "Trains"; a tickbox's words in the settings
    noun: str  # in a sentence: "Not updating: trains"
    empty: str  # what its section says with nothing to show
    # How the board draws its rows: "train" (time, destination, platform,
    # status, the leave words for the section) or "bus" (route, stop,
    # destination, each row's own leave time).
    rows: str
    # The most rows it takes while it shares the board: (e-paper, screen).
    # Alone, it takes all the room there is.
    most_rows: tuple[int, int]
    # Whether it has a source. Until it does, it is never set up.
    ready: bool = True


MODES = (
    Mode("train", "Trains", "trains", "No trains", "train", (3, 3)),
    Mode("bus", "Buses", "buses", "No buses", "bus", (2, 3)),
    # Trams, metro and underground: Manchester's Metrolink trams so far, from
    # TfGM's timetable (sources/trams.py), so it is called trams.
    Mode("metro", "Trams", "trams", "No trams", "train", (3, 3)),
)
KEYS = tuple(mode.key for mode in MODES)
_BY_KEY = {mode.key: mode for mode in MODES}
# How long a list may be, as saved: far more than there are modes.
_MOST = 16


def mode(key: str) -> Mode:
    """The mode called `key`; KeyError if there is none."""
    return _BY_KEY[key]


def ordered(chosen: Sequence[str]) -> tuple[str, ...]:
    """`chosen` in MODES' order, each once."""
    picked = set(chosen)
    return tuple(key for key in KEYS if key in picked)


def set_up(data: Mapping) -> tuple[str, ...]:
    """The modes set up in settings `data`: each one with a source, whose
    table is there and isn't switched off (enabled = false). A table whose
    enabled isn't true or false counts as set up: its loader says what is wrong."""
    found = []
    for item in MODES:
        section = data.get(item.key)
        if item.ready and isinstance(section, dict) and section.get("enabled", True) is not False:
            found.append(item.key)
    return tuple(found)


def checked(view: str, value: object, where: str) -> tuple[str, ...]:
    """A modes list as settings hold it, checked: a list of at least one
    mode, each named once. ValueError, naming `where`, otherwise."""
    if not isinstance(value, list) or len(value) > _MOST or not all(isinstance(key, str) for key in value):
        raise ValueError(f'{where}modes must be a list of modes, like ["train", "bus"], not {value!r}')
    for key in value:
        if key not in KEYS:
            raise ValueError(f"{where}modes: {key[:40]!r} is not a mode; use {', '.join(map(repr, KEYS))}")
    if len(set(value)) != len(value):
        raise ValueError(f"{where}modes names a mode more than once")
    if not value:
        raise ValueError(f"{where}modes names no mode; leave it out to show every mode set up, "
                         f"or take the {view} out of [app] views")
    return tuple(value)


def listed(data: Mapping, view: str) -> tuple[str, ...] | None:
    """[view] modes as written, checked, or None without one. ConfigError
    for a list that isn't one."""
    from .config import ConfigError

    if view not in VIEWS:
        raise ValueError(f"Unknown view {view!r}")
    section = data.get(view)
    if not isinstance(section, dict) or "modes" not in section:
        return None
    try:
        return checked(view, section["modes"], f"[{view}] ")
    except ValueError as error:
        raise ConfigError(str(error)) from None


def too_many(count: int, most: int, room: str) -> str:
    """Why a list of `count` modes is refused where there is room for `most`,
    in words; `room` names where: "the Inky pHAT"."""
    return (f"{room[:1].upper()}{room[1:]} has room for {most} modes at most, each with its own section, "
            f"not {count}")


def shown(data: Mapping, view: str, most: int | None = None, room: str = "this display",
          other: str = "") -> tuple[str, ...]:
    """The modes `view` shows by settings `data`, in MODES' order: those
    its list names that are set up; without a list, or with one naming none
    set up, every mode set up, the first `most` of them if `most` says how
    many fit. ConfigError for a list that isn't one, or names more than
    `most` (`room` says where, for the words, and `other` what else would
    give room for more: "text size 12 or smaller, or another look")."""
    from .config import ConfigError

    named = listed(data, view)
    if named is not None and most is not None and len(named) > most:
        fix = f"; name fewer, or choose {other}" if other else ""
        raise ConfigError(f"[{view}] modes: {too_many(len(named), most, room)}{fix}")
    ready = set_up(data)
    chosen = ordered([key for key in named if key in ready]) if named is not None else ()
    if not chosen:
        chosen = ready if most is None else ready[:most]
    return chosen


def check(most: int | None = None, room: str = "", other: str = "") -> Callable[[str], list[str]]:
    """A settings form's check of a view's ticked modes, sent as their keys
    separated by spaces: the list to save, in MODES' order. schema.Invalid
    for none, a key that is no mode, or more than `most` (`room` says where,
    and `other` what else would give room for more)."""
    from .schema import Invalid

    def check_list(raw: str) -> list[str]:
        keys = raw.split()
        if not keys:
            raise Invalid("Tick at least one, or the departures show nowhere here")
        if any(key not in KEYS for key in keys) or len(set(keys)) != len(keys):
            raise Invalid("Tick only the modes listed")
        if most is not None and len(keys) > most:
            fix = f", or choose {other}" if other else ""
            raise Invalid(f"{too_many(len(keys), most, room)}. Untick {len(keys) - most}{fix}.")
        return list(ordered(keys))
    return check_list
