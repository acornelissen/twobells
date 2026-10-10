"""Command line entry points.

  departureboard run      one tick; systemd runs this every minute
  departureboard run --loop   keep ticking; a screen redraws every second
                          (until the board is set up, both show where to set it up)
  departureboard probe    one live call to check keys and stop codes
  departureboard demo     render sample data (a Glossop morning) to a PNG, no keys needed
  departureboard emulate  run the board against a browser-based panel emulator
  departureboard route    print a [bus.routes] block for the screen's bus page (run on a laptop)
  departureboard web      serve the settings page on the home network
  departureboard timetable  build the BODS bus timetable for your stops, if due
  departureboard setup discover  find the bus operators serving your stops and print their settings
  departureboard setup serve  answer the settings page's setup requests (departureboard-setup.service)
  departureboard dashboard  serve the web dashboard (Mac or NAS), opening it in the browser
  departureboard web|dashboard --set-password | --reset-code
                          set the settings password, or print a code for a new one, and exit
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import migrate
from .app import run_loop, run_once
from .bodsbus import BodsBusSource
from .config import (
    ConfigError,
    first_run_settings,
    holiday_division,
    is_enabled,
    load_config,
    load_env_file,
    needs_setup,
)
from .darwin import DarwinSource, TrainBoard
from .display import DISPLAY_PANELS, PngDisplay, open_display
from .holidays import bank_holidays
from .emulation import build_emulation
from .emulator import DEFAULT_PORT, make_server
from .fake import DEMO_NOTICE, DEMO_TITLE, FakeWeatherSource
from .http import get_bytes
from .model import Departure, Status
from .state import StateStore
from .store import Store
from .timetable import TimetableBusy, TimetableError
from .transportapi import TransportApiBusSource, redact_credentials
from .installs import Install, install_named
from .schema import keys, views
from .version import package_version
from .view import SetupView, build_view
from .themes import THEMES, theme_named
from .walk import WalkTimes
from .weather import rain_line

DEFAULT_CONFIG = "/etc/departureboard/config.toml"
DEFAULT_SECRETS = "/etc/departureboard/secrets.env"


def _local_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone)).replace(tzinfo=None, second=0, microsecond=0)


def _parse_stops(specs: list[str] | None) -> dict[str, str] | None:
    if not specs:
        return None
    stops = {}
    for spec in specs:
        label, sep, atco = spec.partition("=")
        if not sep or not label or not atco:
            raise ConfigError(f"--stop expects LABEL=ATCO, got {spec!r}")
        stops[label] = atco
    return stops


def _store(args) -> Store:
    """The store for a command's --config, --secrets and --state-dir (if it has one)."""
    return Store.open(args.config, args.secrets, state_dir=getattr(args, "state_dir", None))


def _adopt(args, store: Store) -> None:
    """Bring the old dashboard's folder into the store, once (see migrate.py).
    A folder given with --state-dir was the old dashboard's own, so then
    there is nothing to bring."""
    if not getattr(args, "state_dir", None):
        migrate.adopt_legacy(store)


def _env(config: Path | str, secrets: Path | str) -> dict[str, str]:
    """The environment, then secrets.env, then keys saved from the web page."""
    try:
        return Store.open(config, secrets).env()
    except ConfigError:
        # A broken config.toml, needed to find the store: load_config reports that.
        return {**os.environ, **load_env_file(secrets)}


def _holidays(store: Store) -> frozenset:
    """Bank holidays, cached in the store and refreshed from gov.uk weekly,
    unless the settings say to ignore them (then gov.uk is never asked)."""
    try:
        data = store.data()
        division = holiday_division(data)
    except (OSError, ConfigError):
        return frozenset()  # load_config reports it
    if data.get("bank_holidays", True) is not True:
        return frozenset()
    # Only the cache's age depends on the time, so the Pi's own zone will do.
    return bank_holidays(store.folder, datetime.now(), division=division)


def _settings(args, bus_stops: dict[str, str] | None = None, only: set[str] | None = None):
    store = _store(args)
    # A probe makes one call whatever the day, so it needs no holidays.
    holidays = _holidays(store) if only is None else frozenset()
    return load_config(store, store.env(), bus_stops, only, holidays=holidays)


def _print_departures(departures: list[Departure], indent: str = "") -> None:
    for dep in departures:
        print(
            f"{indent}{dep.scheduled}  {dep.service:<10} {dep.destination:<28} "
            f"{dep.status.value} {dep.expected or ''}".rstrip()
        )


def _print_trains(board: TrainBoard) -> None:
    """Each train from the detailed board, then the stops it calls at after
    this one. Darwin's text was cleaned when parsed, so it is safe to print."""
    for train in board.services:
        dep = train.departure
        coaches = f"{train.length} coaches" if train.length else "coaches not known"
        print(f"{dep.scheduled}  {dep.destination:<28} {dep.status.value} {dep.expected or ''}".rstrip())
        print(f"  {dep.service} from {train.origin or 'origin not known'}, {coaches}")
        for reason in (train.cancel_reason, train.delay_reason):
            if reason:
                print(f"  {reason}")
        for stop in train.calling_points:
            state = "cancelled" if stop.cancelled else (stop.actual or stop.expected or "")
            print(f"    {stop.scheduled}  {stop.name:<28} {state}".rstrip())


def _probe_train_details(source: DarwinSource) -> int:
    source.with_details = True
    try:
        board = source.fetch_board()
    except Exception as error:  # noqa: BLE001 - one readable line, not a traceback
        print(f"error: {error}")
        return 1
    _print_trains(board)
    return 0


def _probe_bus(source, save_dir: str | None) -> int:
    print(f"Note: this uses {source.cost} hit(s) of your daily TransportAPI allowance.")
    failed = False
    for label, atco in source.stops.items():
        print(f"{label}  {atco}")
        try:
            payload = source.fetch_raw(atco)
            if save_dir:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
                (Path(save_dir) / f"{atco}.json").write_text(
                    json.dumps(redact_credentials(payload), indent=2)
                )
            departures = source.parse(payload, label)
        except Exception as error:  # noqa: BLE001 - report and try the next stop
            print(f"  error: {error}")
            failed = True
            continue
        if departures:
            _print_departures(departures, indent="  ")
        else:
            print("  no departures")
    return 1 if failed else 0


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


def _run(args) -> int:
    found = _store(args)
    if needs_setup(found):
        return _run_first(args)
    if not _board_on(found):
        return _run_off(args)
    settings = _settings(args)
    display = _open_display(args, settings)
    store = StateStore(settings.state_file)
    state = store.load()
    if args.loop:
        return _run_loop(settings, display, store, state, args)
    # A fresh install (nothing fetched yet, though the set-me-up page may have
    # been drawn) fetches everything once, so the panel isn't blank until the
    # next poll window opens.
    try:
        result = run_once(settings.board, state, display, _local_now(settings.timezone),
                          prime="sources" not in state)
    finally:
        # Even if drawing failed: what was fetched, and the bus budget spent on it, must not be lost.
        store.save(state)
    logging.info("rendered=%s", result.rendered)
    return 0


# Exit code asking systemd (Restart=on-failure) to start the board again.
RESTART = 75


class _Restart(Exception):
    """A change the running display can't take on: start again."""


class _Watcher:
    """Notices when config, secrets or saved settings change, and reloads them.
    Changes to the display itself need a fresh start."""

    def __init__(self, args, settings) -> None:
        self._args = args
        self._settings = settings
        self._paths = _store(args).watched()
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
            new = _settings(self._args)
        except ConfigError as error:
            logging.warning("Settings changed but don't load (%s); keeping the current ones", error)
            return None
        if self._hardware(new) != self._hardware(self._settings) or not _board_on(_store(self._args)):
            raise _Restart  # for the display, or to show the board view is off
        logging.info("Settings changed; reloaded")
        self._settings = new
        return new.board


def _sleep_to_the_tick(seconds: float) -> None:
    """Sleep to the next whole multiple of `seconds`, so a running clock never skips."""
    if seconds > 0:
        time.sleep(seconds - time.time() % seconds)


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
            sleep=_sleep_to_the_tick,
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


# The settings page's port, as departureboard-web.service runs it.
WEB_PORT = 8080
# Until the board is set up (or while its view is off), how often a running
# screen checks whether it has been (or is back on).
SETUP_CHECK_S = 5
# What the board shows while the board view is off. E-paper keeps its last
# image, so showing nothing would leave departures up, going stale.
OFF_HEADING = "Board off"
OFF_NOTE = "Turn it on in Settings:"


def setup_url(hostname: str | None = None, port: int = WEB_PORT) -> str:
    """The settings page's address on the home network, by this Pi's name."""
    name = (socket.gethostname() if hostname is None else hostname).split(".")[0]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,63}", name):
        name = "raspberrypi"  # Raspberry Pi OS's own; a real name never gets here
    return f"http://{name.lower()}.local:{port}/"


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
    claim the settings page, rather than fail every minute.

    A single run (the e-paper timer's) draws the page, only when it isn't
    already showing, and exits 0. A running screen keeps it up and, once the
    board is set up, exits RESTART so systemd starts it with its settings."""
    found = _store(args)
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
    found = _store(args)
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
            result = run_once(board, state, display, _local_now(settings.timezone))
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
            sleep=_sleep_to_the_tick,
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


def _route(args) -> int:
    from . import routesetup
    from .config import _walk
    from .walk import DEFAULT_STOP

    store = _store(args)
    data = store.data()
    bus = data.get("bus", {})
    atco = (bus.get("stops") or {}).get(args.stop)
    if not atco:
        print(f"No stop {args.stop!r} in [bus.stops]", file=sys.stderr)
        return 2
    walk = _walk(data).bus
    walk_minutes = walk.get(args.stop, walk.get(DEFAULT_STOP, 0))
    try:
        if args.txc:
            documents = routesetup.timetable_documents(Path(args.txc).read_bytes())
        else:
            if not args.noc:
                print("Give --noc (the operator code, e.g. BNML) or --txc FILE", file=sys.stderr)
                return 2
            key = store.env().get("BODS_API_KEY", "").strip()
            if not key:
                print("Missing BODS_API_KEY; set it in secrets.env", file=sys.stderr)
                return 2
            # A generator: each download is searched before the next is fetched.
            documents = routesetup.downloaded_documents(
                routesetup.dataset_urls(key, args.noc, args.line),
                lambda url, limit: get_bytes(url, limit, timeout=300),
                lambda url: print(f"Downloading {url}", file=sys.stderr),
            )
        found = routesetup.find_patterns(documents, args.line)
        if not found:
            print(f"No timetable for line {args.line} found", file=sys.stderr)
            return 1
        atcos, covered, journeys = routesetup.strip_before(found, atco, walk_minutes)
        stops = routesetup.naptan_stops(atcos)
        note = (
            f"Line {args.line} to stop {args.stop}: {len(atcos)} stops, about {round(covered / 60)} min "
            f"of travel (walk {walk_minutes} min + {routesetup.EXTRA_MINUTES}); pattern used by {journeys} "
            "timetabled journeys. Check the stops, then paste into config.toml."
        )
        print(routesetup.route_toml(args.line, args.stop, atcos, stops, note))
    except routesetup.RouteSetupError as error:
        print(f"Route setup failed: {error}", file=sys.stderr)
        return 1
    return 0


def _setup_discover(args) -> int:
    from . import discover
    from .config import _walk

    stops = _parse_stops(args.stop)
    store = _store(args)
    data = store.data()
    folder = store.folder
    key = store.env().get("BODS_API_KEY", "").strip()
    if not key:
        print("Missing BODS_API_KEY; set it in secrets.env", file=sys.stderr)
        return 2
    try:
        walk = _walk(data).bus
    except ConfigError:
        walk = {}  # route strips go back a few minutes without it
    timezone = data.get("timezone") if isinstance(data.get("timezone"), str) else "Europe/London"
    job = discover.SetupJob(folder, stops, key, routes=args.routes, walk_minutes=walk,
                            now=lambda: _local_now(timezone))
    try:
        job.start()
    except discover.SetupBusy as error:
        print(error, file=sys.stderr)
        return 2
    shown = None
    try:
        while True:
            status = job.status()
            step, done, total = status["step"], status["done"], status["total"]
            if step == discover.STEP_ROADS and done != total:
                done -= done % 25  # a road a second, up to 300: every 25th, and the last
            if status["state"] == "running" and (step, done, total) != shown:
                shown = (step, done, total)
                count = f": {done} of {total}" if total else ""
                print(f"{step}{count}", file=sys.stderr)
            if job.wait(0.5):
                break
    except KeyboardInterrupt:
        print("Cancelling…", file=sys.stderr)
        job.cancel()
        job.wait()
    status = job.status()
    if status["state"] != "done":
        print(f"Setup failed: {status['message']}", file=sys.stderr)
        return 1
    for note in status["result"]["notes"]:
        print(f"Note: {note}", file=sys.stderr)
    print(status["message"], file=sys.stderr)
    print(discover.fragment_toml(status["result"]), end="")
    return 0


def _setup_serve(args) -> int:
    """The setup service: the wizard's internet work, for a settings page
    started with --setup-via-spool (see setupspool.py)."""
    from . import setupspool

    store, problem = None, ""
    try:
        store = _store(args)
    except ConfigError as error:
        problem = str(error)
    if problem:
        print(problem, file=sys.stderr)
    try:
        spool = setupspool.spool_folder()
    except setupspool.SpoolError as error:
        print(error, file=sys.stderr)
        return 2
    stop = threading.Event()
    if problem:
        # Say so to the page, rather than stop with its requests waiting and be started again and again.
        worker = setupspool.Worker(spool, job_folder=None, load_secrets=dict, unavailable=setupspool.NO_SETTINGS,
                                   stop=stop)
    else:
        target = store.target()

        def settings() -> dict:
            try:
                return target.current()
            except (OSError, ConfigError, ValueError, TypeError):
                return {}  # a key test then asks about Glossop

        worker = setupspool.Worker(spool, job_folder=store.folder, load_secrets=store.env, settings=settings,
                                   stop=stop)
    # systemctl stop, a timeout or update.sh: end cleanly, telling the page a running search was cancelled.
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    try:
        return worker.run()
    except (OSError, setupspool.SpoolError) as error:
        print(f"Can't use the setup spool: {error}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


def _set_password(password, prompt: str) -> int:
    """Ask for a new settings password twice, without echoing it, and save it."""
    import getpass

    first = getpass.getpass(prompt)
    if first != getpass.getpass("Again: "):
        print("Those didn't match", file=sys.stderr)
        return 1
    try:
        password.set(first)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Can't save the password in {password.path}: {error.strerror or error}", file=sys.stderr)
        return 1
    print(f"Password saved to {password.path}")
    return 0


def _reset_code(folder: Path, where: str) -> int:
    """Print a one-time code for setting a new settings password, and where it
    goes. Running this proves access, so no rate limit, and the code comes
    here rather than to the board or the log."""
    from .auth import ResetCodes, spaced

    def show(code: str, expires: float) -> None:
        until = time.strftime("%H:%M", time.localtime(expires))
        print(f"Settings reset code: {spaced(code)}  (valid until {until}, once)")
        print(where)

    try:
        ResetCodes(folder, show=show).issue(time.time(), limit=False)
    except OSError as error:
        print(f"Can't save a reset code in {folder}: {error.strerror or error}", file=sys.stderr)
        return 1
    return 0


def _web(args) -> int:
    from importlib.resources import files

    from . import auth, web
    from .settingsform import SettingsForm
    from .setupapi import SetupApi

    # Only config.toml is read to find the store: a broken settings.toml must
    # not stop the page that fixes it.
    store = _store(args)
    store.make()
    _adopt(args, store)
    password = auth.PasswordStore(store.password_file)
    if args.set_password:
        return _set_password(password, "New settings page password: ")
    if args.reset_code:
        return _reset_code(store.folder, "Enter it on the settings page with a new password.")
    form = SettingsForm(store, store.env)
    stylesheet = files("departureboard").joinpath("web.css").read_bytes()
    # Codes for a forgotten password, or the first claim, show on the board.
    codes = auth.ResetCodes(store.folder)
    runner = None
    if args.setup_via_spool:
        # This service can't reach the internet; departureboard-setup.service does that work.
        from .setupspool import SpoolError, SpoolRunner, spool_folder

        try:
            runner = SpoolRunner(spool_folder())
        except SpoolError as error:
            print(error, file=sys.stderr)
            return 2
    # The search builds the bus timetable in the store, where the board reads it.
    steps = SetupApi(store.target(), store.env, store.folder, links=[("Board settings", "/")], runner=runner)
    # Until the board is set up, claiming the page or signing in opens the wizard.
    wizard = web.Wizard.load(steps, lambda: needs_setup(store))
    handler = web.make_handler(form, password, auth.Sessions(password=password), auth.Lockout(), web.shell, stylesheet,
                               codes, wizard=wizard)
    try:
        server = web.make_server(handler, args.host, args.port)
    except OSError as error:
        print(f"Can't listen on {args.host}:{args.port}: {error.strerror or error}", file=sys.stderr)
        return 2
    if not password.is_set():
        print("No password yet: claim the page with a code from the board, or run "
              "`departureboard web --set-password` as the service user", file=sys.stderr)
    if needs_setup(store):
        print("Not set up yet: sign in and the page opens the setup wizard", file=sys.stderr)
    print(f"Settings page on http://{args.host}:{server.server_address[1]}/  (home network only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _probe(args) -> int:
    if args.details and args.source != "train":
        print("--details is for trains only", file=sys.stderr)
        return 2
    settings = _settings(args, _parse_stops(args.stop), only={args.source})
    for config in settings.board.sources:
        if config.source.name != args.source:
            continue
        if args.details and isinstance(config.source, DarwinSource):
            return _probe_train_details(config.source)
        if isinstance(config.source, TransportApiBusSource):
            return _probe_bus(config.source, args.save)
        if isinstance(config.source, BodsBusSource):
            print(config.source.unusable() or "Using the BODS timetable and live bus positions")
        try:
            departures = config.source.fetch()
        except Exception as error:  # noqa: BLE001 - one readable line, not a traceback
            print(f"error: {error}")
            return 1
        _print_departures(departures)
        return 0
    print(f"No [{args.source}] section in config", file=sys.stderr)
    return 2


def _wants_timetable(store: Store) -> bool:
    """Whether the board's buses are on and come from the BODS timetable,
    in config.toml with the settings saved from the page over it."""
    data = store.data()
    bus = data.get("bus")
    return isinstance(bus, dict) and is_enabled(data, "bus") and bus.get("source") == "bods"


def _timetable(args) -> int:
    if args.needed:  # for update.sh: exit 0 if the timetable is wanted, 1 if not; no keys needed
        try:
            return 0 if _wants_timetable(_store(args)) else 1
        except OSError as error:
            print(f"Can't read {args.config}: {error.strerror or error}", file=sys.stderr)
            return 2
    settings = _settings(args, only={"bus"})
    source = next((c.source for c in settings.board.sources if c.source.name == "bus"), None)
    if not isinstance(source, BodsBusSource):
        # Not a fault with buses off (the wizard does that for trains alone): the weekly timer runs anyway.
        data = _store(args).data()
        if "bus" not in data:
            print("There are no buses on this board, so there's no bus timetable to build")
            return 0
        if not is_enabled(data, "bus"):
            print("Buses are switched off, so there's no bus timetable to build")
            return 0
        print('Bus times come from TransportAPI; set source = "bods" under [bus] first', file=sys.stderr)
        return 2
    # The timetable's bank holiday names come from this list, so keep it fresh
    # whatever bank_holidays says about checking times. Always England and
    # Wales: BODS timetables are England's.
    bank_holidays(settings.state_file.parent, datetime.now())
    try:
        schedule = source.refresh_timetable(force=args.force)
    except TimetableBusy:
        # Not a fault: the timer and the setup search may meet. The other build saves it.
        print("Another bus timetable build is running, so this one is skipped")
        return 0
    except TimetableError as error:
        print(f"Bus timetable not built: {error}", file=sys.stderr)
        return 1
    reason = source.unusable()
    if schedule is None or reason:
        print(f"No usable bus timetable: {reason}", file=sys.stderr)
        return 1
    until = f", until {schedule.valid_until:%d %b %Y}" if schedule.valid_until else ""
    calling = len(schedule.departure_journeys)
    inbound = len(schedule.journeys) - calling
    near = f" (and {inbound} ending near them)" if inbound else ""
    print(f"Bus timetable: {calling} journeys at your stops{near}, built {schedule.built:%a %d %b %H:%M}{until}")
    return 0


def _demo(args) -> int:
    trains = [
        Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "1"),
        Departure("train", "Northern", "Manchester Piccadilly", "07:42", "07:49", Status.LATE, "1"),
        Departure("train", "Northern", "Manchester Piccadilly", "08:12", None, Status.CANCELLED, "2"),
    ]
    buses = [
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A"),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B"),
        Departure("bus", "237", "Ashton", "07:41", None, Status.ON_TIME, stop="A"),
    ]
    theme = theme_named(args.theme)
    display = PngDisplay(args.png, args.accent or theme.accent, DISPLAY_PANELS[args.display])
    walk = WalkTimes(train=5, bus={"*": 8})
    # A screen preview shows what a screen adds: a running clock and countdowns.
    clock = None if display.panel.epaper else "07:03:15"
    now = datetime(2026, 10, 8, 7, 3, 15)
    # Rain likely through the 07:00 hour, before you'd reach the 07:12; a notice takes its place.
    rain = rain_line(FakeWeatherSource(lambda: now).fetch(), now, now.replace(minute=12), 50, live=bool(clock))
    view = build_view(
        DEMO_TITLE, trains, buses, "07:03", set(), walk, theme.wording, clock,
        notices=[DEMO_NOTICE] if args.notice else None,
        rain=rain if args.rain else None,
    )
    display.show(theme.render(view, display.palette, display.panel))
    print(f"Wrote {args.png}")
    return 0


def _emulate(args) -> int:
    env = None if args.demo else _env(args.config, args.secrets)
    em = build_emulation(
        demo=args.demo,
        config=args.config,
        env=env,
        fake_train=args.fake_train,
        accent=args.accent,
        tick_seconds=args.tick,
        display=args.display,
    )
    try:
        server = make_server(em.display, port=args.port, attempts=args.port_attempts)
    except OSError as error:
        print(f"{error.strerror or error}. Stop the other process or pass --port.", file=sys.stderr)
        return 2
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Emulator running at {url}  (Ctrl+C to stop)")
    if not args.demo and any(c.daily_budget is not None for c in em.board.sources):
        print("Live bus polling uses your TransportAPI allowance inside its windows.")
    if not args.no_browser:
        webbrowser.open(url)
    state = em.store.load() if em.store else {}
    try:
        run_loop(
            em.board,
            state,
            em.display,
            em.clock,
            em.tick_seconds,
            ticks=args.ticks,
            # A live clock stays on whole seconds; the demo's simulated clock needs no lining up.
            sleep=None if args.demo else _sleep_to_the_tick,
            store=em.store,
            on_tick=lambda now, result: em.display.tick(now, result.rendered),
            prime=True,  # fill the board straight away rather than waiting for a window
        )
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


# How often the dashboard looks for changed settings files.
SETTINGS_CHECK_S = 2.0


class _SettingsWatch(threading.Thread):
    """Looks every SETTINGS_CHECK_S for a change to any file the dashboard's
    settings come from: the overlay its settings page saved, config.toml or
    the secrets. When one has changed and the settings still load, it stops
    the server, so the dashboard can start afresh with them; when they don't
    load, it says so and keeps the ones running."""

    def __init__(self, store: Store, server) -> None:
        from .dashconfig import FileWatch

        super().__init__(name="settings-watch", daemon=True)
        self._store = store
        self._server = server
        self._watch = FileWatch(store.watched())
        self._stopping = threading.Event()
        self.restart = False

    def run(self) -> None:
        from .dashconfig import load_dashboard

        while not self._stopping.wait(SETTINGS_CHECK_S):
            if not self._watch.changed():
                continue
            try:
                load_dashboard(self._store, self._store.env(), lock=False)
            except ConfigError as error:
                logging.warning("Settings changed but don't load (%s); keeping the current ones", error)
                continue
            except Exception as error:  # noqa: BLE001 - the running dashboard carries on
                logging.warning("Settings changed but can't be checked (%s); keeping the current ones",
                                type(error).__name__)
                continue
            logging.info("Settings changed; starting the dashboard again to use them")
            self.restart = True
            self._server.shutdown()
            return

    def stop(self) -> None:
        self._stopping.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=10)


def _start_again(port: int) -> None:
    """Replace this process with a fresh dashboard, started the same way, on
    the same port and without opening the browser again. Nothing of the old
    one is left: every thread, the lock and the listening socket go with it.
    Inside Docker the process stays the container's main one throughout."""
    argv = [sys.executable, *sys.orig_argv[1:], "--no-browser", "--port", str(port)]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


def _settings_site(store: Store, install: Install, running: bool, runner=None):
    """The password-protected settings site: its tabs and the setup wizard,
    both saving into the store, checked as each view that is on loads.
    `running`: whether the dashboard runs, for the links to it. `runner`
    does the wizard's internet work, if another service does it."""
    from .groupform import check_views
    from .settingsite import PREFIX, SettingsSite
    from .setupapi import SetupApi

    target = store.target(lambda data, env: check_views(data, env, store.folder, install))
    links = [("The dashboard", "/")] if running else []
    extra = {}
    if install.fetch == "process":  # a save starts serve again (_SettingsWatch)
        extra["saved_message"] = "Saved. The dashboard starts again in a few seconds to use the new settings."
    setup = SetupApi(
        target, store.env, store.folder,
        links=[*links, ("Settings", PREFIX)],
        title=f"Set up your {install.noun}",
        noun=install.noun,
        keys=keys("general", *install.offered),
        runner=runner,
        **extra,
    )
    site = SettingsSite(store, install, setup, dashboard_on=lambda: running)
    if not site.password.is_set():
        where = "on the board" if site.to_board() else "in this log"
        print(f"Settings: no password yet. Open {PREFIX} and claim it with a code shown {where}, "
              f"or set one with `{install.password_command}`.", file=sys.stderr)
    return site


def _dashboard(args) -> int:
    import sqlite3

    from . import dashserver
    from .collector import Collector
    from .dashconfig import load_dashboard
    from .history import History
    from .snapshot import Hub

    store = _store(args)
    _adopt(args, store)
    if args.set_password or args.reset_code:
        return _dashboard_password(args, store)
    dash = load_dashboard(store, store.env())
    history = watch = None
    try:
        try:
            history = History(dash.state_dir)
        except sqlite3.Error as error:
            print(f"Can't open the punctuality history in {dash.state_dir}: {error}", file=sys.stderr)
            return 2
        hub = Hub(dash.title, dash.walk, tz=ZoneInfo(dash.timezone))
        collector = Collector(dash, hub, history)
        app = dashserver.DashboardApp(
            hub,
            dashserver.StaticFiles.load(dashserver.default_static_dir()),
            collector.healthy,
            recent=history.recent,
            history_days=dash.options.history_days,
            allowed_hosts=dash.options.allowed_hosts,
            settings=_settings_site(store, install_named("desktop"), True),
        )
        host = "0.0.0.0" if args.lan else args.host
        # A port asked for is that port or nothing (Docker's health check expects
        # it); without one, the next free port after the default will do.
        port, attempts = (dashserver.DEFAULT_PORT, 10) if args.port is None else (args.port, 1)
        try:
            server = dashserver.make_server(app, host, port, attempts)
        except OSError as error:
            hint = " Stop the other process or pass another --port." if args.port is not None else ""
            print(f"Can't listen on {host}:{port}: {error.strerror or error}.{hint}", file=sys.stderr)
            return 2
        port = server.server_address[1]
        collector.start()
        url = f"http://localhost:{port}/"
        print(f"Dashboard at {url}  (Ctrl+C to stop)")
        if args.lan:
            name = socket.gethostname().split(".")[0]
            print(f"On your home network: http://{name}.local:{port}/ or this computer's IP address and port {port}")
        if not args.no_browser:
            webbrowser.open(url)
        # docker stop sends SIGTERM: shut down as for Ctrl+C (signals reach the main thread only).
        on_main = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, _interrupt) if on_main else None
        try:
            watch = _SettingsWatch(store, server)
            watch.start()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if watch is not None:
                watch.stop()
            if on_main:
                signal.signal(signal.SIGTERM, previous)
            hub.close()
            if not collector.stop(timeout=5):
                logging.info("A dashboard poller was still fetching; it ends with the process")
            server.server_close()
    finally:
        if history is not None:
            history.close()
        dash.close()
    if watch is not None and watch.restart:
        _start_again(port)
    return 0


def _dashboard_password(args, store: Store) -> int:
    """--set-password or --reset-code for the settings: in the store, the
    same password as `web`'s, alongside a running dashboard (whose sessions
    end when the password changes; see auth.Sessions). Nothing else is
    loaded, so broken settings can't keep their owner out."""
    from .auth import PasswordStore
    from .settingsite import PREFIX

    store.make()
    if args.set_password:
        return _set_password(PasswordStore(store.password_file), "New dashboard settings password: ")
    return _reset_code(store.folder, f"Enter it on the dashboard's {PREFIX}/reset page with a new password.")


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number from 0 to 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="departureboard")
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p):
        p.add_argument("--config", default=DEFAULT_CONFIG)
        p.add_argument("--secrets", default=DEFAULT_SECRETS)
        return p

    run = with_config(sub.add_parser("run", help="one tick of the board"))
    run.add_argument("--png", help="write a PNG instead of driving the display")
    run.add_argument("--loop", action="store_true", help="keep running; needed for a screen")
    run.add_argument("--tick", type=float, help="seconds per tick with --loop (screen 1, e-paper 60)")
    run.add_argument("--ticks", type=int, help=argparse.SUPPRESS)
    run.set_defaults(handler=_run)

    probe = with_config(sub.add_parser("probe", help="one live call to check keys and codes"))
    probe.add_argument("source", choices=["train", "bus"])
    probe.add_argument(
        "--stop", action="append", metavar="LABEL=ATCO", help="bus stop(s) to probe instead of config"
    )
    probe.add_argument("--save", metavar="DIR", help="save raw bus responses as JSON")
    probe.add_argument(
        "--details", action="store_true", help="trains with coaches and calling points (GetDepBoardWithDetails)"
    )
    probe.set_defaults(handler=_probe)

    tt = with_config(sub.add_parser("timetable", help="build the BODS bus timetable for your stops, if due"))
    tt.add_argument("--force", action="store_true", help="build it now, as after changing operators")
    tt.add_argument("--needed", action="store_true", help=argparse.SUPPRESS)  # update.sh asks this
    tt.set_defaults(handler=_timetable)

    demo = sub.add_parser("demo", help="render sample data (a Glossop morning) to a PNG")
    demo.add_argument("--png", default="preview.png")
    demo.add_argument("--theme", default="sign", choices=sorted(THEMES))
    demo.add_argument("--accent", choices=["red", "yellow"], help="defaults to the theme's")
    demo.add_argument("--display", default="inky", choices=sorted(DISPLAY_PANELS), help="panel to draw for")
    demo.add_argument("--notice", action="store_true", help="show a sample disruption notice")
    demo.add_argument("--rain", action="store_true", help="show a sample rain warning (a notice takes its place)")
    demo.set_defaults(handler=_demo)

    route = with_config(sub.add_parser("route", help="print a [bus.routes] block for the screen's bus page"))
    route.add_argument("line", help="bus line as on the board, e.g. 341")
    route.add_argument("--stop", required=True, help="your stop's label from [bus.stops], e.g. A")
    route.add_argument("--noc", help="operator code (TransportAPI's 'operator', e.g. BNML) to find its BODS timetable")
    route.add_argument("--txc", help="a TransXChange .xml or .zip you downloaded, instead of --noc")
    route.set_defaults(handler=_route)

    setup = sub.add_parser("setup", help="first-run setup steps")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)
    discover = with_config(setup_sub.add_parser(
        "discover", help="find the bus operators serving your stops, build their timetable, print the settings"))
    discover.add_argument("--stop", action="append", required=True, metavar="LABEL=ATCO",
                          help="a stop to search for, e.g. A=1000DGHS0900; repeat for each")
    discover.add_argument("--routes", action="store_true", help="also build a route strip for each line")
    discover.set_defaults(handler=_setup_discover)
    serve = with_config(setup_sub.add_parser(
        "serve", help="answer the settings page's setup requests; departureboard-setup.service runs this"))
    serve.set_defaults(handler=_setup_serve)

    web = with_config(sub.add_parser("web", help="serve the settings page on the home network"))
    web.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    web.add_argument("--port", type=int, default=WEB_PORT)
    web.add_argument("--setup-via-spool", action="store_true",
                     help="hand the setup wizard's internet lookups to departureboard-setup.service")
    once = web.add_mutually_exclusive_group()
    once.add_argument("--set-password", action="store_true", help="set the page's password and exit")
    once.add_argument("--reset-code", action="store_true",
                      help="print a one-time code for setting a new password on the page, and exit")
    web.set_defaults(handler=_web)

    emulate = with_config(sub.add_parser("emulate", help="run against a browser panel emulator"))
    emulate.add_argument("--demo", action="store_true", help="sample Glossop data on a fast simulated clock")
    emulate.add_argument("--fake-train", action="store_true", help="fake trains, live buses")
    emulate.add_argument("--accent", choices=["red", "yellow"], help="demo accent")
    emulate.add_argument("--display", choices=sorted(DISPLAY_PANELS), help="panel to emulate (default: config's)")
    emulate.add_argument("--port", type=int, default=DEFAULT_PORT)
    emulate.add_argument("--port-attempts", type=int, default=10, help=argparse.SUPPRESS)
    emulate.add_argument("--tick", type=float, help="seconds per tick (e-paper: demo 2, live 60; a screen: 1)")
    emulate.add_argument("--ticks", type=int, help=argparse.SUPPRESS)
    emulate.add_argument("--no-browser", action="store_true")
    emulate.set_defaults(handler=_emulate)

    dashboard = with_config(sub.add_parser("dashboard", help="serve the web dashboard and open it in the browser"))
    dashboard.add_argument("--state-dir", help="the dashboard's own folder for its timetable and history")
    dashboard.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    dashboard.add_argument("--port", type=_port, help="port to listen on (default: 8090, or the next free one)")
    dashboard.add_argument("--lan", action="store_true", help="listen on all addresses, for phones at home")
    dashboard.add_argument("--no-browser", action="store_true", help="don't open the browser")
    once = dashboard.add_mutually_exclusive_group()
    once.add_argument("--set-password", action="store_true",
                      help="set the password for the dashboard's settings and exit (it may be running)")
    once.add_argument("--reset-code", action="store_true",
                      help="print a one-time code for setting a new settings password, and exit")
    dashboard.set_defaults(handler=_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
