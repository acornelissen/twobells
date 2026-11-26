"""What the e-paper timer has done since the Pi started.

A Pi with no clock battery can start with its clock near when it last
saved, hours behind, and would take what it saved for new. A running screen
counts from its own start, in memory (board/app.py's run_loop); the timer
runs afresh each minute, so it keeps the count in its runtime folder,
/run/twobells (board/runfolder.py), which is empty after the Pi restarts. A
source not counted there as fetched hasn't been fetched since, and what it
saved is marked "not updating" until it is, as a started screen's is.

The record is {"fetched": {name: last_success}, "tried": [names]}, kept as
board/app.py's Started: the sources fetched since the start, each with the
last success it had at the end of the run, and those given their one fetch
for the start outside their windows. A source counts as fetched only while
the state the run reads (the card's, or what was kept in memory while it
couldn't be saved, board/unsaved.py) has that same last success: one whose
fetch reached neither has what came before, which isn't to be trusted.
Only the board's sources' names count, and a record of any other shape
counts as nothing fetched or tried.

A record only means something if it can be kept: one that can't (a folder
in its place, /run full) would read as nothing fetched every minute, and
the board would mark and fetch over and over. Each run first writes back
what it read (cli/run.py's _single); if that fails, the run goes without
a record, trusting what was saved, as off the Pi.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .app import Started
from .runfolder import MOST_SOURCES, SOURCE, RunFile
from .state import is_time

NAME = "since-boot.json"
MAX_BYTES = 1024  # what is written is about 150; the most there can be, about 600
_KEYS = frozenset({"fetched", "tried"})


def _names(value: object) -> bool:
    """Whether `value` is a list of sources' names, each once."""
    if not isinstance(value, list) or len(value) > MOST_SOURCES:
        return False
    if not all(isinstance(name, str) and SOURCE.fullmatch(name) for name in value):
        return False
    return len(set(value)) == len(value)


def _successes(value: object) -> bool:
    """Whether `value` is sources' last successes, by name."""
    return (isinstance(value, dict) and len(value) <= MOST_SOURCES
            and all(isinstance(name, str) and SOURCE.fullmatch(name) and is_time(when) for name, when in value.items()))


def _valid(record: object) -> bool:
    """Whether `record` is one as written, and nothing more."""
    return (isinstance(record, dict) and set(record) == _KEYS and _successes(record["fetched"])
            and _names(record["tried"]))


def _last_success(state: dict, name: str) -> object:
    entry = state.get("sources", {}).get(name)
    return entry.get("last_success") if isinstance(entry, dict) else None


class SinceBoot:
    """What the timer has done for the board's sources, of `names`, since the
    Pi started, kept in `folder`."""

    def __init__(self, folder: Path, names: Iterable[str]) -> None:
        self._file = RunFile(folder, NAME, MAX_BYTES, _valid, "what was fetched since the Pi started", quiet=True)
        self._names = frozenset(names)
        self._kept: dict | None = None

    def read(self, state: dict) -> Started:
        """What was done since the start, against `state` as this run reads
        it; nothing if the record is missing or isn't one."""
        record = self._file.read()
        if not isinstance(record, dict):
            return Started()
        fetched = {name for name, when in record["fetched"].items()
                   if name in self._names and _last_success(state, name) == when}
        return Started(fetched, {name for name in record["tried"] if name in self._names})

    def keep(self, started: Started, state: dict) -> bool:
        """Keeps `started`, with each fetched source's last success in
        `state`, saying whether it could; why not, if not, in `problem`."""
        fetched = {name: _last_success(state, name) for name in started.fetched & self._names}
        record = {"fetched": {name: when for name, when in fetched.items() if is_time(when)},
                  "tried": sorted(started.tried & self._names)}
        if record == self._kept:
            return True  # written already this run
        if not self._file.keep(record):
            return False
        self._kept = record
        return True

    @property
    def problem(self) -> str | None:
        return self._file.problem
