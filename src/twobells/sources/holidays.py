"""Bank holidays from gov.uk, cached beside the state file.

Trains and buses run a Sunday service on bank holidays. gov.uk lists them for
three divisions: England and Wales, Scotland, and Northern Ireland. The board
uses the one its settings name, England and Wales unless told otherwise. One
download has all three, and all three are cached.

The list is fetched at most once a week (the board runs every minute, so the
cache's age is checked first), and after a failure not again for a few hours.
Whatever goes wrong, the last good list is kept; with none at all, there are
no holidays. Caches from before divisions were kept hold England and Wales
only; another division is fetched the first time it is asked for. A cache
the card won't take can be handed to the caller to keep (Unsaved), so a
full or read-only card doesn't have gov.uk asked at every run.

Each England and Wales holiday's title is kept too, so bus timetables can name
it the way TransXChange does ("SpringBank", "BoxingDayHoliday"). The bus
timetables come from BODS, which covers England only, so they always use that
division. Caches from before titles were kept still work, and gain titles at
their next weekly refresh.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from ..core import files
from ..core.errors import safe_message
from ..core.model import sane_time
from ..core.net import get_bytes
from ..settings.schema import DIVISIONS, ENGLAND_AND_WALES

log = logging.getLogger(__name__)

URL = "https://www.gov.uk/bank-holidays.json"
CACHE_NAME = "bank-holidays.json"
# The real list is about 20 kB; anything far bigger isn't it.
MAX_BYTES = 1_000_000
REFRESH = timedelta(days=7)
RETRY = timedelta(hours=6)
# A clock this far behind the cache's dates was wrong when it fetched (a Pi
# has no clock battery), so the cache's age can't be trusted: fetch again.
_SKEW = timedelta(days=1)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Fetch = Callable[[str, int], bytes]


def _date(value) -> date | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _dates(values) -> frozenset[date]:
    return frozenset(day for day in map(_date, values) if day is not None)


class Event(NamedTuple):
    title: str  # as gov.uk gives it, e.g. "Boxing Day"
    notes: str  # e.g. "Substitute day"


def _text(value) -> str:
    return value[:200] if isinstance(value, str) else ""


def _events(items) -> dict[date, Event]:
    """Each usable entry of a list of gov.uk events, by date. A date is enough;
    a missing or odd title is left blank."""
    events = {}
    for item in items:
        day = _date(item.get("date")) if isinstance(item, dict) else None
        if day is not None:
            events[day] = Event(_text(item.get("title")), _text(item.get("notes")))
    return events


def _parse_divisions(body: bytes) -> dict[str, dict[date, Event]]:
    """Each division in gov.uk's JSON with usable dates, skipping bad entries.
    ValueError if it isn't JSON."""
    data = json.loads(body)
    found = {}
    for division in DIVISIONS:
        section = data.get(division) if isinstance(data, dict) else None
        events = section.get("events") if isinstance(section, dict) else None
        usable = _events(events) if isinstance(events, list) else {}
        if usable:
            found[division] = usable
    return found


def _check(division: str) -> None:
    if division not in DIVISIONS:
        raise ValueError(f"Unknown bank holiday division {division!r}; choose from {', '.join(DIVISIONS)}")


def parse_events(body: bytes, division: str = ENGLAND_AND_WALES) -> dict[date, Event]:
    """One division's bank holidays from gov.uk's JSON, with their titles,
    skipping bad entries. ValueError if it isn't that list or has no usable dates."""
    _check(division)
    found = _parse_divisions(body).get(division)
    if not found:
        raise ValueError(f"No {division} bank holidays in gov.uk's list")
    return found


def parse(body: bytes, division: str = ENGLAND_AND_WALES) -> frozenset[date]:
    """One division's bank holidays from gov.uk's JSON, skipping bad entries.
    ValueError if it isn't that list or has no usable dates."""
    return frozenset(parse_events(body, division))


def _moment(value) -> datetime | None:
    """A time as _save writes one: local, with no offset, and one that could
    be real (sane_time). Anything else reads as none: an offset can't be
    compared with the local time the board passes in."""
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is None and sane_time(moment) else None


class _Cache(NamedTuple):
    fetched: datetime | None
    checked: datetime | None
    # Dates by division. Caches from before divisions were kept have
    # England and Wales only, under "dates".
    divisions: dict[str, frozenset[date]]
    events: dict[date, Event]  # England and Wales; empty in caches from before titles were kept


_EMPTY = _Cache(None, None, {}, {})


def _divisions(saved, dates) -> dict[str, frozenset[date]]:
    found = {ENGLAND_AND_WALES: _dates(dates)}
    if isinstance(saved, dict):
        for division in DIVISIONS:
            if isinstance(saved.get(division), list):
                found[division] = _dates(saved[division])
    return found


def _load(path: Path) -> _Cache:
    """What the cache holds; nothing if there's no usable cache."""
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _EMPTY
    except (OSError, ValueError) as error:
        log.warning("Ignoring unreadable bank holiday cache %s: %s", path, error)
        return _EMPTY
    found = _read(cache)
    if found is None:
        log.warning("Ignoring malformed bank holiday cache %s", path)
        return _EMPTY
    return found


def _read(cache: object) -> _Cache | None:
    """What a cache's record holds, skipping bad entries; None if it isn't one."""
    if not isinstance(cache, dict) or not isinstance(cache.get("dates"), list):
        return None
    events = cache.get("events")
    return _Cache(
        _moment(cache.get("fetched")),
        _moment(cache.get("checked")),
        _divisions(cache.get("divisions"), cache["dates"]),
        _events(events) if isinstance(events, list) else {},
    )


def _days(dates: frozenset[date]) -> list[str]:
    return sorted(day.isoformat() for day in dates)


def _record(cache: _Cache) -> dict:
    """The cache as it is saved."""
    # England and Wales always has a list, as _divisions reads one, so a record reads back as written (held).
    divisions = {ENGLAND_AND_WALES: frozenset(), **cache.divisions}
    return {
        "fetched": cache.fetched.isoformat() if cache.fetched else None,
        "checked": cache.checked.isoformat() if cache.checked else None,
        # England and Wales stays under "dates", where older versions look for it.
        "dates": _days(divisions[ENGLAND_AND_WALES]),
        "divisions": {division: _days(dates) for division, dates in sorted(divisions.items())},
        "events": [
            {"date": day.isoformat(), "title": event.title, "notes": event.notes}
            for day, event in sorted(cache.events.items())
        ],
    }


def _save(path: Path, record: dict) -> bool:
    """Saves the cache's record, saying whether it could."""
    try:
        files.write_atomic(path, json.dumps(record, indent=2), 0o640)
    except OSError as error:
        log.warning("Can't save the bank holiday cache %s: %s", path, error)
        return False
    return True


def _held(record: object) -> _Cache | None:
    """What a record held in Unsaved holds, if it is exactly one _record
    writes, with a last check; else None. Read back from elsewhere (the
    timer's runtime folder), it is not trusted: one an entry of which the
    cache's own reading (_read) would skip, or with anything more, counts
    as nothing held."""
    cache = _read(record)
    if cache is None or cache.checked is None or _record(cache) != record:
        return None
    return cache


def is_held(record: object) -> bool:
    """Whether `record` is one Unsaved can hold (_held)."""
    return _held(record) is not None


class Unsaved:
    """The cache as bank_holidays last tried to save it, while the card
    can't take it, for a caller to keep elsewhere between calls (the
    e-paper timer, a new process each minute, keeps it in memory:
    board/unsaved.py) and hand back. Without it, a card that can't be
    written loses every check, and the next call asks gov.uk again, a
    failure's wait and all. Handed back, the newer of it and the card's is
    used, by when each was last checked (_carried). `record` is None while
    the card has the cache, and is only ever one is_held passes."""

    def __init__(self, record: object = None) -> None:
        self.record: dict | None = record if isinstance(record, dict) and is_held(record) else None


def _carried(path: Path, card: _Cache, unsaved: Unsaved) -> _Cache:
    """The card's cache, or the one `unsaved` holds if it was checked since:
    that one is saved to the card again, and let go of once the card has it."""
    held = _held(unsaved.record)
    if held is None or held.checked is None or (card.checked is not None and card.checked >= held.checked):
        unsaved.record = None
        return card
    if unsaved.record is not None and _save(path, unsaved.record):
        unsaved.record = None
    return held


def _keep(path: Path, cache: _Cache, unsaved: Unsaved | None) -> None:
    """Saves `cache` to the card; one the card won't take goes to `unsaved`, if given."""
    record = _record(cache)
    saved = _save(path, record)
    if unsaved is not None:
        unsaved.record = None if saved else record


def _older(moment: datetime | None, now: datetime, age: timedelta) -> bool:
    return moment is None or now - moment >= age or moment - now > _SKEW


def read_cache(folder: Path | str, division: str = ENGLAND_AND_WALES) -> frozenset[date]:
    """One division's cached bank holidays, without fetching or writing anything."""
    _check(division)
    return _load(Path(folder) / CACHE_NAME).divisions.get(division, frozenset())


def read_events(folder: Path | str) -> dict[date, Event]:
    """The cached England and Wales bank holidays with their titles, for bus
    timetables, without fetching or writing anything."""
    return _load(Path(folder) / CACHE_NAME).events


def bank_holidays(folder: Path | str, now: datetime, fetch: Fetch | None = None,
                  division: str = ENGLAND_AND_WALES, unsaved: Unsaved | None = None) -> frozenset[date]:
    """One division's bank holidays, refreshed from gov.uk if the cache in
    `folder` is a week old or lacks that division. `now` is local time, as
    everywhere on the board. With `unsaved`, the cache is also handed back
    and given out there while the card can't be written (Unsaved)."""
    _check(division)
    path = Path(folder) / CACHE_NAME
    cache = _load(path) if unsaved is None else _carried(path, _load(path), unsaved)
    cached = cache.divisions.get(division)
    if cached is not None and not _older(cache.fetched, now, REFRESH):
        return cached
    # After a failed check (checked moves on, fetched doesn't), wait before the next.
    if cache.checked != cache.fetched and not _older(cache.checked, now, RETRY):
        return cached or frozenset()
    try:
        found = _parse_divisions((fetch or get_bytes)(URL, MAX_BYTES))
        if division not in found:
            raise ValueError(f"No {division} bank holidays in gov.uk's list")
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good list
        kept = cached or frozenset()
        log.warning("Bank holidays not refreshed, keeping %d cached: %s", len(kept), safe_message(error))
        _keep(path, _Cache(cache.fetched, now, cache.divisions, cache.events), unsaved)
        return kept
    # A division missing from this download keeps its last good list.
    divisions = {**cache.divisions, **{name: frozenset(events) for name, events in found.items()}}
    _keep(path, _Cache(now, now, divisions, found.get(ENGLAND_AND_WALES, cache.events)), unsaved)
    return divisions[division]


# TransXChange's names for England and Wales bank holidays. A substitute day
# (gov.uk notes it as one) has its own name, such as "BoxingDayHoliday".
# Titles are matched by their start, so "Early May bank holiday (VE day)" is
# still MayDay. One-off holidays have no name: timetables list those by date.
_TITLES = (
    # (gov.uk title, name, name on a substitute day)
    ("new year's day", "NewYearsDay", "NewYearsDayHoliday"),
    ("good friday", "GoodFriday", "GoodFriday"),
    ("easter monday", "EasterMonday", "EasterMonday"),
    ("early may bank holiday", "MayDay", "MayDay"),
    ("spring bank holiday", "SpringBank", "SpringBank"),
    ("summer bank holiday", "LateSummerBankHolidayNotScotland", "LateSummerBankHolidayNotScotland"),
    ("christmas day", "ChristmasDay", "ChristmasDayHoliday"),
    ("boxing day", "BoxingDay", "BoxingDayHoliday"),
)
# Named by date whether or not they are bank holidays that year: Boxing Day
# is BoxingDay even on a Saturday, when the Monday is BoxingDayHoliday.
_FIXED = {
    (12, 24): "ChristmasEve",
    (12, 25): "ChristmasDay",
    (12, 26): "BoxingDay",
    (12, 31): "NewYearsEve",
    (1, 1): "NewYearsDay",
}


def holiday_names(day: date, events: Mapping[date, Event]) -> frozenset[str]:
    """The TransXChange holiday names for `day` ("GoodFriday", "ChristmasEve", ...),
    from gov.uk's titles (see read_events) and the fixed dates around Christmas."""
    names = set()
    if (day.month, day.day) in _FIXED:
        names.add(_FIXED[(day.month, day.day)])
    event = events.get(day)
    if event is not None:
        title = event.title.replace("’", "'").lower()
        substitute = "substitute" in event.notes.lower()
        for start, name, substitute_name in _TITLES:
            if title.startswith(start):
                names.add(substitute_name if substitute else name)
    return frozenset(names)
