"""The e-paper timer run's last redraw, kept in memory while the state can't be saved.

Each run is a new process, a minute after the last, reading the state
afresh. On a full or read-only SD card its last redraw is never saved, so
the next run would take the panel to show something else and draw it
again, every minute. While the state can't be saved, the run keeps its last
redraw (what was drawn, when, and which page) in a small file in the unit's
runtime folder, /run/twobells (board/runfolder.py). Once a save works the
file goes.

What is read back counts only if it is exactly the shape written; anything
else counts as nothing kept, and the board draws.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .runfolder import RunFile

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


class KeptRender(RunFile):
    """The last redraw, kept in `folder` while the state can't be saved."""

    def __init__(self, folder: Path) -> None:
        super().__init__(folder, NAME, MAX_BYTES, _valid, "the last redraw")
