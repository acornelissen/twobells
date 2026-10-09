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

import json
import logging
import re
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit

from . import routesetup, timetable
from .bods import DEFAULT_BASE_URL, redact_key
from .http import get_bytes
from .routesetup import Pattern
from .status import safe_message

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
