"""Helpers more than one command uses: the store and settings a command line
names, the local time, stops given on the command line, and where the
settings are served."""

from __future__ import annotations

import os
import re
import shlex
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..settings.config import ConfigError, holiday_division, load_config
from ..settings.store import Store, check_owner
from ..sources.holidays import bank_holidays

# The settings' port on a Pi, as twobells-web.service serves them
# (and the old `web` command's). Nothing listens on the old 8080.
WEB_PORT = 8090


def local_now(timezone: str) -> datetime:
    return datetime.now(ZoneInfo(timezone)).replace(tzinfo=None, second=0, microsecond=0)


def parse_stops(specs: list[str] | None) -> dict[str, str] | None:
    if not specs:
        return None
    stops = {}
    for spec in specs:
        label, sep, atco = spec.partition("=")
        if not sep or not label or not atco:
            raise ConfigError(f"--stop expects LABEL=ATCO, got {spec!r}")
        stops[label] = atco
    return stops


def open_store(args) -> Store:
    """The store for a command's --config, --secrets and --state-dir (if it has one)."""
    return Store.open(args.config, args.secrets, state_dir=getattr(args, "state_dir", None))


def as_owner(args, store: Store) -> Store:
    """`store`, for a command about to write to it; WrongUser, saying to run
    this command line as its owner, if this is root and it isn't root's."""
    check_owner(store.folder, shlex.join([_program(), *getattr(args, "argv", sys.argv[1:])]))
    return store


def _program() -> str:
    """This program as it was run, for a line to paste: on a Pi it is in its
    venv, not on the PATH, so a path is kept; otherwise its name."""
    run_as = sys.argv[0] if sys.argv else ""
    if Path(run_as).name == "twobells" and os.sep in run_as:
        return os.path.abspath(run_as)
    return "twobells"


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


def load_settings(args, bus_stops: dict[str, str] | None = None, only: set[str] | None = None):
    store = open_store(args)
    # A probe makes one call whatever the day, so it needs no holidays.
    holidays = _holidays(store) if only is None else frozenset()
    return load_config(store, store.env(), bus_stops, only, holidays=holidays)


def sleep_to_the_tick(seconds: float) -> None:
    """Sleep to the next whole multiple of `seconds`, so a running clock never skips."""
    if seconds > 0:
        time.sleep(seconds - time.time() % seconds)


def setup_url(hostname: str | None = None, port: int = WEB_PORT) -> str:
    """The settings site's address on the home network, by this Pi's name."""
    name = (socket.gethostname() if hostname is None else hostname).split(".")[0]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,63}", name):
        name = "raspberrypi"  # Raspberry Pi OS's own; a real name never gets here
    return f"http://{name.lower()}.local:{port}/settings"
