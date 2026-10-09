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
from .weather import Hour, hours_from_dicts, hours_to_dicts, rain_line

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
    # While the source says it is `falling_back` to a costlier one (the bus
    # timetable to TransportAPI), it is polled at this pace instead.
    fallback_policy: PollPolicy | None = None

    def current_policy(self) -> PollPolicy:
        if self.fallback_policy is not None and getattr(self.source, "falling_back", False):
            return self.fallback_policy
        return self.policy


class VehicleSource(Protocol):
    def fetch(self) -> list[Vehicle]: ...


@dataclass(frozen=True)
class Tracker:
    """Live bus positions for the route strip page, on screens only."""

    source: VehicleSource
    routes: dict[str, RouteStrip]  # by line
    policy: PollPolicy
    stale_after: timedelta = timedelta(minutes=2)


class WeatherSource(Protocol):
    def fetch(self) -> list[Hour]: ...


@dataclass(frozen=True)
class Weather:
    """The rain warning: an hourly forecast, polled while any source's window is open."""

    source: WeatherSource
    policy: PollPolicy
    threshold: int = 50  # per cent chance of rain worth a warning
    # A forecast this old is no longer shown; it should be renewed every 30 minutes.
    max_age: timedelta = timedelta(hours=3)


# Without walk times, how far ahead the rain warning looks.
_RAIN_LOOKAHEAD = timedelta(minutes=60)


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
    weather: Weather | None = None


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
    if not prime and not config.current_policy().is_due(now, from_iso(entry.get("last_attempt"))):
        # Not checked because the fallback is waiting for its own windows: say why.
        if config.fallback_policy is not None and getattr(config.source, "falling_back", False):
            idle = getattr(config.source, "idle_note", None)
            note = idle() if idle else None
            if note:
                entry["note"] = safe_message(note)
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
    # A source that got there the long way round (the bus timetable's fallback)
    # says so, for the settings page.
    note = getattr(config.source, "note", None)
    if note:
        entry["note"] = safe_message(note)
    else:
        entry.pop("note", None)


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


def _keep_messages(config: SourceConfig, state: dict, before: str | None) -> None:
    """A source with messages (Darwin's disruption notices) has them saved beside
    the departures from the same fetch, once that fetch has succeeded."""
    messages = getattr(config.source, "messages", None)
    entry = state.get("sources", {}).get(config.source.name, {})
    if messages is None or entry.get("last_success") in (None, before):
        return
    entry["messages"] = [m for m in messages if isinstance(m, str)]


def _notices(board: Board, state: dict, now: datetime) -> list[str]:
    """Messages from fetches that are still fresh. They age out with the feed:
    once its data is older than stale_after, a notice may no longer be true,
    and a morning's disruption must not still be showing in the evening."""
    notices = []
    for config in board.sources:
        entry = state.get("sources", {}).get(config.source.name, {})
        fetched = from_iso(entry.get("last_success"))
        if fetched is None or now - fetched > config.stale_after:
            continue
        messages = entry.get("messages")
        if isinstance(messages, list):
            notices.extend(m for m in messages if isinstance(m, str) and m)
    return notices


def _poll_weather(weather: Weather, state: dict, now: datetime) -> None:
    entry = state.get("weather")
    if not isinstance(entry, dict):
        entry = state["weather"] = {}
    if not weather.policy.is_due(now, from_iso(entry.get("last_attempt"))):
        return
    entry["last_attempt"] = to_iso(now)
    try:
        hours = weather.source.fetch()
    except Exception as error:  # noqa: BLE001 - keep the last forecast until it is too old
        log.warning("weather: fetch failed: %s", error)
        return
    entry["hours"] = hours_to_dicts(hours)
    entry["last_success"] = to_iso(now)


def _rain_until(board: Board, departures: list[Departure], now: datetime) -> datetime:
    """When you would reach the next train or bus you can still catch: its
    leave time plus the walk, which is when it goes. Without walk times, or
    with nothing to catch, the next hour."""
    hhmm = now.strftime("%H:%M")
    reachable = []
    for dep in departures:
        leave = leave_time(dep, board.walk)
        if leave is not None and minutes_between(hhmm, leave) >= 0:
            reachable.append((minutes_between(hhmm, leave), dep))
    if not reachable:
        return now + _RAIN_LOOKAHEAD
    _, dep = min(reachable, key=lambda pair: pair[0])
    return now.replace(second=0, microsecond=0) + timedelta(minutes=minutes_between(hhmm, dep.departs_at))


def _rain(board: Board, state: dict, departures: list[Departure], now: datetime, live: bool) -> str | None:
    weather = board.weather
    entry = state.get("weather")
    if weather is None or not isinstance(entry, dict):
        return None
    fetched = from_iso(entry.get("last_success"))
    if fetched is None or now - fetched > weather.max_age:
        return None
    upcoming = [d for d in departures if not d.is_gone(now.strftime("%H:%M"))]
    until = _rain_until(board, upcoming, now)
    return rain_line(hours_from_dicts(entry.get("hours")), now, until, weather.threshold, live)


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
    parts = [shown.signature(), theme.name, str(theme.text_size), repr(display.panel), repr(display.palette), rotate]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def run_once(
    board: Board, state: dict, display: Display, now: datetime, prime: bool = False
) -> RunResult:
    # A screen can redraw every second, so it gets a running clock and countdowns.
    live = not display.panel.epaper
    for config in board.sources:
        before = state.get("sources", {}).get(config.source.name, {}).get("last_success")
        _poll(config, state, now, prime)
        _keep_messages(config, state, before)
    if live and board.tracker is not None:
        _poll_vehicles(board.tracker, state, now)
    if board.weather is not None:
        _poll_weather(board.weather, state, now)
    entries = state.get("sources", {})
    stale = {
        c.source.name for c in board.sources if _is_stale(c, entries.get(c.source.name, {}), now)
    }
    trains, buses = _departures(state, "train", now), _departures(state, "bus", now)
    view = build_view(
        title=board.title,
        trains=trains,
        buses=buses,
        now=now.strftime("%H:%M"),
        stale=stale,
        walk=board.walk,
        wording=board.theme.wording,
        clock=now.strftime("%H:%M:%S") if live else None,
        notices=_notices(board, state, now),
        rain=_rain(board, state, trains + buses, now, live),
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


_SAVE_EVERY = timedelta(minutes=5)


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
    saved, saved_at = None, None
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
            # Also every few minutes, so the settings page's status (last redraw,
            # bus positions) doesn't go stale while nothing new is fetched.
            overdue = saved_at is None or not timedelta(0) <= now - saved_at < _SAVE_EVERY
            if fetched != saved or overdue:
                store.save(state)
                saved, saved_at = fetched, now
        if on_tick is not None:
            on_tick(now, result)
        sleep(tick_seconds)
        if hasattr(clock, "advance"):
            clock.advance()
        count += 1
