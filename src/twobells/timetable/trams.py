"""Manchester Metrolink's timetable at your tram stops, from TfGM's open GTFS.

Transport for Greater Manchester publishes every bus and tram timetable in
the county as one GTFS file, built again each night, at GTFS_URL (listed on
data.gov.uk as "GM Public Transport Schedules - GTFS and TXC datasets"),
under the Open Database License; no key is needed. It is about 45 MB, and
its stop_times.txt about 170 MB once unzipped, so it is never held whole:
the download waits in a temporary file in the store, and each part is read
a line at a time, keeping only what is Metrolink's (its agency, METL, and
GTFS route_type 0, trams) and, of that, only what calls at your stops.
Replacement buses for the trams are listed by TfGM as tram routes too, called
"Replacement Bus": they are kept, and said to be buses.

What is kept is small (a few thousand departures a stop over the weeks the
file covers), saved as FILE_NAME beside the bus timetable, and built again
weekly or when your tram stops change (due), by the same hands as the bus
timetable: `twobells timetable` (the Pi's hourly timer) and the dashboard's
upkeep in Docker and on a desktop. A failed build is kept in FAILED_FILE and
not tried again for RETRY_AFTER, as each try downloads the whole file.

Times are minutes after midnight of the service day, as GTFS gives them
(08:09:30 is 489, 25:10:00 is 1510), seconds dropped, so a tram is never
shown later than it goes. Every field is checked as it is read, as
sources/darwin.py does: a row that isn't what GTFS says is left out.

These are booked times only: TfGM's live tram data is closed to new keys,
so the views mark them as the timetable's, and there is no punctuality to
keep.
"""

from __future__ import annotations

import csv
import fcntl
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import IO, BinaryIO

from ..core import bounded, files, net
from ..core.errors import safe_message
from .build import TimetableBusy, TimetableError, TimetableTooBig

log = logging.getLogger(__name__)

GTFS_URL = "https://odata.tfgm.com/opendata/downloads/TfGMgtfsnew.zip"
FILE_NAME = "tram-timetable.json"  # beside state.json and the bus timetable
LOCK_NAME = "tram-timetable.lock"  # beside it; held while a build runs
FAILED_FILE = "tram-timetable-failed.json"  # beside it after a build fails
RETRY_AFTER = timedelta(hours=6)
MAX_AGE = timedelta(days=7)  # built again after this
TOO_OLD = timedelta(days=28)  # not used at all after this
FORMAT = 1

# The download is about 45 MB; its parts, unzipped, at most these.
MAX_DOWNLOAD_BYTES = 250_000_000
_MAX_PART_BYTES = {"stop_times.txt": 2_000_000_000}
_MAX_OTHER_PART_BYTES = 300_000_000
_MAX_LINE = 4096  # characters in one CSV row; real ones are under 200
# How a part may be packed. zipfile unpacks bzip2 and LZMA a whole read at a
# time, with no cap on what comes out (a few hundred bytes of bzip2 can make
# 290 MB in one read), so _Capped would stop it too late; TfGM's are deflated.
_PACKINGS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
# Parts in the zip: GTFS has about a dozen. zipfile holds each one listed
# before any is read, so the list is checked first, and its bytes with it.
MAX_PARTS = 100
_MAX_LIST_BYTES = 100_000
DOWNLOAD_TIMEOUT = 60.0
# The most kept: departures at your stops over the file's weeks, and trips
# and services Metrolink's. Real figures are a tenth of these or less.
MAX_CALLS = 100_000
MAX_TRIPS = 200_000
MAX_SERVICES = 20_000
MAX_FILE_BYTES = 30_000_000
MAX_STOPS = 4  # tram stops on one board

# Metrolink, as TfGM's file names it: its operator code, and GTFS's route_type for trams.
METROLINK_NOC = "METL"
TRAM_ROUTE_TYPE = "0"
REPLACEMENT_BUS = "Replacement Bus"

CODE = re.compile(r"^9400ZZMA[A-Z]{3}$")  # a Metrolink stop: 9400ZZMA and TfGM's three letters
_PLATFORM = re.compile(r"^(9400ZZMA[A-Z]{3})[0-9]{1,2}$")  # one of its platforms
_TIME = re.compile(r"^([0-9]{1,2}):([0-5][0-9]):([0-5][0-9])$")
_DATE = re.compile(r"^[0-9]{8}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_LIMIT = 60
_LATEST_MINUTE = 48 * 60  # GTFS runs past midnight; two days on is nonsense
_NETWORK = re.compile(r"\s*\(Manchester Metrolink\)$")


class NotMetrolink(TimetableError):
    """The file has no Metrolink trams in it."""


@dataclass(frozen=True, slots=True)
class Service:
    """When a service runs: calendar.txt's days and dates, with calendar_dates' changes."""

    days: frozenset[int] = frozenset()  # Monday = 0
    start: date | None = None
    end: date | None = None
    added: frozenset[date] = frozenset()
    removed: frozenset[date] = frozenset()

    def runs_on(self, day: date) -> bool:
        if day in self.removed:
            return False
        if day in self.added:
            return True
        return (self.start is not None and self.end is not None and self.start <= day <= self.end
                and day.weekday() in self.days)


@dataclass(frozen=True, slots=True)
class TramCall:
    """A tram (or a replacement bus) leaving one of your stops."""

    stop: str  # your stop's label
    minute: int  # minutes after midnight of the service day
    line: str  # "Green Line"
    destination: str  # "Bury"
    service: str  # its service's id, in `services`
    bus: bool = False  # a replacement bus


@dataclass(frozen=True)
class TramTimetable:
    built: datetime  # board-local, without a zone
    stops: dict[str, str]  # your labels to stop codes, as when built
    services: dict[str, Service]
    calls: tuple[TramCall, ...]  # by minute
    feed: str = ""  # the file's own version (feed_info.txt), to tell one night's from the next
    valid_until: date | None = None  # the last day any service in it runs
    format: int = FORMAT
    names: dict[str, str] = field(default_factory=dict)  # stop code to name, from the file

    def leaving(self, now: datetime, rows: int, directions=None) -> list[tuple[datetime, TramCall]]:
        """The next `rows` calls from `now` (board-local, without a zone) on,
        soonest first, each with when it leaves; yesterday's service day too,
        for trams after midnight. With `directions`, only those whose
        destination has one of them in its name. Two the same (two
        replacement buses at once, say) are one."""
        found = []
        seen = set()
        for back in (1, 0):
            day = now.date() - timedelta(days=back)
            midnight = datetime.combine(day, datetime.min.time())
            for call in self.calls:
                leaves = midnight + timedelta(minutes=call.minute)
                if leaves < now.replace(second=0, microsecond=0):
                    continue
                if directions and not any(d.lower() in call.destination.lower() for d in directions):
                    continue
                service = self.services.get(call.service)
                same = (leaves, call.stop, call.line, call.destination, call.bus)
                if service is not None and service.runs_on(day) and same not in seen:
                    seen.add(same)
                    found.append((leaves, call))
        found.sort(key=lambda pair: (pair[0], pair[1].stop, pair[1].destination))
        return found[:rows]


# Reading what came from outside.


def _text(value: object, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(_CONTROL.sub(" ", value[: limit * 4]).split())
    return "".join(c for c in text if c.isprintable())[:limit].strip()


def _minute(value: object) -> int | None:
    match = _TIME.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    minute = int(match.group(1)) * 60 + int(match.group(2))
    return minute if minute < _LATEST_MINUTE else None


def _day(value: object) -> date | None:
    if not isinstance(value, str) or not _DATE.match(value):
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def destination_name(value: object) -> str:
    """A headsign or stop name as the views show it: "Bury", not "Bury (Manchester Metrolink)"."""
    return _NETWORK.sub("", _text(value, _TEXT_LIMIT + 30))[:_TEXT_LIMIT].strip()


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    """The rows of one part of the file, a line at a time, as {column: value}.
    TimetableError if the part is missing, bigger than it may be, or packed
    other than stored or deflated."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        raise TimetableError(f"The tram timetable has no {name}") from None
    if info.compress_type not in _PACKINGS:
        raise TimetableError(f"The tram timetable's {name} is packed in a way GTFS files never are")
    limit = _MAX_PART_BYTES.get(name, _MAX_OTHER_PART_BYTES)
    if info.file_size > limit:
        raise TimetableTooBig(f"The tram timetable's {name} is larger than {limit} bytes")
    with archive.open(info) as raw:
        capped = io.BufferedReader(_Capped(raw, limit))
        text = io.TextIOWrapper(capped, encoding="utf-8-sig", errors="replace", newline="")
        lines = bounded.lines(text, _MAX_LINE)
        header = next(csv.reader([next(lines, "")]), [])
        columns = [column.strip() for column in header]
        for line in lines:
            if not line:
                continue  # longer than _MAX_LINE: left out
            # Most rows have no quotes, and splitting them is far quicker than csv on a Pi.
            values = line.rstrip("\r\n").split(",") if '"' not in line else next(csv.reader([line]), [])
            yield dict(zip(columns, values, strict=False))


class _Capped(io.RawIOBase):
    """A stream that stops with TimetableTooBig after `limit` bytes, whatever
    the zip's own sizes claim."""

    def __init__(self, raw: IO[bytes], limit: int) -> None:
        self._raw, self._left = raw, limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self._raw.read(min(len(buffer), max(self._left, 0) + 1))
        self._left -= len(data)
        if self._left < 0:
            raise TimetableTooBig("The tram timetable unzips to more than it may")
        buffer[: len(data)] = data
        return len(data)


def read_gtfs(archive: zipfile.ZipFile, stops: Mapping[str, str], now: datetime) -> TramTimetable:
    """Metrolink's departures from your `stops` (label to stop code) in an
    opened GTFS file. NotMetrolink if it has no Metrolink trams."""
    wanted = {code: label for label, code in stops.items()}
    agencies = {row.get("agency_id", "") for row in _rows(archive, "agency.txt")
                if row.get("agency_noc", "").strip() == METROLINK_NOC or _text(row.get("agency_name")) == "Metrolink"}
    routes = {}
    for row in _rows(archive, "routes.txt"):
        if row.get("route_type", "").strip() == TRAM_ROUTE_TYPE and row.get("agency_id", "") in agencies:
            line = _text(row.get("route_short_name")) or _text(row.get("route_long_name")) or "Metrolink"
            routes[row.get("route_id", "")] = (line, line == REPLACEMENT_BUS)
    if not routes:
        raise NotMetrolink("The tram timetable has no Metrolink trams in it")
    # Your stops' platforms, and the names of every Metrolink stop for destinations without a headsign.
    platforms: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in _rows(archive, "stops.txt"):
        match = _PLATFORM.match(row.get("stop_code", "").strip())
        if match is None:
            continue
        stop_id = row.get("stop_id", "")
        names[stop_id] = destination_name(row.get("stop_name"))
        if match.group(1) in wanted and _ID.match(stop_id):
            platforms[stop_id] = wanted[match.group(1)]
    trips: dict[str, tuple[str, str, str, bool]] = {}
    for row in _rows(archive, "trips.txt"):
        route = routes.get(row.get("route_id", ""))
        trip, service = row.get("trip_id", ""), row.get("service_id", "")
        if route is None or not _ID.match(trip) or not _ID.match(service):
            continue
        if len(trips) >= MAX_TRIPS:
            raise TimetableTooBig(f"The tram timetable has more than {MAX_TRIPS} Metrolink trips")
        trips[trip] = (service, route[0], destination_name(row.get("trip_headsign")), route[1])
    # Each trip's last stop, which no one boards at; and its calls at your stops.
    last: dict[str, tuple[int, str]] = {}
    found: list[tuple[str, int, int, str]] = []
    for row in _rows(archive, "stop_times.txt"):
        trip = row.get("trip_id", "")
        if trip not in trips:
            continue
        try:
            sequence = int(row.get("stop_sequence", ""))
        except ValueError:
            continue
        stop_id = row.get("stop_id", "")
        if trip not in last or sequence > last[trip][0]:
            last[trip] = (sequence, stop_id)
        label = platforms.get(stop_id)
        if label is None or row.get("pickup_type", "").strip() == "1":
            continue
        minute = _minute((row.get("departure_time") or row.get("arrival_time") or "").strip())
        if minute is None:
            continue
        if len(found) >= MAX_CALLS:
            raise TimetableTooBig(f"Your tram stops have more than {MAX_CALLS} departures in the timetable")
        found.append((trip, sequence, minute, label))
    calls = []
    for trip, sequence, minute, label in found:
        if last[trip][0] == sequence:
            continue  # it ends here
        service, line, headsign, bus = trips[trip]
        destination = headsign or names.get(last[trip][1], "")
        if destination:
            calls.append(TramCall(label, minute, line, destination, service, bus))
    calls.sort(key=lambda c: (c.minute, c.stop, c.destination))
    services = _services(archive, {call.service for call in calls})
    ends = [s.end for s in services.values() if s.end is not None] + [d for s in services.values() for d in s.added]
    feed = ""
    if "feed_info.txt" in archive.namelist():
        feed = next((_text(row.get("feed_version")) for row in _rows(archive, "feed_info.txt")), "")
    return TramTimetable(now.replace(microsecond=0), dict(stops), services, tuple(calls), feed,
                         max(ends) if ends else None, names={})


def _services(archive: zipfile.ZipFile, used: set[str]) -> dict[str, Service]:
    """When each service in `used` runs, from calendar.txt and calendar_dates.txt (either may be missing)."""
    base: dict[str, Service] = {}
    names = set(archive.namelist())
    if "calendar.txt" in names:
        for row in _rows(archive, "calendar.txt"):
            service = row.get("service_id", "")
            if service not in used:
                continue
            days = frozenset(i for i, day in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday",
                                                         "saturday", "sunday")) if row.get(day, "").strip() == "1")
            start, end = _day(row.get("start_date", "").strip()), _day(row.get("end_date", "").strip())
            base[service] = Service(days, start, end)
    added: dict[str, set[date]] = {}
    removed: dict[str, set[date]] = {}
    if "calendar_dates.txt" in names:
        for row in _rows(archive, "calendar_dates.txt"):
            service, day = row.get("service_id", ""), _day(row.get("date", "").strip())
            kind = row.get("exception_type", "").strip()
            if service in used and day is not None and kind in ("1", "2"):
                (added if kind == "1" else removed).setdefault(service, set()).add(day)
    services = {}
    for service in used:
        start = base.get(service, Service())
        services[service] = Service(start.days, start.start, start.end, frozenset(added.get(service, ())),
                                    frozenset(removed.get(service, ())))
        if len(services) > MAX_SERVICES:
            raise TimetableTooBig(f"The tram timetable has more than {MAX_SERVICES} services at your stops")
    return services


# Keeping it.


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def to_json(timetable: TramTimetable) -> str:
    """The file: lines and destinations listed once, each call by their place."""
    lines = sorted({c.line for c in timetable.calls})
    destinations = sorted({c.destination for c in timetable.calls})
    return json.dumps({
        "format": timetable.format,
        "built": timetable.built.isoformat(),
        "feed": timetable.feed,
        "valid_until": _iso(timetable.valid_until),
        "stops": timetable.stops,
        "lines": lines,
        "destinations": destinations,
        "services": {name: {"days": sorted(s.days), "start": _iso(s.start), "end": _iso(s.end),
                            "added": sorted(d.isoformat() for d in s.added),
                            "removed": sorted(d.isoformat() for d in s.removed)}
                     for name, s in sorted(timetable.services.items())},
        "calls": [[c.stop, c.minute, lines.index(c.line), destinations.index(c.destination), c.service, int(c.bus)]
                  for c in timetable.calls],
    }, separators=(",", ":"))


def _saved_day(value) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else None


def from_json(text: str) -> TramTimetable:
    """A saved timetable, checked as it is read: ValueError, KeyError or
    TypeError if it isn't one (a hand edit, or another version's)."""
    data = json.loads(text)
    lines = [_text(line) for line in data["lines"]]
    destinations = [_text(d) for d in data["destinations"]]
    stops = {str(label): str(code) for label, code in dict(data["stops"]).items()}
    if not stops or len(stops) > MAX_STOPS or not all(CODE.match(code) for code in stops.values()):
        raise ValueError("the saved tram stops aren't Metrolink stops")
    services = {}
    for name, raw in dict(data["services"]).items():
        days = frozenset(int(d) for d in raw["days"])
        if not days <= set(range(7)):
            raise ValueError("a service runs on a day that isn't one")
        services[str(name)] = Service(days, _saved_day(raw["start"]), _saved_day(raw["end"]),
                                      frozenset(date.fromisoformat(d) for d in raw["added"]),
                                      frozenset(date.fromisoformat(d) for d in raw["removed"]))
    calls = []
    for stop, minute, line, destination, service, bus in data["calls"]:
        if stop not in stops or isinstance(minute, bool) or not 0 <= int(minute) < _LATEST_MINUTE:
            raise ValueError("a saved tram call isn't one")
        calls.append(TramCall(stop, int(minute), lines[int(line)], destinations[int(destination)], str(service),
                              bool(bus)))
    return TramTimetable(datetime.fromisoformat(data["built"]), stops, services, tuple(calls), _text(data["feed"]),
                         _saved_day(data["valid_until"]), int(data["format"]))


def save(folder: Path | str, timetable: TramTimetable) -> None:
    text = to_json(timetable)
    if len(text) > MAX_FILE_BYTES:
        raise TimetableTooBig(f"The tram timetable is larger than {MAX_FILE_BYTES} bytes; not saving it")
    files.write_atomic(Path(folder) / FILE_NAME, text, 0o640)


def load(folder: Path | str) -> TramTimetable | None:
    """The saved timetable, or None if there is none or it can't be read."""
    path = Path(folder) / FILE_NAME
    try:
        with path.open(encoding="utf-8") as file:
            if os.fstat(file.fileno()).st_size > MAX_FILE_BYTES:
                log.warning("Ignoring tram timetable %s: larger than %d bytes", path, MAX_FILE_BYTES)
                return None
            return from_json(file.read())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        log.warning("Ignoring unreadable tram timetable %s: %s", path, safe_message(error))
        return None


def due(timetable: TramTimetable | None, stops: Mapping[str, str], now: datetime) -> str | None:
    """Why the timetable should be built again, or None if it shouldn't."""
    if timetable is None:
        return "there is none"
    if dict(timetable.stops) != dict(stops):
        return "your tram stops changed"
    if timetable.format < FORMAT:
        return "it was built by an older version"
    if now - timetable.built >= MAX_AGE or timetable.built - now > timedelta(days=1):
        return "it is a week old"
    return None


def unusable(timetable: TramTimetable | None, stops: Mapping[str, str], now: datetime) -> str | None:
    """Why the timetable can't be used for departures, or None if it can."""
    if timetable is None:
        return "there is no tram timetable yet"
    if dict(timetable.stops) != dict(stops):
        return "the tram timetable is for other stops"
    if now - timetable.built > TOO_OLD:
        return "the tram timetable is over four weeks old"
    return None


# Building it.

Download = Callable[[str, BinaryIO], None]


def download(url: str, file: BinaryIO, max_bytes: int = MAX_DOWNLOAD_BYTES) -> None:
    """Write the GTFS file into `file` as it arrives: https only, and no more than `max_bytes`."""
    try:
        for chunk in net.stream(url, {}, max_bytes, timeout=DOWNLOAD_TIMEOUT, deadline=net.DOWNLOAD_DEADLINE):
            file.write(chunk)
    except net.TooLarge:
        raise TimetableTooBig(f"The tram timetable download is larger than {max_bytes} bytes; refusing") from None


@contextmanager
def _lock(folder: Path) -> Iterator[None]:
    """Hold the tram build's lock; TimetableBusy at once if another holds it."""
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = files.open_lock(folder / LOCK_NAME)
    except OSError as error:
        raise TimetableError(f"Can't open the tram timetable's lock: {error.strerror or error}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TimetableBusy("Another tram timetable build is running") from None
        yield
    finally:
        os.close(fd)


def _failed_recently(folder: Path, stops: Mapping[str, str], now: datetime) -> bool:
    try:
        record = json.loads((folder / FAILED_FILE).read_text(encoding="utf-8")[:500])
        at = datetime.fromisoformat(record["at"])
        return record["stops"] == dict(stops) and timedelta(0) <= now - at < RETRY_AFTER
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _open(file: BinaryIO) -> zipfile.ZipFile:
    """The zip in `file`. TimetableTooBig if it lists more than MAX_PARTS
    parts, or more than _MAX_LIST_BYTES of them (bounded.open_zip)."""
    try:
        return bounded.open_zip(file, MAX_PARTS, _MAX_LIST_BYTES)
    except bounded.ZipTooBig:
        raise TimetableTooBig(f"The tram timetable has more than {MAX_PARTS} parts") from None


def refresh(folder: Path | str, stops: Mapping[str, str], now: datetime, *, force: bool = False,
            fetch: Download = download, url: str = GTFS_URL) -> TramTimetable | None:
    """The timetable to use, built again first if it is due or `force`d. A
    failed build keeps the saved one and isn't tried again for RETRY_AFTER,
    unless forced or the stops change; TimetableError says why it failed.
    TimetableBusy if another build is running."""
    folder = Path(folder)
    if not stops or len(stops) > MAX_STOPS or not all(CODE.match(code) for code in stops.values()):
        raise TimetableError(f"Tram stops are 1 to {MAX_STOPS} Metrolink stop codes, like 9400ZZMAPIC")
    saved = load(folder)
    if not force and (due(saved, stops, now) is None or _failed_recently(folder, stops, now)):
        return saved
    with _lock(folder):
        try:
            with tempfile.TemporaryFile(dir=folder) as file:
                fetch(url, file)
                file.seek(0)
                try:
                    with _open(file) as archive:
                        timetable = read_gtfs(archive, stops, now)
                except (zipfile.BadZipFile, EOFError, UnicodeError) as error:
                    raise TimetableError(f"The tram timetable isn't a GTFS file: {safe_message(error)}") from None
            save(folder, timetable)
        except Exception as error:
            message = safe_message(error) if isinstance(error, TimetableError) else (
                f"The tram timetable couldn't be had: {safe_message(error)}")
            files.write_atomic(folder / FAILED_FILE, json.dumps({"at": now.isoformat(), "stops": dict(stops),
                                                                  "error": message[:200]}), 0o640)
            raise TimetableError(message) from error
    (folder / FAILED_FILE).unlink(missing_ok=True)
    return timetable
