"""Loads config.toml (settings) and the environment (secrets) into a Board.

Settings saved from the web page live in settings.toml next to the state
file and are layered over config.toml, so the service never writes to /etc
and comments in a hand-edited config.toml survive. Keys saved there go in a
secrets.env beside it, layered over the main one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from .app import Board, SourceConfig, Tracker, Weather
from .bods import BodsVehicleSource
from .darwin import DEFAULT_BASE_URL as DARWIN_BASE_URL
from .darwin import DarwinSource
from .display import DISPLAY_PANELS
from .schedule import PollPolicy, parse_window
from .transportapi import DEFAULT_BASE_URL as TAPI_BASE_URL
from .themes import theme_named
from .transportapi import TransportApiBusSource
from .route import RouteStop, RouteStrip
from .walk import DEFAULT_STOP, WalkTimes
from .weather import OpenMeteoSource


class ConfigError(Exception):
    pass


OVERLAY_NAME = "settings.toml"
SECRETS_OVERLAY_NAME = "secrets.env"


def state_dir(data: dict) -> Path | None:
    """Where the web page saves settings: beside an explicitly set state_file.
    Without one there is no overlay, rather than whatever is in the working directory."""
    state_file = data.get("state_file")
    if state_file is not None and not isinstance(state_file, str):
        raise ConfigError(f"state_file must be a path in quotes, not {state_file!r}")
    return Path(state_file).parent if state_file else None


def overlay_paths(config_path: Path | str) -> tuple[Path, Path] | None:
    """(settings overlay, secrets overlay) for a config file, if it has a state_file."""
    folder = state_dir(read_base(config_path))
    return (folder / OVERLAY_NAME, folder / SECRETS_OVERLAY_NAME) if folder else None


def merge(base: dict, over: dict) -> dict:
    """Top-level tables like [bus] merge key by key, so keys the page doesn't
    manage (like bus.routes) survive; anything else in `over`, tables below the
    top level included, replaces what's in `base`, so the page can remove a stop."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def read_base(path: Path | str) -> dict:
    """config.toml alone, without the web page's settings."""
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error


def read_overlay(data: dict) -> dict:
    """The web page's saved settings for already-read config.toml data; empty if none."""
    folder = state_dir(data)
    if folder is None:
        return {}
    overlay = folder / OVERLAY_NAME
    try:
        saved = tomllib.loads(overlay.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{overlay}: {error}") from error
    saved.pop("state_file", None)  # it decides where the overlay is; only config.toml sets it
    return saved


def read_config(path: Path | str) -> dict:
    """config.toml with the web page's settings layered over it."""
    data = read_base(path)
    return merge(data, read_overlay(data))


@dataclass(frozen=True)
class Settings:
    board: Board
    timezone: str
    state_file: Path
    accent: str
    rotate: int
    display: str = "inky"


def _secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing secret {name}; set it in secrets.env")
    return value


# Hand edits can put anything anywhere; these turn a wrong type into a ConfigError
# naming the setting, rather than a crash now or on some later tick.
_MOST = 100_000  # far above any sensible value, far below what timedelta overflows on


def _table(parent: dict, key: str, where: str) -> dict:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table, not {value!r}")
    return value


def _whole(section: dict, key: str, default: int, where: str = "", least: int = 1) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= _MOST:
        raise ConfigError(f"{where}{key} must be a whole number from {least} to {_MOST}, not {value!r}")
    return value


def _text(section: dict, key: str, default: str, where: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}{key} must be text in quotes, not {value!r}")
    return value


def _flag(section: dict, key: str, default: bool, where: str = "") -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}{key} must be true or false, not {value!r}")
    return value


def _texts(section: dict, key: str, where: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where}{key} must be a list of text, like ["a", "b"], not {value!r}')
    return value


def _policy(section: dict, default_interval: int, where: str, holidays: frozenset[date]) -> PollPolicy:
    try:
        windows = tuple(parse_window(w) for w in _texts(section, "windows", where))
    except ValueError as error:
        raise ConfigError(str(error)) from error
    interval = timedelta(minutes=_whole(section, "interval_minutes", default_interval, where))
    return PollPolicy(windows, interval, holidays)


def _train(section: dict, env: Mapping[str, str], holidays: frozenset[date]) -> SourceConfig:
    where = "[train] "
    crs = section.get("crs")
    if not isinstance(crs, str) or not crs.strip():
        raise ConfigError('[train] needs crs, the 3-letter station code, like crs = "GLO"')
    policy = _policy(section, 2, where, holidays)
    source = DarwinSource(
        api_key=_secret(env, "RDM_API_KEY"),
        crs=crs,
        filter_crs=_text(section, "filter_crs", "", where) or None,  # "" is the page's "no filter"
        rows=_whole(section, "rows", 3, where),
        base_url=_text(section, "base_url", DARWIN_BASE_URL, where),
    )
    return SourceConfig(source, policy, timedelta(minutes=_whole(section, "stale_after_minutes", 6, where)))


def _stops(section: dict) -> dict[str, str]:
    stops = _table(section, "stops", "[bus.stops]")
    if not stops:
        raise ConfigError("Add a [bus.stops] table of label = ATCO code, or remove [bus]")
    for label, atco in stops.items():
        if not isinstance(atco, str) or not atco or atco == "CHANGE_ME":
            raise ConfigError(f"[bus.stops] {label} is {atco!r}; set a real ATCO code in quotes")
    return stops


def _bus(section: dict, env: Mapping[str, str], holidays: frozenset[date]) -> SourceConfig:
    where = "[bus] "
    stops = _stops(section)
    policy = _policy(section, 12, where, holidays)
    source = TransportApiBusSource(
        app_id=_secret(env, "TAPI_APP_ID"),
        app_key=_secret(env, "TAPI_APP_KEY"),
        stops=stops,
        rows=_whole(section, "rows", 2, where),
        directions=_texts(section, "directions", where) or None,
        base_url=_text(section, "base_url", TAPI_BASE_URL, where),
    )
    return SourceConfig(
        source,
        policy,
        timedelta(minutes=_whole(section, "stale_after_minutes", 30, where)),
        daily_budget=_whole(section, "daily_budget", 28, where),
    )


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


def _coordinate(section: dict, key: str, limit: int) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -limit <= value <= limit:
        raise ConfigError(f"[weather] {key} must be a number from -{limit} to {limit}, not {value!r}")
    return float(value)


def _weather(data: dict, sources: list[SourceConfig], timezone: str) -> Weather | None:
    """The rain warning, if [weather] gives a position. It is polled every 30
    minutes, only while a train or bus window is open."""
    section = _table(data, "weather", "[weather]")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"[weather] enabled must be true or false, not {enabled!r}")
    if not enabled:  # switched off on the settings page; the location stays in config.toml
        return None
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
    return WalkTimes(train=train, bus=dict(bus))


def _rotate(data: dict, epaper: bool) -> int:
    # rotate_180 = true is the older spelling of rotate = 180.
    rotate = data.get("rotate", 180 if data.get("rotate_180") else 0)
    allowed = (0, 180) if epaper else (0, 90, 180, 270)
    if rotate not in allowed:
        raise ConfigError(f"rotate must be one of {', '.join(map(str, allowed))} for this display, not {rotate!r}")
    return rotate


def load_config(
    path: Path | str,
    env: Mapping[str, str],
    bus_stops: dict[str, str] | None = None,
    only: set[str] | None = None,
    display: str | None = None,
    holidays: frozenset[date] = frozenset(),
) -> Settings:
    """Load settings. `only` limits which sources are built, so a probe needs only its own keys.
    `display` overrides the configured one, as the emulator's --display does. `holidays`
    are bank holiday dates, used unless the settings say to ignore them."""
    data = read_config(path)
    if display:
        data["display"] = display
    if bus_stops:
        data.setdefault("bus", {})["stops"] = bus_stops
    return settings_from(data, env, only, holidays)


def settings_from(
    data: dict, env: Mapping[str, str], only: set[str] | None = None, holidays: frozenset[date] = frozenset()
) -> Settings:
    """Settings from already-read config data; the web page checks a change with this before saving."""
    try:
        theme = theme_named(_text(data, "theme", "sign"))
    except ValueError as error:
        raise ConfigError(str(error)) from error
    display = _text(data, "display", "inky")
    if display not in DISPLAY_PANELS:
        raise ConfigError(f"Unknown display {display!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")
    rotate = _rotate(data, DISPLAY_PANELS[display].epaper)
    accent = data.get("accent") or theme.accent  # "" is the page's "the look's own"
    if accent not in ("red", "yellow"):
        raise ConfigError(f'accent must be "red" or "yellow", not {accent!r}')
    timezone = _text(data, "timezone", "Europe/London")
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError):  # ZoneInfoNotFoundError is a KeyError
        raise ConfigError(f"Unknown timezone {timezone!r}; use one like Europe/London") from None
    state_file = _text(data, "state_file", "state.json")
    # On by default: services run a Sunday timetable on bank holidays.
    holidays = frozenset(holidays) if _flag(data, "bank_holidays", True) else frozenset()
    sources = []
    tracker = None
    wanted = only or {"train", "bus"}
    if "train" in data and "train" in wanted:
        sources.append(_train(_table(data, "train", "[train]"), env, holidays))
    if "bus" in data and "bus" in wanted:
        bus = _bus(_table(data, "bus", "[bus]"), env, holidays)
        sources.append(bus)
        # Bus positions are only drawn on a screen; e-paper never asks for them.
        if not DISPLAY_PANELS[display].epaper:
            tracker = _tracker(data["bus"], env, bus.policy)
    board = Board(
        title=_text(data, "title", "Departures"),
        sources=sources,
        min_redraw=timedelta(minutes=_whole(data, "min_redraw_minutes", 2)),
        walk=_walk(data),
        theme=theme,
        tracker=tracker,
        # A probe of one source needs no forecast.
        weather=_weather(data, sources, timezone) if only is None else None,
    )
    return Settings(
        board=board,
        timezone=timezone,
        state_file=Path(state_file),
        accent=accent,
        rotate=rotate,
        display=display,
    )
