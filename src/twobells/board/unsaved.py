"""The e-paper timer run's last redraw, kept in memory while the state can't be saved.

Each run is a new process, a minute after the last, reading the state
afresh. On a full or read-only SD card its last redraw is never saved, so
the next run would take the panel to show something else and draw it
again, every minute. While the state can't be saved, the run keeps its last
redraw (what was drawn, when, and which page) in a small file in the unit's
RuntimeDirectory, /run/twobells: in memory, made by systemd for the service
user alone, kept between runs until the Pi restarts. Once a save works the
file goes. Off the Pi there is no such folder ($RUNTIME_DIRECTORY isn't
set), and nothing is kept.

What is read back is untrusted: only a plain file of this user's, with one
link and never through one, within MAX_BYTES, of exactly the shape written,
counts. Anything else counts as nothing kept, and the board draws.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..core import files

log = logging.getLogger(__name__)

NAME = "last-render.json"
MAX_BYTES = 256  # what is written is about 120
_KEYS = frozenset({"signature", "at", "page"})
_PAGES = frozenset({"departures", "route", "clock", "code", "setup"})
_SIGNATURE = re.compile(r"[0-9a-f]{64}")  # board/app.py's _render_key, a SHA-256
_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")  # a time by the board's clock, to the second


def _valid(drawn: object) -> bool:
    """Whether `drawn` is a last redraw as board/app.py saves it, and nothing more."""
    if not isinstance(drawn, dict) or set(drawn) != _KEYS:
        return False
    signature, at, page = drawn["signature"], drawn["at"], drawn["page"]
    if not (isinstance(signature, str) and isinstance(at, str) and isinstance(page, str)):
        return False
    if not _SIGNATURE.fullmatch(signature) or not _AT.fullmatch(at) or page not in _PAGES:
        return False
    try:
        datetime.fromisoformat(at)
    except ValueError:
        return False
    return True


class KeptRender:
    """The last redraw, kept in `folder` while the state can't be saved."""

    def __init__(self, folder: Path) -> None:
        self._path = folder / NAME

    @classmethod
    def here(cls, environ: Mapping[str, str] | None = None) -> KeptRender | None:
        """The one in systemd's $RUNTIME_DIRECTORY, if that is one absolute
        folder of this user's that no one else can open; else None."""
        given = (os.environ if environ is None else environ).get("RUNTIME_DIRECTORY", "")
        if not given:
            return None
        if not os.path.isabs(given) or ":" in given:
            log.warning("Not keeping the last redraw in memory: RUNTIME_DIRECTORY isn't one absolute folder")
            return None
        try:
            info = os.lstat(given)
        except OSError as error:
            log.warning("Not keeping the last redraw in memory: %s", error.strerror or error)
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            log.warning("Not keeping the last redraw in memory: %s isn't a folder of this user's alone", given)
            return None
        return cls(Path(given))

    def read(self) -> dict | None:
        """The last redraw kept, or None if none is, or what is there isn't one."""
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK | files.NOFOLLOW | files.CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as error:  # a link, or something else that won't open as a file
            log.warning("Ignoring the last redraw kept in memory: %s", error.strerror or error)
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or info.st_size > MAX_BYTES):
                body = None
            else:
                body = os.read(fd, MAX_BYTES + 1)
        except OSError:
            body = None
        finally:
            os.close(fd)
        drawn = None
        if body is not None and len(body) <= MAX_BYTES:
            try:
                drawn = json.loads(body.decode("utf-8"))
            except ValueError:  # bad JSON or bad UTF-8
                drawn = None
        if not _valid(drawn):
            log.warning("Ignoring the last redraw kept in memory: it isn't one")
            return None
        return drawn

    def keep(self, drawn: object) -> None:
        """Keep `drawn`, the state's last redraw, in place of any kept before.
        One that isn't a last redraw is never written."""
        if not _valid(drawn):
            self.forget()
            return
        try:
            files.write_atomic(self._path, json.dumps(drawn, sort_keys=True), 0o600)
        except OSError as error:
            log.warning("Can't keep the last redraw in memory either: %s", error.strerror or error)

    def forget(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            log.warning("Can't remove the last redraw kept in memory: %s", error.strerror or error)
