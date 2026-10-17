"""How punctual each regular train and bus has been, kept in SQLite.

So the dashboard can say "Late 4 of the last 10 weekdays (avg 6 min)", and
its Performance page "Right Time 62% (31 of 50)" (measure()). The
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

It also keeps when trains reached the filter station, for the Performance
page's Delay Repay watch (dashboard/repay.py): one row per train per day in
`arrival`, kept up to date (watch(), report()) until it has arrived:
  1. Darwin reports the time it got there, or that it was cancelled there;
  2. or its estimate is ARRIVED old, having been seen within WATCHED of it:
     it got there then, as near as anyone can tell;
  3. or it is still undecided an hour after its estimate, or was only ever
     "Delayed": nobody saw it arrive, so it is deleted (settle()).

Version 1 had only `outcome`; opening one adds `arrival` and keeps every row.
An older release refuses a newer version, so before moving a file on, a copy
of it as it was is kept beside it, once (history.sqlite3.v1), to put back.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from ..core import files
from ..core.model import iso

FILE_NAME = "history.sqlite3"
SCHEMA_VERSION = 2

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
# Trains due at the filter station this soon, or overdue, are looked for.
FOLLOW_AHEAD = timedelta(minutes=10)
# An arrival's estimate this old, seen within WATCHED["train"] of it, is when it got there.
ARRIVED = timedelta(minutes=2)
# Still undecided this long after its estimate: nobody saw it arrive.
_ARRIVAL_STALE = timedelta(hours=1)
# Text from the feeds is kept short; nobody needs more on a dashboard.
_MAX_TEXT = 200

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CRS = re.compile(r"[A-Z]{3}")

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

# Version 2: when each train reached the filter station. `scheduled` is when
# it was booked to leave your station, `booked` when to get there; `expected`
# is the latest estimate (`known` 0 while it is only "Delayed"), `actual`
# what Darwin reported. All three are ISO local times.
_ARRIVALS = """
CREATE TABLE arrival (
    day TEXT NOT NULL,
    scheduled TEXT NOT NULL,
    route TEXT NOT NULL,
    station TEXT NOT NULL,
    station_name TEXT NOT NULL,
    identity TEXT,
    operator TEXT NOT NULL,
    destination TEXT NOT NULL,
    booked TEXT NOT NULL,
    expected TEXT NOT NULL,
    known INTEGER NOT NULL,
    actual TEXT,
    cancelled INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, route, scheduled, station)
);
CREATE INDEX arrival_open ON arrival (final, expected);
CREATE INDEX arrival_day ON arrival (station, final, day);
"""

# What takes a file from each version to the next.
_MIGRATIONS = {1: _ARRIVALS}

_ARRIVAL_UPSERT = """
INSERT INTO arrival (day, scheduled, route, station, station_name, identity, operator, destination,
                     booked, expected, known, actual, cancelled, last_seen, final)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (day, route, scheduled, station) DO UPDATE SET
    station_name = excluded.station_name,
    identity = excluded.identity,
    operator = excluded.operator,
    destination = excluded.destination,
    booked = excluded.booked,
    expected = CASE WHEN excluded.known THEN excluded.expected ELSE arrival.expected END,
    known = excluded.known,
    actual = excluded.actual,
    cancelled = excluded.cancelled,
    last_seen = excluded.last_seen,
    final = excluded.final
WHERE arrival.final = 0
"""

_ARRIVAL_REPORT = """
UPDATE arrival SET
    expected = CASE WHEN ? THEN ? ELSE expected END,
    known = ?, actual = ?, cancelled = ?, last_seen = ?, final = ?
WHERE day = ? AND route = ? AND scheduled = ? AND station = ? AND final = 0
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


class Watched(NamedTuple):
    """A train that calls at the filter station, as a train poll showed it:
    when it is due there, and when it got there if Darwin has said."""

    route: str  # as Observed's
    scheduled: str  # "HH:MM", booked to leave your station
    day: date  # the local date it was booked to leave
    station: str  # the filter station's CRS code
    station_name: str
    identity: str | None  # Darwin's service ID
    operator: str
    destination: str
    booked: datetime  # booked to get there
    expected: datetime | None  # the estimate; None if not known ("Delayed")
    actual: datetime | None  # when it got there, if reported
    cancelled: bool  # cancelled there, or at your station


class Following(NamedTuple):
    """A train not yet known to have reached the filter station (History.following)."""

    day: date
    route: str
    scheduled: str
    station: str
    identity: str | None
    booked: datetime
    expected: datetime


class Report(NamedTuple):
    """What a later look said about a train being followed, as Watched's."""

    expected: datetime | None
    actual: datetime | None
    cancelled: bool


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


class Measures(NamedTuple):
    """How departures ran, the way the rail industry counts it, over some days.

    A departure whose delay isn't known (no time given, or a bus with no
    tracker) is `unknown` and left out of the rest; `counted` are the
    others, cancelled ones included, which the measures are out of, as the
    industry counts a cancelled train as one that missed every measure.
    Delays are whole minutes, so "under 3 minutes late" is 2 or less."""

    days: int  # days with at least one outcome
    counted: int  # cancelled, or with a known delay
    right_time: int  # left at or before its booked time
    within_3: int  # T-3: under 3 minutes late
    within_15: int  # T-15: under 15 minutes late
    on_time: int  # under late_minutes late: the dashboard's own "on time"
    cancelled: int
    unknown: int


def measure(outcomes: list[tuple[str, str, int | None]], late_minutes: int) -> Measures:
    """Measures of (day, status, delay_min) outcomes; on time is under `late_minutes` late."""
    right_time = within_3 = within_15 = on_time = cancelled = unknown = 0
    for _, status, delay_min in outcomes:
        if status == "cancelled":
            cancelled += 1
        elif delay_min is None:
            unknown += 1
        else:
            right_time += delay_min <= 0
            within_3 += delay_min < 3
            within_15 += delay_min < 15
            on_time += delay_min < late_minutes
    return Measures(
        days=len({day for day, _, _ in outcomes}),
        counted=len(outcomes) - unknown,
        right_time=right_time,
        within_3=within_3,
        within_15=within_15,
        on_time=on_time,
        cancelled=cancelled,
        unknown=unknown,
    )


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


def _watched(row: Watched, polled_at: str) -> tuple:
    """The values to upsert for one watched train. ValueError if it doesn't make sense."""
    if not isinstance(row.day, date) or isinstance(row.day, datetime):
        raise ValueError("day must be a date")
    if not isinstance(row.scheduled, str) or not _HHMM.match(row.scheduled):
        raise ValueError(f"scheduled must be HH:MM, not {row.scheduled!r}")
    if not isinstance(row.station, str) or not _CRS.fullmatch(row.station):
        raise ValueError(f"station must be a CRS code, not {row.station!r}")
    booked = iso(_naive(row.booked, "booked"))
    expected = iso(_naive(row.expected, "expected")) if row.expected is not None else booked
    actual = iso(_naive(row.actual, "actual")) if row.actual is not None else None
    cancelled = bool(row.cancelled)
    return (
        row.day.isoformat(), row.scheduled, _text(row.route), row.station, _text(row.station_name) or row.station,
        _text(row.identity), _text(row.operator) or "", _text(row.destination) or "",
        booked, expected, int(row.expected is not None), actual, int(cancelled), polled_at,
        int(cancelled or actual is not None),
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


def _keep_copy(db: sqlite3.Connection, path: Path) -> None:
    """Copy the history as it is to `path`, before it is moved on to a newer
    version, so an older release that reads only this one can have it back
    (deploy/pi/upgrade.sh says how). Kept once: a copy already there, or
    anything else at `path`, is left alone. A copy that fails part way is
    removed, and the history isn't moved on."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | files.NOFOLLOW, 0o640)
    except FileExistsError:
        return
    try:
        os.fchmod(fd, 0o640)  # whatever the umask
    finally:
        os.close(fd)
    try:
        copy = sqlite3.connect(path)
        try:
            db.backup(copy)  # the WAL's pages too, which a copy of the file alone would miss
        finally:
            copy.close()
    except Exception:
        path.unlink(missing_ok=True)
        raise


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
                schema = _SCHEMA + "".join(_MIGRATIONS[step] for step in range(1, SCHEMA_VERSION))
                self._db.executescript(f"BEGIN; {schema} PRAGMA user_version={SCHEMA_VERSION}; COMMIT;")
            elif version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"{path} has history version {version}, not {SCHEMA_VERSION}")
            elif version < SCHEMA_VERSION:
                _keep_copy(self._db, path.with_name(f"{FILE_NAME}.v{version}"))
            for step in range(version or SCHEMA_VERSION, SCHEMA_VERSION):
                # A version at a time, each in a transaction of its own: the rows already there are kept.
                self._db.executescript(f"BEGIN; {_MIGRATIONS[step]} PRAGMA user_version={step + 1}; COMMIT;")
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
        "Delayed" that never got a time, or one that left while the board was
        off. The arrivals that can be are settled too."""
        stale = iso(_naive(now, "now") - _STALE)
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT mode, day, route, scheduled, last_seen, departs_at FROM outcome"
                " WHERE final = 0 AND departs_at < ?",
                (stale,),
            ).fetchall()
            self._finalise(rows)
        self.settle(now)

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

    # Arrivals at the filter station, for the Delay Repay watch.

    def watch(self, rows: list[Watched], polled_at: datetime) -> None:
        """Record the trains a successful train poll showed calling at the filter station."""
        now = iso(_naive(polled_at, "polled_at"))
        values = [_watched(row, now) for row in rows]  # all checked before any is written
        with self._lock, self._db:
            self._db.executemany(_ARRIVAL_UPSERT, values)

    def following(self, now: datetime) -> list[Following]:
        """The trains not yet known to have arrived that are due within
        FOLLOW_AHEAD of `now`, or overdue: the ones to look for, soonest first."""
        cut = iso(_naive(now, "now") + FOLLOW_AHEAD)
        with self._lock:
            rows = self._db.execute(
                "SELECT day, route, scheduled, station, identity, booked, expected FROM arrival"
                " WHERE final = 0 AND expected <= ? ORDER BY expected",
                (cut,),
            ).fetchall()
        return [
            Following(date.fromisoformat(day), route, scheduled, station, identity,
                      datetime.fromisoformat(booked), datetime.fromisoformat(expected))
            for day, route, scheduled, station, identity, booked, expected in rows
        ]

    def report(self, train: Following, found: Report, seen_at: datetime) -> None:
        """What a later look at `seen_at` said about `train`: its estimate, or when it got there."""
        now = iso(_naive(seen_at, "seen_at"))
        expected = iso(_naive(found.expected, "expected")) if found.expected is not None else None
        actual = iso(_naive(found.actual, "actual")) if found.actual is not None else None
        cancelled = bool(found.cancelled)
        with self._lock, self._db:
            self._db.execute(_ARRIVAL_REPORT, (
                expected is not None, expected, int(expected is not None), actual, int(cancelled), now,
                int(cancelled or actual is not None),
                train.day.isoformat(), train.route, train.scheduled, train.station,
            ))

    def settle(self, now: datetime) -> None:
        """Decide the arrivals that can be: an estimate ARRIVED old that was
        seen within WATCHED of it is when the train got there; one an hour
        old that wasn't, or was only "Delayed", is deleted, as nobody knows."""
        local = _naive(now, "now")
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT day, route, scheduled, station, expected, known, last_seen FROM arrival"
                " WHERE final = 0 AND expected <= ?",
                (iso(local - ARRIVED),),
            ).fetchall()
            for day, route, scheduled, station, expected, known, last_seen in rows:
                key = (day, route, scheduled, station)
                at = datetime.fromisoformat(expected)
                if known and at - datetime.fromisoformat(last_seen) <= WATCHED["train"]:
                    self._db.execute("UPDATE arrival SET final = 1"
                                     " WHERE day = ? AND route = ? AND scheduled = ? AND station = ?", key)
                elif local - at >= _ARRIVAL_STALE:
                    self._db.execute("DELETE FROM arrival"
                                     " WHERE day = ? AND route = ? AND scheduled = ? AND station = ?", key)

    def arrivals(self, station: str, since: date) -> list[dict]:
        """Every train that reached `station` on a day on or after `since`
        (the day it left your station), newest first, cancelled ones left
        out. `arrived` is when it got there: Darwin's report, else its last
        estimate (`reported` false). Times are ISO local times."""
        if not isinstance(since, date) or isinstance(since, datetime):
            raise ValueError("since must be a date")
        with self._lock:
            rows = self._db.execute(
                "SELECT day, scheduled, route, station_name, operator, destination, booked, expected, actual"
                " FROM arrival WHERE station = ? AND final = 1 AND cancelled = 0 AND day >= ?"
                " ORDER BY day DESC, booked DESC",
                (station, since.isoformat()),
            ).fetchall()
        return [
            {"day": day, "scheduled": scheduled, "route": route, "station_name": station_name,
             "operator": operator, "destination": destination, "booked": booked,
             "arrived": actual or expected, "reported": actual is not None}
            for day, scheduled, route, station_name, operator, destination, booked, expected, actual in rows
        ]

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

    def usual_platform(self, mode: str, route: str, scheduled: str, n: int = 10) -> str | None:
        """The platform `route` at `scheduled` left from on most of its last
        `n` final days with a platform known, any kind of day: one used on
        more than half of them, and at least three. None without that much
        to go on, or with no one platform used most."""
        _mode(mode)
        with self._lock:
            rows = self._db.execute(
                "SELECT platform FROM outcome WHERE mode = ? AND route = ? AND scheduled = ? AND final = 1"
                " AND platform IS NOT NULL ORDER BY day DESC LIMIT ?",
                (mode, route, scheduled, n),
            ).fetchall()
        counts = Counter(platform for (platform,) in rows)
        if not counts:
            return None
        platform, used = counts.most_common(1)[0]
        return platform if used >= _ENOUGH and used * 2 > len(rows) else None

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

    def outcomes(self, mode: str, since: date, route: str | None = None) -> list[tuple[str, str, str, int | None]]:
        """(route, day, status, delay_min) of every final departure of `mode`
        on or after `since`, of `route` only if given, for measure()."""
        _mode(mode)
        if not isinstance(since, date) or isinstance(since, datetime):
            raise ValueError("since must be a date")
        query = "SELECT route, day, status, delay_min FROM outcome WHERE mode = ? AND final = 1 AND day >= ?"
        values: tuple = (mode, since.isoformat())
        if route is not None:
            query += " AND route = ?"
            values += (route,)
        with self._lock:
            return self._db.execute(query, values).fetchall()

    def purge(self, now: datetime, days: int = 90) -> None:
        """Forget departures from more than `days` days before `now`."""
        oldest = (_naive(now, "now").date() - timedelta(days=days)).isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM outcome WHERE day < ?", (oldest,))
            self._db.execute("DELETE FROM arrival WHERE day < ?", (oldest,))


class HistoryReader:
    """History.recent, outcomes and arrivals from the history another process
    writes (the fetch service, on a Pi), opened read-only the first time it is there."""

    def __init__(self, folder: Path | str) -> None:
        self._folder = Path(folder)
        self._lock = threading.Lock()
        self._history: History | None = None

    def _opened(self) -> History | None:
        with self._lock:
            if self._history is None:
                self._history = History.reader(self._folder)
            return self._history

    def recent(self, mode: str, route: str, scheduled: str, days: int = 90) -> list[dict]:
        """As History.recent; nothing while there is no history yet."""
        history = self._opened()
        return [] if history is None else history.recent(mode, route, scheduled, days)

    def outcomes(self, mode: str, since: date, route: str | None = None) -> list[tuple[str, str, str, int | None]]:
        """As History.outcomes; nothing while there is no history yet."""
        history = self._opened()
        return [] if history is None else history.outcomes(mode, since, route)

    def arrivals(self, station: str, since: date) -> list[dict]:
        """As History.arrivals; nothing while there is no history yet."""
        history = self._opened()
        return [] if history is None else history.arrivals(station, since)

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
    if stats.late and stats.avg_late is not None:  # there is an average whenever there are late days
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
