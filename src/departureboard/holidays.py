"""Bank holidays in England and Wales, from gov.uk, cached beside the state file.

Trains and buses run a Sunday service on bank holidays. The list is fetched
at most once a week (the board runs every minute, so the cache's age is
checked first), and after a failure not again for a few hours. Whatever goes
wrong, the last good list is kept; with none at all, there are no holidays.

Each holiday's title is kept too, so bus timetables can name it the way
TransXChange does ("SpringBank", "BoxingDayHoliday"). Caches from before
titles were kept still work, and gain titles at their next weekly refresh.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, NamedTuple

from .http import get_bytes
from .status import safe_message

log = logging.getLogger(__name__)

URL = "https://www.gov.uk/bank-holidays.json"
REGION = "england-and-wales"
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
    if not isinstance(value, str) or not _DATE.match(value):
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


def parse_events(body: bytes) -> dict[date, Event]:
    """England and Wales bank holidays from gov.uk's JSON, with their titles,
    skipping bad entries. ValueError if it isn't that list or has no usable dates."""
    data = json.loads(body)
    region = data.get(REGION) if isinstance(data, dict) else None
    events = region.get("events") if isinstance(region, dict) else None
    if not isinstance(events, list):
        raise ValueError("Not gov.uk's list of bank holidays")
    found = _events(events)
    if not found:
        raise ValueError("No bank holidays in gov.uk's list")
    return found


def parse(body: bytes) -> frozenset[date]:
    """England and Wales bank holidays from gov.uk's JSON, skipping bad entries.
    ValueError if it isn't that list or has no usable dates."""
    return frozenset(parse_events(body))


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


class _Cache(NamedTuple):
    fetched: datetime | None
    checked: datetime | None
    dates: frozenset[date]
    events: dict[date, Event]  # empty in caches from before titles were kept


_EMPTY = _Cache(None, None, frozenset(), {})


def _load(path: Path) -> _Cache:
    """What the cache holds; nothing if there's no usable cache."""
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _EMPTY
    except (OSError, ValueError) as error:
        log.warning("Ignoring unreadable bank holiday cache %s: %s", path, error)
        return _EMPTY
    if not isinstance(cache, dict) or not isinstance(cache.get("dates"), list):
        log.warning("Ignoring malformed bank holiday cache %s", path)
        return _EMPTY
    events = cache.get("events")
    return _Cache(
        _moment(cache.get("fetched")),
        _moment(cache.get("checked")),
        _dates(cache["dates"]),
        _events(events) if isinstance(events, list) else {},
    )


def _save(path: Path, fetched: datetime | None, checked: datetime, dates: frozenset[date], events: dict) -> None:
    record = {
        "fetched": fetched.isoformat() if fetched else None,
        "checked": checked.isoformat(),
        "dates": sorted(day.isoformat() for day in dates),
        "events": [
            {"date": day.isoformat(), "title": event.title, "notes": event.notes}
            for day, event in sorted(events.items())
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as error:
        log.warning("Can't save the bank holiday cache %s: %s", path, error)


def _older(moment: datetime | None, now: datetime, age: timedelta) -> bool:
    return moment is None or now - moment >= age or moment - now > _SKEW


def read_cache(folder: Path | str) -> frozenset[date]:
    """The cached bank holidays, without fetching or writing anything."""
    return _load(Path(folder) / CACHE_NAME).dates


def read_events(folder: Path | str) -> dict[date, Event]:
    """The cached bank holidays with their titles, without fetching or writing anything."""
    return _load(Path(folder) / CACHE_NAME).events


def bank_holidays(folder: Path | str, now: datetime, fetch: Fetch | None = None) -> frozenset[date]:
    """England and Wales bank holidays, refreshed from gov.uk if the cache in
    `folder` is a week old. `now` is local time, as everywhere on the board."""
    path = Path(folder) / CACHE_NAME
    cache = _load(path)
    if not _older(cache.fetched, now, REFRESH) or not _older(cache.checked, now, RETRY):
        return cache.dates
    try:
        events = parse_events((fetch or get_bytes)(URL, MAX_BYTES))
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good list
        log.warning("Bank holidays not refreshed, keeping %d cached: %s", len(cache.dates), safe_message(error))
        _save(path, cache.fetched, now, cache.dates, cache.events)
        return cache.dates
    fresh = frozenset(events)
    _save(path, now, now, fresh, events)
    return fresh


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
