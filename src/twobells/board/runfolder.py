"""Small files the e-paper timer's runs keep in memory, from one run to the next.

Each run is a new process, a minute after the last, reading the state
afresh. What one leaves for the next that mustn't be written to the SD card,
or must not outlive a restart of the Pi, goes in the unit's
RuntimeDirectory, /run/twobells: in memory, made by systemd for the service
user alone, kept between runs and empty again once the Pi restarts. Off the
Pi there is no such folder ($RUNTIME_DIRECTORY isn't set), and nothing is
kept. A running screen keeps what its sources fetched in a folder of its
own, /run/twobells-screen, from one start to the next while systemd starts
it again (cli/run.py's _KeptStore).

Each file has one fixed shape, which its reader checks: the last redraw
and what each source last fetched while the state can't be saved
(board/unsaved.py), and what was fetched since the Pi started
(board/sinceboot.py). What is read back is untrusted: only a plain file of
this user's, with one link and never through one, within its size, of
exactly its shape, counts. Anything else counts as nothing kept. Sources
are named as SOURCE has it, at most MOST_SOURCES of them, and only the
board's own count; a name is never part of a path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from ..core import files

log = logging.getLogger(__name__)

SOURCE = re.compile(r"[a-z]{1,16}")  # a source's name, like train
MOST_SOURCES = 8


def here(environ: Mapping[str, str] | None = None) -> Path | None:
    """systemd's $RUNTIME_DIRECTORY, if that is one absolute folder of this
    user's that no one else can open; else None."""
    given = (os.environ if environ is None else environ).get("RUNTIME_DIRECTORY", "")
    if not given:
        return None
    if not os.path.isabs(given) or ":" in given:
        log.warning("Keeping nothing in memory between runs: RUNTIME_DIRECTORY isn't one absolute folder")
        return None
    try:
        info = os.lstat(given)
    except OSError as error:
        log.warning("Keeping nothing in memory between runs: %s", error.strerror or error)
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        log.warning("Keeping nothing in memory between runs: %s isn't a folder of this user's alone", given)
        return None
    return Path(given)


class RunFile:
    """One file in the runtime folder: `name`, at most `most_bytes`, holding
    JSON that `valid` passes. `what` names it in warnings; `quiet` leaves
    them to the caller, with what went wrong in `problem`."""

    def __init__(self, folder: Path, name: str, most_bytes: int, valid: Callable[[object], bool], what: str,
                 quiet: bool = False) -> None:
        self._path = folder / name
        self._most_bytes = most_bytes
        self._valid = valid
        self._what = what
        self._quiet = quiet
        self.problem: str | None = None
        # What this one last wrote, so a running screen, keeping it as it
        # goes, doesn't write the same again (board/unsaved.py's KeptSources).
        self._written: str | None = None

    def _warn(self, message: str, *args: object) -> None:
        self.problem = message % args
        if not self._quiet:
            log.warning(message, *args)

    def read(self) -> object | None:
        """What is kept, or None if nothing is, or what is there isn't of its shape."""
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK | files.NOFOLLOW | files.CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as error:  # a link, or something else that won't open as a file
            self._warn("Ignoring %s kept in memory: %s", self._what, error.strerror or error)
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_size > self._most_bytes):
                body = None
            else:
                body = os.read(fd, self._most_bytes + 1)
        except OSError:
            body = None
        finally:
            os.close(fd)
        value = None
        if body is not None and len(body) <= self._most_bytes:
            try:
                value = json.loads(body.decode("utf-8"))
            except (ValueError, RecursionError):  # bad JSON or bad UTF-8, or nested too deep to read
                value = None
        if not self._valid(value):
            self._warn("Ignoring %s kept in memory: it isn't of the shape kept", self._what)
            return None
        return value

    def keep(self, value: object) -> bool:
        """Keep `value` in place of anything kept before, saying whether it
        is. One that isn't of the shape, or is too big, is never written,
        and what was kept goes."""
        text = json.dumps(value, sort_keys=True) if self._valid(value) else None
        if text is None or len(text.encode("utf-8")) > self._most_bytes:
            self.problem = "it isn't of the shape kept" if text is None else "it is too big to keep"
            self.forget()
            return False
        if text == self._written:
            return True
        try:
            files.write_atomic(self._path, text, 0o600)
        except OSError as error:
            self._written = None
            self._warn("Can't keep %s in memory: %s", self._what, error.strerror or error)
            return False
        self._written = text
        return True

    def forget(self) -> None:
        self._written = None
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            self._warn("Can't remove %s kept in memory: %s", self._what, error.strerror or error)
