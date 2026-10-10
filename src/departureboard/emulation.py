"""Wires a board to the emulator: demo (sample Glossop data from fake.py,
simulated clock) or live."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .app import Board, SourceConfig, Tracker, Weather
from .config import load_config
from .display import DISPLAY_PANELS
from .emulator import EmulatorDisplay
from .fake import DEMO_STRIP, DEMO_TITLE, FakeBusSource, FakeTrainSource, FakeVehicleSource, FakeWeatherSource, SimClock
from .schedule import PollPolicy, parse_window
from .state import StateStore
from .themes import THEMES
from .walk import WalkTimes

DEMO_START = datetime(2026, 10, 8, 6, 58)  # a Thursday
_ALWAYS = (parse_window("Mon-Sun 00:00-23:59"),)
# Bus feed drops out for part of the demo so the STALE flag can be seen.
_OUTAGE = (time(7, 30), time(7, 52))


def demo_outage(now: datetime) -> bool:
    return _OUTAGE[0] <= now.time() < _OUTAGE[1]


# Rain is likely through the 07:00 hour, so the rain line shows from the start;
# a disruption notice takes its place for part of that time.
_DISRUPTION = (time(7, 10), time(7, 25))


def demo_disruption(now: datetime) -> bool:
    return _DISRUPTION[0] <= now.time() < _DISRUPTION[1]


@dataclass(frozen=True)
class Emulation:
    board: Board
    clock: Callable[[], datetime]
    display: EmulatorDisplay
    store: StateStore | None
    tick_seconds: float


def _fake_train(clock, interval_minutes: int = 2, notice_when=None) -> SourceConfig:
    policy = PollPolicy(_ALWAYS, timedelta(minutes=interval_minutes))
    source = FakeTrainSource(clock, notice_when=notice_when)
    return SourceConfig(source, policy, stale_after=timedelta(minutes=6))


def _demo(accent: str | None, tick_seconds: float | None, display: str) -> Emulation:
    theme = THEMES["sign"]
    panel = DISPLAY_PANELS[display]
    # E-paper: a simulated minute per tick. A screen: 5 s per tick, so its
    # running clock, page changes and the moving bus can be followed.
    clock = SimClock(DEMO_START) if panel.epaper else SimClock(DEMO_START, timedelta(seconds=5))
    bus = FakeBusSource(clock, rows=3 if panel.epaper else 5, failing_when=demo_outage)
    tracker = None
    if not panel.epaper:
        tracker = Tracker(FakeVehicleSource(clock), {"341": DEMO_STRIP}, PollPolicy(_ALWAYS, timedelta(seconds=30)))
    board = Board(
        title=DEMO_TITLE,
        sources=[
            _fake_train(clock, notice_when=demo_disruption),
            SourceConfig(bus, PollPolicy(_ALWAYS, timedelta(minutes=6)), timedelta(minutes=12)),
        ],
        walk=WalkTimes(train=5, bus={"A": 8, "B": 8}),
        theme=theme,
        tracker=tracker,
        weather=Weather(FakeWeatherSource(clock), PollPolicy(_ALWAYS, timedelta(minutes=30))),
    )

    return Emulation(
        board,
        clock,
        EmulatorDisplay(accent or theme.accent, mode="demo", panel=panel),
        store=None,
        tick_seconds=tick_seconds if tick_seconds is not None else (2 if panel.epaper else 1),
    )


def _live(
    config: Path | str,
    env: Mapping[str, str],
    fake_train: bool,
    tick_seconds: float | None,
    display: str | None,
) -> Emulation:
    only = {"bus"} if fake_train else None
    settings = load_config(config, env, only=only, display=display)
    zone = ZoneInfo(settings.timezone)
    panel = DISPLAY_PANELS[settings.display]

    def clock() -> datetime:
        now = datetime.now(zone).replace(tzinfo=None, microsecond=0)
        return now.replace(second=0) if panel.epaper else now

    sources = list(settings.board.sources)
    if fake_train:
        sources.insert(0, _fake_train(clock))
    board = replace(settings.board, sources=sources)
    return Emulation(
        board,
        clock,
        EmulatorDisplay(settings.accent, mode="live", panel=panel),
        store=StateStore(settings.state_file),
        tick_seconds=tick_seconds if tick_seconds is not None else (60 if panel.epaper else 1),
    )


def build_emulation(
    demo: bool = False,
    config: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    fake_train: bool = False,
    accent: str | None = None,
    tick_seconds: float | None = None,
    display: str | None = None,
) -> Emulation:
    """`display` names the panel to emulate; live mode defaults to the configured one."""
    if demo:
        return _demo(accent, tick_seconds, display or "inky")
    return _live(config, env or {}, fake_train, tick_seconds, display)
