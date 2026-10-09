"""Which bus companies and lines serve your stops, for first-run setup.

BODS lists the timetable data sets with services stopping in an admin area
(the first three digits of an ATCO code). Each one is downloaded to a
temporary file in the state folder and its documents unpacked one at a time,
as for the timetable; only documents that mention one of your stops are
parsed. A big area has many data sets, some hundreds of megabytes, so the
whole search is capped by data sets, bytes and time.

Operator codes. The timetable is built from the data sets BODS files under
each operator code (routesetup.dataset_urls asks by `noc`), and that needn't
be the code inside: Glossop's 341 is filed under BNML, but its documents say
BNGN. So the operators found are the codes the data sets are filed under,
which is what [bus] operators and timetable.build_schedule need. The
documents' own codes are kept beside them, to show.
"""

from __future__ import annotations

import copy
import fcntl
import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit

from . import routesetup, timetable
from .bods import DEFAULT_BASE_URL, redact_key
from .http import get_bytes
from .routesetup import Pattern
from .settingsform import _toml_key, _toml_string
from .setup import in_uk
from .status import safe_message
from .walk import DEFAULT_STOP

log = logging.getLogger(__name__)

MAX_DATASETS = 300  # across every area; a county has a few dozen
PAGE_SIZE = 100
MAX_PAGES = 5  # per area: enough for MAX_DATASETS, even with a few bad entries
MAX_LIST_BYTES = 5_000_000  # one page of the data set list
MAX_TOTAL_BYTES = 5_000_000_000  # every download, added up
TIME_BUDGET_S = 60 * 60
MAX_NOCS = 20  # per data set

_BODS_HOST = urlsplit(DEFAULT_BASE_URL).hostname
_AREA = re.compile(r"^[0-9]{3}$")
_ATCO = re.compile(r"^[0-9]{3}[0-9A-Za-z]{1,9}$")
_NOC = re.compile(r"^[A-Za-z0-9]{2,8}$")  # as config accepts in [bus] operators
# What a bus shows: "341", "X57", "Skyline 1". Anything else is skipped, so
# a line is always safe to write into config.toml and print.
_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./+-]{0,15}$")

HttpGetBytes = Callable[[str, int], bytes]


class DiscoveryError(Exception):
    """Discovery can't finish; the text is safe to show."""


class Cancelled(DiscoveryError):
    pass


@dataclass(frozen=True)
class Dataset:
    url: str
    nocs: tuple[str, ...]  # the operator codes BODS files it under


FindDatasets = Callable[[str, str, int], list[Dataset]]  # (api_key, area, at most) -> data sets


@dataclass
class Discovery:
    operators: list[str]  # the codes data sets are filed under: [bus] operators
    lines: dict[str, list[str]]  # operator -> lines calling at your stops
    datasets_scanned: int
    journeys_found: int
    # Operator -> the codes its documents give, where they differ (BNML -> BNGN).
    document_operators: dict[str, list[str]] = field(default_factory=dict)
    stop_lines: dict[str, list[str]] = field(default_factory=dict)  # stop label -> lines calling there
    notes: list[str] = field(default_factory=list)  # what was skipped or left unsearched, safe to show
    patterns: dict[str, list[Pattern]] = field(default_factory=dict)  # line -> its patterns, if asked for


def line_order(line: str) -> tuple:
    """Numbers in numeric order, then the rest: 6, 61, 237, 341, X57."""
    return (0, int(line), "") if line.isdigit() else (1, 0, line)


# The data set list.


def _nocs(value) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    found = []
    for noc in values[:MAX_NOCS]:
        if isinstance(noc, str) and _NOC.match(noc) and noc not in found:
            found.append(noc)
    return tuple(found)


def _dataset(entry) -> Dataset | None:
    """A data set from one result of BODS's list, if its link is BODS's own."""
    if not isinstance(entry, dict):
        return None
    url = entry.get("url")
    if not isinstance(url, str) or len(url) > 500:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _BODS_HOST:
        return None
    return Dataset(url, _nocs(entry.get("noc")))


def list_datasets(api_key: str, area: str, at_most: int = MAX_DATASETS,
                  http_get: HttpGetBytes = get_bytes) -> list[Dataset]:
    """BODS's published timetable data sets with services stopping in an
    admin area: at most `at_most` of them, from at most MAX_PAGES pages.
    Pages are asked for by offset, never by the `next` link BODS sends back."""
    if not _AREA.match(area):
        raise DiscoveryError(f"{area!r} is not an admin area code")
    found: list[Dataset] = []
    for page in range(MAX_PAGES):
        if len(found) >= at_most:
            break
        query = urlencode({"adminArea": area, "status": "published", "limit": PAGE_SIZE,
                           "offset": page * PAGE_SIZE, "api_key": api_key})
        try:
            body = http_get(f"{DEFAULT_BASE_URL}/dataset/?{query}", MAX_LIST_BYTES)
        except Exception as error:  # noqa: BLE001 - re-raised without the key
            raise DiscoveryError(f"BODS data set list failed: {safe_message(redact_key(str(error)))}") from None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise DiscoveryError("BODS sent a data set list that isn't JSON") from None
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise DiscoveryError("BODS sent a data set list without results")
        found.extend(d for d in map(_dataset, results[:PAGE_SIZE]) if d is not None)
        if len(results) < PAGE_SIZE or not payload.get("next"):
            break
    return found[:at_most]


# Searching the data sets.


class _Limits:
    """Stops a search that is cancelled, or runs out of bytes or time."""

    def __init__(self, max_total_bytes: int, max_download_bytes: int, time_budget: float,
                 clock: Callable[[], float], cancelled: Callable[[], bool]):
        self._left = max_total_bytes
        self._max_total = max_total_bytes
        self.max_download = max_download_bytes
        self._budget = time_budget
        self._clock = clock
        self._deadline = clock() + time_budget
        self._cancelled = cancelled

    def check(self) -> None:
        if self._cancelled():
            raise Cancelled("Cancelled")
        if self._clock() > self._deadline:
            raise DiscoveryError(f"Gave up after {round(self._budget / 60)} minutes of searching timetables")

    def spend(self, size: int) -> None:
        self._left -= size
        if self._left < 0:
            raise DiscoveryError(f"Gave up after downloading {self._max_total // 1_000_000} MB of timetables")
        self.check()


class _MeteredFile:
    """The file a download is written to, stopping it at a limit."""

    def __init__(self, file: BinaryIO, limits: _Limits):
        self._file = file
        self._limits = limits
        self._written = 0

    def write(self, data: bytes) -> int:
        self._written += len(data)
        if self._written > self._limits.max_download:
            raise timetable.TimetableError(
                f"Timetable download larger than {self._limits.max_download} bytes; skipped")
        self._limits.spend(len(data))
        return self._file.write(data)


def metered(fetch: timetable.Download, limits: _Limits) -> timetable.Download:
    return lambda url, file: fetch(url, _MeteredFile(file, limits))


def _filed_under(dataset: Dataset, document_nocs: Iterable[str]) -> str | None:
    """The code to find this data set by: the documents' own if BODS files it
    under that too, else the first BODS gives; the documents' own only if
    BODS gives none."""
    inside = sorted(n for n in set(document_nocs) if _NOC.match(n))
    for noc in inside:
        if noc in dataset.nocs:
            return noc
    if dataset.nocs:
        return dataset.nocs[0]
    return inside[0] if inside else None


def discover_operators(
    stops: Mapping[str, str],
    api_key: str,
    scratch_dir: Path | str,
    *,
    find: FindDatasets = list_datasets,
    fetch: timetable.Download = timetable.download,
    progress: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
    with_patterns: bool = False,
    today: date | None = None,
    max_datasets: int = MAX_DATASETS,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_download_bytes: int = routesetup.MAX_DOWNLOAD_BYTES,
    time_budget: float = TIME_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> Discovery:
    """The operators and lines whose timetabled journeys call at your `stops`
    (labels to ATCO codes), searching every BODS data set in their admin
    areas. `progress(done, total)` follows the data sets; `cancelled()` is
    asked often. With `with_patterns`, each line's journey patterns are kept
    for route strips. A data set that can't be read is skipped and noted in
    `notes`, as is any beyond `max_datasets`; running out of bytes or time,
    being cancelled, or finding nothing raises DiscoveryError."""
    labels: dict[str, list[str]] = defaultdict(list)
    for label, atco in stops.items():
        if not isinstance(atco, str) or not _ATCO.match(atco):
            raise DiscoveryError(f"Stop {label}: {atco!r} is not an ATCO code")
        labels[atco].append(label)
    if not labels:
        raise DiscoveryError("Pick at least one stop")
    atcos = set(labels)
    wanted = [atco.encode() for atco in atcos]
    today = today or date.today()
    limits = _Limits(max_total_bytes, max_download_bytes, time_budget, clock, cancelled)
    fetch = metered(fetch, limits)
    Path(scratch_dir).mkdir(parents=True, exist_ok=True)

    datasets: dict[str, Dataset] = {}
    for area in sorted({atco[:3] for atco in atcos}):
        limits.check()
        # One more than will be searched, to tell whether any were left out.
        for dataset in find(api_key, area, max_datasets + 1 - len(datasets)):
            datasets.setdefault(dataset.url, dataset)
        if len(datasets) > max_datasets:
            break
    found = list(datasets.values())[:max_datasets]
    notes: list[str] = []
    if len(datasets) > max_datasets:
        notes.append(f"Only the first {max_datasets} BODS data sets were searched")

    lines: dict[str, set[str]] = defaultdict(set)
    inside: dict[str, set[str]] = defaultdict(set)
    stop_lines: dict[str, set[str]] = defaultdict(set)
    patterns: dict[str, list[Pattern]] = defaultdict(list)
    unreadable = 0
    journeys_found = 0
    progress(0, len(found))
    for done, dataset in enumerate(found, 1):
        limits.check()
        fallback = dataset.nocs[0] if dataset.nocs else ""
        try:
            # Closed whatever happens, so its temporary file goes at once.
            with closing(timetable._documents([(fallback, dataset.url)], fetch, scratch_dir)) as documents:
                for _, document in documents:
                    limits.check()
                    # Skip, unparsed, the many documents that never mention your stops.
                    if not any(code in document for code in wanted):
                        continue
                    try:
                        journeys, _ = timetable._parse_document(document, atcos, fallback)
                    except (timetable.TimetableError, routesetup.RouteSetupError) as error:
                        notes.append(f"Skipped a timetable: {safe_message(error)}")
                        continue
                    current = [j for j in journeys if _LINE.match(j.line)
                               and not (j.calendar.end and j.calendar.end < today)]
                    noc = _filed_under(dataset, (j.operator for j in current))
                    if noc is None:
                        continue
                    for journey in current:
                        lines[noc].add(journey.line)
                        if journey.operator != noc and _NOC.match(journey.operator):
                            inside[noc].add(journey.operator)
                        for call in journey.calls:
                            for label in labels.get(call.atco, ()):
                                stop_lines[label].add(journey.line)
                    journeys_found += len(current)
                    if with_patterns:
                        for line in {j.line for j in current}:
                            patterns[line].extend(routesetup.patterns(document, line))
        except DiscoveryError:
            raise
        except Exception as error:  # noqa: BLE001 - one bad data set mustn't stop the search
            log.warning("Skipping a timetable data set: %s", safe_message(error))
            unreadable += 1
            notes.append(f"Skipped a data set: {safe_message(error)}")
        progress(done, len(found))

    if not lines:
        skipped = f"; {unreadable} couldn't be read" if unreadable else ""
        raise DiscoveryError(
            f"No timetabled bus calls at your stops in {len(found)} BODS data sets{skipped}")
    return Discovery(
        operators=sorted(lines),
        lines={noc: sorted(found_lines, key=line_order) for noc, found_lines in sorted(lines.items())},
        datasets_scanned=len(found),
        journeys_found=journeys_found,
        document_operators={noc: sorted(codes) for noc, codes in sorted(inside.items())},
        stop_lines={label: sorted(stop_lines[label], key=line_order) for label in stops if stop_lines[label]},
        notes=notes,
        patterns=dict(patterns),
    )


# The setup job: the search, then the timetable, then route strips, in a
# thread, so a web page can start it and ask how it is going.

LOCK_NAME = "setup-job.lock"  # in the state folder; held while a job runs
STEP_DISCOVER = "Finding the bus companies that serve your stops"
STEP_TIMETABLE = "Building the bus timetable"
STEP_ROUTES = "Building route strips"
_NAME_LIMIT = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SetupBusy(Exception):
    """Another setup job is running for this state folder."""


def _local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


class SetupJob:
    """Finds the operators serving `stops`, builds their timetable into the
    state `folder` (so the board can use it as soon as the settings are
    saved) and, with `routes`, a route strip for each line at your stops.

    start() runs it in a thread; status() says how it is going; cancel()
    stops it at the next chunk of a download. One job at a time per state
    folder: a lock file there, held while the job runs and let go by the
    system if the process dies. It writes no settings: its result holds the
    [bus] settings to add, for the caller to show and save."""

    def __init__(
        self,
        folder: Path | str,
        stops: Mapping[str, str],
        api_key: str,
        *,
        routes: bool = False,
        walk_minutes: Mapping[str, int] | None = None,
        now: Callable[[], datetime] = _local_now,
        find: FindDatasets = list_datasets,
        find_datasets: timetable.FindDatasets = routesetup.dataset_urls,
        fetch: timetable.Download = timetable.download,
        naptan: timetable.Naptan = routesetup.naptan_stops,
        discover_options: Mapping | None = None,
    ) -> None:
        self._folder = Path(folder)
        self._stops = dict(stops)
        self._api_key = api_key
        self._routes = routes
        self._walk = dict(walk_minutes or {})
        self._now = now
        self._find = find
        self._find_datasets = find_datasets
        self._fetch = fetch
        self._naptan = naptan
        self._options = dict(discover_options or {})
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        self._status = {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}

    # Asking and telling.

    def status(self) -> dict:
        """{state: queued, running, done or failed; step; done; total;
        message; result}, a copy safe to keep."""
        with self._lock:
            return copy.deepcopy(self._status)

    def _set(self, **changes) -> None:
        with self._lock:
            self._status.update(changes)

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._status["state"] == "queued":
                self._status.update(state="failed", message="Cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        """Whether the job has finished, waiting up to `timeout` seconds."""
        thread = self._thread
        if thread is None:
            return self.status()["state"] != "queued"
        thread.join(timeout)
        return not thread.is_alive()

    def start(self) -> None:
        """Run the job in a thread. SetupBusy if another holds the folder."""
        with self._lock:
            if self._thread is not None or self._status["state"] != "queued":
                raise RuntimeError("This setup job has already been started")
            self._take_lock()
            self._status.update(state="running", step=STEP_DISCOVER)
            self._thread = threading.Thread(target=self._run, name="setup-job", daemon=True)
        self._thread.start()

    def _take_lock(self) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self._folder / LOCK_NAME, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SetupBusy("A setup search is already running for this board") from None
        self._lock_fd = fd

    def _let_go(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # Doing.

    def _run(self) -> None:
        try:
            result, message = self._steps()
        except Cancelled:
            self._set(state="failed", message="Cancelled")
        except DiscoveryError as error:
            self._set(state="failed", message=safe_message(error))
        except Exception as error:  # noqa: BLE001 - shown on the page, not a traceback
            log.warning("Setup job failed: %s", safe_message(error))
            self._set(state="failed", message=safe_message(error))
        else:
            self._set(state="done", message=message, result=result)
        finally:
            self._let_go()

    def _steps(self) -> tuple[dict, str]:
        found = discover_operators(
            self._stops, self._api_key, self._folder, find=self._find, fetch=self._fetch,
            progress=lambda done, total: self._set(done=done, total=total),
            cancelled=self._cancel.is_set, with_patterns=self._routes, today=self._now().date(), **self._options,
        )
        self._set(step=STEP_TIMETABLE, done=0, total=1)
        schedule = self._timetable(found.operators)
        self._set(done=1)
        notes = list(found.notes)
        bus: dict = {"source": "bods", "operators": list(found.operators)}
        if self._routes:
            routes = self._strips(found, notes)
            if routes:
                bus["routes"] = routes
        result = {
            "fragment": {"bus": bus},
            "lines": found.lines,
            "document_operators": found.document_operators,
            "stop_lines": found.stop_lines,
            "datasets_scanned": found.datasets_scanned,
            "journeys_found": found.journeys_found,
            "timetable_journeys": len(schedule.journeys),
            "notes": notes,
        }
        companies = "bus company" if len(found.operators) == 1 else "bus companies"
        message = (f"Found {len(found.operators)} {companies} ({', '.join(found.operators)}) and built a "
                   f"timetable of {len(schedule.journeys)} journeys")
        return result, message

    def _timetable(self, operators: list[str]):
        """Build the timetable now, as `departureboard timetable --force` does."""
        failures: list[Exception] = []
        limits = _Limits(sys.maxsize, routesetup.MAX_DOWNLOAD_BYTES, math.inf, time.monotonic, self._cancel.is_set)

        def build(*args, **options):
            try:
                return timetable.build_schedule(*args, find_datasets=self._find_datasets,
                                                fetch=metered(self._fetch, limits), naptan=self._naptan, **options)
            except Exception as error:  # noqa: BLE001 - refresh keeps the old one; the job says why
                failures.append(error)
                raise

        now = self._now()
        schedule = timetable.refresh(self._folder, self._stops, operators, now, self._api_key,
                                     force=True, build=build)
        if failures:
            if isinstance(failures[0], Cancelled):
                raise failures[0]
            raise DiscoveryError(f"The bus timetable couldn't be built: {safe_message(failures[0])}")
        if schedule is None or timetable.due(schedule, self._stops, now, operators):
            raise DiscoveryError("The bus timetable was built but couldn't be saved")  # refresh logged why
        return schedule

    def _strips(self, found: Discovery, notes: list[str]) -> dict:
        """[bus.routes] for each line at your stops: the stops before yours on
        its busiest pattern, back far enough for your walk and a few minutes."""
        lines = sorted({line for found_lines in found.stop_lines.values() for line in found_lines}, key=line_order)
        self._set(step=STEP_ROUTES, done=0, total=len(lines))
        chosen: dict[str, tuple[str, list[str]]] = {}
        for done, line in enumerate(lines, 1):
            if self._cancel.is_set():
                raise Cancelled("Cancelled")
            for label, atco in self._stops.items():
                if line not in found.stop_lines.get(label, ()):
                    continue
                minutes = self._walk.get(label, self._walk.get(DEFAULT_STOP, 0))
                try:
                    atcos, _, _ = routesetup.strip_before(found.patterns.get(line, []), atco, minutes)
                except routesetup.RouteSetupError:
                    continue  # it only starts here; try your next stop
                if len(atcos) >= 2:
                    chosen[line] = (label, atcos)
                    break
            else:
                notes.append(f"No route strip for line {line}: no bus reaches your stops from an earlier one")
            self._set(done=done)
        if not chosen:
            return {}
        try:
            places = self._naptan(sorted({atco for _, atcos in chosen.values() for atco in atcos}))
        except Exception as error:  # noqa: BLE001 - route strips are optional
            notes.append(f"No route strips: NaPTAN failed: {safe_message(error)}")
            return {}
        routes = {}
        for line, (label, atcos) in chosen.items():
            stops = [_strip_stop(places.get(atco)) for atco in atcos]
            if None in stops:
                notes.append(f"No route strip for line {line}: NaPTAN has no position for some of its stops")
                continue
            routes[line] = {"stop": label, "stops": stops}
        return routes


def fragment_toml(result: dict) -> str:
    """A finished job's [bus] settings as TOML for config.toml, with what was
    found as comments. Operator codes and lines were checked when found, so
    they can't break out of a comment; stop names are TOML-escaped."""
    bus = result["fragment"]["bus"]
    out = [f"# Found in {result['datasets_scanned']} BODS timetable data sets. Merge into config.toml's [bus]."]
    for noc, lines in result["lines"].items():
        inside = result["document_operators"].get(noc)
        said = f" (its timetables say {', '.join(inside)})" if inside else ""
        out.append(f"# {noc}: {'line' if len(lines) == 1 else 'lines'} {', '.join(lines)}{said}")
    out += [
        "[bus]",
        f"source = {_toml_string(bus['source'])}",
        f"operators = [{', '.join(_toml_string(noc) for noc in bus['operators'])}]",
    ]
    for line, route in bus.get("routes", {}).items():
        out += ["", f"[bus.routes.{_toml_key(line)}]", f"stop = {_toml_string(route['stop'])}", "stops = ["]
        out += [f"  {{ name = {_toml_string(s['name'])}, lat = {s['lat']:.6f}, lon = {s['lon']:.6f} }},"
                for s in route["stops"]]
        out.append("]")
    return "\n".join(out) + "\n"


def _strip_stop(place) -> dict | None:
    """A route strip stop from NaPTAN's (name, lat, lon), if it is usable."""
    try:
        name, lat, lon = place
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    name = " ".join(_CONTROL.sub(" ", str(name)).split())[:_NAME_LIMIT]
    if not name or not (math.isfinite(lat) and math.isfinite(lon)) or not in_uk(lat, lon):
        return None
    return {"name": name, "lat": round(lat, 6), "lon": round(lon, 6)}
