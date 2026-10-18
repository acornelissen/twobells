"""Loads the settings (config.toml and the store's settings.toml) and the
environment (secrets) into a Board: its sources, look, display and pages.

settings/config.py reads and checks the settings every view shares; this
module builds the board from them, so it sits with the board, above the
settings and the sources it builds.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.errors import ConfigError
from ..core.places import london_stops_by_code
from ..core.polling import PollPolicy
from ..core.walk import DEFAULT_STOP, WalkTimes
from ..settings import modes as mode_list
from ..settings import show
from ..settings.config import (
    _flag,
    _table,
    _text,
    _text_size,
    _texts,
    _whole,
    _windows,
    board_modes,
    holiday_division,
    state_dir,
    switched_on,
)
from ..settings.schema import (
    BOARD_DEFAULTS,
    CRS,
    DEFAULT_BUS_SOURCE,
    LABEL,
    LONDON_STOP,
    MAX_TRAM_STOPS,
    NOC,
    TRAM_STOP,
    bus_defaults,
    views,
)
from ..settings.store import Store
from ..sources.bods import BodsVehicleSource
from ..sources.bodsbus import BodsBusSource
from ..sources.darwin import DEFAULT_BASE_URL as DARWIN_BASE_URL
from ..sources.darwin import DarwinSource, ServiceDetailsClient, TrainDetails
from ..sources.holidays import ENGLAND_AND_WALES
from ..sources.tfl import TflSource
from ..sources.trams import TramTimetableSource
from ..sources.transportapi import DEFAULT_BASE_URL as TAPI_BASE_URL
from ..sources.transportapi import TransportApiBusSource
from ..sources.weather import OpenMeteoSource
from .app import Board, SourceConfig, Tracker, Weather, reset_code_page
from .names import DISPLAY_PANELS, themes_for
from .positions import Positions
from .route import RouteStop, RouteStrip
from .themes import theme_named, with_text_size

# What the set-me-up page is drawn with, from config.toml where they load.
_LOOK = ("display", "rotate", "rotate_180", "theme", "accent", "panel_colour", "text_size", "timezone")


def first_run_settings(store: Store | Path | str) -> Settings:
    """Settings for a board not yet set up: no departures, the display and
    look the settings give (or the defaults, if they don't load), and the
    reset code page, with the state file in the store."""
    store = Store.of(store)
    try:
        data = store.data()
    except (OSError, ConfigError):
        data = {}
    known = {"state_file": str(store.state_file)}
    try:
        return settings_from({**known, **{key: data[key] for key in _LOOK if key in data}}, {})
    except ConfigError:
        return settings_from(known, {})


@dataclass(frozen=True)
class Settings:
    board: Board
    timezone: str
    state_file: Path
    accent: str
    rotate: int
    display: str = "inky"
    # The original pHAT can't be detected, so its colour is set here; it is
    # separate from the accent, which the look draws its highlights in.
    panel_colour: str = "red"
    # Which of gov.uk's bank holiday lists the checking times follow.
    bank_holiday_division: str = ENGLAND_AND_WALES


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing secret {name}; set it in secrets.env")
    return value


def _policy(section: dict, defaults: dict, where: str, holidays: frozenset[date]) -> PollPolicy:
    """When and how often to check, from the section or else `defaults`. A
    section without windows has the defaults' (none would mean never)."""
    windows = _windows(section, "windows", defaults["windows"], where)
    interval = timedelta(minutes=_whole(section, "interval_minutes", defaults["interval_minutes"], where))
    return PollPolicy(windows, interval, holidays)


def _train(section: dict, env: Mapping[str, str], holidays: frozenset[date]) -> SourceConfig:
    where = "[train] "
    crs = section.get("crs")
    if not isinstance(crs, str) or not crs.strip():
        raise ConfigError('[train] needs crs, the 3-letter station code, like crs = "GLO"')
    if not CRS.fullmatch(crs.strip()):
        raise ConfigError(f'[train] crs is {crs!r}; set your station\'s 3-letter code, like crs = "GLO"')
    defaults = BOARD_DEFAULTS["train"]
    policy = _policy(section, defaults, where, holidays)
    source = DarwinSource(
        api_key=_secret(env, "RDM_API_KEY"),
        crs=crs,
        filter_crs=_text(section, "filter_crs", "", where) or None,  # "" is the page's "no filter"
        rows=_whole(section, "rows", defaults["rows"], where),
        base_url=_text(section, "base_url", DARWIN_BASE_URL, where),
    )
    stale_after = timedelta(minutes=_whole(section, "stale_after_minutes", defaults["stale_after_minutes"], where))
    return SourceConfig(source, policy, stale_after)


def _stops(section: dict) -> dict[str, str]:
    stops = _table(section, "stops", "[bus.stops]")
    if not stops:
        raise ConfigError("Add a [bus.stops] table of label = ATCO code, or remove [bus]")
    for label, atco in stops.items():
        if not isinstance(atco, str) or not atco or atco == "CHANGE_ME":
            raise ConfigError(f"[bus.stops] {label} is {atco!r}; set a real ATCO code in quotes")
    return stops


def _transportapi(section: dict, env: Mapping[str, str], stops: dict[str, str]) -> TransportApiBusSource:
    return TransportApiBusSource(
        app_id=_secret(env, "TAPI_APP_ID"),
        app_key=_secret(env, "TAPI_APP_KEY"),
        stops=stops,
        rows=_whole(section, "rows", BOARD_DEFAULTS["bus"]["rows"], "[bus] "),
        directions=_texts(section, "directions", "[bus] ") or None,
        base_url=_text(section, "base_url", TAPI_BASE_URL, "[bus] "),
    )


def _operators(section: dict) -> list[str]:
    operators = _texts(section, "operators", "[bus] ")
    if not operators:
        raise ConfigError(
            '[bus] needs operators, the codes of the bus companies whose timetables to read, '
            'like operators = ["BNML", "HIPK"]; or source = "transportapi" to use TransportAPI alone'
        )
    for code in operators:
        if not NOC.fullmatch(code):
            raise ConfigError(f"[bus] operators: {code!r} is not an operator code, like BNML")
    return operators


def _bus(
    section: dict, env: Mapping[str, str], holidays: frozenset[date], folder: Path, timezone: str
) -> SourceConfig:
    """The BODS timetable and live positions, falling back to TransportAPI if
    its keys are set; or with source = "transportapi" TransportAPI alone."""
    where = "[bus] "
    stops = _stops(section)
    kind = _text(section, "source", DEFAULT_BUS_SOURCE, where)
    if kind not in ("transportapi", "bods"):
        raise ConfigError(f'[bus] source must be "transportapi" or "bods", not {kind!r}')
    defaults = bus_defaults(kind)
    stale_after = timedelta(minutes=_whole(section, "stale_after_minutes", defaults["stale_after_minutes"], where))
    daily_budget = _whole(section, "daily_budget", defaults["daily_budget"], where)
    if kind == "transportapi":
        return SourceConfig(_transportapi(section, env, stops), _policy(section, defaults, where, holidays),
                            stale_after, daily_budget=daily_budget)
    has_tapi = env.get("TAPI_APP_ID", "").strip() and env.get("TAPI_APP_KEY", "").strip()
    source = BodsBusSource(
        folder,
        stops,
        _secret(env, "BODS_API_KEY"),
        ZoneInfo(timezone),
        operators=_operators(section),
        rows=_whole(section, "rows", defaults["rows"], where),
        directions=_texts(section, "directions", where) or None,
        fallback=_transportapi(section, env, stops) if has_tapi else None,
    )
    # The timetable costs nothing to read, so it is checked often and all day;
    # TransportAPI, only a backup, at its own slower pace and in its own windows
    # (the commute), never the bus windows, so the daily allowance lasts.
    policy = _policy(section, defaults, where, holidays)
    fallback_windows = _windows(section, "fallback_windows", BOARD_DEFAULTS["bus"]["windows"], where)
    fallback_every = timedelta(minutes=_whole(section, "fallback_interval_minutes",
                                              defaults["fallback_interval_minutes"], where))
    return SourceConfig(
        source,
        policy,
        stale_after,
        daily_budget=daily_budget,
        fallback_policy=PollPolicy(fallback_windows, fallback_every, policy.holidays),
    )


def _tram_stops(section: dict) -> dict[str, str]:
    stops = _table(section, "stops", "[metro.stops]")
    if not stops:
        raise ConfigError("Add a [metro.stops] table of label = tram stop code, like T = \"9400ZZMAPIC\", "
                          "or remove [metro]")
    if len(stops) > MAX_TRAM_STOPS:
        raise ConfigError(f"[metro.stops] has {len(stops)} stops; a board has at most {MAX_TRAM_STOPS}")
    for label, code in stops.items():
        if not isinstance(label, str) or not LABEL.fullmatch(label):
            raise ConfigError(f"[metro.stops] label {str(label)[:20]!r} must be 1 to 3 letters or digits")
        if not isinstance(code, str) or not (TRAM_STOP.fullmatch(code) or LONDON_STOP.fullmatch(code)):
            raise ConfigError(f"[metro.stops] {label} is {str(code)[:40]!r}; set a Metrolink stop code in quotes, "
                              'like "9400ZZMAPIC", or TfL\'s for a London station or pier, like "940GZZLUBNK"')
    if len({bool(LONDON_STOP.fullmatch(code)) for code in stops.values()}) > 1:
        raise ConfigError("[metro.stops] has Metrolink stops and London ones; a board has one or the other")
    return stops


# A key as TfL gives one: letters and digits. It goes in a header, so nothing else is let through.
_TFL_KEY = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")


def _tfl_key(env: Mapping[str, str]) -> str:
    """TFL_APP_KEY, optional: without one TfL allows fewer calls a minute."""
    key = env.get("TFL_APP_KEY", "").strip()
    if key and not _TFL_KEY.fullmatch(key):
        raise ConfigError("TFL_APP_KEY in secrets.env isn't a key: paste it again from TfL's API portal")
    return key


def _metro(section: dict, env: Mapping[str, str], holidays: frozenset[date], folder: Path,
           timezone: str) -> SourceConfig:
    """Manchester Metrolink's trams, from TfGM's timetable kept in the store;
    or, for London stops, live times from TfL."""
    where = "[metro] "
    defaults = BOARD_DEFAULTS["metro"]
    stops = _tram_stops(section)
    rows = _whole(section, "rows", defaults["rows"], where)
    directions = _texts(section, "directions", where) or None
    source: TramTimetableSource | TflSource
    if any(LONDON_STOP.fullmatch(code) for code in stops.values()):
        known = london_stops_by_code()
        source = TflSource(stops, ZoneInfo(timezone), app_key=_tfl_key(env), rows=rows, directions=directions,
                           lines={code: known[code].lines for code in stops.values() if code in known})
    else:
        source = TramTimetableSource(folder, stops, ZoneInfo(timezone), rows=rows, directions=directions)
    stale_after = timedelta(minutes=_whole(section, "stale_after_minutes", defaults["stale_after_minutes"], where))
    return SourceConfig(source, _policy(section, defaults, where, holidays), stale_after)


def _route_stop(line: str, entry) -> RouteStop:
    try:
        name, lat, lon = str(entry["name"]).strip(), float(entry["lat"]), float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"[bus.routes.{line}] each stop needs name, lat and lon") from None
    if not name or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ConfigError(f"[bus.routes.{line}] stop {entry!r} has no name or an impossible position")
    return RouteStop(name, lat, lon)


def _tracker(section: dict, env: Mapping[str, str], bus: PollPolicy) -> Tracker | None:
    """The route strip page's bus positions, if any routes are set up."""
    routes = _table(section, "routes", "[bus.routes]")
    if not routes:
        return None
    strips = {}
    for line, route in routes.items():
        if not isinstance(route, dict):
            raise ConfigError(f"[bus.routes.{line}] must be a table of stop and stops, not {route!r}")
        stops = route.get("stops") or []
        if not isinstance(stops, list) or len(stops) < 2:
            raise ConfigError(f"[bus.routes.{line}] needs a list of at least two stops, yours last")
        label = route.get("stop")
        if not isinstance(label, str) or label not in _stops(section):
            raise ConfigError(f"[bus.routes.{line}] stop = {label!r} is not one of [bus.stops]")
        strips[str(line)] = RouteStrip(str(line), label, tuple(_route_stop(line, e) for e in stops))
    boxes = [strip.box() for strip in strips.values()]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    source = BodsVehicleSource(_secret(env, "BODS_API_KEY"), box, set(strips))
    interval = timedelta(seconds=_whole(section, "position_interval_seconds", 30, "[bus] "))
    if interval < timedelta(seconds=10):
        raise ConfigError("position_interval_seconds must be at least 10; BODS refreshes every 10 s")
    # The buses' own windows and holidays: positions are only wanted while buses are.
    return Tracker(source, strips, PollPolicy(bus.windows, interval, bus.holidays))


def _positions(env: Mapping[str, str], trains: bool) -> Positions:
    """Where each shown train and bus is, for a screen's rows. The buses come
    from the bus source's own live board; the trains need Darwin's Service
    Details, with its own optional key, which goes in a header: anything that
    could split one is refused, without repeating the key."""
    key = env.get("RDM_DETAILS_API_KEY", "").strip()
    if key and (not key.isprintable() or any(c.isspace() for c in key)):
        raise ConfigError("RDM_DETAILS_API_KEY has spaces or control characters in it")
    return Positions(details=TrainDetails(ServiceDetailsClient(key)) if key and trains else None)


def _coordinate(section: dict, key: str, limit: int) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -limit <= value <= limit:
        raise ConfigError(f"[weather] {key} must be a number from -{limit} to {limit}, not {value!r}")
    return float(value)


def _weather(data: dict, sources: list[SourceConfig], timezone: str) -> Weather | None:
    """The rain warning, if [weather] gives a position. It is polled every 30
    minutes, only while a train or bus window is open."""
    section = _table(data, "weather", "[weather]")  # absent if switched off; see switched_on
    if section.get("lat", "") == "" and section.get("lon", "") == "":  # "" is a cleared field
        return None
    lat, lon = _coordinate(section, "lat", 90), _coordinate(section, "lon", 180)
    threshold = section.get("threshold_percent", 50)
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 100:
        raise ConfigError(f"[weather] threshold_percent must be a whole number from 1 to 100, not {threshold!r}")
    windows = tuple(window for config in sources for window in config.policy.windows)
    return Weather(OpenMeteoSource(lat, lon, timezone), PollPolicy(windows, timedelta(minutes=30)), threshold)


def _walk(data: dict) -> WalkTimes:
    train_section = _table(data, "train", "[train]")
    train = None
    if train_section.get("walk_minutes", "") != "":  # "" is the page's "no walk time"
        train = _whole(train_section, "walk_minutes", 0, "[train] ", least=0)
    bus_section = _table(data, "bus", "[bus]")
    bus = bus_section.get("walk_minutes", {})
    if isinstance(bus, int) and not isinstance(bus, bool):
        bus = {DEFAULT_STOP: _whole(bus_section, "walk_minutes", 0, "[bus] ", least=0)}
    elif isinstance(bus, dict):
        for label in bus:
            _whole(bus, label, 0, "[bus.walk_minutes] ", least=0)
    else:
        raise ConfigError(f"[bus] walk_minutes must be a number or a table of label = minutes, not {bus!r}")
    metro = _table(_table(data, "metro", "[metro]"), "walk_minutes", "[metro.walk_minutes]")
    for label in metro:
        _whole(metro, label, 0, "[metro.walk_minutes] ", least=0)
    return WalkTimes(train=train, bus=dict(bus), metro=dict(metro))


def _rotate(data: dict, epaper: bool) -> int:
    # rotate_180 = true is the older spelling of rotate = 180.
    rotate = data.get("rotate", 180 if data.get("rotate_180") else BOARD_DEFAULTS["rotate"])
    allowed = (0, 180) if epaper else (0, 90, 180, 270)
    if rotate not in allowed:
        raise ConfigError(f"rotate must be one of {', '.join(map(str, allowed))} for this display, not {rotate!r}")
    return rotate


# What [board] may hold: what the board shows (settings/show.py) and its
# modes (settings/modes.py).
_BOARD_KEYS = {"show", "modes", "leave_modes"}


def _board_show(data: dict) -> frozenset[str]:
    """What the board shows: [board] show, or without it everything, the
    clock page only if the older clock_page = true says so."""
    board = _table(data, "board", "[board]")
    unknown = sorted(set(board) - _BOARD_KEYS)
    if unknown:
        raise ConfigError(f"[board] has unknown settings: {', '.join(map(str, unknown))}")
    _flag(data, show.OLDER_CLOCK_PAGE, False)
    return show.shown(data, "board")


def _quiet_hours(data: dict) -> tuple:
    """The quiet hours ([dashboard] quiet_windows, or the dashboard's own
    default), when a screen's speaker says nothing either."""
    section = _table(data, "dashboard", "[dashboard]")
    if "quiet_windows" not in section:
        from ..dashboard.config import DashboardOptions  # here: it imports this module

        return DashboardOptions.quiet_windows
    return _windows(section, "quiet_windows", (), "[dashboard] ")


def load_config(
    store: Store | Path | str,
    env: Mapping[str, str],
    bus_stops: dict[str, str] | None = None,
    only: set[str] | None = None,
    display: str | None = None,
    holidays: frozenset[date] = frozenset(),
) -> Settings:
    """Load settings from a store (or the store of a config.toml path). `only`
    limits which sources are built, so a probe needs only its own keys.
    `display` overrides the configured one, as the emulator's --display does. `holidays`
    are bank holiday dates, used unless the settings say to ignore them."""
    data = Store.of(store).data()
    if display:
        data["display"] = display
    if bus_stops:
        data.setdefault("bus", {})["stops"] = bus_stops
    return settings_from(data, env, only, holidays)


def settings_from(
    data: dict, env: Mapping[str, str], only: set[str] | None = None, holidays: frozenset[date] = frozenset(),
    modes: Sequence[str] | None = None,
) -> Settings:
    """Settings from already-read config data; the web page checks a change with this before saving.
    Sources are built for the modes the board shows, or for `modes` (the
    dashboard's), or for those in `only` alone (a probe's)."""
    data = switched_on(data)
    views(data)  # checked here too, so a malformed [app] views is never saved
    try:
        theme = theme_named(_text(data, "theme", BOARD_DEFAULTS["theme"]))
    except ValueError as error:
        raise ConfigError(str(error)) from error
    theme = with_text_size(theme, _text_size(data))
    display = _text(data, "display", BOARD_DEFAULTS["display"])
    if display not in DISPLAY_PANELS:
        raise ConfigError(f"Unknown display {display!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")
    if theme.screen_only and DISPLAY_PANELS[display].epaper:
        raise ConfigError(f"The {theme.name} look is for a screen; choose from "
                          f"{', '.join(themes_for(epaper=True))} for this display")
    rotate = _rotate(data, DISPLAY_PANELS[display].epaper)
    accent = data.get("accent") or theme.accent  # "" is the page's "the look's own"
    if accent not in ("red", "yellow"):
        raise ConfigError(f'accent must be "red" or "yellow", not {accent!r}')
    panel_colour = data.get("panel_colour", accent)
    if panel_colour not in ("red", "yellow", "black"):
        raise ConfigError(f"panel_colour must be red, yellow or black, not {panel_colour!r}")
    timezone = _text(data, "timezone", BOARD_DEFAULTS["timezone"])
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError):  # ZoneInfoNotFoundError is a KeyError
        raise ConfigError(f"Unknown timezone {timezone!r}; use one like Europe/London") from None
    state_file = _text(data, "state_file", "state.json")
    # On by default: services run a Sunday timetable on bank holidays.
    holidays = frozenset(holidays) if _flag(data, "bank_holidays", BOARD_DEFAULTS["bank_holidays"]) else frozenset()
    division = holiday_division(data)
    # Only the board refuses modes its display has no room for: the dashboard's
    # modes, and a probe's or timetable build's own, are loaded without it.
    if only is None and modes is None:
        shown_modes = board_modes(data)
    else:
        shown_modes = mode_list.ordered(list(only or modes or ()))
    sources = []
    tracker = None
    wanted = set(shown_modes)
    if "train" in data and "train" in wanted:
        sources.append(_train(_table(data, "train", "[train]"), env, holidays))
    if "metro" in data and "metro" in wanted:
        sources.append(_metro(_table(data, "metro", "[metro]"), env, holidays, Path(state_file).parent, timezone))
    if "bus" in data and "bus" in wanted:
        bus = _bus(_table(data, "bus", "[bus]"), env, holidays, Path(state_file).parent, timezone)
        sources.append(bus)
        # Bus positions are only drawn on a screen; e-paper never asks for them.
        if not DISPLAY_PANELS[display].epaper:
            tracker = _tracker(data["bus"], env, bus.policy)
    # Nor where each train and bus is; nor does a probe of one source.
    positions = None
    if sources and only is None and not DISPLAY_PANELS[display].epaper:
        positions = _positions(env, any(isinstance(c.source, DarwinSource) for c in sources))
        # A screen scrolls the first train's "Calling at" line: the board
        # with calling points, the same call with more in its answer.
        for config in sources:
            if isinstance(config.source, DarwinSource):
                config.source.with_details = True
    # Where the settings site leaves a reset code: beside the state file, in the store.
    folder = state_dir(data)
    shown = _board_show(data)
    # Only a screen speaks; the quiet hours are read only for its speaker.
    speaks = "announce_speaker" in shown and not DISPLAY_PANELS[display].epaper
    board = Board(
        title=_text(data, "title", BOARD_DEFAULTS["title"]),
        sources=sources,
        min_redraw=timedelta(minutes=_whole(data, "min_redraw_minutes", BOARD_DEFAULTS["min_redraw_minutes"])),
        walk=_walk(data),
        theme=theme,
        tracker=tracker,
        # A probe of one source needs no forecast.
        weather=_weather(data, sources, timezone) if only is None else None,
        reset_code=reset_code_page(folder, timezone) if folder and only is None else None,
        positions=positions,
        show=shown,
        quiet=_quiet_hours(data) if speaks else (),
        scrolling=_flag(data, "scrolling", BOARD_DEFAULTS["scrolling"]),
        modes=shown_modes,
        leave=mode_list.leave(data, "board"),
    )
    return Settings(
        board=board,
        timezone=timezone,
        state_file=Path(state_file),
        accent=accent,
        rotate=rotate,
        display=display,
        panel_colour=panel_colour,
        bank_holiday_division=division,
    )
