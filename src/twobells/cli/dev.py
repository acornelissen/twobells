"""Development helpers: `demo`, `emulate`, `probe` and `route`."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from ..board.app import run_loop
from ..board.displays.display import PngDisplay
from ..board.names import DISPLAY_PANELS
from ..board.themes import theme_named
from ..board.view import build_view, stop_names_shown
from ..core.errors import ConfigError, redact_keys
from ..core.model import Departure, Status, Strip, strip_key
from ..core.net import DOWNLOAD_DEADLINE, get_bytes
from ..core.walk import WalkTimes
from ..emulator.emulation import build_emulation
from ..emulator.fake import DEMO_CALLING, DEMO_NOTICE, DEMO_STOP_NAMES, DEMO_TITLE, FakeWeatherSource
from ..emulator.server import make_server
from ..settings.config import load_env_file
from ..settings.store import Store
from ..sources.bodsbus import BodsBusSource
from ..sources.darwin import DarwinSource, TrainBoard
from ..sources.transportapi import TransportApiBusSource
from ..sources.weather import rain_line
from .common import as_owner, load_settings, open_store, parse_stops, sleep_to_the_tick


def _env(config: Path | str, secrets: Path | str) -> dict[str, str]:
    """The environment, then secrets.env, then keys saved from the web page."""
    try:
        return Store.open(config, secrets).env()
    except ConfigError:
        # A broken config.toml, needed to find the store: load_config reports that.
        return {**os.environ, **load_env_file(secrets)}


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
                    json.dumps(redact_keys(payload), indent=2)
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


def probe_command(args) -> int:
    if args.details and args.source != "train":
        print("--details is for trains only", file=sys.stderr)
        return 2
    settings = load_settings(args, parse_stops(args.stop), only={args.source})
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


def _download(url: str, max_bytes: int) -> bytes:
    """A timetable zip or a NaPTAN area: big, perhaps on a slow line."""
    return get_bytes(url, max_bytes, timeout=300, deadline=DOWNLOAD_DEADLINE)


def route_command(args) -> int:
    from ..board.config import _walk
    from ..core.walk import DEFAULT_STOP
    from ..timetable import routesetup

    store = open_store(args)
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
                _download,
                lambda url: print(f"Downloading {url}", file=sys.stderr),
            )
        found = routesetup.find_patterns(documents, args.line)
        if not found:
            print(f"No timetable for line {args.line} found", file=sys.stderr)
            return 1
        atcos, covered, journeys = routesetup.strip_before(found, atco, walk_minutes)
        stops = routesetup.naptan_stops(atcos, http_get=_download)
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


def demo_command(args) -> int:
    trains = [
        Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "1",
                  calling_at=DEMO_CALLING),
        Departure("train", "Northern", "Manchester Piccadilly", "07:42", "07:49", Status.LATE, "1",
                  calling_at=DEMO_CALLING),
        Departure("train", "Northern", "Manchester Piccadilly", "08:12", None, Status.CANCELLED, "2"),
    ]
    buses = [
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A"),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B"),
        Departure("bus", "237", "Ashton", "07:41", None, Status.ON_TIME, stop="A"),
    ]
    # Where each is, as a wide screen shows it. The cancelled train and the 61,
    # with no live position, have none; the 237 hasn't started its journey yet.
    strips = {
        strip_key(trains[0]): Strip("Left Hadfield", 4, 2, 0.5),
        strip_key(trains[1]): Strip("At Dinting", 4, 2, 1.0),
        strip_key(buses[0]): Strip("3 stops away", 7, 5, 2.4),
        strip_key(buses[2]): Strip("Not started", 7, 5, None),
    }
    theme = theme_named(args.theme)
    if theme.screen_only and DISPLAY_PANELS[args.display].epaper:
        print(f"The {theme.name} look is for a screen: pass --display hdmi-1280x400 or hdmi-1920x440.",
              file=sys.stderr)
        return 2
    display = PngDisplay(args.png, args.accent or theme.accent, DISPLAY_PANELS[args.display])
    walk = WalkTimes(train=5, bus={"*": 8})
    # A screen preview shows what a screen adds: a running clock and countdowns.
    clock = None if display.panel.epaper else "07:03:15"
    now = datetime(2026, 10, 8, 7, 3, 15)
    # Rain likely through the 07:00 hour, before you'd reach the 07:12; on e-paper a notice takes its place.
    rain = rain_line(FakeWeatherSource(lambda: now).fetch(), now, now.replace(minute=12), 50, live=bool(clock))
    view = build_view(
        DEMO_TITLE, {"train": trains, "bus": buses}, "07:03", set(), walk, theme.wording, clock,
        notices=[DEMO_NOTICE] if args.notice else None,
        rain=rain if args.rain else None,
        strips=strips,
        # Glossop's stops share a name, so a screen wide enough says "Henry Street A".
        stop_names=stop_names_shown(DEMO_STOP_NAMES),
    )
    display.show(theme.render(view, display.palette, display.panel))
    print(f"Wrote {args.png}")
    return 0


def emulate_command(args) -> int:
    if not args.demo:  # keeps the board's state in the store
        with contextlib.suppress(ConfigError):  # loading the settings says what is wrong
            as_owner(args, open_store(args))
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
            sleep=None if args.demo else sleep_to_the_tick,
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
