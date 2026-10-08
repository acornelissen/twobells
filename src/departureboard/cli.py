"""Command line entry points.

  departureboard run    one tick; systemd runs this every minute
  departureboard probe  one live call to check keys and stop codes
  departureboard demo   render sample data to a PNG, no keys needed
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .app import run_once
from .config import ConfigError, load_config
from .display import InkyDisplay, PngDisplay
from .model import Departure, Status
from .render import render
from .state import StateStore
from .view import build_view

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


def _settings(args):
    env = {**os.environ, **load_env_file(args.secrets)}
    return load_config(args.config, env)


def _run(args) -> int:
    settings = _settings(args)
    display = (
        PngDisplay(args.png, settings.accent)
        if args.png
        else InkyDisplay(settings.accent, rotate_180=settings.rotate_180)
    )
    store = StateStore(settings.state_file)
    state = store.load()
    result = run_once(settings.board, state, display, _local_now(settings.timezone))
    store.save(state)
    logging.info("rendered=%s", result.rendered)
    return 0


def _probe(args) -> int:
    settings = _settings(args)
    for config in settings.board.sources:
        if config.source.name != args.source:
            continue
        if config.daily_budget is not None:
            print("Note: this uses one hit of your daily API allowance.")
        for dep in config.source.fetch():
            print(f"{dep.scheduled}  {dep.service:<10} {dep.destination:<28} {dep.status.value} {dep.expected or ''}")
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
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN),
    ]
    display = PngDisplay(args.png, args.accent)
    view = build_view("Glossop > Manchester Picc", trains, buses, "07:03", set())
    display.show(render(view, display.palette))
    print(f"Wrote {args.png}")
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
    probe.set_defaults(handler=_probe)

    demo = sub.add_parser("demo", help="render sample data to a PNG")
    demo.add_argument("--png", default="preview.png")
    demo.add_argument("--accent", default="red", choices=["red", "yellow"])
    demo.set_defaults(handler=_demo)
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
