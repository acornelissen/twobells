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


def _bus(section: dict, env: Mapping[str, str]) -> SourceConfig:
    atco = section.get("atco", "")
    if not atco or atco == "CHANGE_ME":
        raise ConfigError("Set [bus] atco to your stop's ATCO code, or remove the [bus] section")
    policy = _policy(section, 6)
    source = TransportApiBusSource(
        app_id=_secret(env, "TAPI_APP_ID"),
        app_key=_secret(env, "TAPI_APP_KEY"),
        atco=atco,
        rows=section.get("rows", 2),
        directions=section.get("directions") or None,
        base_url=section.get("base_url", TAPI_BASE_URL),
    )
    return SourceConfig(
        source,
        policy,
        timedelta(minutes=section.get("stale_after_minutes", 18)),
        daily_budget=section.get("daily_budget", 28),
    )


def load_config(path: Path | str, env: Mapping[str, str]) -> Settings:
    data = tomllib.loads(Path(path).read_text())
    sources = []
    if "train" in data:
        sources.append(_train(data["train"], env))
    if "bus" in data:
        sources.append(_bus(data["bus"], env))
    board = Board(
        title=data.get("title", "Departures"),
        sources=sources,
        refresh_every=timedelta(minutes=data.get("refresh_every_minutes", 10)),
    )
    return Settings(
        board=board,
        timezone=data.get("timezone", "Europe/London"),
        state_file=Path(data.get("state_file", "state.json")),
        accent=data.get("accent", "red"),
        rotate_180=bool(data.get("rotate_180", False)),
    )
