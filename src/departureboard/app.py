"""One tick of the board: fetch whatever is due, redraw only if something changed."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from PIL.Image import Image

from .budget import DailyBudget
from .model import Departure
from .render import Palette
from .schedule import PollPolicy
from .state import departure_from_dict, departure_to_dict, from_iso, to_iso
from .view import BoardView, build_view
from .themes import THEMES, Theme
from .walk import WalkTimes

log = logging.getLogger(__name__)


class DepartureSource(Protocol):
    name: str

    def fetch(self) -> list[Departure]: ...


class Display(Protocol):
    palette: Palette

    def show(self, image: Image) -> None: ...


@dataclass(frozen=True)
class SourceConfig:
    source: DepartureSource
    policy: PollPolicy
    stale_after: timedelta
    daily_budget: int | None = None


@dataclass(frozen=True)
class Board:
    title: str
    sources: list[SourceConfig]
    # E-paper refreshes are slow and wear the panel: never redraw more often than this.
    min_redraw: timedelta = timedelta(minutes=2)
    walk: WalkTimes = WalkTimes()
    theme: Theme = THEMES["plain"]


@dataclass(frozen=True)
class RunResult:
    view: BoardView
    rendered: bool


def _poll(config: SourceConfig, state: dict, now: datetime, prime: bool = False) -> None:
    """Fetch if due. `prime` fetches regardless of windows; the budget still applies."""
    name = config.source.name
    entry = state.setdefault("sources", {}).setdefault(name, {})
    if not prime and not config.policy.is_due(now, from_iso(entry.get("last_attempt"))):
        return

    if config.daily_budget is not None:
        budgets = state.setdefault("budgets", {})
        budget = DailyBudget.from_dict(budgets.get(name), config.daily_budget)
        if not budget.try_spend(now.date(), getattr(config.source, "cost", 1)):
            log.warning("%s: daily budget of %d spent, skipping", name, config.daily_budget)
            return
        budgets[name] = budget.to_dict()

    entry["last_attempt"] = to_iso(now)
    try:
        departures = config.source.fetch()
    except Exception as error:  # noqa: BLE001 - keep showing last good data on any failure
        log.warning("%s: fetch failed: %s", name, error)
        return
    entry["departures"] = [departure_to_dict(d) for d in departures]
    entry["last_success"] = to_iso(now)


def _is_stale(config: SourceConfig, entry: dict, now: datetime) -> bool:
    if not config.policy.is_active(now):
        return False
    last_success = from_iso(entry.get("last_success"))
    return last_success is None or now - last_success > config.stale_after


def _departures(state: dict, name: str) -> list[Departure]:
    entry = state.get("sources", {}).get(name, {})
    return [departure_from_dict(d) for d in entry.get("departures", [])]


def run_once(
    board: Board, state: dict, display: Display, now: datetime, prime: bool = False
) -> RunResult:
    for config in board.sources:
        _poll(config, state, now, prime)

    entries = state.get("sources", {})
    stale = {
        c.source.name for c in board.sources if _is_stale(c, entries.get(c.source.name, {}), now)
    }
    view = build_view(
        title=board.title,
        trains=_departures(state, "train"),
        buses=_departures(state, "bus"),
        now=now.strftime("%H:%M"),
        stale=stale,
        walk=board.walk,
        wording=board.theme.wording,
    )

    last = state.get("last_render", {})
    last_at = from_iso(last.get("at"))
    changed = view.signature() != last.get("signature")
    too_soon = last_at is not None and now - last_at < board.min_redraw

    if changed and not too_soon:
        display.show(board.theme.render(view, display.palette))
        state["last_render"] = {"signature": view.signature(), "at": to_iso(now)}
        return RunResult(view, rendered=True)
    return RunResult(view, rendered=False)


def run_loop(
    board: Board,
    state: dict,
    display: Display,
    clock,
    tick_seconds: float,
    ticks: int | None = None,
    sleep=None,
    on_tick=None,
    store=None,
    prime: bool = False,
) -> None:
    """Tick forever (or `ticks` times). A clock with `advance()` is simulated.
    With `prime`, the first tick fetches every source so the board starts filled."""
    sleep = sleep or time.sleep
    count = 0
    while ticks is None or count < ticks:
        now = clock()
        result = run_once(board, state, display, now, prime=prime and count == 0)
        if store is not None:
            store.save(state)
        if on_tick is not None:
            on_tick(now, result)
        sleep(tick_seconds)
        if hasattr(clock, "advance"):
            clock.advance()
        count += 1
