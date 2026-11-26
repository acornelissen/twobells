"""What the e-paper timer has fetched since the Pi started.

A Pi with no clock battery can start with its clock near when it last
saved, hours behind, and would take what it saved for new. A running screen
counts from its own start, in memory (board/app.py's run_loop); the timer
runs afresh each minute, so it keeps the count in its runtime folder,
/run/twobells (board/runfolder.py), which is empty after the Pi restarts. A
source not named there hasn't been fetched since, and what it saved is
marked "not updating" until it is, as a started screen's is.

A source is named only once a fetch of it since the start is saved in the
state: the next run reads the state afresh, and one whose save failed
still has what came before. The record is {"fetched": [names]}; only the
board's sources' names count, and a record of any other shape counts as
nothing fetched.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .runfolder import MOST_SOURCES, SOURCE, RunFile

NAME = "since-boot.json"
MAX_BYTES = 256  # what is written is about 50
_KEYS = frozenset({"fetched"})


def _names(value: object) -> bool:
    """Whether `value` is a list of sources' names, each once."""
    if not isinstance(value, list) or len(value) > MOST_SOURCES:
        return False
    if not all(isinstance(name, str) and SOURCE.fullmatch(name) for name in value):
        return False
    return len(set(value)) == len(value)


def _valid(record: object) -> bool:
    """Whether `record` is one as written, and nothing more."""
    return isinstance(record, dict) and set(record) == _KEYS and _names(record["fetched"])


class SinceBoot:
    """The board's sources, of `names`, fetched since the Pi started, kept in `folder`."""

    def __init__(self, folder: Path, names: Iterable[str]) -> None:
        self._file = RunFile(folder, NAME, MAX_BYTES, _valid, "what was fetched since the Pi started")
        self._names = frozenset(names)

    def fetched(self) -> set[str]:
        """Those fetched since the start; none if the record is missing or isn't one."""
        record = self._file.read()
        if not isinstance(record, dict):
            return set()
        return {name for name in record["fetched"] if name in self._names}

    def keep(self, fetched: Iterable[str]) -> None:
        self._file.keep({"fetched": sorted(set(fetched) & self._names)})
