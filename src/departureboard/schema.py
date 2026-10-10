"""Every setting the settings pages and setup save, in one registry.

Each field belongs to one group of settings: general (both views use it),
the board's or the dashboard's. The settings pages draw their fields from
here, and what the store's settings.toml may hold (saved_keys) is these
fields' keys and the few setup writes that no field shows (MANAGED).
Anything else is set in config.toml alone: addresses keys are sent to
(base_url), state_file, which decides where the store is, and display,
which decides which service runs.

The board's defaults (BOARD_DEFAULTS) are here too, for its loader
(config.py) and the board's fields; the dashboard's stay in
dashconfig.DashboardOptions, and its fields show the same values.

[app] views says which views are on (views()). The checks the fields use,
and the patterns setup and its spool check against, are defined once here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .display import DISPLAY_PANELS
from .holidays import DIVISIONS, ENGLAND_AND_WALES
from .schedule import parse_window
from .themes import THEMES

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
        raise Invalid("Add at least one window, like Mon-Fri 06:30-09:30")
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
    from .disruptions import KB_URL_PLACEHOLDER  # imported here: disruptions imports the bus loaders

    value = raw.strip()
    if not value:
        return KB_URL_PLACEHOLDER
    if not at_raildata(value):
        raise Invalid(f"Enter the https address from the Rail Data Marketplace product page, at {RAILDATA}, "
                      "or leave it empty. Any other address goes in config.toml.")
    return value


def _accents(display: str, theme: str) -> tuple[str, ...]:
    """Accents that stand out on the look's background: yellow is too faint on
    the plain look's white (1.9:1), red too dim on the sign's black on a screen
    (about 3.6:1)."""
    if theme == "plain":
        return ("red",)
    if theme == "sign" and not DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"]).epaper:
        return ("yellow",)
    return ("yellow", "red")


def _accent(allowed: tuple[str, ...]) -> Callable[[str], str]:
    def check(raw: str) -> str:
        if raw == "" or raw in allowed:
            return raw
        if raw in ("red", "yellow"):
            raise Invalid("That colour is too faint on this look; pick another or the look's own")
        raise Invalid("Pick one of the options")
    return check


# The board's defaults, as config.example.toml has them: what its loader uses
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
# With [bus] source = "bods" the timetable costs nothing to read, so it is
# checked often and all day; TransportAPI, if it has to stand in, every
# fallback_interval_minutes in the bus windows given, or else TransportAPI's own.
BODS_DEFAULTS: dict = {
    "interval_minutes": 2,
    "windows": ("Mon-Sun 06:00-23:00",),
    "fallback_interval_minutes": 12,
}


def bus_defaults(source: object) -> dict:
    """[bus]'s defaults for its source."""
    return {**BOARD_DEFAULTS["bus"], **(BODS_DEFAULTS if source == "bods" else {})}


@dataclass(frozen=True)
class Field:
    name: str  # form field name and element id; unique
    group: str  # "general", "board" or "dashboard"
    section: str | None  # the table it is saved in: None (the top level), "app", "train", "bus", "weather", "dashboard"
    key: str
    label: str
    check: Callable[[str], object]
    kind: str = "text"  # text, number, select, area, check
    hint: str = ""
    options: tuple[tuple[str, str], ...] = ()
    default: object = None  # shown when the settings don't say
    needs: str | None = None  # the table that must be set up for the field to show: "train", "bus" or "weather"


@dataclass(frozen=True)
class Key:
    name: str
    group: str
    label: str
    hint: str


_ON_OFF = (("true", "On"), ("false", "Off"))
_TEXT_SIZES = tuple(str(size) for size in range(9, 17))
_TEXT_SIZE_NAMES = {"9": "smallest", "11": "standard", "16": "biggest"}
_KEPT = "The settings below are kept for turning them back on."


def fields(display: str = "inky", theme: str = "sign") -> tuple[Field, ...]:
    """Every field, in the order the pages show them. The accents and turns
    offered depend on the display and the look."""
    epaper = DISPLAY_PANELS.get(display, DISPLAY_PANELS["inky"]).epaper
    rotations = ("0", "180") if epaper else ("0", "90", "180", "270")
    accents = _accents(display, theme)
    board, train, bus = BOARD_DEFAULTS, BOARD_DEFAULTS["train"], BOARD_DEFAULTS["bus"]
    return (
        # General: what both views show.
        Field("title", "general", None, "title", "Title", text(60), default=board["title"],
              hint="Shown at the top of the dashboard, and on the board's plain look."),
        Field("timezone", "general", None, "timezone", "Time zone", timezone, default=board["timezone"],
              hint="Like Europe/London. Departures and checking times are in this zone."),
        Field("bank_holidays", "general", None, "bank_holidays", "Bank holidays", on_off, "select",
              hint="Trains and buses run a Sunday service on bank holidays. Treated like Sundays, only "
                   "checking times that include Sun apply then. Dates come from GOV.UK.",
              options=(("true", "Treat bank holidays like Sundays"), ("false", "Ignore bank holidays")),
              default=board["bank_holidays"]),
        Field("bank_holiday_division", "general", None, "bank_holiday_division", "Bank holidays for",
              choice(DIVISIONS), "select", hint="The part of the UK you live in; each has its own bank holidays.",
              options=(("england-and-wales", "England and Wales"), ("scotland", "Scotland"),
                       ("northern-ireland", "Northern Ireland")),
              default=ENGLAND_AND_WALES),
        Field("view_board", "general", "app", "views", "The board", ticked, "check",
              hint="Departures on the e-paper or screen board."),
        Field("view_dashboard", "general", "app", "views", "The dashboard", ticked, "check",
              hint="Departures, maps and the weather in a web browser."),
        Field("train_enabled", "general", "train", "enabled", "Trains", on_off, "select",
              hint=f"Off: no trains are shown or checked for. {_KEPT}", options=_ON_OFF, default=True,
              needs="train"),
        Field("train_crs", "general", "train", "crs", "Station code", crs(),
              hint="3 letters, like GLO for Glossop.", needs="train"),
        Field("train_filter_crs", "general", "train", "filter_crs", "Only trains calling at", crs(optional=True),
              hint="Optional station code, like MAN. Empty shows every train.", needs="train"),
        Field("train_walk_minutes", "general", "train", "walk_minutes", "Walk to the station (minutes)",
              number(0, 60, optional=True), "number", hint="Shows when to leave. Empty for none.", needs="train"),
        Field("bus_enabled", "general", "bus", "enabled", "Buses", on_off, "select",
              hint=f"Off: no buses are shown or checked for. {_KEPT}", options=_ON_OFF, default=True, needs="bus"),
        Field("bus_stops", "general", "bus", "stops", "Stops", stops, "area",
              hint="One per line, LABEL = ATCO code, like A = 1000DGHS0900. To find stops and their bus "
                   "companies on a map, use setup.", needs="bus"),
        Field("bus_directions", "general", "bus", "directions", "Only buses heading to", directions,
              hint="Optional, comma-separated, like Ashton, Hyde. Empty shows every bus.", needs="bus"),
        Field("bus_walk_minutes", "general", "bus", "walk_minutes", "Walk to each stop (minutes)", walk_per_stop,
              "area", hint="One per line, LABEL = minutes, or one number for every stop. Empty for none.",
              needs="bus"),
        Field("weather_enabled", "general", "weather", "enabled", "Weather", on_off, "select",
              hint="A warning when rain is likely before you'd reach your next train or bus, and the "
                   "dashboard's forecast, from Open-Meteo.",
              options=_ON_OFF, default=True, needs="weather"),
        Field("weather_threshold_percent", "general", "weather", "threshold_percent",
              "Warn of rain from (chance, %)", number(1, 100), "number",
              hint="50 means warn when rain is more likely than not.", default=50, needs="weather"),
        # The board's own.
        Field("theme", "board", None, "theme", "Look", choice(tuple(THEMES)), "select", default=board["theme"],
              options=(("sign", "Station board (amber dot matrix)"), ("plain", "Plain (black on white)"))),
        Field("accent", "board", None, "accent", "Accent colour", _accent(accents), "select", default="",
              hint="Only reinforces meaning; the words always carry it. The station board look uses amber "
                   "and the plain look uses red; a colour too faint on the look's background isn't offered.",
              options=(("", "The look's own"),
                       *((v, t) for v, t in (("yellow", "Yellow / amber"), ("red", "Red")) if v in accents))),
        Field("rotate", "board", None, "rotate", "Rotation", whole_choice(rotations), "select",
              hint="Degrees clockwise, to suit how the display is mounted.",
              options=tuple((r, f"{r}°") for r in rotations), default=board["rotate"]),
        Field("min_redraw_minutes", "board", None, "min_redraw_minutes", "Least minutes between redraws",
              number(1, 60), "number", hint="E-paper only. Refreshes are slow and wear the panel.",
              default=board["min_redraw_minutes"]),
        Field("text_size", "board", None, "text_size", "Text size", whole_choice(_TEXT_SIZES), "select",
              hint="The plain look only. Bigger text shows fewer departures on a small display.",
              options=tuple((s, f"{s} ({_TEXT_SIZE_NAMES[s]})" if s in _TEXT_SIZE_NAMES else s)
                            for s in _TEXT_SIZES),
              default=board["text_size"]),
        Field("board_train_rows", "board", "train", "rows", "Trains shown", number(1, 6), "number",
              default=train["rows"], needs="train"),
        Field("train_interval_minutes", "board", "train", "interval_minutes", "Check every (minutes)",
              number(1, 60), "number", default=train["interval_minutes"], needs="train"),
        Field("train_stale_after_minutes", "board", "train", "stale_after_minutes",
              "Say “not updating” after (minutes)", number(1, 240), "number",
              default=train["stale_after_minutes"], needs="train"),
        Field("train_windows", "board", "train", "windows", "When to check", windows, "area",
              hint="One per line, like Mon-Fri 06:30-09:30.", default=train["windows"], needs="train"),
        Field("board_bus_rows", "board", "bus", "rows", "Buses shown", number(1, 6), "number",
              default=bus["rows"], needs="bus"),
        Field("bus_interval_minutes", "board", "bus", "interval_minutes", "Check every (minutes)",
              number(1, 120), "number", hint="With TransportAPI, each check costs one hit per stop.",
              default=bus["interval_minutes"], needs="bus"),
        Field("bus_stale_after_minutes", "board", "bus", "stale_after_minutes",
              "Say “not updating” after (minutes)", number(1, 480), "number",
              default=bus["stale_after_minutes"], needs="bus"),
        Field("bus_daily_budget", "board", "bus", "daily_budget", "Most TransportAPI hits a day",
              number(1, 1000), "number", hint="The free TransportAPI plan allows 30.",
              default=bus["daily_budget"], needs="bus"),
        Field("bus_windows", "board", "bus", "windows", "When to check", windows, "area",
              hint="One per line, like Mon-Fri 07:00-08:30.", default=bus["windows"], needs="bus"),
        # The dashboard's own, in its [dashboard] table; the defaults are
        # dashconfig.DashboardOptions' (test_schema checks they agree).
        Field("quiet_windows", "dashboard", "dashboard", "quiet_windows", "When not to check", quiet_windows,
              "area", hint="One per line, like Mon-Sun 01:00-05:00. The dashboard still checks while someone "
                           "has it open. Empty checks all the time.",
              default=("Mon-Sun 01:00-05:00",)),
        Field("history_days", "dashboard", "dashboard", "history_days", "Days of punctuality history to keep",
              number(1, 3650), "number", default=90),
        Field("dashboard_train_rows", "dashboard", "dashboard", "train_rows", "Trains shown", number(1, 20),
              "number", default=10, needs="train"),
        Field("train_interval_seconds", "dashboard", "dashboard", "train_interval_seconds",
              "Check every (seconds)", number(30, 3600), "number", default=60, needs="train"),
        Field("late_minutes_train", "dashboard", "dashboard", "late_minutes_train", "Late means at least (minutes)",
              number(1, 60), "number", hint="For the punctuality history.", default=3, needs="train"),
        Field("kb_incidents_url", "dashboard", "dashboard", "kb_incidents_url", "Knowledgebase Incidents address",
              https_feed, hint=f"For engineering works: the address on the Rail Data Marketplace product page, "
                               f"at {RAILDATA}. Empty leaves them off.",
              default="", needs="train"),
        Field("dashboard_bus_rows", "dashboard", "dashboard", "bus_rows", "Buses shown", number(1, 20), "number",
              default=10, needs="bus"),
        Field("bus_interval_seconds", "dashboard", "dashboard", "bus_interval_seconds", "Check every (seconds)",
              number(15, 3600), "number", default=30, needs="bus"),
        Field("late_minutes_bus", "dashboard", "dashboard", "late_minutes_bus", "Late means at least (minutes)",
              number(1, 60), "number", hint="For the punctuality history.", default=2, needs="bus"),
        Field("weather_interval_minutes", "dashboard", "dashboard", "weather_interval_minutes",
              "Check the forecast every (minutes)", number(10, 1440), "number", default=30, needs="weather"),
    )


FIELDS = fields()

KEYS = (
    Key("RDM_API_KEY", "general", "Rail Data Marketplace key", "Trains: the consumer key for Live Departure Board."),
    Key("BODS_API_KEY", "general", "Bus Open Data Service key",
        "Buses: the BODS timetable, live positions and disruptions."),
    Key("TAPI_APP_ID", "board", "TransportAPI app ID", "The board's buses, unless they come from BODS."),
    Key("TAPI_APP_KEY", "board", "TransportAPI app key", "The board's buses, unless they come from BODS."),
    Key("RDM_DETAILS_API_KEY", "dashboard", "Rail Data Marketplace Service Details key",
        "Optional: shows where each of the next few trains is."),
    Key("RDM_KB_API_KEY", "dashboard", "Rail Data Marketplace Knowledgebase Incidents key",
        "Optional: engineering works, with the Knowledgebase address under Trains."),
)


def keys(*groups: str) -> tuple[tuple[str, str, str], ...]:
    """(name, label, hint) of each key in `groups`, as setup and the pages list them."""
    return tuple((k.name, k.label, k.hint) for k in KEYS if k.group in groups)


# What setup saves that no field shows: the bus companies and route strips
# its search finds, home's position for the weather, and the views.
MANAGED: dict[str | None, frozenset[str]] = {
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
        found.setdefault(f.section, set()).add(f.key)
    for table, names in MANAGED.items():
        found.setdefault(table, set()).update(names)
    return {table: frozenset(names) for table, names in found.items()}


def refused(table: str | None, key: str, value: object) -> str | None:
    """Why a saved value is ignored although its key may be saved, or None.
    The Knowledgebase address is sent RDM_KB_API_KEY, so one saved must be
    at raildata.org.uk (or the placeholder, which is never asked)."""
    from .disruptions import kb_url_is_placeholder

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
