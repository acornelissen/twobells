"""The e-paper timer run's last redraw and "not updating" marks, kept in memory while the state can't be saved.

Each run is a new process, a minute after the last, reading the state
afresh. On a full or read-only SD card its last redraw is never saved, so
the next run would take the panel to show something else and draw it
again, every minute. While the state can't be saved, the run keeps its last
redraw (what was drawn, when, and which page) in a small file in the unit's
runtime folder, /run/twobells (board/runfolder.py), and its marks
(KeptMarks) in another. Once a save works the files go.

What is read back counts only if it is exactly the shape written; anything
else counts as nothing kept, and the board draws.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .runfolder import MOST_SOURCES, SOURCE, RunFile
from .state import NOT_UPDATING, is_mark

NAME = "last-render.json"
MAX_BYTES = 256  # what is written is about 120
MARKS_NAME = "not-updating.json"
MARKS_MAX_BYTES = 512  # three marks are about 150
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


def _marks(kept: object) -> bool:
    """Whether `kept` is "not updating" marks as KeptMarks keeps them: each
    as the state saves it in a source's entry, by the source's name."""
    if not isinstance(kept, dict) or len(kept) > MOST_SOURCES:
        return False
    return all(isinstance(name, str) and SOURCE.fullmatch(name) and is_mark(mark) for name, mark in kept.items())


class KeptMarks:
    """The "not updating" marks of the board's sources, of `names`, kept in
    `folder` while the state can't be saved, in a file of their own, so a
    release that knows nothing of them still reads the last redraw.

    A mark that shows as a source's window closes is saved in the state, so
    that it stays after the close until a fetch succeeds (board/app.py's
    _is_stale). On a card that can't be written it never reaches the state,
    and the next run, outside the window, would show the source unmarked.
    Kept here it is put back in place of the card's; a source with no mark
    kept keeps the card's, as what is shown of it is still the card's. A
    mark is honoured only while the source's last success is the one it was
    made against, so one kept for a card that has moved on since shows
    nothing."""

    def __init__(self, folder: Path, names: Iterable[str]) -> None:
        self._file = RunFile(folder, MARKS_NAME, MARKS_MAX_BYTES, _marks, "the not updating marks")
        self._names = frozenset(names)

    def carry(self, state: dict) -> None:
        """Puts the marks kept into the state's sources, in place of the card's."""
        kept = self._file.read()
        if not isinstance(kept, dict):
            return
        sources = state.setdefault("sources", {})
        for name, mark in kept.items():
            if name in self._names:
                sources.setdefault(name, {})[NOT_UPDATING] = mark

    def keep(self, state: dict) -> None:
        """Keeps the marks the state's sources have, in place of any kept before."""
        marks = {
            name: entry[NOT_UPDATING] for name, entry in state.get("sources", {}).items()
            if name in self._names and isinstance(entry, dict) and is_mark(entry.get(NOT_UPDATING))
        }
        if marks:
            self._file.keep(marks)
        else:
            self._file.forget()

    def forget(self) -> None:
        self._file.forget()
