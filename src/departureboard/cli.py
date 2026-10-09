"""Command line entry points.

  departureboard run      one tick; systemd runs this every minute
  departureboard run --loop   keep ticking; a screen redraws every second
  departureboard probe    one live call to check keys and stop codes
  departureboard demo     render sample data to a PNG, no keys needed
  departureboard emulate  run the board against a browser-based panel emulator
  departureboard route    print a [bus.routes] block for the screen's bus page (run on a laptop)
  departureboard web      serve the settings page on the home network
  departureboard timetable  build the BODS bus timetable for your stops, if due
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .app import run_loop, run_once
from .bodsbus import BodsBusSource
from .config import ConfigError, load_config, overlay_paths, read_config, state_dir
from .display import DISPLAY_PANELS, PngDisplay, open_display
from .holidays import bank_holidays
from .emulation import build_emulation
from .emulator import DEFAULT_PORT, make_server
from .fake import DEMO_NOTICE, FakeWeatherSource
from .http import get_bytes
from .model import Departure, Status
from .state import StateStore
from .transportapi import TransportApiBusSource, redact_credentials
from .view import build_view
from .themes import THEMES, theme_named
from .walk import WalkTimes
from .weather import rain_line

DEFAULT_CONFIG = "/etc/departureboard/config.toml"
DEFAULT_SECRETS = "/etc/departureboard/secrets.env"


def load_env_file(path: Path | str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


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


def _env(config: Path | str, secrets: Path | str) -> dict[str, str]:
    """The environment, then secrets.env, then keys saved from the web page."""
    env = {**os.environ, **load_env_file(secrets)}
    try:
        paths = overlay_paths(config)
    except (OSError, ConfigError):
        return env  # no config file, or a broken one: load_config reports that
    if paths:
        env.update(load_env_file(paths[1]))
    return env


def _holidays(config: Path | str) -> frozenset:
    """Bank holidays, cached beside the state file and refreshed from gov.uk
    weekly, unless the settings say to ignore them (then gov.uk is never asked)."""
    try:
        data = read_config(config)
        folder = state_dir(data)
    except (OSError, ConfigError):
        return frozenset()  # load_config reports it
    if folder is None or data.get("bank_holidays", True) is not True:
        return frozenset()
    # Only the cache's age depends on the time, so the Pi's own zone will do.
    return bank_holidays(folder, datetime.now())


def _settings(args, bus_stops: dict[str, str] | None = None, only: set[str] | None = None):
    # A probe makes one call whatever the day, so it needs no holidays.
    holidays = _holidays(args.config) if only is None else frozenset()
    return load_config(args.config, _env(args.config, args.secrets), bus_stops, only, holidays=holidays)


def _print_departures(departures: list[Departure], indent: str = "") -> None:
    for dep in departures:
        print(
            f"{indent}{dep.scheduled}  {dep.service:<10} {dep.destination:<28} "
            f"{dep.status.value} {dep.expected or ''}".rstrip()
        )


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


def _run(args) -> int:
    settings = _settings(args)
    display = (
        PngDisplay(args.png, settings.accent, DISPLAY_PANELS[settings.display])
        if args.png
        else open_display(settings.display, settings.accent, settings.rotate, settings.panel_colour)
    )
    store = StateStore(settings.state_file)
    state = store.load()
    if args.loop:
        return _run_loop(settings, display, store, state, args)
    # A fresh install (no saved state) fetches everything once, so the panel
    # isn't blank until the next poll window opens.
    try:
        result = run_once(settings.board, state, display, _local_now(settings.timezone), prime=not state)
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
        paths = [Path(args.config), Path(args.secrets)]
        overlays = overlay_paths(args.config)
        if overlays:
            paths.extend(overlays)
        self._paths = paths
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
        if self._hardware(new) != self._hardware(self._settings):
            raise _Restart
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
            prime=not state,  # as for a single run: only a fresh install fetches outside windows
            reload=_Watcher(args, settings),
        )
    except KeyboardInterrupt:
        pass
    except _Restart:
        logging.info("Display settings changed; exiting so the service starts again")
        store.save(state)
        return RESTART
    return 0


def _route(args) -> int:
    from . import routesetup
    from .config import _walk, read_config
    from .walk import DEFAULT_STOP

    data = read_config(args.config)
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
            key = _env(args.config, args.secrets).get("BODS_API_KEY", "").strip()
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


def _web(args) -> int:
    import getpass
    from importlib.resources import files

    from . import web
    from .config import read_base, state_dir
    from .settingsform import SettingsForm

    # config.toml alone: a broken settings.toml must not stop the page that fixes it.
    folder = state_dir(read_base(args.config))
    if folder is None:
        print("config.toml needs a state_file; the settings page saves beside it", file=sys.stderr)
        return 2
    password = web.Password(folder / "web.json")
    if args.set_password:
        first = getpass.getpass("New settings page password: ")
        if first != getpass.getpass("Again: "):
            print("Those didn't match", file=sys.stderr)
            return 1
        try:
            password.set(first)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        print(f"Password saved to {folder / 'web.json'}")
        return 0
    form = SettingsForm(args.config, lambda: _env(args.config, args.secrets))
    stylesheet = files("departureboard").joinpath("web.css").read_bytes()
    handler = web.make_handler(form, password, web.Sessions(), web.SignInLimiter(), web.shell, stylesheet)
    try:
        server = web.make_server(handler, args.host, args.port)
    except OSError as error:
        print(f"Can't listen on {args.host}:{args.port}: {error.strerror or error}", file=sys.stderr)
        return 2
    if not password.is_set():
        print("No password yet: run `departureboard web --set-password` as the service user", file=sys.stderr)
    print(f"Settings page on http://{args.host}:{server.server_address[1]}/  (home network only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _probe(args) -> int:
    settings = _settings(args, _parse_stops(args.stop), only={args.source})
    for config in settings.board.sources:
        if config.source.name != args.source:
            continue
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


def _timetable(args) -> int:
    settings = _settings(args, only={"bus"})
    source = next((c.source for c in settings.board.sources if c.source.name == "bus"), None)
    if not isinstance(source, BodsBusSource):
        print('Bus times come from TransportAPI; set source = "bods" under [bus] first', file=sys.stderr)
        return 2
    # The timetable's bank holiday names come from this list, so keep it fresh
    # whatever bank_holidays says about checking times.
    bank_holidays(settings.state_file.parent, datetime.now())
    schedule = source.refresh_timetable(force=args.force)
    reason = source.unusable()
    if schedule is None or reason:
        print(f"No usable bus timetable: {reason}", file=sys.stderr)
        return 1
    until = f", until {schedule.valid_until:%d %b %Y}" if schedule.valid_until else ""
    print(f"Bus timetable: {len(schedule.journeys)} journeys at your stops, built {schedule.built:%a %d %b %H:%M}{until}")
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
    walk = WalkTimes(train=7, bus={"*": 7})
    # A screen preview shows what a screen adds: a running clock and countdowns.
    clock = None if display.panel.epaper else "07:03:15"
    now = datetime(2026, 10, 8, 7, 3, 15)
    # Rain likely through the 07:00 hour, before you'd reach the 07:12; a notice takes its place.
    rain = rain_line(FakeWeatherSource(lambda: now).fetch(), now, now.replace(minute=12), 50, live=bool(clock))
    view = build_view(
        "Glossop > Manchester Picc", trains, buses, "07:03", set(), walk, theme.wording, clock,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="departureboard")
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
    probe.set_defaults(handler=_probe)

    tt = with_config(sub.add_parser("timetable", help="build the BODS bus timetable for your stops, if due"))
    tt.add_argument("--force", action="store_true", help="build it now, as after changing operators")
    tt.set_defaults(handler=_timetable)

    demo = sub.add_parser("demo", help="render sample data to a PNG")
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

    web = with_config(sub.add_parser("web", help="serve the settings page on the home network"))
    web.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument("--set-password", action="store_true", help="set the page's password and exit")
    web.set_defaults(handler=_web)

    emulate = with_config(sub.add_parser("emulate", help="run against a browser panel emulator"))
    emulate.add_argument("--demo", action="store_true", help="fake data on a fast simulated clock")
    emulate.add_argument("--fake-train", action="store_true", help="fake trains, live buses")
    emulate.add_argument("--accent", choices=["red", "yellow"], help="demo accent")
    emulate.add_argument("--display", choices=sorted(DISPLAY_PANELS), help="panel to emulate (default: config's)")
    emulate.add_argument("--port", type=int, default=DEFAULT_PORT)
    emulate.add_argument("--port-attempts", type=int, default=10, help=argparse.SUPPRESS)
    emulate.add_argument("--tick", type=float, help="seconds per tick (demo 2; live 60, or 1 for a screen)")
    emulate.add_argument("--ticks", type=int, help=argparse.SUPPRESS)
    emulate.add_argument("--no-browser", action="store_true")
    emulate.set_defaults(handler=_emulate)
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
