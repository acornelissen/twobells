"""Command line entry points.

  twobells run      one tick; systemd runs this every minute
  twobells run --loop   keep ticking; a screen redraws every second
                          (until the board is set up, both show where to set it up)
  twobells probe    one live call to check keys and stop codes
  twobells demo     render sample data (a Glossop morning) to a PNG, no keys needed
  twobells emulate  run the board against a browser-based panel emulator
  twobells route    print a [bus.routes] block for the screen's bus page (run on a laptop)
  twobells timetable  build the BODS bus timetable for your stops and the Metrolink tram
                          timetable for your tram stops, each if due
  twobells setup discover  find the bus operators serving your stops and print their settings
  twobells fetch    a Pi's internet work for its web server: the setup wizard's lookups and the
                          dashboard's pollers (twobells-fetch.service)
  twobells serve    serve the settings, and the dashboard where its view is on, on the home
                          network: --install pi, docker or desktop (the default, opening the browser)
  twobells serve --set-password | --reset-code
                          set the settings password, or print a code for a new one, and exit

`query` (unlisted) answers install.sh and update.sh.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys

from ..board.names import DISPLAY_PANELS
from ..board.themes import THEMES
from ..core.errors import ConfigError
from ..emulator.server import DEFAULT_PORT
from ..settings.installs import DEFAULT as DEFAULT_INSTALL
from ..settings.installs import INSTALLS
from ..settings.store import WrongUser
from ..version import package_version
from .dev import demo_command, emulate_command, probe_command, route_command
from .run import run_command
from .serve import fetch_command, serve_command
from .setup import QUERIES, discover_command, query_command
from .timetable import timetable_command

DEFAULT_CONFIG = "/etc/twobells/config.toml"
DEFAULT_SECRETS = "/etc/twobells/secrets.env"


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number from 0 to 65535")
    return port


def _tick(text: str) -> float:
    """Seconds per tick: a finite number above 0. At 0 or less a running
    board would redraw flat out; nan or inf would never tick at all."""
    try:
        seconds = float(text)
    except ValueError:
        seconds = math.nan
    if not (math.isfinite(seconds) and seconds > 0):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0")
    return seconds


# Unlisted: query, for the Pi's scripts.
HIDDEN_COMMANDS = ("query",)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="twobells")
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p):
        p.add_argument("--config", default=DEFAULT_CONFIG, help="settings file, optional (default: %(default)s)")
        p.add_argument("--secrets", default=DEFAULT_SECRETS, help="keys file, optional (default: %(default)s)")
        return p

    run = with_config(sub.add_parser("run", help="one tick of the board"))
    run.add_argument("--png", help="write a PNG instead of driving the display")
    run.add_argument("--loop", action="store_true", help="keep running; needed for a screen")
    run.add_argument("--tick", type=_tick, help="seconds per tick with --loop (screen 1, e-paper 60)")
    run.add_argument("--ticks", type=int, help=argparse.SUPPRESS)
    run.add_argument("--timings", action="store_true",
                     help="with --loop on a screen, log how long each second's board took to make and show")
    run.set_defaults(handler=run_command)

    probe = with_config(sub.add_parser("probe", help="one live call to check keys and codes"))
    probe.add_argument("source", choices=["train", "bus"])
    probe.add_argument(
        "--stop", action="append", metavar="LABEL=ATCO", help="bus stop(s) to probe instead of config"
    )
    probe.add_argument("--save", metavar="DIR", help="save TransportAPI's raw bus responses as JSON")
    probe.add_argument(
        "--details", action="store_true", help="trains with coaches and calling points (GetDepBoardWithDetails)"
    )
    probe.set_defaults(handler=probe_command)

    tt = with_config(sub.add_parser(
        "timetable", help="build the bus timetable for your stops and the tram timetable for your tram stops, "
                          "each if due"))
    tt.add_argument("--force", action="store_true",
                    help="build each now, even within 6 hours of a failed build, as after changing operators")
    tt.add_argument("--install", choices=list(INSTALLS), default=DEFAULT_INSTALL,
                    help="what this runs on: a Pi reads less of each timetable file, to fit its memory")
    tt.set_defaults(handler=timetable_command)

    # For install.sh and update.sh, so they ask the app rather than read config.toml. Hidden.
    query = sub.add_parser("query")
    query.add_argument("what", choices=QUERIES)
    query.add_argument("--install", choices=list(INSTALLS), default="pi")
    query.add_argument("--config", default=DEFAULT_CONFIG)
    query.add_argument("--state-dir")
    query.set_defaults(handler=query_command)

    demo = sub.add_parser("demo", help="render sample data (a Glossop morning) to a PNG")
    demo.add_argument("--png", default="preview.png", help="picture to write (default: %(default)s)")
    demo.add_argument("--theme", default="sign", choices=sorted(THEMES), help="the look (default: %(default)s)")
    demo.add_argument("--accent", choices=["red", "yellow"], help="defaults to the theme's")
    demo.add_argument("--display", default="inky", choices=sorted(DISPLAY_PANELS), help="panel to draw for")
    demo.add_argument("--notice", action="store_true", help="show a sample disruption notice")
    demo.add_argument("--rain", action="store_true",
                      help="show a sample rain warning (beside a notice on a screen; on e-paper a notice hides it)")
    demo.set_defaults(handler=demo_command)

    route = with_config(sub.add_parser("route", help="print a [bus.routes] block for the screen's bus page"))
    route.add_argument("line", help="bus line as on the board, e.g. 341")
    route.add_argument("--stop", required=True, help="your stop's label from [bus.stops], e.g. A")
    route.add_argument("--noc", help="national operator code, e.g. BNML, to find its BODS timetable")
    route.add_argument("--txc", help="a TransXChange .xml or .zip you downloaded, instead of --noc")
    route.set_defaults(handler=route_command)

    setup = sub.add_parser("setup", help="first-run setup steps")
    setup_sub = setup.add_subparsers(dest="setup_command", required=True)
    discover = with_config(setup_sub.add_parser(
        "discover", help="find the bus operators serving your stops, build their timetable, print the settings"))
    discover.add_argument("--stop", action="append", required=True, metavar="LABEL=ATCO",
                          help="a stop to search for, e.g. A=1000DGHS0900; repeat for each")
    discover.add_argument("--routes", action="store_true", help="also build a route strip for each line")
    discover.set_defaults(handler=discover_command)

    fetch = with_config(sub.add_parser(
        "fetch", help="do a Pi's internet work for its web server; twobells-fetch.service runs this"))
    fetch.add_argument("--install", choices=["pi"], default="pi", help="only a Pi has a fetch service")
    fetch.add_argument("--state-dir", help="the store: the folder for saved settings, keys, the password and state")
    fetch.set_defaults(handler=fetch_command)

    emulate = with_config(sub.add_parser("emulate", help="run against a browser panel emulator"))
    emulate.add_argument("--demo", action="store_true", help="sample Glossop data on a fast simulated clock")
    emulate.add_argument("--fake-train", action="store_true", help="fake trains, live buses")
    emulate.add_argument("--accent", choices=["red", "yellow"], help="demo accent")
    emulate.add_argument("--display", choices=sorted(DISPLAY_PANELS), help="panel to emulate (default: config's)")
    emulate.add_argument("--port", type=_port, default=DEFAULT_PORT, help="port to serve on (default: %(default)s)")
    emulate.add_argument("--port-attempts", type=int, default=10, help=argparse.SUPPRESS)
    emulate.add_argument("--tick", type=_tick, help="seconds per tick (e-paper: demo 2, live 60; a screen: 1)")
    emulate.add_argument("--ticks", type=int, help=argparse.SUPPRESS)
    emulate.add_argument("--no-browser", action="store_true", help="don't open a browser")
    emulate.set_defaults(handler=emulate_command)

    serve = with_config(sub.add_parser(
        "serve", help="serve the settings, and the dashboard where it is on, on the home network"))
    serve.add_argument("--install", choices=list(INSTALLS), default=DEFAULT_INSTALL,
                       help="what this runs on: a Pi with its board, Docker, or a desktop (the default)")
    serve.add_argument("--state-dir", help="the store: the folder for saved settings, keys, the password and state")
    serve.add_argument("--host", help="address to listen on (default: all on a Pi and in Docker, "
                                      "this computer only on a desktop)")
    serve.add_argument("--port", type=_port,
                       help="port to listen on (default: 8090, or on a desktop the next free one)")
    serve.add_argument("--lan", action="store_true", help="listen on all addresses, for phones at home")
    serve.add_argument("--no-browser", action="store_true", help="don't open the browser")
    _password_flags(serve)
    serve.set_defaults(handler=serve_command)

    sub.metavar = "{" + ",".join(name for name in sub.choices if name not in HIDDEN_COMMANDS) + "}"
    return parser


def _password_flags(parser: argparse.ArgumentParser) -> None:
    once = parser.add_mutually_exclusive_group()
    once.add_argument("--set-password", action="store_true",
                      help="set the settings password and exit (the server may be running)")
    once.add_argument("--reset-code", action="store_true",
                      help="print a one-time code for setting a new settings password, and exit")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    args.argv = sys.argv[1:] if argv is None else list(argv)  # to say how to run it as someone else
    try:
        return args.handler(args)
    except ConfigError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2
    except WrongUser as error:
        print(error, file=sys.stderr)
        return 2
