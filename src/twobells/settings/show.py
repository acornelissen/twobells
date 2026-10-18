"""What each view shows: the items the board and the dashboard can leave out.

Each view has a show list, [board] show and [dashboard] show, saved by the
Board and Dashboard settings pages (and setup) like any other setting. It
names the items that view shows, from ITEMS: anything not named is left
out, and the departures' times always show. Without one, a view shows
everything, as it did before the lists, except the board's clock page,
which shows only while the older setting clock_page = true says so, and
an item in no preset (the board's speaker), which is on only when chosen.

Three presets fill a list (PRESETS): Just the times, The usual (what setup
gives a new install) and Everything, which is every item but those in no
preset.

shown() reads a view's list from settings, and shows() asks about one item.
This module imports nothing of the app's, so the board, the dashboard and
the settings can all use it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

VIEWS = ("board", "dashboard")

# (name, words), in the order the settings pages offer them.
PRESETS = (
    ("times", "Just the times"),
    ("usual", "The usual"),
    ("everything", "Everything"),
)
# What setup gives a new install.
NEW_INSTALL = "usual"
# The setting the board's clock page had before the lists: still read while
# the board has no show list.
OLDER_CLOCK_PAGE = "clock_page"
# How long a list may be, as saved: far more than there are items.
_MOST = 64


@dataclass(frozen=True)
class Item:
    key: str  # as saved in the list
    view: str  # "board" or "dashboard"
    label: str
    hint: str
    presets: tuple[str, ...]  # the presets that include it
    # The board draws it on an HDMI screen only; e-paper ignores it.
    screen: bool = False


_USUAL = ("usual", "everything")
_EVERYTHING = ("everything",)
_ALL = ("times", "usual", "everything")

ITEMS = (
    # The board.
    Item("platforms", "board", "Platforms",
         "Each train's platform, a changed one marked, and its via words. Displays wide enough only.", _USUAL),
    Item("where", "board", "Where it is", "Where each train and bus is, in words and a small strip.", _USUAL,
         screen=True),
    Item("disruption", "board", "Disruption", "The station's disruption notices.", _USUAL),
    Item("rain", "board", "Rain warning", "When rain is likely before you'd reach your train or bus.", _USUAL),
    Item("last_bus", "board", "Last bus", "When the last bus goes, in its last hour.", _USUAL, screen=True),
    Item("bus_page", "board", "Bus page",
         "A page following the next bus in on its route, where setup has its stops.", _USUAL, screen=True),
    Item("calling_at", "board", "Calling at", "The first train's stops, scrolling under it.", _EVERYTHING,
         screen=True),
    Item("clock_page", "board", "Clock page", "10 seconds of a platform clock after the departures.",
         _EVERYTHING, screen=True),
    # Sound in the room, so never ticked by a preset: on only when chosen.
    Item("announce_speaker", "board", "Announcements",
         "Station announcements through the screen's speaker. Needs a speaker and espeak-ng.", (), screen=True),
    # The dashboard.
    Item("next_to_catch", "dashboard", "Next to catch",
         "The departure to leave for next, with a countdown.", _ALL),
    Item("where", "dashboard", "Where it is", "Where each train and bus is, in its row.", _USUAL),
    Item("disruptions", "dashboard", "Disruption",
         "Disruption on your journeys, under Next to catch and on bus rows.", _USUAL),
    Item("weather", "dashboard", "Weather", "The forecast, and the rain sentence under Next to catch.", _USUAL),
    Item("last_buses", "dashboard", "Last buses",
         "When each line's last bus goes, under the buses and in a bus's details.", _USUAL),
    Item("area_map", "dashboard", "Around here", "Trains on the line, and the map of buses nearby.", _USUAL),
    Item("remind", "dashboard", "Remind me", "Reminders to leave, from Next to catch and a departure's details.",
         _USUAL),
    Item("calling_at", "dashboard", "Calling at", "The first train's stops, under its row.", _EVERYTHING),
    Item("coaches", "dashboard", "Coaches", "A train's coaches, in its details.", _EVERYTHING),
    Item("punctuality", "dashboard", "Punctuality",
         "How often a departure is late, in its row and its details.", _EVERYTHING),
    Item("performance", "dashboard", "Performance", "The link to the Performance page, with Delay Repay.",
         _EVERYTHING),
    Item("fleet_numbers", "dashboard", "Fleet numbers", "Which bus it is, in a live bus's details.", _EVERYTHING),
    Item("announce", "dashboard", "Announcements",
         "Announce in a train's details, and a wall tablet's own station announcements.", _EVERYTHING),
)


def items(view: str) -> tuple[Item, ...]:
    """The items `view` can show, in the order the settings pages list them."""
    if view not in VIEWS:
        raise ValueError(f"Unknown view {view!r}")
    return tuple(item for item in ITEMS if item.view == view)


def keys(view: str) -> tuple[str, ...]:
    return tuple(item.key for item in items(view))


def preset(view: str, name: str) -> tuple[str, ...]:
    """The keys preset `name` shows in `view`, in order."""
    if name not in dict(PRESETS):
        raise ValueError(f"Unknown preset {name!r}")
    return tuple(item.key for item in items(view) if name in item.presets)


def preset_of(view: str, shown_keys: Iterable[str]) -> str | None:
    """The preset that shows exactly `shown_keys` in `view`, or None."""
    chosen = set(shown_keys)
    return next((name for name, _ in PRESETS if set(preset(view, name)) == chosen), None)


def default(view: str, data: Mapping | None = None) -> frozenset[str]:
    """What `view` shows without a list: everything in a preset, except the
    board's clock page unless settings `data` say clock_page = true."""
    found = {item.key for item in items(view) if item.presets}
    if view == "board" and (data or {}).get(OLDER_CLOCK_PAGE) is not True:
        found.discard("clock_page")
    return frozenset(found)


def ordered(view: str, chosen: Iterable[str]) -> list[str]:
    """`chosen` in the registry's order, for saving and sending."""
    picked = set(chosen)
    return [key for key in keys(view) if key in picked]


def checked(view: str, value: object, where: str) -> frozenset[str]:
    """A show list as settings hold it, checked: a list of `view`'s keys,
    each named once. ValueError, naming `where`, otherwise."""
    known = keys(view)
    if not isinstance(value, list) or len(value) > _MOST or not all(isinstance(key, str) for key in value):
        raise ValueError(f'{where}show must be a list of what to show, like ["{known[0]}", "{known[1]}"], '
                         f"not {value!r}")
    for key in value:
        if key not in known:
            raise ValueError(f"{where}show: {key[:40]!r} is not something the {view} shows; "
                             f"use {', '.join(known)}")
    if len(set(value)) != len(value):
        raise ValueError(f"{where}show names something more than once")
    return frozenset(value)


def shown(data: Mapping, view: str) -> frozenset[str]:
    """What `view` shows by settings `data`: its [view] show list, or
    without one, default(). ConfigError for a list that isn't one, or names
    anything the view can't show."""
    from .config import ConfigError

    section = data.get(view)
    if not isinstance(section, dict) or "show" not in section:
        return default(view, data)
    try:
        return checked(view, section["show"], f"[{view}] ")
    except ValueError as error:
        raise ConfigError(str(error)) from None


def shows(data: Mapping, view: str, key: str) -> bool:
    """Whether `view` shows item `key` by settings `data`. A `key` the
    view has no item for is an error (ValueError), not a quiet no."""
    if key not in keys(view):
        raise ValueError(f"The {view} has no item {key!r}")
    return key in shown(data, view)


def check(view: str) -> Callable[[str], list[str]]:
    """A settings form's check of a view's ticked items, sent as their keys
    separated by spaces: the list to save, in order. schema.Invalid for a
    key the view has no item for."""
    from .schema import Invalid

    def check_list(raw: str) -> list[str]:
        try:
            return ordered(view, checked(view, raw.split(), ""))
        except ValueError:
            raise Invalid("Tick only the items listed") from None
    return check_list
