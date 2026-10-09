"""Bank holidays in England and Wales, from gov.uk, cached beside the state file.

Trains and buses run a Sunday service on bank holidays. The list is fetched
at most once a week (the board runs every minute, so the cache's age is
checked first), and after a failure not again for a few hours. Whatever goes
wrong, the last good list is kept; with none at all, there are no holidays.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

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


def parse(body: bytes) -> frozenset[date]:
    """England and Wales bank holidays from gov.uk's JSON, skipping bad entries.
    ValueError if it isn't that list or has no usable dates."""
    data = json.loads(body)
    region = data.get(REGION) if isinstance(data, dict) else None
    events = region.get("events") if isinstance(region, dict) else None
    if not isinstance(events, list):
        raise ValueError("Not gov.uk's list of bank holidays")
    dates = _dates(event.get("date") for event in events if isinstance(event, dict))
    if not dates:
        raise ValueError("No bank holidays in gov.uk's list")
    return dates


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _load(path: Path) -> tuple[datetime | None, datetime | None, frozenset[date]]:
    """(last fetched, last tried, dates) from the cache; nothing if there's no usable cache."""
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, frozenset()
    except (OSError, ValueError) as error:
        log.warning("Ignoring unreadable bank holiday cache %s: %s", path, error)
        return None, None, frozenset()
    if not isinstance(cache, dict) or not isinstance(cache.get("dates"), list):
        log.warning("Ignoring malformed bank holiday cache %s", path)
        return None, None, frozenset()
    return _moment(cache.get("fetched")), _moment(cache.get("checked")), _dates(cache["dates"])


def _save(path: Path, fetched: datetime | None, checked: datetime, dates: frozenset[date]) -> None:
    record = {
        "fetched": fetched.isoformat() if fetched else None,
        "checked": checked.isoformat(),
        "dates": sorted(day.isoformat() for day in dates),
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
    return _load(Path(folder) / CACHE_NAME)[2]


def bank_holidays(folder: Path | str, now: datetime, fetch: Fetch | None = None) -> frozenset[date]:
    """England and Wales bank holidays, refreshed from gov.uk if the cache in
    `folder` is a week old. `now` is local time, as everywhere on the board."""
    path = Path(folder) / CACHE_NAME
    fetched, checked, dates = _load(path)
    if not _older(fetched, now, REFRESH) or not _older(checked, now, RETRY):
        return dates
    try:
        fresh = parse((fetch or get_bytes)(URL, MAX_BYTES))
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good list
        log.warning("Bank holidays not refreshed, keeping %d cached: %s", len(dates), safe_message(error))
        _save(path, fetched, now, dates)
        return dates
    _save(path, now, now, fresh)
    return fresh
