"""What the e-paper timer run keeps in memory while the state can't be saved.

Each run is a new process, a minute after the last, reading the state
afresh. On a full or read-only SD card its last redraw is never saved, so
the next run would take the panel to show something else and draw it
again, every minute. While the state can't be saved, the run keeps its last
redraw (what was drawn, when, and which page) in a small file in the unit's
runtime folder, /run/twobells (board/runfolder.py), and what each source
and the forecast last fetched and tried in another (KeptSources), with the
bank holidays' cache while the card won't take that either. Once a save
works the files go.

What is read back counts only if it is exactly the shape written; anything
else counts as nothing kept, and the board draws.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ..core.bounded import finite
from ..sources.holidays import is_held
from .runfolder import MOST_SOURCES, SOURCE, RunFile
from .state import NOT_UPDATING, departure_from_dict, is_mark, is_time

log = logging.getLogger(__name__)

NAME = "last-render.json"
MAX_BYTES = 256  # what is written is about 120
SOURCES_NAME = "unsaved-sources.json"
SOURCES_MAX_BYTES = 65536  # three sources of 6 departures are about 5000, a forecast of 48 hours about 3700
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


# What a run changes in a source's entry, each kept as the state saves it.
_ENTRY_KEYS = frozenset({"departures", "last_attempt", "last_success", "last_error", "messages", "note", NOT_UPDATING})
_MOST_DEPARTURES = 32  # a section shows 6 at most
_MOST_MESSAGES = 16
_MOST_CHARS = 2000  # a notice; an error or a note is shorter (core/errors.py)


def _text(value: object) -> bool:
    return isinstance(value, str) and len(value) <= _MOST_CHARS


def _departures(value: object) -> bool:
    if not isinstance(value, list) or len(value) > _MOST_DEPARTURES:
        return False
    try:
        for item in value:
            departure_from_dict(item)
    except (TypeError, ValueError, KeyError):
        return False
    return True


def _error(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"at", "message"} and is_time(value["at"]) and _text(
        value["message"])


def _messages(value: object) -> bool:
    return isinstance(value, list) and len(value) <= _MOST_MESSAGES and all(_text(message) for message in value)


_CHECKS = {
    "departures": _departures,
    "last_attempt": is_time,
    "last_success": is_time,
    "last_error": _error,
    "messages": _messages,
    "note": _text,
    NOT_UPDATING: is_mark,
}


def _entry(entry: object) -> bool:
    """Whether `entry` is what KeptSources keeps of a source's entry, and nothing more."""
    return (isinstance(entry, dict) and set(entry) <= _ENTRY_KEYS
            and all(_CHECKS[key](value) for key, value in entry.items()))


# The rain warning's forecast (board/app.py's _poll_weather), kept beside
# the sources' entries under its own name, which no source has: the state
# has it beside "sources", not in them.
WEATHER = "weather"
_WEATHER_KEYS = frozenset({"hours", "last_attempt", "last_success"})
_HOUR_KEYS = frozenset({"start", "probability", "precipitation"})
_MOST_HOURS = 72  # sources/weather.py's parse_forecast reads no more


def _hour(item: object) -> bool:
    """Whether `item` is an hour of the forecast as sources/weather.py's hours_to_dicts writes it."""
    if not isinstance(item, dict) or set(item) != _HOUR_KEYS or not is_time(item["start"]):
        return False
    chance, rainfall = item["probability"], item["precipitation"]
    if chance is not None and (type(chance) is not int or not 0 <= chance <= 100):
        return False
    if rainfall is None:
        return True
    number = finite(rainfall)
    return number is not None and number >= 0


def _hours(value: object) -> bool:
    return isinstance(value, list) and len(value) <= _MOST_HOURS and all(_hour(item) for item in value)


_WEATHER_CHECKS = {"hours": _hours, "last_attempt": is_time, "last_success": is_time}


def _weather(entry: object) -> bool:
    """Whether `entry` is what KeptSources keeps of the forecast, and nothing more."""
    return (isinstance(entry, dict) and set(entry) <= _WEATHER_KEYS
            and all(_WEATHER_CHECKS[key](value) for key, value in entry.items()))


# The bank holidays' cache (sources/holidays.py's Unsaved), kept beside the
# sources' entries under a name no source has, and checked as that module
# reads its own cache, with nothing more. The real list as cached is about
# 11 kB; one too big to keep with the rest is left out (KeptSources.keep).
HOLIDAYS = "holidays"
_NOT_SOURCES = frozenset({WEATHER, HOLIDAYS})


def _sources(kept: object) -> bool:
    """Whether `kept` is sources' entries as KeptSources keeps them, by
    name, and the forecast's and the bank holidays', if there are those. A
    file from before they were kept has neither."""
    if not isinstance(kept, dict) or (WEATHER in kept and not _weather(kept[WEATHER])):
        return False
    if HOLIDAYS in kept and not is_held(kept[HOLIDAYS]):
        return False
    sources = {name: entry for name, entry in kept.items() if name not in _NOT_SOURCES}
    if len(sources) > MOST_SOURCES:
        return False
    return all(isinstance(name, str) and SOURCE.fullmatch(name) and _entry(entry) for name, entry in sources.items())


class KeptSources:
    """What the board's sources, of `names`, and its forecast last fetched
    and tried, kept in `folder` while the state can't be saved: each
    source's departures, notices, last attempt and success, last error and
    note, and "not updating" mark, and the forecast's hours, last attempt
    and success, as the state has them. A file of its own, so a release
    that knows nothing of it still reads the last redraw.

    On a card that can't be written, each run would read what the card had
    before it failed: every source and the forecast due again at once (the
    timer asking every minute, not at its interval), what they fetched
    gone, and a mark that showed as a window closed gone after the close.
    Kept here, each entry is put back in place of the card's, unless the
    card's has been tried since (by a release that knows nothing of this
    file, say), so is newer. A mark is still honoured only while the
    source's last success is the one it was made against (board/app.py's
    _is_stale).

    Beside them it keeps the bank holidays' cache while the card won't take
    it (sources/holidays.py's Unsaved), for the run to hand back to
    bank_holidays, which says which of it and the card's is newer."""

    def __init__(self, folder: Path, names: Iterable[str]) -> None:
        self._file = RunFile(folder, SOURCES_NAME, SOURCES_MAX_BYTES, _sources, "what each source last fetched")
        self._names = frozenset(names)

    def carry(self, state: dict) -> None:
        """Puts the entries kept into the state's sources and forecast, in place of the card's."""
        kept = self._file.read()
        if not isinstance(kept, dict):
            return
        sources = state.setdefault("sources", {})
        for name, entry in kept.items():
            if name in self._names:
                _put(sources.setdefault(name, {}), entry, _ENTRY_KEYS)
        if WEATHER in kept:
            if not isinstance(state.get(WEATHER), dict):
                state[WEATHER] = {}
            _put(state[WEATHER], kept[WEATHER], _WEATHER_KEYS)

    def holidays(self) -> dict | None:
        """The bank holidays' cache kept (sources/holidays.py's Unsaved), if there is one."""
        kept = self._file.read()
        return kept.get(HOLIDAYS) if isinstance(kept, dict) else None

    def keep(self, state: dict, holidays: dict | None = None) -> bool:
        """Keeps the entries the state's sources and forecast have, and the
        bank holidays' cache, if given, in place of any kept before, saying
        whether it could. One that isn't of the shape is left out, and the
        next run reads the card's for it; so is the holidays' cache if the
        file would be too big with it, as the sources matter more."""
        kept = {}
        for name, entry in state.get("sources", {}).items():
            if name not in self._names or not isinstance(entry, dict):
                continue
            part = {key: entry[key] for key in _ENTRY_KEYS if key in entry}
            if part and _entry(part):
                kept[name] = part
        weather = state.get(WEATHER)
        if isinstance(weather, dict):
            part = {key: weather[key] for key in _WEATHER_KEYS if key in weather}
            if part and _weather(part):
                kept[WEATHER] = part
        if holidays is not None and is_held(holidays):
            if self._file.keep({**kept, HOLIDAYS: holidays}):
                return True
            log.warning("Keeping what each source fetched in memory without the bank holidays: %s",
                        self._file.problem)
        if not kept:
            self._file.forget()
            return True
        return self._file.keep(kept)

    def forget(self) -> None:
        self._file.forget()


def _put(card: dict, entry: dict, keys: frozenset[str]) -> None:
    """Puts what `entry` kept of `keys` in place of the card's, unless the card's was tried since (_newer)."""
    if _newer(card.get("last_attempt"), entry.get("last_attempt")):
        return
    for key in keys:
        if key in entry:
            card[key] = entry[key]
        else:
            card.pop(key, None)


def _newer(card: object, kept: object) -> bool:
    """Whether the card's last attempt is after the one kept, or the card has one and nothing kept has."""
    if not is_time(card):
        return False
    return not is_time(kept) or datetime.fromisoformat(str(card)) > datetime.fromisoformat(str(kept))
