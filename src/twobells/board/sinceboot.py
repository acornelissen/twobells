"""What the e-paper timer has done since the Pi started.

A Pi with no clock battery can start with its clock near when it last
saved, hours behind, and would take what it saved for new. A running screen
counts from its own start, in memory (board/app.py's run_loop); the timer
runs afresh each minute, so it keeps the count in its runtime folder,
/run/twobells (board/runfolder.py), which is empty after the Pi restarts. A
source not named there as fetched hasn't been fetched since, and what it
saved is marked "not updating" until it is, as a started screen's is.

The record is {"fetched": [names], "tried": [names]}, board/app.py's
Started: the sources fetched since the start, and those given their one
fetch for it outside their windows. A source is named as fetched only once
a fetch of it is saved in the state: the next run reads the state afresh,
and one whose save failed still has what came before. Only the board's
sources' names count, and a record of any other shape counts as nothing
fetched or tried.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .app import Started
from .runfolder import MOST_SOURCES, SOURCE, RunFile

NAME = "since-boot.json"
MAX_BYTES = 256  # what is written is about 80
_KEYS = frozenset({"fetched", "tried"})


def _names(value: object) -> bool:
    """Whether `value` is a list of sources' names, each once."""
    if not isinstance(value, list) or len(value) > MOST_SOURCES:
        return False
    if not all(isinstance(name, str) and SOURCE.fullmatch(name) for name in value):
        return False
    return len(set(value)) == len(value)


def _valid(record: object) -> bool:
    """Whether `record` is one as written, and nothing more."""
    return isinstance(record, dict) and set(record) == _KEYS and all(_names(record[key]) for key in _KEYS)


class SinceBoot:
    """What the timer has done for the board's sources, of `names`, since the
    Pi started, kept in `folder`."""

    def __init__(self, folder: Path, names: Iterable[str]) -> None:
        self._file = RunFile(folder, NAME, MAX_BYTES, _valid, "what was fetched since the Pi started")
        self._names = frozenset(names)

    def read(self) -> Started:
        """What was done since the start; nothing if the record is missing or isn't one."""
        record = self._file.read()
        if not isinstance(record, dict):
            return Started()
        return Started({name for name in record["fetched"] if name in self._names},
                       {name for name in record["tried"] if name in self._names})

    def keep(self, started: Started) -> None:
        self._file.keep({"fetched": sorted(started.fetched & self._names),
                         "tried": sorted(started.tried & self._names)})
