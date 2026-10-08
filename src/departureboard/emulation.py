"""Wires a board to the emulator: demo (all fake, simulated clock) or live."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from .app import Board, SourceConfig
from .config import load_config
from .emulator import EmulatorDisplay
from .fake import FakeBusSource, FakeTrainSource, SimClock
from .schedule import PollPolicy, parse_window
from .state import StateStore

DEMO_START = datetime(2026, 10, 8, 6, 58)  # a Thursday
_ALWAYS = (parse_window("Mon-Sun 00:00-23:59"),)
# Bus feed drops out for part of the demo so the STALE flag can be seen.
_OUTAGE = (time(7, 30), time(7, 52))


def demo_outage(now: datetime) -> bool:
    return _OUTAGE[0] <= now.time() < _OUTAGE[1]


@dataclass(frozen=True)
class Emulation:
    board: Board
    clock: Callable[[], datetime]
    display: EmulatorDisplay
    store: StateStore | None
    tick_seconds: float


def _fake_train(clock, interval_minutes: int = 2) -> SourceConfig:
    policy = PollPolicy(_ALWAYS, timedelta(minutes=interval_minutes))
    return SourceConfig(FakeTrainSource(clock), policy, stale_after=timedelta(minutes=6))


def _demo(accent: str, tick_seconds: float | None) -> Emulation:
    clock = SimClock(DEMO_START)
    bus = FakeBusSource(clock, failing_when=demo_outage)
    board = Board(
        title="Glossop > Manchester Picc",
        sources=[
            _fake_train(clock),
            SourceConfig(bus, PollPolicy(_ALWAYS, timedelta(minutes=6)), timedelta(minutes=12)),
        ],
        refresh_every=timedelta(minutes=10),
    )

    return Emulation(
        board,
        clock,
        EmulatorDisplay(accent, mode="demo"),
        store=None,
        tick_seconds=2 if tick_seconds is None else tick_seconds,
    )


def _live(
    config: Path | str,
    env: Mapping[str, str],
    fake_train: bool,
    tick_seconds: float | None,
) -> Emulation:
    only = {"bus"} if fake_train else None
    settings = load_config(config, env, only=only)
    zone = ZoneInfo(settings.timezone)

    def clock() -> datetime:
        return datetime.now(zone).replace(tzinfo=None, second=0, microsecond=0)

    sources = list(settings.board.sources)
    if fake_train:
        sources.insert(0, _fake_train(clock))
    board = replace(settings.board, sources=sources)
    return Emulation(
        board,
        clock,
        EmulatorDisplay(settings.accent, mode="live"),
        store=StateStore(settings.state_file),
        tick_seconds=60 if tick_seconds is None else tick_seconds,
    )


def build_emulation(
    demo: bool = False,
    config: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    fake_train: bool = False,
    accent: str = "red",
    tick_seconds: float | None = None,
) -> Emulation:
    if demo:
        return _demo(accent, tick_seconds)
    return _live(config, env or {}, fake_train, tick_seconds)
