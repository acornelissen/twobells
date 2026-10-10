"""How punctual each regular train and bus has been, kept in SQLite.

So the dashboard can say "Late 4 of the last 10 weekdays (avg 6 min)". The
dashboard's pollers call observe() after each successful fetch with what the
board showed; a failed fetch calls nothing, so nothing is decided from it.

One row per departure per day: the 07:42 to Manchester on 9 October. Each
poll overwrites the row with what it showed, and the row becomes final, its
outcome, once the departure has left:
  1. it drops off the board within a minute of its time (or after it), so
     the last thing seen before it left is the outcome;
  2. or it is still not final half an hour after its time (sweep), which
     covers "Delayed" with no time and a board that was restarted.
A departure that drops off earlier than that (pushed off the end of the
list, or filtered out) stays undecided. If nobody saw it near its time
(within WATCHED of it), its outcome isn't known and it is deleted instead.

Final rows never change. Times are naive local time, as everywhere on the
board, stored as ISO text so they sort as they compare. One connection is
shared by the dashboard's threads, guarded by a lock.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from . import files
from .model import iso

FILE_NAME = "history.sqlite3"
SCHEMA_VERSION = 1

MODES = ("train", "bus")
STATUSES = ("on_time", "late", "cancelled", "unknown")
DAY_TYPES = ("weekday", "saturday", "sunday")

# Last seen further than this before it left, nobody was watching when it
# went, so its outcome isn't known. Buses are polled more often than trains.
WATCHED = {"train": timedelta(minutes=5), "bus": timedelta(minutes=3)}
# Dropping off the board this close to its time (or after) means it has left.
_LEFT = timedelta(minutes=1)
# Still undecided this long after its time: take the last sighting.
_STALE = timedelta(minutes=30)
# Text from the feeds is kept short; nobody needs more on a dashboard.
_MAX_TEXT = 200

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_SCHEMA = """
CREATE TABLE outcome (
    mode TEXT NOT NULL CHECK (mode IN ('train', 'bus')),
    day TEXT NOT NULL,
    scheduled TEXT NOT NULL,
    route TEXT NOT NULL,
    identity TEXT,
    service TEXT NOT NULL,
    destination TEXT NOT NULL,
    platform TEXT,
    status TEXT NOT NULL,
    delay_min INTEGER,
    live INTEGER NOT NULL,
    reason TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    departs_at TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mode, day, route, scheduled)
);
CREATE INDEX outcome_route ON outcome (mode, route, scheduled, final, day);
"""

_UPSERT = """
INSERT INTO outcome (mode, day, scheduled, route, identity, service, destination, platform,
                     status, delay_min, live, reason, first_seen, last_seen, departs_at, final)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
ON CONFLICT (mode, day, route, scheduled) DO UPDATE SET
    identity = excluded.identity,
    service = excluded.service,
    destination = excluded.destination,
    platform = excluded.platform,
    status = excluded.status,
    delay_min = excluded.delay_min,
    live = excluded.live,
    reason = excluded.reason,
    last_seen = excluded.last_seen,
    departs_at = excluded.departs_at
WHERE outcome.final = 0
"""


class Observed(NamedTuple):
    """One departure as a poll showed it."""

    route: str  # a regular departure, e.g. "train|GLO>MAN|Manchester Piccadilly"
    scheduled: str  # "HH:MM"
    day: date  # the local date of the scheduled departure
    departs_at: datetime  # expected, else scheduled; scheduled if cancelled
    status: str  # one of STATUSES
    delay_min: int | None  # None if not known
    live: bool
    service: str
    destination: str
    platform: str | None
    reason: str | None
    identity: str | None


class Stats(NamedTuple):
    """Outcomes over the last few matching days. avg_late and worst are over
    the late days only, None if there were none."""

    days: int
    late: int
    cancelled: int
    unknown: int
    on_time: int
    avg_late: float | None
    worst: int | None


def day_type(day: date, holidays: frozenset[date] = frozenset()) -> str:
    """"weekday", "saturday" or "sunday". A bank holiday runs a Sunday service,
    so it counts as a Sunday."""
    if day in holidays or day.weekday() == 6:
        return "sunday"
    return "saturday" if day.weekday() == 5 else "weekday"


_day_type = day_type  # stats() has a parameter of the same name


def _naive(moment: datetime, name: str) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is not None:
        raise ValueError(f"{name} must be a naive local datetime")
    return moment


def _mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}")
    return mode


def _text(value: str | None) -> str | None:
    return None if value is None else str(value)[:_MAX_TEXT]


def _row(mode: str, row: Observed, polled_at: str) -> tuple:
    """The values to upsert for one observation. ValueError if it doesn't make sense."""
    if not isinstance(row.day, date) or isinstance(row.day, datetime):
        raise ValueError("day must be a date")
    if not isinstance(row.scheduled, str) or not _HHMM.match(row.scheduled):
        raise ValueError(f"scheduled must be HH:MM, not {row.scheduled!r}")
    if row.status not in STATUSES:
        raise ValueError(f"Unknown status {row.status!r}")
    if row.delay_min is not None and (isinstance(row.delay_min, bool) or not isinstance(row.delay_min, int)):
        raise ValueError("delay_min must be a whole number of minutes or None")
    departs_at = _naive(row.departs_at, "departs_at")
    return (
        mode, row.day.isoformat(), row.scheduled, _text(row.route), _text(row.identity),
        _text(row.service) or "", _text(row.destination) or "", _text(row.platform),
        row.status, row.delay_min, int(bool(row.live)), _text(row.reason),
        polled_at, polled_at, iso(departs_at),
    )


def _create(path: Path) -> None:
    """Create the file if need be, readable by its owner and group only, as
    timetable.save does. SQLite gives its -wal and -shm files the same mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | files.NOFOLLOW
    fd = os.open(path, flags, 0o640)
    try:
        os.fchmod(fd, 0o640)  # whatever the umask, or an old file's mode
    finally:
        os.close(fd)


class History:
    """Punctuality history in `folder`/history.sqlite3. Safe to share between threads."""

    def __init__(self, folder: Path | str) -> None:
        path = Path(folder) / FILE_NAME
        _create(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            # Safe with WAL (a power cut loses at most the last poll) and kinder to an SD card.
            self._db.execute("PRAGMA synchronous=NORMAL")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._db.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
            elif version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"{path} has history version {version}, not {SCHEMA_VERSION}")
        except Exception:
            self._db.close()
            raise

    @classmethod
    def reader(cls, folder: Path | str) -> History | None:
        """The history in `folder`, to read only, as the web server on a Pi
        does while the fetch service writes it: None if there is none yet,
        or it isn't set up. Never makes or changes the file or its schema
        (PRAGMA query_only), and opens nothing but a plain file."""
        path = Path(folder) / FILE_NAME
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
        except FileNotFoundError:
            return None
        db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=rw", uri=True, check_same_thread=False)
        try:
            db.execute("PRAGMA query_only=ON")
            version = db.execute("PRAGMA user_version").fetchone()[0]
        except Exception:
            db.close()
            raise
        if version != SCHEMA_VERSION:
            db.close()
            return None
        history = cls.__new__(cls)
        history._lock = threading.Lock()
        history._db = db
        return history

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def observe(self, mode: str, rows: list[Observed], polled_at: datetime) -> None:
        """Record what a successful poll of `mode` showed at `polled_at`, and
        decide the outcome of anything that has left since the last one."""
        _mode(mode)
        now = iso(_naive(polled_at, "polled_at"))
        values = [_row(mode, row, now) for row in rows]  # all checked before any is written
        seen = {(value[1], value[3], value[2]) for value in values}  # (day, route, scheduled)
        left = iso(polled_at + _LEFT)
        with self._lock, self._db:
            self._db.executemany(_UPSERT, values)
            gone = [
                row for row in self._db.execute(
                    "SELECT mode, day, route, scheduled, last_seen, departs_at FROM outcome"
                    " WHERE mode = ? AND final = 0 AND departs_at <= ?",
                    (mode, left),
                )
                if (row[1], row[2], row[3]) not in seen
            ]
            self._finalise(gone)

    def sweep(self, now: datetime) -> None:
        """Decide anything still undecided half an hour after its time: a
        "Delayed" that never got a time, or one that left while the board was off."""
        stale = iso(_naive(now, "now") - _STALE)
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT mode, day, route, scheduled, last_seen, departs_at FROM outcome"
                " WHERE final = 0 AND departs_at < ?",
                (stale,),
            ).fetchall()
            self._finalise(rows)

    def _finalise(self, rows: list[tuple]) -> None:
        """Make each row final with its last sighting, or delete it if that
        was too long before it left. Call with the lock held, in a transaction."""
        for mode, day, route, scheduled, last_seen, departs_at in rows:
            key = (mode, day, route, scheduled)
            unwatched = datetime.fromisoformat(departs_at) - datetime.fromisoformat(last_seen) > WATCHED[mode]
            if unwatched:
                self._db.execute(
                    "DELETE FROM outcome WHERE mode = ? AND day = ? AND route = ? AND scheduled = ?", key
                )
            else:
                self._db.execute(
                    "UPDATE outcome SET final = 1 WHERE mode = ? AND day = ? AND route = ? AND scheduled = ?", key
                )

    def stats(
        self,
        mode: str,
        route: str,
        scheduled: str,
        day_type: str,
        *,
        n: int = 10,
        late_minutes: int,
        holidays: frozenset[date] = frozenset(),
    ) -> Stats:
        """Outcomes of `route` at `scheduled` over its last `n` final days of
        `day_type`. Late is at least `late_minutes` late and not cancelled; late
        or unknown with no delay known counts as unknown."""
        outcomes = [(status, delay_min) for status, delay_min, _ in
                    self._last_days(mode, route, scheduled, day_type, n, holidays)]
        late, cancelled, unknown, on_time = [], 0, 0, 0
        for status, delay_min in outcomes:
            if status == "cancelled":
                cancelled += 1
            elif delay_min is None:
                if status in ("late", "unknown"):
                    unknown += 1
                else:
                    on_time += 1
            elif delay_min >= late_minutes:
                late.append(delay_min)
            else:
                on_time += 1
        return Stats(
            days=len(outcomes),
            late=len(late),
            cancelled=cancelled,
            unknown=unknown,
            on_time=on_time,
            avg_late=sum(late) / len(late) if late else None,
            worst=max(late) if late else None,
        )

    def no_shows(
        self,
        mode: str,
        route: str,
        scheduled: str,
        day_type: str,
        n: int = 10,
        *,
        holidays: frozenset[date] = frozenset(),
    ) -> tuple[int, int]:
        """(days without a live tracker, days) for `route` at `scheduled` over
        its last `n` final days of `day_type`. "Without a live tracker" means
        the last sighting before it left had no live position: for a bus, no
        vehicle was placed on its journey. It doesn't prove the bus didn't run."""
        found = self._last_days(mode, route, scheduled, day_type, n, holidays)
        return sum(1 for _, _, live in found if not live), len(found)

    def _last_days(self, mode: str, route: str, scheduled: str, day_type: str, n: int,
                   holidays: frozenset[date]) -> list[tuple[str, int | None, bool]]:
        """(status, delay_min, live) of the last `n` final days of `day_type`, newest first."""
        _mode(mode)
        if day_type not in DAY_TYPES:
            raise ValueError(f"Unknown day type {day_type!r}")
        found = []
        with self._lock:
            cursor = self._db.execute(
                "SELECT day, status, delay_min, live FROM outcome"
                " WHERE mode = ? AND route = ? AND scheduled = ? AND final = 1 ORDER BY day DESC",
                (mode, route, scheduled),
            )
            for day, status, delay_min, live in cursor:
                if len(found) >= n:
                    break
                if _day_type(date.fromisoformat(day), holidays) == day_type:
                    found.append((status, delay_min, bool(live)))
        return found

    def recent(self, mode: str, route: str, scheduled: str, days: int = 90) -> list[dict]:
        """The outcomes of `route` at `scheduled` on its last `days` final days,
        newest first. day is ISO text ("2026-10-09"), ready for JSON."""
        _mode(mode)
        with self._lock:
            rows = self._db.execute(
                "SELECT day, status, delay_min, live, reason FROM outcome"
                " WHERE mode = ? AND route = ? AND scheduled = ? AND final = 1 ORDER BY day DESC LIMIT ?",
                (mode, route, scheduled, days),
            ).fetchall()
        return [
            {"day": day, "status": status, "delay_min": delay_min, "live": bool(live), "reason": reason}
            for day, status, delay_min, live, reason in rows
        ]

    def purge(self, now: datetime, days: int = 90) -> None:
        """Forget departures from more than `days` days before `now`."""
        oldest = (_naive(now, "now").date() - timedelta(days=days)).isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM outcome WHERE day < ?", (oldest,))


class HistoryReader:
    """History.recent from the history another process writes (the fetch
    service, on a Pi), opened read-only the first time it is there."""

    def __init__(self, folder: Path | str) -> None:
        self._folder = Path(folder)
        self._lock = threading.Lock()
        self._history: History | None = None

    def recent(self, mode: str, route: str, scheduled: str, days: int = 90) -> list[dict]:
        """As History.recent; nothing while there is no history yet."""
        with self._lock:
            if self._history is None:
                self._history = History.reader(self._folder)
            history = self._history
        return [] if history is None else history.recent(mode, route, scheduled, days)

    def close(self) -> None:
        with self._lock:
            if self._history is not None:
                self._history.close()
                self._history = None


_NOUNS = {"weekday": "weekdays", "saturday": "Saturdays", "sunday": "Sundays"}
_ENOUGH = 3  # fewer days than this say nothing


def phrase(stats: Stats, day_type: str) -> str | None:
    """A short line for the dashboard, e.g. "Late 4 of the last 10 weekdays
    (avg 6 min)", or None with fewer than three days to go on."""
    if stats.days < _ENOUGH:
        return None
    last = f"the last {stats.days} {_NOUNS[day_type]}"
    if stats.cancelled and stats.late:
        return f"Cancelled {stats.cancelled} and late {stats.late} of {last}"
    if stats.cancelled:
        return f"Cancelled {stats.cancelled} of {last}"
    if stats.late:
        return f"Late {stats.late} of {last} (avg {int(stats.avg_late + 0.5)} min)"
    if stats.on_time == stats.days:
        return f"On time {last}"
    if stats.on_time:
        return f"On time {stats.on_time} of {last}"
    return None  # nothing known about any of them


def no_show_phrase(without: int, days: int, day_type: str) -> str | None:
    """A short line for a bus with no tracker yet, e.g. "No tracker on 3 of
    the last 10 weekdays", or None with fewer than three days to go on or
    when every one of them had a tracker. Worded as what was seen (no
    tracker), not as a claim that the bus didn't run."""
    if days < _ENOUGH or without <= 0:
        return None
    last = f"the last {days} {_NOUNS[day_type]}"
    if without == days:
        return f"No tracker on any of {last}"
    return f"No tracker on {without} of {last}"
