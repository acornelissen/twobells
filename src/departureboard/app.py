"""One tick of the board: fetch whatever is due, redraw only if something changed."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from PIL.Image import Image

from .bods import Vehicle
from .budget import DailyBudget
from .model import Departure, minutes_between
from .panel import Panel
from .render import Palette
from .route import RouteStrip, RouteView, locate, target, where
from .schedule import PollPolicy
from .state import departure_to_dict, departures_from_dicts, from_iso, to_iso
from .status import safe_message
from .view import BoardView, build_view
from .themes import THEMES, Theme
from .walk import WalkTimes, leave_time

log = logging.getLogger(__name__)


class DepartureSource(Protocol):
    name: str

    def fetch(self) -> list[Departure]: ...


class Display(Protocol):
    palette: Palette
    panel: Panel

    def show(self, image: Image) -> None: ...


@dataclass(frozen=True)
class SourceConfig:
    source: DepartureSource
    policy: PollPolicy
    stale_after: timedelta
    daily_budget: int | None = None


class VehicleSource(Protocol):
    def fetch(self) -> list[Vehicle]: ...


@dataclass(frozen=True)
class Tracker:
    """Live bus positions for the route strip page, on screens only."""

    source: VehicleSource
    routes: dict[str, RouteStrip]  # by line
    policy: PollPolicy
    stale_after: timedelta = timedelta(minutes=2)


# A screen with a route page shows departures for 15 s, then the bus for 10 s.
_PAGE_CYCLE_S = 25
_DEPARTURES_S = 15


@dataclass(frozen=True)
class Board:
    title: str
    sources: list[SourceConfig]
    # E-paper refreshes are slow and wear the panel: never redraw more often than this.
    # A screen has no such limit.
    min_redraw: timedelta = timedelta(minutes=2)
    walk: WalkTimes = WalkTimes()
    theme: Theme = THEMES["plain"]
    tracker: Tracker | None = None


@dataclass(frozen=True)
class RunResult:
    view: BoardView
    rendered: bool
    route: RouteView | None = None
    page: str = "departures"


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
        # The settings page shows the last error; neither it nor the log may carry a key.
        message = safe_message(error)
        log.warning("%s: fetch failed: %s", name, message)
        entry["last_error"] = {"at": to_iso(now), "message": message}
        return
    entry["departures"] = [departure_to_dict(d) for d in departures]
    entry["last_success"] = to_iso(now)
    entry.pop("last_error", None)


def _is_stale(config: SourceConfig, entry: dict, now: datetime) -> bool:
    if not config.policy.is_active(now):
        return False
    last_success = from_iso(entry.get("last_success"))
    return last_success is None or now - last_success > config.stale_after


def _departures(state: dict, name: str, now: datetime) -> list[Departure]:
    """What was last fetched, less anything that has left since. Clock times
    alone can't tell this morning from tonight, so each is dated by its fetch."""
    entry = state.get("sources", {}).get(name, {})
    fetched = from_iso(entry.get("last_success"))
    if fetched is None:
        return []
    departures = departures_from_dicts(entry.get("departures", []))
    return [d for d in departures if not d.has_left(now, fetched)]


def _poll_vehicles(tracker: Tracker, state: dict, now: datetime) -> None:
    entry = state.setdefault("vehicles", {})
    if not tracker.policy.is_due(now, from_iso(entry.get("last_attempt"))):
        return
    entry["last_attempt"] = to_iso(now)
    try:
        vehicles = tracker.source.fetch()
    except Exception as error:  # noqa: BLE001 - the page says positions have stopped
        message = safe_message(error)
        log.warning("vehicles: fetch failed: %s", message)
        entry["last_error"] = {"at": to_iso(now), "message": message}
        return
    entry["items"] = [vars(v) for v in vehicles]
    entry["last_success"] = to_iso(now)
    entry.pop("last_error", None)


def _route_view(board: Board, state: dict, view: BoardView, now: datetime, clock: str) -> RouteView | None:
    """The page for the next bus you can catch that has a route strip, if any."""
    tracker = board.tracker
    if tracker is None or not tracker.policy.is_active(now):
        return None
    hhmm = now.strftime("%H:%M")
    upcoming = [d for d in _departures(state, "bus", now) if not d.is_gone(hhmm)]

    def catchable(dep: Departure) -> bool:
        strip = tracker.routes.get(dep.service)
        if strip is None or strip.stop != dep.stop:
            return False
        leave = leave_time(dep, board.walk)
        return leave is None or minutes_between(hhmm, leave) >= 0

    chosen = next((d for d in upcoming if catchable(d)), None)
    if chosen is None:
        return None
    row = view.buses.rows[upcoming.index(chosen)]
    strip = tracker.routes[chosen.service]
    entry = state.get("vehicles", {})
    last_success = from_iso(entry.get("last_success"))
    stale = last_success is None or now - last_success > tracker.stale_after
    vehicles = [] if stale else [Vehicle(**v) for v in entry.get("items", [])]
    bus_at = None if stale else locate(strip, vehicles, target(upcoming, chosen))
    names = tuple(s.name for s in strip.stops)
    return RouteView(
        title=f"{chosen.service} to {chosen.destination}",
        due=row.right,
        leave=row.leave,
        stops=names,
        bus_at=bus_at,
        where=where(names, bus_at, stale),
        clock=clock,
    )


def _render_key(shown: BoardView | RouteView, theme: Theme, display: Display) -> str:
    """What is on the panel: the words, and how they are drawn. A new look, accent
    or rotation has to reach e-paper even when the words have not changed."""
    # The display classes keep their rotation private; a public `rotate` wins if there is one.
    rotate = getattr(display, "rotate", getattr(display, "_rotate", 0))
    parts = [shown.signature(), theme.name, repr(display.panel), repr(display.palette), rotate]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def run_once(
    board: Board, state: dict, display: Display, now: datetime, prime: bool = False
) -> RunResult:
    # A screen can redraw every second, so it gets a running clock and countdowns.
    live = not display.panel.epaper
    for config in board.sources:
        _poll(config, state, now, prime)
    if live and board.tracker is not None:
        _poll_vehicles(board.tracker, state, now)
    entries = state.get("sources", {})
    stale = {
        c.source.name for c in board.sources if _is_stale(c, entries.get(c.source.name, {}), now)
    }
    view = build_view(
        title=board.title,
        trains=_departures(state, "train", now),
        buses=_departures(state, "bus", now),
        now=now.strftime("%H:%M"),
        stale=stale,
        walk=board.walk,
        wording=board.theme.wording,
        clock=now.strftime("%H:%M:%S") if live else None,
    )

    route = _route_view(board, state, view, now, view.clock) if live else None
    seconds = now.minute * 60 + now.second
    page = "route" if route and seconds % _PAGE_CYCLE_S >= _DEPARTURES_S else "departures"
    shown = route if page == "route" else view

    last = state.get("last_render", {})
    last_at = from_iso(last.get("at"))
    key = _render_key(shown, board.theme, display)
    changed = key != last.get("signature")
    # A last redraw in the future means the clock went back (summer time ending): not too soon.
    too_soon = not live and last_at is not None and timedelta(0) <= now - last_at < board.min_redraw

    if changed and not too_soon:
        draw = board.theme.render_route if page == "route" else board.theme.render
        display.show(draw(shown, display.palette, display.panel))
        state["last_render"] = {"signature": key, "at": to_iso(now)}
        return RunResult(view, rendered=True, route=route, page=page)
    return RunResult(view, rendered=False, route=route, page=page)


def _fetched(state: dict) -> str:
    """The part of the state worth keeping: not the last redraw, nor bus positions,
    which change every poll and are useless a few minutes later."""
    return json.dumps({k: v for k, v in state.items() if k not in ("last_render", "vehicles")}, sort_keys=True)


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
    reload: Callable[[], Board | None] | None = None,
) -> None:
    """Tick forever (or `ticks` times). A clock with `advance()` is simulated.
    With `prime`, the first tick fetches every source so the board starts filled.
    State is written only when what was fetched changes: a screen redraws every
    second, and writing the SD card that often would wear it out. `reload`, if
    given, is asked each tick for a new Board when the settings have changed."""
    sleep = sleep or time.sleep
    count = 0
    saved = None
    while ticks is None or count < ticks:
        if reload is not None:
            board = reload() or board
        now = clock()
        try:
            result = run_once(board, state, display, now, prime=prime and count == 0)
        except Exception:
            # Keep what was fetched, so a display fault doesn't cost a refetch on restart.
            if store is not None:
                store.save(state)
            raise
        if store is not None:
            fetched = _fetched(state)
            if fetched != saved:
                store.save(state)
                saved = fetched
        if on_tick is not None:
            on_tick(now, result)
        sleep(tick_seconds)
        if hasattr(clock, "advance"):
            clock.advance()
        count += 1
