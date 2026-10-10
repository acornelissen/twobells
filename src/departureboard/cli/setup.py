"""`setup discover`, which finds the bus operators serving the board's stops,
and the unlisted `query`, which answers install.sh and update.sh."""

from __future__ import annotations

import sys
from typing import cast

from ..board.displays.display import DISPLAY_PANELS
from ..settings.config import ConfigError, is_enabled
from ..settings.installs import install_named
from ..settings.schema import BOARD_DEFAULTS, bus_source, views
from ..settings.store import Store
from .common import as_owner, local_now, open_store, parse_stops

QUERIES = ("views", "display", "needs-timetable")


def discover_command(args) -> int:
    from ..settings.config import _walk
    from ..setup import discover

    # argparse requires at least one --stop here, so there are always stops.
    stops = cast(dict[str, str], parse_stops(args.stop))
    store = as_owner(args, open_store(args))
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
    timezone = data.get("timezone")
    timezone = timezone if isinstance(timezone, str) else "Europe/London"
    job = discover.SetupJob(folder, stops, key, routes=args.routes, walk_minutes=walk,
                            now=lambda: local_now(timezone))
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
            running = status["state"] == "running"
            roads = status.get("roads") or {}
            if roads.get("state") == "running":  # once the search is done; waited for here
                step, done, total, running = discover.STEP_ROADS, roads["done"], roads["total"], True
                if done != total:
                    done -= done % 25  # a road a second, up to 300: every 25th, and the last
            if running and (step, done, total) != shown:
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


def _wants_timetable(store: Store) -> bool:
    """Whether the board's buses are on and come from the BODS timetable,
    in config.toml with the settings saved from the page over it."""
    data = store.data()
    bus = data.get("bus")
    return isinstance(bus, dict) and is_enabled(data, "bus") and bus_source(bus) == "bods"


def query_command(args) -> int:
    """For install.sh and update.sh: one thing about the settings in force
    (config.toml with the saved settings over it), one value per line. No
    keys are read and nothing is written.
      views            each view switched on (none: no lines)
      display          the display config.toml sets (it alone may)
      needs-timetable  yes or no: whether buses come from the BODS timetable;
                       exits 0 for yes and 1 for no, for `if`
    Settings that can't be read exit 2, saying why."""
    install = install_named(args.install)
    try:
        store = Store.open(args.config, None, state_dir=args.state_dir, install=install.name)
        if args.what == "needs-timetable":
            wanted = _wants_timetable(store)
            print("yes" if wanted else "no")
            return 0 if wanted else 1
        data = store.data()
    except OSError as error:
        print(f"Can't read the settings: {error.strerror or error}", file=sys.stderr)
        return 2
    if args.what == "views":
        values = list(views(data, install))
    else:
        display = data.get("display", BOARD_DEFAULTS["display"])
        if not isinstance(display, str) or display not in DISPLAY_PANELS:
            raise ConfigError(f"Unknown display {display!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")
        values = [display]
    for value in values:
        print(value)
    return 0
