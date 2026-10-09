"""Loads config.toml (settings) and the environment (secrets) into a Board.

Settings saved from the web page live in settings.toml next to the state
file and are layered over config.toml, so the service never writes to /etc
and comments in a hand-edited config.toml survive. Keys saved there go in a
secrets.env beside it, layered over the main one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping

from .app import Board, SourceConfig, Tracker
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


class ConfigError(Exception):
    pass


OVERLAY_NAME = "settings.toml"
SECRETS_OVERLAY_NAME = "secrets.env"


def state_dir(data: dict) -> Path | None:
    """Where the web page saves settings: beside an explicitly set state_file.
    Without one there is no overlay, rather than whatever is in the working directory."""
    state_file = data.get("state_file")
    return Path(state_file).parent if state_file else None


def overlay_paths(config_path: Path | str) -> tuple[Path, Path] | None:
    """(settings overlay, secrets overlay) for a config file, if it has a state_file."""
    folder = state_dir(tomllib.loads(Path(config_path).read_text()))
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
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


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


def _policy(section: dict, default_interval: int) -> PollPolicy:
    try:
        windows = tuple(parse_window(w) for w in section.get("windows", []))
    except ValueError as error:
        raise ConfigError(str(error)) from error
    interval = timedelta(minutes=section.get("interval_minutes", default_interval))
    return PollPolicy(windows, interval)


def _train(section: dict, env: Mapping[str, str]) -> SourceConfig:
    policy = _policy(section, 2)
    source = DarwinSource(
        api_key=_secret(env, "RDM_API_KEY"),
        crs=section["crs"],
        filter_crs=section.get("filter_crs") or None,  # "" is the page's "no filter"
        rows=section.get("rows", 3),
        base_url=section.get("base_url", DARWIN_BASE_URL),
    )
    return SourceConfig(source, policy, timedelta(minutes=section.get("stale_after_minutes", 6)))


def _stops(section: dict) -> dict[str, str]:
    stops = section.get("stops") or {}
    if not stops:
        raise ConfigError("Add a [bus.stops] table of label = ATCO code, or remove [bus]")
    for label, atco in stops.items():
        if not atco or atco == "CHANGE_ME":
            raise ConfigError(f"[bus.stops] {label} is {atco!r}; set a real ATCO code")
    return stops


def _bus(section: dict, env: Mapping[str, str]) -> SourceConfig:
    stops = _stops(section)
    policy = _policy(section, 12)
    source = TransportApiBusSource(
        app_id=_secret(env, "TAPI_APP_ID"),
        app_key=_secret(env, "TAPI_APP_KEY"),
        stops=stops,
        rows=section.get("rows", 2),
        directions=section.get("directions") or None,
        base_url=section.get("base_url", TAPI_BASE_URL),
    )
    return SourceConfig(
        source,
        policy,
        timedelta(minutes=section.get("stale_after_minutes", 30)),
        daily_budget=section.get("daily_budget", 28),
    )


def _route_stop(line: str, entry) -> RouteStop:
    try:
        name, lat, lon = str(entry["name"]).strip(), float(entry["lat"]), float(entry["lon"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"[bus.routes.{line}] each stop needs name, lat and lon") from None
    if not name or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ConfigError(f"[bus.routes.{line}] stop {entry!r} has no name or an impossible position")
    return RouteStop(name, lat, lon)


def _tracker(section: dict, env: Mapping[str, str], windows) -> Tracker | None:
    """The route strip page's bus positions, if any routes are set up."""
    routes = section.get("routes") or {}
    if not routes:
        return None
    strips = {}
    for line, route in routes.items():
        stops = route.get("stops") or []
        if len(stops) < 2:
            raise ConfigError(f"[bus.routes.{line}] needs at least two stops, yours last")
        label = route.get("stop")
        if label not in (section.get("stops") or {}):
            raise ConfigError(f"[bus.routes.{line}] stop = {label!r} is not one of [bus.stops]")
        strips[str(line)] = RouteStrip(str(line), label, tuple(_route_stop(line, e) for e in stops))
    boxes = [strip.box() for strip in strips.values()]
    box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
    source = BodsVehicleSource(_secret(env, "BODS_API_KEY"), box, set(strips))
    interval = timedelta(seconds=section.get("position_interval_seconds", 30))
    if interval < timedelta(seconds=10):
        raise ConfigError("position_interval_seconds must be at least 10; BODS refreshes every 10 s")
    return Tracker(source, strips, PollPolicy(windows, interval))


def _walk(data: dict) -> WalkTimes:
    train = data.get("train", {}).get("walk_minutes")
    if train == "":
        train = None  # the page's "no walk time"
    bus = data.get("bus", {}).get("walk_minutes", {})
    if isinstance(bus, int):
        bus = {DEFAULT_STOP: bus}
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
) -> Settings:
    """Load settings. `only` limits which sources are built, so a probe needs only its own keys.
    `display` overrides the configured one, as the emulator's --display does."""
    data = read_config(path)
    if display:
        data["display"] = display
    if bus_stops:
        data.setdefault("bus", {})["stops"] = bus_stops
    return settings_from(data, env, only)


def settings_from(data: dict, env: Mapping[str, str], only: set[str] | None = None) -> Settings:
    """Settings from already-read config data; the web page checks a change with this before saving."""
    try:
        theme = theme_named(data.get("theme", "sign"))
    except ValueError as error:
        raise ConfigError(str(error)) from error
    display = data.get("display", "inky")
    if display not in DISPLAY_PANELS:
        raise ConfigError(f"Unknown display {display!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")
    rotate = _rotate(data, DISPLAY_PANELS[display].epaper)
    sources = []
    tracker = None
    wanted = only or {"train", "bus"}
    if "train" in data and "train" in wanted:
        sources.append(_train(data["train"], env))
    if "bus" in data and "bus" in wanted:
        bus = _bus(data["bus"], env)
        sources.append(bus)
        # Bus positions are only drawn on a screen; e-paper never asks for them.
        if not DISPLAY_PANELS[display].epaper:
            tracker = _tracker(data["bus"], env, bus.policy.windows)
    board = Board(
        title=data.get("title", "Departures"),
        sources=sources,
        min_redraw=timedelta(minutes=data.get("min_redraw_minutes", 2)),
        walk=_walk(data),
        theme=theme,
        tracker=tracker,
    )
    return Settings(
        board=board,
        timezone=data.get("timezone", "Europe/London"),
        state_file=Path(data.get("state_file", "state.json")),
        accent=data.get("accent") or theme.accent,  # "" is the page's "the look's own"
        rotate=rotate,
        display=display,
    )
