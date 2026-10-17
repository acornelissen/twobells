"""Every setting the settings pages and setup save, in one registry.

Each field belongs to one group of settings: general (both views use it),
the board's or the dashboard's; and to one of the settings pages (PAGES),
under a heading there, with what the page needs to draw it: its tier, the
width of its box and its unit. The settings pages draw their fields from
here, and what the store's settings.toml may hold (saved_keys) is these
fields' keys and the few setup writes that no field shows (MANAGED).
Anything else is set in config.toml alone: addresses keys are sent to
(base_url), state_file, which decides where the store is, and display,
which decides which service runs.

The board's defaults (BOARD_DEFAULTS) are here too, for its loader
(settings/config.py) and the board's fields; the dashboard's stay in
dashconfig.DashboardOptions, and its fields show the same values.

[app] views says which views are on (views()), and each view's modes list
which modes it shows (settings/modes.py); the board's display has room for
so many (board_room). The checks the fields use,
and the patterns setup and its spool check against, are defined once here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal, overload
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, available_timezones

from ..board.displays.display import DISPLAY_PANELS
from ..board.panel import most_modes, panel_name
from ..board.themes import THEMES, themes_for
from ..core.polling import parse_window
from ..sources.holidays import DIVISIONS, ENGLAND_AND_WALES
from . import modes, show

if TYPE_CHECKING:
    from .installs import Install

log = logging.getLogger(__name__)

# Station codes (CRS) are three letters, like GLO.
CRS = re.compile(r"^[A-Za-z]{3}$")
# A stop's label on the board: 1 to 3 letters or digits, like A.
LABEL = re.compile(r"^[A-Za-z0-9]{1,3}$")
# A stop's ATCO code: its area's 3 digits, then up to 9 letters and digits.
ATCO = re.compile(r"^[0-9]{3}[0-9A-Za-z]{1,9}$")
# National operator codes: a few letters or digits, like BNML.
NOC = re.compile(r"^[A-Za-z0-9]{2,8}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# What a pasted key may hold: enough for every key we use, and nothing that
# could start another line of secrets.env.
KEY_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")
_ZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+/-]{0,63}$")
_MAX_URL = 300
# Where the Rail Data Marketplace's feeds are. A Knowledgebase address saved
# from the page must be there, as RDM_KB_API_KEY is sent to it.
RAILDATA = "raildata.org.uk"

VIEWS = ("board", "dashboard")
GROUPS = ("general", "board", "dashboard")


class Invalid(ValueError):
    pass


# The checks: each takes what a form field sent and gives the value to save,
# or raises Invalid with words to show beside the field.


def text(limit: int) -> Callable[[str], str]:
    def check(raw: str) -> str:
        value = raw.strip()
        if not value:
            raise Invalid("Enter some text")
        if len(value) > limit or CONTROL.search(value):
            raise Invalid(f"Use up to {limit} ordinary characters")
        return value
    return check


@overload
def number(low: int, high: int, optional: Literal[False] = False) -> Callable[[str], int]: ...
@overload
def number(low: int, high: int, optional: bool) -> Callable[[str], int | None]: ...


def number(low: int, high: int, optional: bool = False) -> Callable[[str], int | None]:
    def check(raw: str) -> int | None:
        value = raw.strip()
        if not value and optional:
            return None
        if not value.isdigit() or not low <= int(value) <= high:
            raise Invalid(f"Enter a whole number from {low} to {high}")
        return int(value)
    return check


def choice(options: tuple[str, ...]) -> Callable[[str], str]:
    def check(raw: str) -> str:
        if raw not in options:
            raise Invalid("Pick one of the options")
        return raw
    return check


def whole_choice(options: tuple[str, ...]) -> Callable[[str], int]:
    """One of `options`, all whole numbers, as a number."""
    pick = choice(options)
    return lambda raw: int(pick(raw))


def crs(optional: bool = False) -> Callable[[str], str | None]:
    def check(raw: str) -> str | None:
        value = raw.strip().upper()
        if not value and optional:
            return None
        if not CRS.match(value):
            raise Invalid("Enter a 3-letter station code, like GLO")
        return value
    return check


def windows(raw: str) -> list[str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise Invalid("Add at least one checking time: tick its days and give it a From and To")
    for line in lines:
        try:
            parse_window(line)
        except ValueError:
            raise Invalid(f"“{line[:40]}” isn't a window like Mon-Fri 06:30-09:30") from None
    return lines


def quiet_windows(raw: str) -> list[str]:
    """Windows one per line, like the board's checking times, but none is fine:
    then the dashboard checks all day and night."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        try:
            parse_window(line)
        except ValueError:
            raise Invalid(f"“{line[:40]}” isn't a window like Mon-Sun 01:00-05:00") from None
    return lines


def pairs(raw: str, value_check: Callable[[str], object], what: str) -> dict[str, object]:
    found = {}
    for line in (line.strip() for line in raw.splitlines()):
        if not line:
            continue
        label, sep, value = (part.strip() for part in line.partition("="))
        if not sep or not LABEL.match(label):
            raise Invalid(f"“{line[:40]}” isn't LABEL = {what}; use a label of 1 to 3 letters or digits")
        try:
            found[label] = value_check(value)
        except Invalid as error:
            raise Invalid(f"“{line[:40]}”: {error}") from None
    return found


def _atco(raw: str) -> str:
    if not ATCO.fullmatch(raw):
        raise Invalid("A stop code starts with 3 digits, then up to 9 letters and digits, like 1000DGHS0900")
    return raw


def stops(raw: str) -> dict[str, object]:
    found = pairs(raw, _atco, "ATCO code")
    if not found:
        raise Invalid("Add at least one stop, like A = 1000DGHS0900")
    return found


def walk_per_stop(raw: str) -> dict[str, object] | int:
    if not raw.strip():
        return {}
    if raw.strip().isdigit():
        return number(0, 60)(raw)
    return pairs(raw, lambda v: number(0, 60)(v), "minutes")


def directions(raw: str) -> list[str]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    for part in parts:
        if len(part) > 40 or CONTROL.search(part):
            raise Invalid("Keep each direction under 40 characters")
    return parts


def on_off(raw: str) -> bool:
    return choice(("true", "false"))(raw) == "true"


def ticked(raw: str) -> bool:
    """A checkbox: sent as "on" when ticked, not sent at all when not."""
    return choice(("on", ""))(raw) == "on"


def timezone(raw: str) -> str:
    value = raw.strip()
    try:
        if not _ZONE.fullmatch(value):
            raise ValueError(value)
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        raise Invalid("Enter a time zone like Europe/London") from None
    return value


def zone(data: Mapping) -> ZoneInfo:
    """The time zone settings `data` name, for saying a time as the board
    would; the default (Europe/London) if they name none or a bad one, so
    broken settings can't stop a page or a reset code saying a time. Saving
    a bad zone is refused (timezone), so this is for settings from elsewhere."""
    try:
        return ZoneInfo(timezone(str(data.get("timezone", BOARD_DEFAULTS["timezone"]))))
    except Invalid:
        return ZoneInfo(BOARD_DEFAULTS["timezone"])


def at_raildata(url: str) -> bool:
    """Whether an address is plain https at raildata.org.uk (or a name under it)."""
    if len(url) > _MAX_URL or CONTROL.search(url) or any(c.isspace() or c == "\\" for c in url):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        return False
    host = parts.hostname or ""
    return (parts.scheme == "https" and not parts.username and not parts.password
            and (host == RAILDATA or host.endswith("." + RAILDATA)))


def https_feed(raw: str) -> str:
    """The Knowledgebase Incidents address, at raildata.org.uk, or empty for
    none (saved as the placeholder, which keeps engineering works off)."""
    from ..sources.disruptions import KB_URL_PLACEHOLDER  # imported here: disruptions imports the bus loaders

    value = raw.strip()
    if not value:
        return KB_URL_PLACEHOLDER
    if not at_raildata(value):
        raise Invalid(f"Enter the https address from the Rail Data Marketplace product page, at {RAILDATA}, "
                      "or leave it empty. Any other address goes in config.toml.")
    return value


def board_room(display: str) -> tuple[int, str]:
    """(how many modes the board's `display` has room for, the display in
    words), from board/panel.py: (2, "the Inky pHAT")."""
    panel = DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"])
    return most_modes(panel), panel_name(panel)


def room_words(display: str) -> str:
    """Why the board shows only so many modes, in words, for the settings and setup."""
    most, room = board_room(display)
    return (f"{room[:1].upper()}{room[1:]} has room for {most}: each takes a section with its own heading, and "
            "the board's text is sized to the display's height.")


def _accents(display: str, theme: str) -> tuple[str, ...]:
    """Accents that stand out on the look's background: yellow is too faint on
    the plain look's white (1.9:1), red too dim on black on a screen (about
    3.6:1), in the sign look there and always in the TFT and split-flap looks
    (on the split-flap's grey tiles, 2.6:1)."""
    if theme == "plain":
        return ("red",)
    if theme in ("tft", "flap") or (theme == "sign" and not DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"]).epaper):
        return ("yellow",)
    return ("yellow", "red")


def _look(allowed: tuple[str, ...]) -> Callable[[str], str]:
    """One of the looks the display can show (themes_for)."""
    def check(raw: str) -> str:
        if raw in allowed:
            return raw
        if raw in THEMES:
            raise Invalid("That look is for a screen; pick one for e-paper")
        raise Invalid("Pick one of the options")
    return check


def _accent(allowed: tuple[str, ...]) -> Callable[[str], str]:
    def check(raw: str) -> str:
        if raw == "" or raw in allowed:
            return raw
        if raw in ("red", "yellow"):
            raise Invalid("That colour is too faint on this look; pick another or the look's own")
        raise Invalid("Pick one of the options")
    return check


# The board's defaults, as examples/config.example.toml has them: what its loader uses
# for a setting not given, and what its fields show then.
BOARD_DEFAULTS: dict = {
    "title": "Departures",
    "timezone": "Europe/London",
    "min_redraw_minutes": 2,
    "theme": "sign",
    "display": "inky",
    "rotate": 0,
    "text_size": 11,
    "bank_holidays": True,
    "scrolling": True,
    "train": {
        "rows": 3,
        "interval_minutes": 2,
        "stale_after_minutes": 6,
        "windows": ("Mon-Fri 06:30-09:30", "Mon-Fri 16:30-19:30"),
    },
    # Buses from TransportAPI: 2 stops checked in these windows every 12
    # minutes is 28 of the free plan's 30 hits a day.
    "bus": {
        "rows": 3,
        "interval_minutes": 12,
        "stale_after_minutes": 30,
        "daily_budget": 28,
        "windows": ("Mon-Fri 07:00-08:30", "Mon-Fri 17:00-18:00"),
    },
}
# Buses come from the BODS timetable unless [bus] says source = "transportapi"
# (BODS covers England only); TransportAPI otherwise only stands in for it.
DEFAULT_BUS_SOURCE = "bods"
# The timetable costs nothing to read, so it is checked often and all day;
# TransportAPI, if it has to stand in, every fallback_interval_minutes in
# fallback_windows, or else its own windows above, so its daily hits last.
BODS_DEFAULTS: dict = {
    "interval_minutes": 2,
    "windows": ("Mon-Sun 06:00-23:00",),
    "fallback_interval_minutes": 12,
}


def bus_source(bus: object) -> object:
    """Where [bus] says buses come from: its source, or the default."""
    return bus.get("source", DEFAULT_BUS_SOURCE) if isinstance(bus, dict) else DEFAULT_BUS_SOURCE


def bus_defaults(source: object) -> dict:
    """[bus]'s defaults for its source."""
    return {**BOARD_DEFAULTS["bus"], **(BODS_DEFAULTS if source == "bods" else {})}


@dataclass(frozen=True)
class Field:
    name: str  # form field name and element id; unique
    group: str  # the view that uses it: "general" (both), "board" or "dashboard"
    section: str | None  # the table it is saved in: None (the top level), "app", "train", "bus", "weather", "dashboard"
    key: str
    label: str
    check: Callable[[str], object]
    # text, number, radios (2 to 4 choices), select (more), area, check; windows: time windows as rows
    # of days and times (web/windowrows.py), each row called `item`; per_stop: a number box for each
    # bus stop, saved as a table by stop label; show: a view's show list (settings/show.py), a
    # tickbox for each item, with its presets; or modes: a view's modes list (settings/modes.py), a
    # tickbox for each mode set up
    kind: str = "text"
    hint: str = ""
    options: tuple[tuple[str, str], ...] = ()  # a choice's (value, words); a text box's suggestions
    default: object = None  # shown when the settings don't say
    needs: str | None = None  # the table that must be set up for the field to show: "train", "bus" or "weather"
    # Where the settings pages show it: its page (PAGES), the heading of its
    # section there, and whether it waits under the section's Advanced.
    page: str = ""
    heading: str = ""
    tier: str = "common"  # or "advanced"
    width: str = ""  # a box for about this many characters: WIDTHS; "" for the full width
    unit: str = ""  # said after the box, like "minutes"
    keyboard: str = ""  # "code": capitals, no corrections (station codes); "plain": no capitals or corrections
    also: tuple[str, ...] = ()  # other keys it saves, in the same table (bank holidays: the division)
    item: str = ""  # what each of a windows field's rows is called, numbered: "Checking time" 1, 2...


@dataclass(frozen=True)
class Key:
    name: str
    group: str
    label: str
    hint: str
    screen: bool = False  # the board uses it too while its display is a screen


# The settings pages, by task. The overview only shows; every other page
# has a form of the fields whose page it is (the keys have theirs).
PAGES = ("overview", "trains_buses", "board", "dashboard", "keys", "general")
TIERS = ("common", "advanced")
WIDTHS = ("2", "3", "4", "5", "10", "20")
KEYBOARDS = ("code", "plain")
# The bank holidays choice: a part of the UK whose bank holidays run Sunday
# times, or none. It saves bank_holidays and bank_holiday_division, as setup does.
IGNORE_HOLIDAYS = "ignore"


def holidays(raw: str) -> str:
    return choice((*DIVISIONS, IGNORE_HOLIDAYS))(raw)


def holiday_settings(chosen: str) -> dict[str, object]:
    """What a bank holidays choice saves. Ignoring them keeps the part of the
    UK, for turning them back on."""
    if chosen == IGNORE_HOLIDAYS:
        return {"bank_holidays": False}
    return {"bank_holidays": True, "bank_holiday_division": chosen}


def holiday_choice(data: dict) -> str:
    """The bank holidays choice settings `data` make: England and Wales
    unless they say otherwise (saving says what is wrong with a bad one)."""
    if data.get("bank_holidays") is False:
        return IGNORE_HOLIDAYS
    division = data.get("bank_holiday_division", ENGLAND_AND_WALES)
    return division if division in DIVISIONS else ENGLAND_AND_WALES


@cache
def _zones() -> tuple[tuple[str, str], ...]:
    """Every time zone this computer knows, Europe/London first, as suggestions."""
    found = sorted(zone for zone in available_timezones() if _ZONE.fullmatch(zone) and zone != "Europe/London")
    return tuple((zone, zone) for zone in ("Europe/London", *found))


_ON_OFF = (("true", "On"), ("false", "Off"))
_TEXT_SIZES = tuple(str(size) for size in range(9, 17))
_TEXT_SIZE_NAMES = {"9": "smallest", "11": "standard", "16": "biggest"}
_KEPT = "The settings below are kept for turning them back on."
_LISTED = "The first 5 show; the rest are behind Show later departures. A wall tablet shows 4."
_MODES = "Only the modes switched on in Trains and buses are listed."
_CHECKING = ("Outside these times the board shows what it last knew, without saying “not updating”. Bank holidays "
             "count as Sundays, unless the General page says to ignore them.")


def fields(display: str = "inky", theme: str = "sign") -> tuple[Field, ...]:
    """Every field, in the order the pages show them. The looks, accents and
    turns offered depend on the display and the look."""
    epaper = DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"]).epaper
    looks = themes_for(epaper)
    rotations = ("0", "180") if epaper else ("0", "90", "180", "270")
    accents = _accents(display, theme)
    board, train, bus = BOARD_DEFAULTS, BOARD_DEFAULTS["train"], BOARD_DEFAULTS["bus"]
    return (
        # Trains and buses: what both views show.
        Field("title", "general", None, "title", "Title", text(60), default=board["title"],
              page="trains_buses", heading="Title",
              hint="The board shows all of it. The dashboard shows the part before “>” as its heading: "
                   "Glossop, from Glossop > Manchester."),
        Field("train_enabled", "general", "train", "enabled", "Show trains", on_off, "radios",
              hint=f"Off: no trains are shown or checked for. {_KEPT}", options=_ON_OFF, default=True,
              needs="train", page="trains_buses", heading="Trains"),
        Field("train_crs", "general", "train", "crs", "Station code", crs(),
              hint="3 letters, like GLO for Glossop.", needs="train", page="trains_buses", heading="Trains",
              width="4", keyboard="code"),
        Field("train_filter_crs", "general", "train", "filter_crs", "Only trains calling at", crs(optional=True),
              hint="A station code, like MAN. Leave it empty to show every train.", needs="train",
              page="trains_buses", heading="Trains", width="4", keyboard="code"),
        Field("train_walk_minutes", "general", "train", "walk_minutes", "Walk to the station",
              number(0, 60, optional=True), "number", hint="Shows when to leave. Leave it empty for none.",
              needs="train", page="trains_buses", heading="Trains", width="3", unit="minutes"),
        Field("bus_enabled", "general", "bus", "enabled", "Show buses", on_off, "radios",
              hint=f"Off: no buses are shown or checked for. {_KEPT}", options=_ON_OFF, default=True, needs="bus",
              page="trains_buses", heading="Buses"),
        Field("bus_walk_minutes", "general", "bus", "walk_minutes", "Walk to each stop", walk_per_stop,
              "per_stop", hint="Shows when to leave. Leave a box empty for none.", needs="bus",
              page="trains_buses", heading="Buses", width="3", unit="minutes"),
        Field("bus_directions", "general", "bus", "directions", "Only buses heading to", directions,
              hint="Optional, separated by commas, like Ashton, Hyde. Leave it empty to show every bus.",
              needs="bus", page="trains_buses", heading="Buses"),
        Field("bus_stops", "general", "bus", "stops", "Stop codes", stops, "area",
              hint="One per line, LABEL = stop code, like A = 1000DGHS0900. Setup finds stops and their bus "
                   "companies on a map.", needs="bus", page="trains_buses", heading="Buses", tier="advanced"),
        Field("weather_enabled", "general", "weather", "enabled", "Show the weather", on_off, "radios",
              hint="A warning when rain is likely before you'd reach your next train or bus, and the "
                   "dashboard's forecast, from Open-Meteo.",
              options=_ON_OFF, default=True, needs="weather", page="trains_buses", heading="Weather"),
        Field("weather_threshold_percent", "general", "weather", "threshold_percent",
              "Warn of rain when the chance is at least", number(1, 100), "number",
              hint="50 means warn when rain is more likely than not.", default=50, needs="weather",
              page="trains_buses", heading="Weather", width="3", unit="%"),
        # The board's own.
        Field("theme", "board", None, "theme", "Look", _look(looks), "radios", default=board["theme"],
              options=tuple((name, THEMES[name].label) for name in looks),
              page="board", heading="Display"),
        Field("rotate", "board", None, "rotate", "Rotation", whole_choice(rotations), "radios",
              hint="Degrees clockwise, to suit how the display is mounted.",
              options=tuple((r, f"{r}°") for r in rotations), default=board["rotate"],
              page="board", heading="Display"),
        Field("accent", "board", None, "accent", "Accent colour", _accent(accents), "radios", default="",
              hint="Only reinforces meaning; the words always carry it. The station board look uses amber, "
                   "the TFT and split-flap looks use yellow and the plain look uses red; a colour too faint on the "
                   "look's background isn't offered.",
              options=(("", "The look's own"),
                       *((v, t) for v, t in (("yellow", "Yellow / amber"), ("red", "Red")) if v in accents)),
              page="board", heading="Display", tier="advanced"),
        Field("min_redraw_minutes", "board", None, "min_redraw_minutes", "Redraw at most every",
              number(1, 60), "number", hint="E-paper only. Refreshes are slow and wear the panel.",
              default=board["min_redraw_minutes"], page="board", heading="Display", tier="advanced",
              width="3", unit="minutes"),
        Field("text_size", "board", None, "text_size", "Text size", whole_choice(_TEXT_SIZES), "select",
              hint="The plain look only. Bigger text shows fewer departures on a small display.",
              options=tuple((s, f"{s} ({_TEXT_SIZE_NAMES[s]})" if s in _TEXT_SIZE_NAMES else s)
                            for s in _TEXT_SIZES),
              default=board["text_size"], page="board", heading="Display", tier="advanced"),
        Field("board_modes", "board", "board", "modes", "Modes of transport", modes.check(*board_room(display)),
              "modes", hint=f"{_MODES} {room_words(display)}", page="board", heading="Modes of transport"),
        Field("board_show", "board", "board", "show", "What to show", show.check("board"), "show",
              default=show.ordered("board", show.default("board")),
              hint="The departures' times always show. What says “Screens only” shows only on an HDMI screen.",
              page="board", heading="What to show"),
        Field("board_train_rows", "board", "train", "rows", "Trains shown", number(1, 6), "number",
              default=train["rows"], needs="train", page="board", heading="Trains", width="2"),
        Field("train_windows", "board", "train", "windows", "When to check for trains", windows, "windows",
              hint=_CHECKING, default=train["windows"], needs="train", page="board", heading="Trains",
              item="Checking time"),
        Field("train_interval_minutes", "board", "train", "interval_minutes", "Check every",
              number(1, 60), "number", default=train["interval_minutes"], needs="train",
              page="board", heading="Trains", tier="advanced", width="3", unit="minutes"),
        Field("train_stale_after_minutes", "board", "train", "stale_after_minutes",
              "Say “not updating” after", number(1, 240), "number",
              default=train["stale_after_minutes"], needs="train", page="board", heading="Trains",
              tier="advanced", width="3", unit="minutes"),
        Field("board_bus_rows", "board", "bus", "rows", "Buses shown", number(1, 6), "number",
              default=bus["rows"], needs="bus", page="board", heading="Buses", width="2"),
        Field("bus_windows", "board", "bus", "windows", "When to check for buses", windows, "windows",
              hint=_CHECKING, default=bus["windows"], needs="bus", page="board", heading="Buses",
              item="Checking time"),
        Field("bus_interval_minutes", "board", "bus", "interval_minutes", "Check every",
              number(1, 120), "number", hint="With TransportAPI, each check costs one lookup per stop.",
              default=bus["interval_minutes"], needs="bus", page="board", heading="Buses", tier="advanced",
              width="3", unit="minutes"),
        Field("bus_stale_after_minutes", "board", "bus", "stale_after_minutes",
              "Say “not updating” after", number(1, 480), "number",
              default=bus["stale_after_minutes"], needs="bus", page="board", heading="Buses", tier="advanced",
              width="3", unit="minutes"),
        Field("bus_daily_budget", "board", "bus", "daily_budget", "Most TransportAPI lookups a day",
              number(1, 1000), "number", hint="The free TransportAPI plan allows 30.",
              default=bus["daily_budget"], needs="bus", page="board", heading="Buses", tier="advanced",
              width="4", unit="lookups"),
        # The dashboard's own, in its [dashboard] table; the defaults are
        # dashconfig.DashboardOptions' (test_schema checks they agree).
        Field("dashboard_modes", "dashboard", "dashboard", "modes", "Modes of transport", modes.check(), "modes",
              hint=f"{_MODES} The dashboard lists them one after another.", page="dashboard",
              heading="Departures"),
        Field("dashboard_train_rows", "dashboard", "dashboard", "train_rows", "Most trains listed", number(1, 20),
              "number", hint=_LISTED, default=10, needs="train", page="dashboard", heading="Departures",
              width="2"),
        Field("dashboard_bus_rows", "dashboard", "dashboard", "bus_rows", "Most buses listed", number(1, 20),
              "number", hint=_LISTED, default=15, needs="bus", page="dashboard", heading="Departures",
              width="2"),
        Field("row_strips", "dashboard", "dashboard", "row_strips", "Position strips in each row", on_off, "radios",
              hint="A small drawing of the stops in each departure row, with the train or bus where it is, while "
                   "What to show has Where it is. Never on a wall tablet.",
              options=_ON_OFF, default=True, page="dashboard", heading="Departures"),
        Field("dashboard_show", "dashboard", "dashboard", "show", "What to show", show.check("dashboard"), "show",
              default=show.ordered("dashboard", show.default("dashboard")),
              hint="The departures' times always show.", page="dashboard", heading="What to show"),
        Field("train_interval_seconds", "dashboard", "dashboard", "train_interval_seconds",
              "Check for trains every", number(30, 3600), "number", default=60, needs="train",
              page="dashboard", heading="Departures", tier="advanced", width="4", unit="seconds"),
        Field("bus_interval_seconds", "dashboard", "dashboard", "bus_interval_seconds", "Check for buses every",
              number(15, 3600), "number", default=30, needs="bus", page="dashboard", heading="Departures",
              tier="advanced", width="4", unit="seconds"),
        Field("weather_interval_minutes", "dashboard", "dashboard", "weather_interval_minutes",
              "Check the forecast every", number(10, 1440), "number", default=30, needs="weather",
              page="dashboard", heading="Departures", tier="advanced", width="4", unit="minutes"),
        Field("late_minutes_train", "dashboard", "dashboard", "late_minutes_train", "Count trains as late from",
              number(1, 60), "number", hint="For the punctuality history.", default=3, needs="train",
              page="dashboard", heading="Departures", tier="advanced", width="3", unit="minutes"),
        Field("late_minutes_bus", "dashboard", "dashboard", "late_minutes_bus", "Count buses as late from",
              number(1, 60), "number", hint="For the punctuality history.", default=2, needs="bus",
              page="dashboard", heading="Departures", tier="advanced", width="3", unit="minutes"),
        Field("history_days", "dashboard", "dashboard", "history_days", "Keep punctuality history for",
              number(1, 3650), "number", default=90, page="dashboard", heading="Departures", tier="advanced",
              width="4", unit="days"),
        Field("quiet_windows", "dashboard", "dashboard", "quiet_windows", "When to stop checking", quiet_windows,
              "windows", hint="The dashboard stops checking for departures and the weather then, unless a browser "
                              "has it open. A wall tablet keeps it checking, and dims. Leave every row empty to "
                              "check all the time.",
              default=("Mon-Sun 01:00-05:00",), page="dashboard", heading="Quiet hours", item="Quiet hours"),
        # Keys: each key's box is the page's own (KEYS); the engineering works
        # address sits with its key.
        Field("kb_incidents_url", "dashboard", "dashboard", "kb_incidents_url", "Engineering works address",
              https_feed, hint=f"The address on the Rail Data Marketplace's Knowledgebase Incidents product page, "
                               f"at {RAILDATA}. Leave it empty to leave engineering works off.",
              default="", needs="train", page="keys", heading="Keys"),
        # General.
        Field("view_board", "general", "app", "views", "The board", ticked, "check",
              hint="Departures on the e-paper or screen board.", page="general", heading="Views"),
        Field("view_dashboard", "general", "app", "views", "The dashboard", ticked, "check",
              hint="Departures, maps and the weather in a web browser.", page="general", heading="Views"),
        Field("scrolling", "general", None, "scrolling", "Scrolling text", on_off, "radios",
              hint="Text too long for its line scrolls along it: the dashboard's calling-at line (in every look but "
                   "Standard) and long words on a board's HDMI screen. Off, the dashboard wraps it and the screen "
                   "cuts it short with “…”, as e-paper does.",
              options=_ON_OFF, default=BOARD_DEFAULTS["scrolling"], page="general", heading="Long text"),
        Field("bank_holidays", "general", None, "bank_holidays", "Bank holidays", holidays, "radios",
              hint="Trains and buses run Sunday times on bank holidays, so only checking times that include "
                   "Sun apply then. Each part of the UK has its own; the dates come from GOV.UK.",
              options=(("england-and-wales", "Like Sundays, for England and Wales"),
                       ("scotland", "Like Sundays, for Scotland"),
                       ("northern-ireland", "Like Sundays, for Northern Ireland"),
                       (IGNORE_HOLIDAYS, "Ignore them")),
              default=ENGLAND_AND_WALES, page="general", heading="Dates and times",
              also=("bank_holiday_division",)),
        Field("timezone", "general", None, "timezone", "Time zone", timezone, default=board["timezone"],
              hint="Like Europe/London. Departures and checking times are in this zone.", options=_zones(),
              page="general", heading="Dates and times", tier="advanced", width="20", keyboard="plain"),
    )


FIELDS = fields()

KEYS = (
    Key("RDM_API_KEY", "general", "Rail Data Marketplace key",
        "Trains: the Live Departure Board key from Rail Data Marketplace, National Rail's data site."),
    Key("BODS_API_KEY", "general", "Bus data key",
        "Buses: the key from the Bus Open Data Service, the government's free bus data, for the timetable, "
        "live positions and disruptions."),
    Key("TAPI_APP_ID", "board", "TransportAPI app ID", "The board's buses, unless they come from the bus data."),
    Key("TAPI_APP_KEY", "board", "TransportAPI app key", "The board's buses, unless they come from the bus data."),
    Key("RDM_DETAILS_API_KEY", "dashboard", "Rail Data Marketplace key for where each train is",
        "Optional: shows where each of the next few trains is. Rail Data Marketplace calls it Service Details.",
        screen=True),
    Key("RDM_KB_API_KEY", "dashboard", "Rail Data Marketplace key for engineering works",
        "Optional: engineering works, with the address below. Rail Data Marketplace calls it Knowledgebase "
        "Incidents."),
)


def keys(*groups: str) -> tuple[tuple[str, str, str], ...]:
    """(name, label, hint) of each key in `groups`, as setup and the pages list them."""
    return tuple((k.name, k.label, k.hint) for k in KEYS if k.group in groups)


def keys_used(views: tuple[str, ...] | list[str], display: str) -> tuple[tuple[str, str, str], ...]:
    """(name, label, hint) of each key the views on use: the general keys,
    each view's own, and while the board is on a screen, the keys its rows
    use too (where each train is). E-paper never asks for those."""
    screen = "board" in views and not DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"]).epaper
    return tuple((k.name, k.label, k.hint) for k in KEYS
                 if k.group == "general" or k.group in views or (screen and k.screen))


# What setup saves that no field shows: the bus companies and route strips
# its search finds, home's position for the weather, and the views. And
# the Clock page setting from before the show lists, which the board still
# reads until its show list is saved (settings/show.py).
MANAGED: dict[str | None, frozenset[str]] = {
    None: frozenset({show.OLDER_CLOCK_PAGE}),
    "app": frozenset({"views"}),
    "train": frozenset({"enabled"}),
    "bus": frozenset({"enabled", "source", "operators", "routes"}),
    "weather": frozenset({"lat", "lon"}),
}


def saved_keys() -> dict[str | None, frozenset[str]]:
    """What the store's settings.toml may hold, by table (None is the top
    level): every field's key and MANAGED. Anything else in it is ignored,
    so whatever can write that file can't send the keys to another server
    or move the store: base_url, state_file, display and the like are set
    in config.toml only."""
    found: dict[str | None, set[str]] = {None: set()}
    for f in FIELDS:
        found.setdefault(f.section, set()).update((f.key, *f.also))
    for table, names in MANAGED.items():
        found.setdefault(table, set()).update(names)
    return {table: frozenset(names) for table, names in found.items()}


def refused(table: str | None, key: str, value: object) -> str | None:
    """Why a saved value is ignored although its key may be saved, or None.
    The Knowledgebase address is sent RDM_KB_API_KEY, so one saved must be
    at raildata.org.uk (or the placeholder, which is never asked)."""
    from ..sources.disruptions import kb_url_is_placeholder

    if (table, key) == ("dashboard", "kb_incidents_url") and (
            not isinstance(value, str) or not (kb_url_is_placeholder(value) or at_raildata(value))):
        return f"only config.toml can set an address outside {RAILDATA}"
    return None


def views(data: dict, install: Install | None = None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The views switched on: [app] views, less any `install` doesn't offer
    (each said in the log, as a store copied from a Pi to Docker has them).
    Without [app] views: the install's default views, or `default`.
    ConfigError if [app] views isn't a list of views, each named once. An
    empty list is allowed: then only the settings pages run."""
    from .config import ConfigError

    section = data.get("app")
    if section is None or (isinstance(section, dict) and "views" not in section):
        return tuple(install.default_views if install is not None else default)
    if not isinstance(section, dict):
        raise ConfigError(f"[app] must be a table, not {section!r}")
    named = section["views"]
    if not isinstance(named, list) or not all(isinstance(view, str) for view in named):
        raise ConfigError(f'[app] views must be a list of views, like ["board", "dashboard"], not {named!r}')
    for view in named:
        if view not in VIEWS:
            raise ConfigError(f'[app] views: {view!r} is not a view; use "board" or "dashboard"')
    if len(set(named)) != len(named):
        raise ConfigError("[app] views names a view more than once")
    if install is None:
        return tuple(named)
    for view in named:
        if view not in install.offered:
            log.info("[app] views: this install has no %s view, so it is left off", view)
    return tuple(view for view in named if view in install.offered)
