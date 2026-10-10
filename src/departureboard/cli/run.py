"""The `run` command: one tick of the board, or with --loop a running
screen. Until the board is set up, or while its view is off, it shows where
to set it up instead of departures."""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from ..board.app import run_loop, run_once
from ..board.displays.display import DISPLAY_PANELS, PngDisplay, open_display
from ..board.state import StateStore
from ..board.view import SetupView
from ..settings.config import ConfigError, first_run_settings, needs_setup
from ..settings.schema import views
from ..settings.store import Store
from .common import as_owner, load_settings, local_now, open_store, setup_url, sleep_to_the_tick

# Exit code asking systemd (Restart=on-failure) to start the board again.
RESTART = 75
# Until the board is set up (or while its view is off), how often a running
# screen checks whether it has been (or is back on).
SETUP_CHECK_S = 5
# What the board shows while the board view is off. E-paper keeps its last
# image, so showing nothing would leave departures up, going stale.
OFF_HEADING = "Board off"
OFF_NOTE = "Turn it on in Settings:"


def _open_display(args, settings):
    if args.png:
        return PngDisplay(args.png, settings.accent, DISPLAY_PANELS[settings.display])
    return open_display(settings.display, settings.accent, settings.rotate, settings.panel_colour)


def _board_on(store: Store) -> bool:
    """Whether the board view is on ([app] views; on unless they say). On if
    the settings can't be read, so load_config reports why."""
    try:
        return "board" in views(store.data(), default=("board",))
    except (OSError, ConfigError):
        return True


def run_command(args) -> int:
    found = as_owner(args, open_store(args))
    if needs_setup(found):
        return _run_first(args)
    if not _board_on(found):
        return _run_off(args)
    settings = load_settings(args)
    display = _open_display(args, settings)
    store = StateStore(settings.state_file)
    state = store.load()
    if args.loop:
        return _run_loop(settings, display, store, state, args)
    # A fresh install (nothing fetched yet, though the set-me-up page may have
    # been drawn) fetches everything once, so the panel isn't blank until the
    # next poll window opens.
    try:
        result = run_once(settings.board, state, display, local_now(settings.timezone),
                          prime="sources" not in state)
    finally:
        # Even if drawing failed: what was fetched, and the bus budget spent on it, must not be lost.
        store.save(state)
    logging.info("rendered=%s", result.rendered)
    return 0


class _Restart(Exception):
    """A change the running display can't take on: start again."""


class _Watcher:
    """Notices when config, secrets or saved settings change, and reloads them.
    Changes to the display itself need a fresh start."""

    def __init__(self, args, settings) -> None:
        self._args = args
        self._settings = settings
        self._paths = open_store(args).watched()
        self._stamp = self._stamps()

    def _stamps(self):
        stamps = []
        for path in self._paths:
            try:
                info = path.stat()
                stamps.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamps.append(None)
        return stamps

    @staticmethod
    def _hardware(settings):
        return (settings.display, settings.rotate, settings.accent, settings.timezone)

    def __call__(self):
        stamp = self._stamps()
        if stamp == self._stamp:
            return None
        self._stamp = stamp
        try:
            new = load_settings(self._args)
        except ConfigError as error:
            logging.warning("Settings changed but don't load (%s); keeping the current ones", error)
            return None
        if self._hardware(new) != self._hardware(self._settings) or not _board_on(open_store(self._args)):
            raise _Restart  # for the display, or to show the board view is off
        logging.info("Settings changed; reloaded")
        self._settings = new
        return new.board


def _run_loop(settings, display, store, state, args) -> int:
    zone = ZoneInfo(settings.timezone)

    def clock() -> datetime:
        return datetime.now(zone).replace(tzinfo=None, microsecond=0)

    tick = args.tick if args.tick is not None else (60 if display.panel.epaper else 1)
    try:
        run_loop(
            settings.board,
            state,
            display,
            clock,
            tick,
            ticks=args.ticks,
            sleep=sleep_to_the_tick,
            store=store,
            # As for a single run: only a fresh install fetches outside windows.
            prime="sources" not in state,
            reload=_Watcher(args, settings),
        )
    except KeyboardInterrupt:
        pass
    except _Restart:
        logging.info("Display settings or views changed; exiting so the service starts again")
        store.save(state)
        return RESTART
    return 0


class _SetUpYet:
    """A running board's reload hook until it is set up: every SETUP_CHECK_S
    asks whether it is now, and if so starts the board again to load it."""

    def __init__(self, store, clock=time.monotonic) -> None:
        self._store = store
        self._clock = clock
        self._checked: float | None = None

    def _ready(self) -> bool:
        return not needs_setup(self._store)

    def __call__(self):
        now = self._clock()
        if self._checked is not None and 0 <= now - self._checked < SETUP_CHECK_S:
            return None
        self._checked = now
        if self._ready():
            raise _Restart
        return None


class _ViewOn(_SetUpYet):
    """A running board's reload hook while the board view is off: every
    SETUP_CHECK_S asks whether it is on again, and if so starts the board
    again to load its settings."""

    def _ready(self) -> bool:
        return _board_on(self._store)


def _run_first(args) -> int:
    """Not set up yet (config.toml missing, or still the template): show
    where to set the board up, and a reset code when one is asked for to
    claim the settings site, rather than fail every minute.

    A single run (the e-paper timer's) draws the page, only when it isn't
    already showing, and exits 0. A running screen keeps it up and, once the
    board is set up, exits RESTART so systemd starts it with its settings."""
    found = open_store(args)
    logging.info("Not set up yet: showing %s on the board", setup_url())
    return _show_instead(args, found, SetupView(setup_url()), _SetUpYet(found),
                         "Set up; exiting so the service starts the board with its settings")


def _run_off(args) -> int:
    """The board view is off ([app] views): say so, with where to turn it
    back on, and fetch nothing, so no TransportAPI allowance is spent. A
    reset code asked for still shows first.

    As for a board not yet set up, a single run draws the page once and
    exits 0, and a running screen keeps it up until the view is on again,
    then exits RESTART. No service is ever stopped or disabled."""
    found = open_store(args)
    logging.info("The board view is off: showing where to turn it on, %s, on the board", setup_url())
    page = SetupView(setup_url(), heading=OFF_HEADING, note=OFF_NOTE)
    return _show_instead(args, found, page, _ViewOn(found),
                         "Board view on; exiting so the service starts the board with its settings")


def _show_instead(args, found: Store, page: SetupView, reload, restarting: str) -> int:
    """Show `page` instead of departures, with the display and look the
    settings give (or the defaults) and no sources, so nothing is fetched.
    A running screen calls `reload` each tick; when it raises _Restart, says
    `restarting` and exits RESTART."""
    settings = first_run_settings(found)
    board = dataclasses.replace(settings.board, setup=page)
    display = _open_display(args, settings)
    store = StateStore(settings.state_file)
    state = store.load()
    zone = ZoneInfo(settings.timezone)
    if not args.loop:
        try:
            result = run_once(board, state, display, local_now(settings.timezone))
        finally:
            store.save(state)
        logging.info("rendered=%s", result.rendered)
        return 0
    tick = args.tick if args.tick is not None else (60 if display.panel.epaper else 1)
    try:
        run_loop(
            board,
            state,
            display,
            lambda: datetime.now(zone).replace(tzinfo=None, microsecond=0),
            tick,
            ticks=args.ticks,
            sleep=sleep_to_the_tick,
            store=store,
            reload=reload,
        )
    except KeyboardInterrupt:
        pass
    except _Restart:
        logging.info(restarting)
        store.save(state)
        return RESTART
    return 0
