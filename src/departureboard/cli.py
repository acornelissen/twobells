"""Command line entry points.

  departureboard run      one tick; systemd runs this every minute
  departureboard probe    one live call to check keys and stop codes
  departureboard demo     render sample data to a PNG, no keys needed
  departureboard emulate  run the board against a browser-based panel emulator
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .app import run_loop, run_once
from .config import ConfigError, load_config
from .display import InkyDisplay, PngDisplay
from .emulation import build_emulation
from .emulator import DEFAULT_PORT, make_server
from .model import Departure, Status
from .state import StateStore
from .transportapi import redact_credentials
from .view import build_view
from .themes import THEMES, theme_named
from .walk import WalkTimes

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


def _settings(args, bus_stops: dict[str, str] | None = None, only: set[str] | None = None):
    env = {**os.environ, **load_env_file(args.secrets)}
    return load_config(args.config, env, bus_stops, only)


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
        PngDisplay(args.png, settings.accent)
        if args.png
        else InkyDisplay(settings.accent, rotate_180=settings.rotate_180)
    )
    store = StateStore(settings.state_file)
    state = store.load()
    # A fresh install (no saved state) fetches everything once, so the panel
    # isn't blank until the next poll window opens.
    result = run_once(settings.board, state, display, _local_now(settings.timezone), prime=not state)
    store.save(state)
    logging.info("rendered=%s", result.rendered)
    return 0


def _probe(args) -> int:
    settings = _settings(args, _parse_stops(args.stop), only={args.source})
    for config in settings.board.sources:
        if config.source.name != args.source:
            continue
        if args.source == "bus":
            return _probe_bus(config.source, args.save)
        try:
            departures = config.source.fetch()
        except Exception as error:  # noqa: BLE001 - one readable line, not a traceback
            print(f"error: {error}")
            return 1
        _print_departures(departures)
        return 0
    print(f"No [{args.source}] section in config", file=sys.stderr)
    return 2


def _demo(args) -> int:
    trains = [
        Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME),
        Departure("train", "Northern", "Manchester Piccadilly", "07:42", "07:49", Status.LATE),
        Departure("train", "Northern", "Manchester Piccadilly", "08:12", None, Status.CANCELLED),
    ]
    buses = [
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A"),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B"),
        Departure("bus", "237", "Ashton", "07:41", None, Status.ON_TIME, stop="A"),
    ]
    theme = theme_named(args.theme)
    display = PngDisplay(args.png, args.accent or theme.accent)
    walk = WalkTimes(train=7, bus={"*": 7})
    view = build_view(
        "Glossop > Manchester Picc", trains, buses, "07:03", set(), walk, theme.wording
    )
    display.show(theme.render(view, display.palette))
    print(f"Wrote {args.png}")
    return 0


def _emulate(args) -> int:
    env = None if args.demo else {**os.environ, **load_env_file(args.secrets)}
    em = build_emulation(
        demo=args.demo,
        config=args.config,
        env=env,
        fake_train=args.fake_train,
        accent=args.accent,
        tick_seconds=args.tick,
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
    run.add_argument("--png", help="write a PNG instead of driving the Inky pHAT")
    run.set_defaults(handler=_run)

    probe = with_config(sub.add_parser("probe", help="one live call to check keys and codes"))
    probe.add_argument("source", choices=["train", "bus"])
    probe.add_argument(
        "--stop", action="append", metavar="LABEL=ATCO", help="bus stop(s) to probe instead of config"
    )
    probe.add_argument("--save", metavar="DIR", help="save raw bus responses as JSON")
    probe.set_defaults(handler=_probe)

    demo = sub.add_parser("demo", help="render sample data to a PNG")
    demo.add_argument("--png", default="preview.png")
    demo.add_argument("--theme", default="sign", choices=sorted(THEMES))
    demo.add_argument("--accent", choices=["red", "yellow"], help="defaults to the theme's")
    demo.set_defaults(handler=_demo)

    emulate = with_config(sub.add_parser("emulate", help="run against a browser panel emulator"))
    emulate.add_argument("--demo", action="store_true", help="fake data on a fast simulated clock")
    emulate.add_argument("--fake-train", action="store_true", help="fake trains, live buses")
    emulate.add_argument("--accent", choices=["red", "yellow"], help="demo accent")
    emulate.add_argument("--port", type=int, default=DEFAULT_PORT)
    emulate.add_argument("--port-attempts", type=int, default=10, help=argparse.SUPPRESS)
    emulate.add_argument("--tick", type=float, help="seconds per tick (demo 2, live 60)")
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
