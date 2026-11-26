"""The `run` command: one tick of the board, or with --loop a running
screen. Until the board is set up, or while its view is off, it shows where
to set it up instead of departures."""

from __future__ import annotations

import dataclasses
import logging
import signal
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from ..board import runfolder
from ..board.app import RunResult, Started, run_loop, run_once
from ..board.config import first_run_settings
from ..board.displays.display import PngDisplay, open_display
from ..board.names import DISPLAY_PANELS
from ..board.scroll import SCROLL_FRAME_S
from ..board.sinceboot import SinceBoot
from ..board.speaker import Speaker
from ..board.state import StateStore
from ..board.unsaved import KeptRender, KeptSources
from ..board.view import SetupView
from ..core.errors import ConfigError
from ..settings.config import needs_setup
from ..settings.schema import views
from ..settings.store import Store
from .common import as_owner, load_settings, local_now, open_store, setup_url, sleep_to_the_tick

# Exit code asking systemd (Restart=on-failure) to start the board again.
RESTART = 75
# Exit code of a single run that drew but couldn't save the state (EX_IOERR),
# so systemd marks the unit failed: a full or read-only SD card.
UNSAVED = 74
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


def _stop(signum, frame) -> None:
    # A second SIGTERM mustn't cut short the cleanup the first set going.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit(0)


@contextmanager
def _stopped_cleanly():
    """systemd stops a run with SIGTERM: at the unit's TimeoutStartSec, or on
    systemctl stop. Left alone it kills Python there and then, skipping the
    finally blocks that save the state (and the bus budget spent) and put an
    e-paper panel to sleep, which mustn't be left powered. Raising SystemExit
    runs them; it exits 0, as being asked to stop isn't a fault (systemd counts
    a death by SIGTERM as clean too). Signals reach the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_command(args) -> int:
    with _stopped_cleanly():
        return _run(args)


def _run(args) -> int:
    if args.timings:
        logging.getLogger("twobells.timings").setLevel(logging.DEBUG)  # board/ahead.py's and the screen's
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
    # next poll window opens. Decided once what was kept in memory while the
    # card can't be saved is back in the state (_single), or a card that has
    # never saved would have every source fetched every minute.
    prime = "sources" not in state
    # The bus budget is saved as spent before each lookup, or none is made (board/app.py's _poll).
    return _single(store, state, lambda started: run_once(
        settings.board, state, display, local_now(settings.timezone), prime=prime and not state.get("sources"),
        save=lambda kept: _save(store, kept), started=started,
    ), {config.source.name for config in settings.board.sources})


def _single(store: StateStore, state: dict, draw: Callable[[Started | None], RunResult],
            sources: set[str] | None = None) -> int:
    """A single run, the e-paper timer's: `draw` it, then save the state, even
    if drawing failed, as what was fetched and the bus budget spent on it
    must not be lost. 0, or UNSAVED if the state couldn't be saved.

    On a Pi, it keeps what it must between runs in memory, in the unit's
    runtime folder (board/runfolder.py). What it has done for the board's
    `sources` since the Pi started (board/sinceboot.py) is given to `draw`
    as a Started, which it adds to as it goes: until a source is fetched
    since, its saved departures are marked not updating, as a started
    screen's are, and outside its windows it has one fetch for the start. A
    source counts as fetched only while what the next run reads has that
    fetch; one tried counts either way, so it isn't tried again every
    minute. Each run first writes back the record it read: if it can't, it
    goes without one. Without the folder or the record, `draw` is given
    None, and trusts what was saved, as ever.

    While the state can't be saved, the last redraw and what each source
    last fetched and tried are kept in memory instead (board/unsaved.py),
    so the next minute's run, reading the state afresh, doesn't take the
    panel to show something else and draw it again, ask each source again
    before its interval, nor lose a mark that showed as a window closed.
    They are forgotten before each save, and kept again only after one that
    fails with the board drawn, so what is there is never older than the
    state on the card; a draw that failed may have left the panel half
    drawn, and keeps nothing, so the next run draws."""
    folder = runfolder.here()
    kept = KeptRender(folder) if folder is not None else None
    entries = KeptSources(folder, sources or ()) if folder is not None else None
    boot = SinceBoot(folder, sources) if folder is not None and sources else None
    if kept is not None:
        drawn = kept.read()
        if drawn is not None:
            state["last_render"] = drawn
    if entries is not None:
        entries.carry(state)
    started = boot.read(state) if boot is not None else None
    if boot is not None and started is not None and not boot.keep(started, state):
        # A record that can't be kept would read as nothing fetched every run.
        logging.warning("Not counting what is fetched since the Pi started, so trusting what was saved: %s",
                        boot.problem)
        boot = started = None
    try:
        result = draw(started)
    except BaseException:
        _forget(kept, entries)
        _save(store, state)
        _count_since_boot(boot, started, state)
        raise
    _forget(kept, entries)
    saved = _save(store, state)
    if not saved and kept is not None and entries is not None:
        kept.keep(state.get("last_render"))
        entries.keep(state)
    _count_since_boot(boot, started, state)
    logging.info("rendered=%s", result.rendered)
    return 0 if saved else UNSAVED


def _forget(kept: KeptRender | None, entries: KeptSources | None) -> None:
    """Forgets what is kept in memory while the state can't be saved, before a save."""
    if kept is not None:
        kept.forget()
    if entries is not None:
        entries.forget()


def _count_since_boot(boot: SinceBoot | None, started: Started | None, state: dict) -> None:
    """Records what this run did since the Pi started: the sources it tried,
    and those it fetched, each with its last success now. Last, after the
    state is saved or kept in memory, so a fetch that reached neither reads
    next time as not fetched."""
    if boot is not None and started is not None:
        boot.keep(started, state)


def _save(store: StateStore, state: dict) -> bool:
    """Saves the state, saying whether it could. A full or read-only SD card
    is warned of, never raised: it mustn't hide a fault already on its way
    out, nor turn a clean stop or a restart into a crash."""
    try:
        store.save(state)
    except OSError as error:
        logging.warning("Can't save the board's state: %s", error)
        return False
    return True


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
            # A screen draws frames between ticks while text scrolls; e-paper never animates.
            frame_seconds=None if display.panel.epaper else SCROLL_FRAME_S,
            # A screen can speak, while [board] show says so; it outlives a settings reload.
            speaker=None if display.panel.epaper else Speaker(),
        )
    except KeyboardInterrupt:
        pass
    except _Restart:
        logging.info("Display settings or views changed; exiting so the service starts again")
        return RESTART
    finally:
        # However it ends (Ctrl+C, SIGTERM, a restart): keep what was fetched.
        _save(store, state)
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
        return _single(store, state, lambda started: run_once(board, state, display, local_now(settings.timezone)))
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
        return RESTART
    finally:
        _save(store, state)
    return 0
