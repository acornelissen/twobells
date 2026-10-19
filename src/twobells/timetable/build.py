"""The bus timetable for your stops, built from BODS TransXChange.

The way bustimes.org does it: every timetabled journey that calls at one of
your stops, with the time it leaves each stop on its way, and the days it
runs. sources/livebus.py then adjusts it with live positions. Each
document is read by timetable/txc.py; this downloads them, builds the
timetable from what it reads, and saves and refreshes it.

Inbound-only journeys. A bus often comes in to a stand near your stop
without calling at it, then works a departure from your stop. To find that
bus (enrich.inbound), journeys of the lines departing from your stops that
end within NEAR_M of one of them, without calling at any, are kept too,
marked inbound_only: they never give departures. They are looked for only in
documents that mention your stops (a document holds a whole service, both
ways), among the lines calling at your stops in that document. Distances
are by NaPTAN's positions, or the timetables' own where NaPTAN has none.
To hold fewer journeys while reading, a journey is dropped as it is read
when the timetables place its last stop further than NEAR_M from all of
your stops; when they can't tell, it is held for NaPTAN to measure only if
its last stop is in the same ATCO area (the first three characters) as one
of yours.

Positions. The stops a journey calls at on its way to your stops (the
CALLS_BEFORE before each, and the one after) are placed, by NaPTAN or the
timetables' own positions, so livebus can place its bus there; so are those
an inbound-only journey calls at on its way in. A bus that turns round at
your stop, though, is out on the journey before for most of the time before
it leaves, often further out than that. So a journey that ends at one of
your stops, or within NEAR_M of one, on a line (and operator) with
departures starting there, has every stop it calls at placed (_turning).
The BODS request's box is drawn round them all (bodsbus), so it grows to
match. Those placed only for that (Schedule.turning_only) are marked apart:
they are not near your stops, so a disruption there isn't counted as
nearby (sources/disruptions.py), and a box too big is drawn without them.
While reading, before NaPTAN has placed them, every stop of a
journey that may end near one of yours (txc.may_be_near) is held, and those
of journeys that don't are let go once it has.

Built about once a week by a timer, the command line or the dashboard's
upkeep, and by the setup search, never from the minutely board run: an
operator's timetables can run to hundreds of megabytes. Each download waits
on disk and its documents are parsed one at a time, so a Pi Zero holds one
document in memory, never the lot. One too big for the install to read or
parse (routesetup.DocumentLimits, smaller on a Pi) is skipped with a warning
naming it, and the rest are read.

One build at a time. Every build goes through build(), which holds a lock
file in the store (LOCK_NAME) while it runs, let go by the system if the
process dies. A build that finds it held is skipped (TimetableBusy), except
the setup search's, which waits for it: the timetable it saves must be the
one for what it found.

Roads. TransXChange gives no road between stops, so once a new timetable is
saved, refresh asks the OpenStreetMap car router for each pair of consecutive
positioned stops, once, and saves it again with the roads for the
dashboard's maps (add_roads, timetable/roads.py). A road the last timetable has
between the same two stops, neither of them moved, is kept rather than asked
for again. A pair it can't find is drawn straight; the router never costs a
timetable. Until its roads are found the saved timetable says so
(roads_pending), so if they are stopped part way (the wizard's search
cancelled, the process stopped or started again) the next build finds the
rest, due or not. While they are being found, how far they have got is kept
in ROADS_FILE for the settings site (roads_progress).

Versions. BODS keeps an old version of a service (its ServiceCode, line and
operator) beside the one replacing it, often open-ended, so both would run.
As bustimes.org decides, only a higher RevisionNumber replaces: a version
ends the day before one of a higher revision, open-ended, starts (or never
runs, if that one starts first). One of a higher revision with an end
(roadworks, say) only switches it off while it runs, as days of
non-operation, so it runs again after. Versions of the same revision are
halves of one timetable, each with a start of its own (High Peak's 393 in
October 2026: Saturdays from 2024, weekdays from 2026), and all run.

Calendars. runs_on() decides, in this order:
  1. outside the service's OperatingPeriod: no;
  2. a SpecialDaysOperation day of non-operation: no; a day of operation: yes;
  3. a bank holiday the journey doesn't run on: no; one it does run on: yes.
     A bank holiday listed in neither follows the regular days, as
     TransXChange says;
  4. school days (ServicedOrganisation working days or holidays): outside the
     days it runs only on, or inside the days it never runs on: no;
  5. otherwise the regular days of the week.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import tempfile
import time
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Set
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit

from ..core import files, net
from ..core.bounded import finite
from ..core.errors import redact_keys, safe_message
from . import roads as roadshape
from . import routesetup, txc
from .model import NEAR_M, Calendar, DateRange, Journey, Schedule, StopPosition, metres
from .routesetup import DEFAULT_LIMITS, DocumentLimits
from .txc import TimetableError

log = logging.getLogger(__name__)

FILE_NAME = "bus-timetable.json"  # beside state.json
LOCK_NAME = "timetable.lock"  # beside it; held while any build runs
LOCK_WAIT_S = 1.0  # between looks at a held lock, when waiting for it
ROADS_FILE = "bus-roads.json"  # beside it while its roads are being found: how far they have got
ROADS_EVERY_S = 5.0  # written at most this often (and the last), to spare an SD card
ROADS_STALE_S = 60.0  # unwritten this long, whatever was finding them has stopped
_ROADS_FILE_BYTES = 200
FAILED_FILE = "bus-timetable-failed.json"  # beside it after a build fails: when, and for what (see refresh)
RETRY_AFTER = timedelta(hours=6)  # a failed build waits this long before it is tried again
_FAILED_FILE_BYTES = 200
# The builder's version, saved in the file: an older one is rebuilt (see due).
# 2: stops keep the letter on their sign.
# 3: the roads between stops, for the dashboard's maps (shapes).
# 4: the block each journey is worked in (Journey.block), to follow its bus.
# 5: the stops placed only for a bus turning round (Schedule.turning_only).
# 6: only a higher RevisionNumber replaces a version (_until_replaced); no
#    more is held, so the memory below stands.
FORMAT = 6
MAX_AGE = timedelta(days=7)
# Positions are kept for this many calls before each of your stops, and the one
# after: livebus places buses along that stretch and sees them pass your stop.
# A journey whose bus turns round at your stop has all its calls placed
# (see the module's notes).
CALLS_BEFORE = 20
CALLS_AFTER = 1
# The most memory each journey found takes, held until the timetable is
# saved, that the installs' limits are sized by (settings/installs.py). Held
# as the build holds them (tracemalloc), BODS data set 2100's (First South
# Yorkshire) largest documents' journeys, of 80 calls on average, took 15.6 KB
# each on Python 3.11 and 14.6 KB on 3.12, before journeys had slots and
# shared what they have in common (txc.Shared). Journeys made up as long, with
# the same holidays off and a calendar each, took 13.4 and 12.7 KB then, and
# take 7.5 and 7.4 KB now. Real ones share more than those: in October
# 2026, data set 2100 (by then all of First Bus) held at Sheffield's busiest
# stops took 3.7 to 4.5 KB each on Python 3.11, at 63 to 73 calls on
# average and up to 134. 8 KB covers the made-up ones, each with a calendar
# of its own, and twice the real ones. Keeping each journey's block (format
# 4) added 8 bytes a journey, and its block numbers, shared: about 50 with
# one of its own each, so this stands. Placing every stop of the journeys
# whose bus turns round at your stop (_turning) holds a position per stop,
# not per journey: Glossop's build held 38 KB more for its 531 journeys
# (from 117 stops placed to 316), and its peak was unchanged. Marking those
# placed only for that (format 5) holds a set of their codes, sharing the
# positions' own: under 10 KB for Glossop's 199.
JOURNEY_BYTES = 8_000
# The most a journey takes in the saved file (save, a journey at a time):
# one of 80 calls with school terms, bank holidays and Christmas off takes
# 3.9 KB (tests/timetable/test_build.py). Its stops and roads take a few MB
# beside the journeys; roads that would make it too big are left out, for
# good (_with_roads).
SAVED_JOURNEY_BYTES = 4_000
STOPS_AND_ROADS_BYTES = 10_000_000
# The largest timetable file saved, and read: the most journeys any install
# holds (a desktop's 20,000 and 10,000 inbound-only), at SAVED_JOURNEY_BYTES
# each, and their stops and roads. Each install's limits fit it, and load()
# refuses anything bigger, which no build here made
# (tests/settings/test_installs.py).
MAX_FILE_BYTES = 130_000_000
DOWNLOAD_TIMEOUT = 60.0  # seconds without data before a download is given up
# A clock this far behind a timetable's build time was wrong when it was built
# (a Pi has no clock battery), so its age can't be trusted: build again.
_SKEW = timedelta(days=1)

FindDatasets = Callable[[str, str, "str | None"], list]  # (api_key, noc, line) -> links, as routesetup.dataset_urls


class Writable(Protocol):
    """Where a download is written: an open binary file, or one that meters it."""

    def write(self, data: bytes, /) -> int: ...


Download = Callable[[str, Writable], None]  # (link, file to write it to)
# atcos -> {atco: (name, lat, lon[, sign letter])}, as routesetup.naptan_stop_details
Naptan = Callable[[Iterable[str]], dict]
# ({(atco, atco): ((lat, lon), (lat, lon))}, progress=...) -> {(atco, atco): [(lat, lon), ...]},
# as roadshape.segment_shapes
Route = Callable[..., Mapping]
Progress = Callable[[int, int], None]  # (done, total)

class TimetableBusy(TimetableError):
    """Another build holds the lock, so this one didn't run."""


class TimetableTooBig(TimetableError):
    """A timetable larger than load() reads (MAX_FILE_BYTES), so not saved."""


# When journeys run.


def _within(day: date, ranges: Iterable[DateRange]) -> bool:
    return any(start <= day <= end for start, end in ranges)


def runs_on(calendar: Calendar, day: date, holiday_names: Mapping[date, Set[str]] = {}) -> bool:
    """Whether a journey with this calendar runs on `day`, its service day (the
    day it starts). `holiday_names` gives each day's TransXChange bank holiday
    names, from holidays.holiday_names. See the module's notes for the order."""
    if (calendar.start and day < calendar.start) or (calendar.end and day > calendar.end):
        return False
    if _within(day, calendar.dates_off):
        return False
    if _within(day, calendar.dates_on):
        return True
    names = holiday_names.get(day, ())
    if calendar.bank_holidays_off.intersection(names):
        return False
    if calendar.bank_holidays_on.intersection(names):
        return True
    if calendar.school_days_only and not _within(day, calendar.school_days_only):
        return False
    if _within(day, calendar.not_on_school_days):
        return False
    return day.weekday() in calendar.days


def journeys_on(schedule: Schedule, day: date, holiday_names: Mapping[date, Set[str]] = {}) -> list[Journey]:
    """The schedule's journeys that run on `day`, their service day."""
    return [journey for journey in schedule.journeys if runs_on(journey.calendar, day, holiday_names)]


# Building the schedule.


def download(url: str, file: Writable, max_bytes: int = routesetup.MAX_DOWNLOAD_BYTES) -> None:
    """Write a timetable download into `file` as it arrives, refusing anything
    but https, and more than `max_bytes`."""
    if urlsplit(url).scheme != "https":
        raise TimetableError("Refusing a timetable link that isn't https")
    try:
        for chunk in net.stream(url, {}, max_bytes, timeout=DOWNLOAD_TIMEOUT, deadline=net.DOWNLOAD_DEADLINE):
            file.write(chunk)
    except net.TooLarge:
        raise TimetableError(f"Timetable download larger than {max_bytes} bytes; refusing") from None


def _dataset_links(operators: Iterable[str], api_key: str, lines, find_datasets: FindDatasets) -> list[tuple[str, str]]:
    """(operator code, link) for each data set to fetch, each link once. With
    `lines`, only the data sets BODS says carry one of them."""
    links: dict[str, str] = {}
    wanted_lines = sorted(set(lines or ())) or [None]
    for noc in operators:
        for line in wanted_lines:
            for url in find_datasets(api_key, noc, line):
                links.setdefault(url, noc)
    return [(noc, url) for url, noc in links.items()]


def documents(
    links: list[tuple[str, str]], fetch: Download, scratch, max_bytes: int = routesetup.MAX_XML_BYTES,
    skipped: Callable[[str], None] = routesetup.log_skipped,
) -> Generator[tuple[str, str, bytes], None, None]:
    """(operator code, name, document) for every TransXChange document behind
    the links: one over `max_bytes` is skipped and told to `skipped`
    (routesetup.named_documents). Each download waits in a temporary file,
    gone once its documents have been used; each document is unpacked only
    when wanted."""
    for noc, url in links:
        with tempfile.TemporaryFile(dir=scratch) as file:
            fetch(url, file)
            file.seek(0)
            for name, document in routesetup.named_documents(file, max_bytes, name=redact_keys(url), skipped=skipped):
                yield noc, name, document


def _until_replaced(version: txc.Version, journey: Journey, versions: Mapping[tuple, Set[txc.Seen]],
                    shared: txc.Shared | None = None) -> Journey | None:
    """The journey as the versions of its service with a higher RevisionNumber
    leave it, as bustimes.org decides (Route.active_on): BODS keeps an old
    version beside the one replacing it, often open-ended, and both would
    run. It runs only on days no version of a higher revision runs, whichever
    starts first. It ends the day before the first open-ended one starts
    (None if that is on or before its own start); one with an end only
    switches it off from that one's start to its end (calendar.dates_off,
    which runs_on puts first), within the journey's own period. A version of
    the same revision replaces nothing: one timetable can be split across
    documents with starts of their own (Saturdays from one date, weekdays
    from another). `versions` has every version of each service seen. A
    journey whose document gives no ServiceCode is kept as it is. A new
    calendar is from `shared`, if given."""
    if not version.service:
        return journey
    calendar = journey.calendar
    start = calendar.start or date.min
    higher = [(other, other_end) for other, other_end, revision in versions.get(txc.service_of(version, journey), ())
              if revision > version.revision]
    replaced = [other for other, other_end in higher if other_end is None]
    if replaced and min(replaced) <= start:
        return None
    end = calendar.end
    if replaced and (end is None or min(replaced) <= end):
        end = min(replaced) - timedelta(days=1)
    off = tuple(sorted((max(other, start), other_end if end is None else min(other_end, end))
                       for other, other_end in higher
                       if other_end is not None and other_end >= start and (end is None or other <= end)))
    if end == calendar.end and not off:
        return journey
    calendar = replace(calendar, end=end, dates_off=calendar.dates_off + off)
    return replace(journey, calendar=shared(calendar) if shared is not None else calendar)


def _current(found: Iterable[tuple[txc.Version, Journey]], versions: Mapping[tuple, Set[txc.Seen]],
             today: date) -> tuple[Journey, ...]:
    """The journeys still running from `today`, each until a version of its
    service of a higher revision replaces it (_until_replaced), and each
    once: duplicates (same line, operator, calls and calendar) can't be told
    apart by live times. Those a version ends the same way share their new
    calendar."""
    kept: dict[tuple, Journey] = {}
    calendars = txc.Shared()
    for version, journey in found:
        current = _until_replaced(version, journey, versions, calendars)
        if current is None or (current.calendar.end and current.calendar.end < today):
            continue
        kept.setdefault((current.line, current.operator, current.calls, current.calendar), current)
    return tuple(kept.values())


def _nearby(journey: Journey, atcos: Collection[str]) -> set[str]:
    """The stops a journey calls at on its way to each of `atcos`, and the one after."""
    found = set()
    for index, call in enumerate(journey.calls):
        if call.atco in atcos:
            found.update(c.atco for c in journey.calls[max(0, index - CALLS_BEFORE) : index + CALLS_AFTER + 1])
    return found


def _approach(journey: Journey) -> set[str]:
    """The stop an inbound-only journey ends at and the ones it calls at on its way."""
    return {call.atco for call in journey.calls[-(CALLS_BEFORE + 1) :]}


def _all_calls(journey: Journey) -> set[str]:
    return {call.atco for call in journey.calls}


def _turning(journeys: Iterable[Journey], atcos: Collection[str], positions: Mapping[str, StopPosition]) -> set[str]:
    """Every stop of each journey whose bus may turn round at your stop: it
    ends at one of `atcos`, or within NEAR_M of one by `positions`, and a
    journey of its line and operator not inbound-only starts there."""
    journeys = list(journeys)
    starts = {(txc.line_and_operator(j), j.calls[0].atco)
              for j in journeys if not j.inbound_only and j.calls[0].atco in atcos}
    found: set[str] = set()
    for journey in journeys:
        end, here = journey.calls[-1].atco, positions.get(journey.calls[-1].atco)
        for line, stop in starts:
            if line != txc.line_and_operator(journey):
                continue
            there = positions.get(stop)
            if end == stop or (here is not None and there is not None and metres(here, there) <= NEAR_M):
                found |= _all_calls(journey)
                break
    return found


def _ends_near(journey: Journey, atcos: Collection[str], positions: Mapping[str, StopPosition]) -> bool:
    """Whether a journey ends within NEAR_M of one of `atcos`, by `positions`."""
    end = positions.get(journey.calls[-1].atco)
    return end is not None and any(atco in positions and metres(end, positions[atco]) <= NEAR_M for atco in atcos)


def _positions(atcos: set[str], known: dict[str, StopPosition], naptan: Naptan) -> dict[str, StopPosition]:
    """NaPTAN's name and position for each stop, or the timetables' own where
    NaPTAN has none or can't be reached."""
    try:
        found = naptan(sorted(atcos))
    except Exception as error:  # noqa: BLE001 - the timetables' own positions will do
        log.warning("NaPTAN not reached, using the timetables' stop positions: %s", safe_message(error))
        found = {}
    positions = {}
    for atco in sorted(atcos):
        row = found.get(atco)
        position = txc.stop_position(str(row[0]).strip()[:txc.TEXT_LIMIT], str(row[1]), str(row[2])) if row else None
        if position is not None and row and len(row) > 3:
            position = replace(position, indicator=txc.sign(row[3]))
        position = position or known.get(atco)
        if position is not None:
            positions[atco] = position
    return positions


def _kept_roads(pairs: Mapping, previous: Schedule | None) -> dict[str, tuple[tuple[float, float], ...]]:
    """The roads in `previous` between the same two stops, neither of them moved."""
    if previous is None:
        return {}
    kept = {}
    for (a, b), (start, end) in pairs.items():
        road = previous.shapes.get(roadshape.shape_key((a, b)))
        was_a, was_b = previous.positions.get(a), previous.positions.get(b)
        if road and was_a and was_b and (was_a.lat, was_a.lon) == start and (was_b.lat, was_b.lon) == end:
            kept[roadshape.shape_key((a, b))] = road
    return kept


def add_roads(schedule: Schedule, route: Route = roadshape.segment_shapes,
              previous: Schedule | None = None, progress: Progress = lambda done, total: None) -> Schedule:
    """The schedule with the road between each pair of consecutive positioned
    stops, by roadshape.shape_key, for the maps; none where the router can't
    say (the maps draw those straight). A road in `previous` (the timetable
    this one replaces) between the same two stops, neither of them moved, is
    kept rather than asked for again. `progress(done, total)` follows the
    router's requests. Never fails for the router's sake."""
    pairs = roadshape.segment_pairs(schedule.journeys, schedule.positions)
    kept = _kept_roads(pairs, previous)
    wanted = {pair: ends for pair, ends in pairs.items() if roadshape.shape_key(pair) not in kept}
    try:
        found = route(wanted, progress=progress)
        new = {roadshape.shape_key(pair): tuple((float(lat), float(lon)) for lat, lon in found[pair])
               for pair in wanted if found.get(pair)}
    except Exception as error:  # noqa: BLE001 - straight lines will do
        log.warning("Bus map roads not found, drawing straight lines: %s", safe_message(error))
        new = {}
    if kept:
        log.info("Bus map roads: kept %d from the last timetable", len(kept))
    return replace(schedule, shapes={**kept, **new})


class _Read(NamedTuple):
    """What build_schedule keeps from the timetables (_read_timetables)."""

    journeys: tuple[Journey, ...]  # those calling at your stops, current (_current)
    inbound: tuple[Journey, ...]  # the inbound-only ones, current, not yet measured
    # To place: on the way to your stops and after, where inbound-only ones end
    # and approach, and every stop of journeys that may end near your stops.
    stops: set[str]
    known: dict[str, StopPosition]  # the timetables' own positions of those stops
    left_out: int  # inbound-only journeys left out, past inbound_limit
    inbound_limit: int


def _read_timetables(links: list[tuple[str, str]], atcos: set[str], today: date, fetch: Download, scratch,
                     limits: DocumentLimits) -> _Read:
    """The journeys behind the links calling at `atcos`, and the inbound-only
    ones of their lines, as build_schedule keeps them, with the stops to place."""
    wanted = [atco.encode() for atco in atcos]
    # Journeys as read, by version too: which are current is known only once
    # every version of their service has been seen (_current).
    journeys: dict[tuple, tuple[txc.Version, Journey]] = {}
    inbound: dict[tuple, tuple[txc.Version, Journey]] = {}  # inbound-only, not yet measured
    versions: dict[tuple, set[txc.Seen]] = {}  # by txc.service_of
    nearby: set[str] = set()
    approaches: set[str] = set()
    turning: set[str] = set()  # every stop of journeys that may end near yours, until NaPTAN tells (_turning)
    known: dict[str, StopPosition] = {}
    yours: dict[str, StopPosition] = {}
    left_out = 0
    room = txc.Room(limits.journeys, txc.inbound_limit(limits))
    for noc, name, document in documents(links, fetch, scratch, limits.read):
        # Skip, unparsed, the many documents of a big operator that never mention your stops.
        if not any(code in document for code in wanted):
            continue
        if len(document) > limits.parse:
            log.warning("%s", routesetup.too_big(name, limits.parse, len(document)))
            continue
        room.held, room.held_inbound = len(journeys), len(inbound)
        found, positions = txc.read_document(document, atcos, noc, yours, room, today=today, versions=versions)
        yours.update((atco, positions[atco]) for atco in atcos if atco in positions)
        # Those that ended before today were let go as they were read, their
        # versions noted: even an ended version switches lower revisions off.
        for version, journey in found:
            group, seen = txc.seen_version(version, journey)
            versions.setdefault(group, set()).add(seen)
            key = (version, journey.line, journey.operator, journey.calls, journey.calendar)
            if not journey.inbound_only:
                journeys.setdefault(key, (version, journey))
                nearby |= _nearby(journey, atcos)
                if txc.may_be_near(journey.calls[-1].atco, positions, atcos, yours):
                    turning |= _all_calls(journey)
            elif key in inbound or len(inbound) < room.inbound:
                inbound.setdefault(key, (version, journey))
                approaches |= _approach(journey)
                turning |= _all_calls(journey)
            else:
                left_out += 1
        known.update((atco, position) for atco, position in positions.items()
                     if atco in nearby or atco in approaches or atco in turning)
    return _Read(_current(journeys.values(), versions, today), _current(inbound.values(), versions, today),
                 nearby | approaches | turning, known, left_out + room.left_out, room.inbound)


def build_schedule(
    stops: Mapping[str, str],
    operators: Iterable[str],
    api_key: str,
    now: datetime,
    *,
    lines: Iterable[str] | None = None,
    scratch: Path | str | None = None,
    limits: DocumentLimits = DEFAULT_LIMITS,
    find_datasets: FindDatasets = routesetup.dataset_urls,
    fetch: Download = download,
    naptan: Naptan = routesetup.naptan_stop_details,
) -> Schedule:
    """A schedule of every timetabled journey calling at your `stops` (labels
    to ATCO codes), from the BODS timetables of `operators` (national operator
    codes), and the inbound-only journeys of their lines (see the module's
    notes). Downloads wait in `scratch` (the state folder: /tmp may be memory).
    A document bigger than `limits` allow (the install's) is skipped with a
    warning naming it. It has no roads yet: see add_roads.

    BODS keeps an old version of a service beside the one replacing it, so
    each version ends the day before an open-ended one of a higher
    RevisionNumber starts, and is switched off while one of a higher
    revision with an end runs (_until_replaced).
    Journeys whose timetable ended before today are left out, and so are
    duplicates (same line, operator, calls and calendar), which live times
    couldn't tell apart. valid_until is the earliest end among the rest that
    call at your stops: when due() builds it again. TimetableError or
    RouteSetupError if anything fails, or no journey calls at your stops."""
    atcos = set(stops.values())
    links = _dataset_links(operators, api_key, lines, find_datasets)
    # Read in a function of its own: once it returns, nothing read from the
    # timetables is held but what is kept, as NaPTAN's response can be as big
    # as a document (routesetup.MAX_NAPTAN_BYTES).
    read = _read_timetables(links, atcos, now.date(), fetch, scratch, limits)
    kept = read.journeys
    if not kept:
        raise TimetableError("No timetabled journeys call at your stops")
    if read.left_out:
        log.warning("Left out %d journeys ending near your stops: more than %d", read.left_out, read.inbound_limit)
    placed = _positions(read.stops, read.known, naptan)
    near = tuple(journey for journey in read.inbound if _ends_near(journey, atcos, placed))
    approach = set().union(*(_nearby(journey, atcos) for journey in kept), *(_approach(journey) for journey in near))
    needed = approach | _turning(kept + near, atcos, placed)
    ends = [journey.calendar.end for journey in kept if journey.calendar.end]
    positions = {atco: position for atco, position in placed.items() if atco in needed}
    return Schedule(
        built=now,
        valid_until=min(ends, default=None),
        stops=dict(stops),
        journeys=kept + near,
        positions=positions,
        operators=tuple(sorted(set(operators))),
        format=FORMAT,
        turning_only=frozenset(atco for atco in positions if atco not in approach),
    )


# Keeping it.


def save(folder: Path | str, schedule: Schedule) -> None:
    """Save the schedule beside the state file, whole or not at all, readable
    by its owner and group only. It is written a journey at a time
    (Schedule.json_parts), never held whole or copied beside the journeys.
    TimetableTooBig, keeping the saved one, if it is larger than load()
    reads."""
    files.write_atomic(Path(folder) / FILE_NAME, _at_most(schedule.json_parts(), MAX_FILE_BYTES), 0o640)


def _at_most(parts: Iterable[str], limit: int) -> Iterator[bytes]:
    """The parts as bytes; TimetableTooBig once they come to more than `limit`."""
    size = 0
    for part in parts:
        data = part.encode("utf-8")
        size += len(data)
        if size > limit:
            raise TimetableTooBig(f"Bus timetable is larger than {limit} bytes; not saving it")
        yield data


def load(folder: Path | str, journeys: bool = True) -> Schedule | None:
    """The saved schedule, or None if there is none or it can't be read. It
    is read a piece at a time, holding little more than its journeys
    (Schedule.from_file). Without `journeys`, it has neither them nor its
    roads, and holds little more than its stops: as the settings pages
    want it, in a Pi's small web server."""
    path = Path(folder) / FILE_NAME
    try:
        with path.open(encoding="utf-8") as file:
            if os.fstat(file.fileno()).st_size > MAX_FILE_BYTES:
                log.warning("Ignoring bus timetable %s: larger than %d bytes", path, MAX_FILE_BYTES)
                return None
            return Schedule.from_file(file, journeys)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        log.warning("Ignoring unreadable bus timetable %s: %s", path, safe_message(error))
        return None


class RoadsProgress:
    """How far finding the roads has got, kept in ROADS_FILE beside the
    timetable for the settings site (roads_progress), which may be another
    process's. Call it as roadshape.segment_shapes's `progress`; clear() once
    the roads are found. Written whole, at most every ROADS_EVERY_S seconds
    and the last; it never fails, as it only says how things are going."""

    def __init__(self, folder: Path | str, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(folder) / ROADS_FILE
        self._clock = clock
        self._written: float | None = None

    def __call__(self, done: int, total: int) -> None:
        now = self._clock()
        if self._written is not None and done < total and now - self._written < ROADS_EVERY_S:
            return
        self._written = now
        try:
            files.write_atomic(self._path, json.dumps({"done": done, "total": total, "at": now}), 0o640)
        except OSError as error:
            log.warning("Can't keep how far the bus map roads have got: %s", error.strerror or error)

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            log.warning("Can't remove %s: %s", self._path, error.strerror or error)


def _small_json(path: Path, limit: int) -> dict | None:
    """The JSON object in a small plain file of at most `limit` bytes; None if
    there is none, it is bigger, a link or not a plain file, or not an object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | files.NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        body = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(body) > limit:
        return None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def roads_progress(folder: Path | str, now: Callable[[], float] = time.time) -> tuple[int, int] | None:
    """(done, total) while the roads for the maps are being found, from
    ROADS_FILE. None when they aren't, when it was left by a process that
    stopped (unwritten for ROADS_STALE_S), or when it isn't a small plain
    file of two counts and a time."""
    data = _small_json(Path(folder) / ROADS_FILE, _ROADS_FILE_BYTES)
    if data is None:
        return None
    done, total, at = data.get("done"), data.get("total"), data.get("at")
    if (not isinstance(done, int) or isinstance(done, bool) or not isinstance(total, int) or isinstance(total, bool)
            or not 0 <= done <= total):
        return None
    written = finite(at)
    if written is None or abs(now() - written) > ROADS_STALE_S:
        return None
    return done, total


def due(
    schedule: Schedule | None, stops: Mapping[str, str], now: datetime, operators: Iterable[str] | None = None
) -> str | None:
    """Why the schedule should be built again, or None if it shouldn't. With
    `operators`, a schedule built from others is due too."""
    if schedule is None:
        return "there is none"
    if dict(schedule.stops) != dict(stops):
        return "your stops changed"
    if schedule.format < FORMAT:
        return "it was built by an older version"
    if operators is not None and schedule.operators != tuple(sorted(set(operators))):
        return "your operators changed"
    if now - schedule.built >= MAX_AGE:
        return "it is a week old"
    if schedule.built - now > _SKEW:
        return "it was built in the future"
    if schedule.valid_until and now.date() > schedule.valid_until:
        return "a timetable in it has ended"
    return None


def _built_for(stops: Mapping[str, str], operators: Iterable[str], api_key: str) -> str:
    """A digest of the stops, operators and BODS key a build is for, so a
    failed build's record (FAILED_FILE) says what it was for without keeping
    them: a refused key fails every build, and saving the right one should
    build at once. The key goes in only as its own digest."""
    key = hashlib.sha256(api_key.encode()).hexdigest()
    plain = json.dumps([sorted(dict(stops).items()), sorted(set(operators)), key], separators=(",", ":"))
    return hashlib.sha256(plain.encode()).hexdigest()


def _note_failure(folder: Path | str, built_for: str, now: datetime) -> None:
    """Keep when a build failed and what for (_built_for), in FAILED_FILE (see refresh)."""
    try:
        files.write_atomic(Path(folder) / FAILED_FILE, json.dumps({"at": now.isoformat(), "for": built_for}), 0o640)
    except OSError as error:
        log.warning("Can't keep when the bus timetable build failed: %s", error.strerror or error)


def _failed_lately(folder: Path | str, built_for: str, now: datetime) -> datetime | None:
    """When the last build failed, if it was for the same (_built_for) less
    than RETRY_AFTER ago; None otherwise, or if FAILED_FILE can't be read. A
    failure the clock puts in the future doesn't count."""
    data = _small_json(Path(folder) / FAILED_FILE, _FAILED_FILE_BYTES)
    if data is None or data.get("for") != built_for or not isinstance(data.get("at"), str):
        return None
    try:
        at = datetime.fromisoformat(data["at"])
    except ValueError:
        return None
    if at.tzinfo is not None or not timedelta(0) <= now - at < RETRY_AFTER:
        return None
    return at


def _forget_failure(folder: Path | str) -> None:
    try:
        (Path(folder) / FAILED_FILE).unlink(missing_ok=True)
    except OSError as error:
        log.warning("Can't remove %s: %s", FAILED_FILE, error.strerror or error)


def refresh(
    folder: Path | str,
    stops: Mapping[str, str],
    operators: Iterable[str],
    now: datetime,
    api_key: str,
    *,
    lines: Iterable[str] | None = None,
    force: bool = False,
    build: Callable[..., Schedule] = build_schedule,
    route: Route = roadshape.segment_shapes,
    saved: Callable[[Schedule], None] = lambda schedule: None,
    cancelled: Callable[[], bool] = lambda: False,
    limits: DocumentLimits = DEFAULT_LIMITS,
) -> Schedule | None:
    """The schedule to use, from `folder` (the state folder). It is built
    again first if it is due (see due: weekly, or when the stops or operators
    change) or `force`d. A failed build keeps the saved one, and is kept in
    FAILED_FILE: it isn't tried again for RETRY_AFTER (each try downloads
    every timetable again), unless `force`d or the stops, operators or key
    change. A build that fails once `cancelled()` says so was stopped, not
    failed, and isn't kept. With nothing due this only reads the saved file,
    unless its roads are still to be found. It is built within `limits`,
    the install's (see build_schedule).

    A new timetable is saved before its roads are asked for, marked
    roads_pending, and `saved(schedule)` is told; then it is saved again
    with them (add_roads, with `route`, keeping the saved one's where it
    can). However long the router takes, or if this is stopped meanwhile,
    the timetable is kept, at worst with straight lines on the maps. If
    `cancelled()` says so once they have been asked for (they stopped part
    way), or this is stopped, they stay pending: the next refresh finds the
    rest.

    It takes no lock: build it with build(), which does."""
    current = load(folder)
    operators = list(operators)
    reason = "asked to" if force else due(current, stops, now, operators)
    built_for = _built_for(stops, operators, api_key)
    failed = _failed_lately(folder, built_for, now) if reason and not force else None
    if failed is not None:
        log.info("Bus timetable due (%s), but building it failed at %s: trying again after %s", reason,
                 f"{failed:%H:%M}", f"{failed + RETRY_AFTER:%H:%M}")
        reason = None
    if reason is None:
        if current is not None and current.roads_pending:
            log.info("Bus timetable saved before all its roads were found: finding the rest")
            return _with_roads(folder, current, route, current, cancelled)
        return current
    # Only its stops and roads are wanted from now on (add_roads keeps what
    # it can of them): its journeys aren't held beside the build.
    previous = replace(current, journeys=()) if current is not None else None
    del current
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        fresh = replace(build(stops, operators, api_key, now, lines=lines, scratch=folder, limits=limits),
                        roads_pending=True)
        save(folder, fresh)
    except Exception as error:  # noqa: BLE001 - any failure keeps the last good schedule
        log.warning("Bus timetable not rebuilt (%s), keeping the saved one: %s", reason, safe_message(error))
        if not cancelled():  # stopped, as the setup search's Cancel does: not to wait RETRY_AFTER for
            _note_failure(folder, built_for, now)
        return load(folder) if previous is not None else None  # as it was: a failed build saves nothing
    _forget_failure(folder)
    calling = len(fresh.departure_journeys)
    log.info("Bus timetable rebuilt (%s): %d journeys at your stops, %d ending near them", reason, calling,
             len(fresh.journeys) - calling)
    saved(fresh)
    return _with_roads(folder, fresh, route, previous, cancelled)


def _with_roads(folder: Path | str, schedule: Schedule, route: Route, previous: Schedule | None,
                cancelled: Callable[[], bool]) -> Schedule:
    """The saved `schedule` with its roads (add_roads), saved again: still
    pending if `cancelled()` by the end. As it was if they can't be saved;
    without them for good if they would make it too big to load, or they
    would be asked for again by every refresh."""
    progress = RoadsProgress(folder)
    try:
        routed = add_roads(schedule, route, previous, progress)
        routed = replace(routed, roads_pending=cancelled())
        save(folder, routed)
    except TimetableTooBig as error:
        log.warning("Bus map roads left out, drawing straight lines: %s", error)
        straight = replace(schedule, roads_pending=False)
        try:
            save(folder, straight)
        except Exception as again:  # noqa: BLE001 - the timetable is saved, its roads pending
            log.warning("Bus timetable not saved without its roads: %s", safe_message(again))
            return schedule
        return straight
    except Exception as error:  # noqa: BLE001 - the timetable is saved; the maps draw straight lines
        log.warning("Bus map roads not saved, drawing straight lines: %s", safe_message(error))
        return schedule
    finally:
        progress.clear()
    return routed


@contextmanager
def _lock(folder: Path, wait: bool, cancelled: Callable[[], bool], waiting: Callable[[], None]) -> Iterator[None]:
    """Hold the store's build lock. Without `wait`, TimetableBusy at once if
    another holds it; with it, look again every LOCK_WAIT_S seconds, calling
    `waiting()` once, until it is free or `cancelled()` (TimetableBusy too).
    The file is private, and a link put in its place is refused."""
    path = folder / LOCK_NAME
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = files.open_lock(path)
    except OSError as error:
        raise TimetableError(f"Can't open {path}: {error.strerror or error}") from None
    try:
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or cancelled():
                    raise TimetableBusy("Another bus timetable build is running") from None
                if not told:
                    told = True
                    waiting()
                time.sleep(LOCK_WAIT_S)
        yield
    finally:
        os.close(fd)  # lets go of the lock, if it was taken


def build(
    folder: Path | str,
    stops: Mapping[str, str],
    operators: Iterable[str],
    now: datetime,
    api_key: str,
    *,
    wait: bool = False,
    cancelled: Callable[[], bool] = lambda: False,
    waiting: Callable[[], None] = lambda: None,
    **options,
) -> Schedule | None:
    """The one way to build the timetable: refresh (with `options`: lines,
    force, build, route, saved, limits) while holding the store's lock, so two
    builds never run at once, in this process or another, and its roads are
    found under it too. Without `wait`, TimetableBusy if another build is
    running; with it, wait for that one (see _lock). `cancelled()` stops the
    wait, and tells refresh whether finding the roads was stopped.
    TimetableError if the lock file can't be opened."""
    folder = Path(folder)
    with _lock(folder, wait, cancelled, waiting):
        return refresh(folder, stops, operators, now, api_key, cancelled=cancelled, **options)
