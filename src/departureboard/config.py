"""Loads config.toml (settings) and the environment (secrets) into a Board."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Mapping

from .app import Board, SourceConfig
from .darwin import DEFAULT_BASE_URL as DARWIN_BASE_URL
from .darwin import DarwinSource
from .schedule import PollPolicy, parse_window
from .transportapi import DEFAULT_BASE_URL as TAPI_BASE_URL
from .transportapi import TransportApiBusSource
from .walk import DEFAULT_STOP, WalkTimes


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    board: Board
    timezone: str
    state_file: Path
    accent: str
    rotate_180: bool


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
        filter_crs=section.get("filter_crs"),
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


def _walk(data: dict) -> WalkTimes:
    train = data.get("train", {}).get("walk_minutes")
    bus = data.get("bus", {}).get("walk_minutes", {})
    if isinstance(bus, int):
        bus = {DEFAULT_STOP: bus}
    return WalkTimes(train=train, bus=dict(bus))


def load_config(
    path: Path | str,
    env: Mapping[str, str],
    bus_stops: dict[str, str] | None = None,
    only: set[str] | None = None,
) -> Settings:
    """Load settings. `only` limits which sources are built, so a probe needs only its own keys."""
    data = tomllib.loads(Path(path).read_text())
    if bus_stops:
        data.setdefault("bus", {})["stops"] = bus_stops
    sources = []
    wanted = only or {"train", "bus"}
    if "train" in data and "train" in wanted:
        sources.append(_train(data["train"], env))
    if "bus" in data and "bus" in wanted:
        sources.append(_bus(data["bus"], env))
    board = Board(
        title=data.get("title", "Departures"),
        sources=sources,
        min_redraw=timedelta(minutes=data.get("min_redraw_minutes", 2)),
        walk=_walk(data),
    )
    return Settings(
        board=board,
        timezone=data.get("timezone", "Europe/London"),
        state_file=Path(data.get("state_file", "state.json")),
        accent=data.get("accent", "red"),
        rotate_180=bool(data.get("rotate_180", False)),
    )
