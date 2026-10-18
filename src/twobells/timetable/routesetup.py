"""Builds a route strip for config.toml from a BODS timetable and NaPTAN.

Run once on a laptop (`twobells route`), not on the Pi: timetables are
large. TransXChange gives the order a line calls at its stops and the run time
between them; NaPTAN gives each stop's name and position.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING
from urllib.parse import urlencode
from xml.etree.ElementTree import Element

from ..core import bounded, safexml
from ..core.errors import redact_keys
from ..core.net import get_bytes
from ..sources.bods import DEFAULT_BASE_URL

if TYPE_CHECKING:
    from ..settings.installs import Install

log = logging.getLogger(__name__)

NAPTAN_URL = "https://naptan.api.dft.gov.uk/v1/access-nodes"
# The most of NaPTAN's stops for your stops' areas read. A build holds it
# beside the journeys it has kept, once it has let go of the timetables
# (tests/test_deploy.py).
MAX_NAPTAN_BYTES = 100_000_000
MAX_DOWNLOAD_BYTES = 300_000_000
# One TransXChange file, uncompressed, unless an install reads less (see
# DocumentLimits).
MAX_XML_BYTES = 200_000_000
# The most elements in one part of a document, which is built whole: a
# journey, a service, a section of its journey patterns, a stop and so on
# (safexml.iter_grandchildren). The largest in the fixtures, a section of
# BNML's 237, has 395; a part of empty elements, 4 bytes each, would hold
# about 100 bytes each in memory, so this is 10 MB at most.
MAX_PART_ELEMENTS = 100_000
# Journeys calling at your stops a build holds, unless an install holds fewer.
MAX_JOURNEYS = 20_000
# Zip-bomb guards; a zip's files are read one at a time, so these bound the
# work, not the memory. Every file in one download, uncompressed: the largest
# seen, BODS data set 2100 (First South Yorkshire, 2026), is 1.29 GB from a
# 39 MB download, so this is three times that, a few minutes' unpacking on a
# Pi Zero.
MAX_UNZIPPED_BYTES = 4_000_000_000
# Files one zip lists, whatever they are, and the bytes of its list: zipfile
# holds every one listed before any is read, so these bound the memory, and
# are checked first (bounded.open_zip). The most seen, in nine data sets of
# BNML's and HIPK's (2026), is 814 files in 89 KB of list (data set 2100 has
# 159); a list takes about 110 bytes a file.
MAX_ZIP_MEMBERS = 5_000
MAX_ZIP_LIST_BYTES = 1_000_000
# A line of NaPTAN's CSV: real ones are under 600 characters.
MAX_NAPTAN_LINE = 4096
# The most one file in a zip may unpack to, as a multiple of what it takes of
# the zip: by its header and as it is unpacked. Data set 2100's files are at
# most 52 times; deflate can reach about 1,000.
MAX_RATIO = 200
_UNZIP_CHUNK = 65_536
MAX_STOPS = 8  # including yours; more and the names get unreadable
EXTRA_MINUTES = 5  # beyond your walk, so the bus shows up before you need to leave
_DEFAULT_RUN_S = 60  # when a timing link has no run time

_NS = {"t": "http://www.transxchange.org.uk/"}
_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

HttpGetBytes = Callable[[str, int], bytes]


class RouteSetupError(Exception):
    pass


@dataclass(frozen=True)
class DocumentLimits:
    """The most of one TransXChange document read, to look for your stops
    in, and parsed if it mentions them; a bigger one is skipped, the rest
    still read. Unpacking one from a zip can hold it twice for a moment;
    parsing one holds it with up to as much again for what is read from it
    (build reads it an element at a time, never as a whole tree), beside
    the journeys found: at most `journeys` calling at your stops, from it and
    the documents before, or the build fails as it finds one more, and half
    as many inbound-only ones, the rest left out (build.inbound_limit). Each
    install's are in its table (settings/installs.py): a Pi's fit its units'
    MemoryMax=256M."""

    read: int = MAX_XML_BYTES
    parse: int = MAX_XML_BYTES
    journeys: int = MAX_JOURNEYS


DEFAULT_LIMITS = DocumentLimits()  # the timetable's own, as a desktop's are


def document_limits(install: Install) -> DocumentLimits:
    """The DocumentLimits of an install, from its table."""
    return DocumentLimits(install.timetable_read_bytes, install.timetable_parse_bytes, install.timetable_journeys)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NAME_LIMIT = 120


def too_big(name: str, limit: int, size: int | None = None) -> str:
    """What to say of a timetable file skipped for its size: `size` if it is
    known, else only that it is over `limit`. A zip's names are its maker's,
    so they are cut short and lose their control characters."""
    shown = _CONTROL.sub("?", name)[:_NAME_LIMIT]
    known = f"{size:,} bytes, " if size is not None else ""
    return f"Skipped {shown}: {known}more than the {limit:,} bytes this install can read"


def log_skipped(message: str) -> None:
    log.warning("%s", message)


@dataclass(frozen=True)
class Pattern:
    """One way a line runs: its stops in order, the run time to each next stop,
    and how many timetabled journeys use it."""

    stops: tuple[str, ...]
    run_seconds: tuple[int, ...]
    journeys: int


def _seconds(duration: str | None) -> int:
    match = _DURATION.match((duration or "").strip())
    if not match:
        return _DEFAULT_RUN_S
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


_TXC = "{" + _NS["t"] + "}"  # as in an element's tag
_SECTIONS, _SERVICES, _JOURNEYS = (_TXC + name for name in ("JourneyPatternSections", "Services", "VehicleJourneys"))
_CHUNK = 64 * 1024  # bytes of a document parsed at a time


def _elements(xml: bytes, sections: frozenset[str]) -> Iterator[tuple[str, Element]]:
    """safexml.iter_grandchildren over a document held in memory."""
    chunks = (xml[start : start + _CHUNK] for start in range(0, len(xml), _CHUNK))
    try:
        yield from safexml.iter_grandchildren(chunks, sections, max_elements=MAX_PART_ELEMENTS)
    except safexml.DoctypeError:
        raise RouteSetupError("Timetable file has a DOCTYPE; refusing to parse it") from None
    except safexml.TooManyElements:
        raise RouteSetupError(f"Timetable file has a part of more than {MAX_PART_ELEMENTS:,} elements; "
                              "refusing to parse it") from None


def patterns(xml: bytes, line: str) -> list[Pattern]:
    """Every journey pattern of `line` in one TransXChange document. It is
    read an element at a time (safexml.iter_grandchildren), never as a whole
    tree, which took about 8 times the document: the setup search reads it in
    a Pi's fetch service. Its sections, services and journeys can come in any
    order, so what each says is noted and the patterns found at the end."""
    line_ids = set()
    sections = {}
    services = []  # each service's line ids, and its journey patterns' (id, section refs)
    uses = Counter()  # journeys, by (LineRef, JourneyPatternRef)
    for section, node in _elements(xml, frozenset({_SECTIONS, _SERVICES, _JOURNEYS})):
        if section == _SECTIONS and node.tag == _TXC + "JourneyPatternSection":
            links = []
            for link in node.iterfind("t:JourneyPatternTimingLink", _NS):
                links.append(
                    (
                        link.findtext("t:From/t:StopPointRef", "", _NS).strip(),
                        link.findtext("t:To/t:StopPointRef", "", _NS).strip(),
                        _seconds(link.findtext("t:RunTime", None, _NS)),
                    )
                )
            sections[node.get("id")] = links
        elif section == _SERVICES and node.tag == _TXC + "Service":
            lines = list(node.iterfind("t:Lines/t:Line", _NS))
            line_ids.update(n.get("id") for n in lines if (n.findtext("t:LineName", "", _NS) or "").strip() == line)
            services.append((
                {n.get("id") for n in lines},
                [(pattern.get("id"), [r.text.strip() for r in pattern.iterfind("t:JourneyPatternSectionRefs", _NS)
                                      if r.text])
                 for pattern in node.iterfind(".//t:StandardService/t:JourneyPattern", _NS)],
            ))
        elif section == _JOURNEYS and node.tag == _TXC + "VehicleJourney":
            line_ref = node.findtext("t:LineRef", "", _NS).strip()
            uses[(line_ref, node.findtext("t:JourneyPatternRef", "", _NS).strip())] += 1
    if not line_ids:
        return []
    journey_patterns = {}
    for service_lines, service_patterns in services:
        if service_lines & line_ids:
            journey_patterns.update(service_patterns)
    used = Counter()
    for (line_ref, pattern_ref), count in uses.items():
        if line_ref in line_ids:
            used[pattern_ref] += count
    found = []
    for pattern_id, refs in journey_patterns.items():
        links = [link for ref in refs for link in sections.get(ref, [])]
        if not links or not used[pattern_id]:
            continue
        stops = (links[0][0], *tuple(link[1] for link in links))
        found.append(Pattern(stops, tuple(link[2] for link in links), used[pattern_id]))
    return found


def strip_before(found: Iterable[Pattern], atco: str, walk_minutes: int) -> tuple[list[str], int, int]:
    """Stops before yours on the busiest pattern through it, back far enough to
    cover your walk plus a few minutes. Returns (stops ending with yours,
    seconds covered, journeys using the pattern)."""
    through = [p for p in found if atco in p.stops[1:]]
    if not through:
        raise RouteSetupError(f"No timetabled journey of this line calls at {atco} after its first stop")
    best = max(through, key=lambda p: p.journeys)
    index = best.stops.index(atco, 1)
    wanted = (walk_minutes + EXTRA_MINUTES) * 60
    covered, start = 0, index
    while start > 0 and index - start < MAX_STOPS - 1 and covered < wanted:
        start -= 1
        covered += best.run_seconds[start]
    return list(best.stops[start : index + 1]), covered, best.journeys


class _CountedReads:
    """A zip download that counts the bytes read from it: how much of it
    unpacking a file really took, whatever the file's header says."""

    def __init__(self, file: IO[bytes]):
        self._file = file
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        self.count += len(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        return self._file.tell()

    def seekable(self) -> bool:
        return True


def _bomb(name: str) -> RouteSetupError:
    shown = _CONTROL.sub("?", name)[:_NAME_LIMIT]
    return RouteSetupError(f"Timetable zip file {shown} unpacks to more than {MAX_RATIO} times its size; refusing")


# How a timetable zip may pack its files. zipfile unpacks bzip2 and LZMA a
# whole read at a time, with no cap on what comes out, so _unpacked's ratio
# check would come after a bomb had filled the memory; timetables are stored
# or deflated.
_PACKINGS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


def _odd_packing(name: str) -> RouteSetupError:
    shown = _CONTROL.sub("?", name)[:_NAME_LIMIT]
    return RouteSetupError(f"Timetable zip file {shown} is packed in a way timetables never are; refusing")


def _unpacked(archive: zipfile.ZipFile, member: zipfile.ZipInfo, reads: _CountedReads, max_bytes: int) -> bytes:
    """A file in a zip, up to one byte more than `max_bytes`, unpacked a
    chunk at a time and refused (_bomb) once it has come to more than
    MAX_RATIO times what was read of the zip for it. zipfile reads at most a
    chunk ahead, so a bomb is stopped within MAX_RATIO chunks."""
    chunks = []
    size = 0
    with archive.open(member) as handle:
        start = reads.count  # after its local header
        while size <= max_bytes:
            chunk = handle.read(min(_UNZIP_CHUNK, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RATIO * (reads.count - start):
                raise _bomb(member.filename)
    return b"".join(chunks)


def _read_up_to(file: IO[bytes], max_bytes: int) -> bytes:
    """A file, up to one byte more than `max_bytes`, read a chunk at a time:
    one read of that many would take room for them all, however small the
    file."""
    chunks = []
    size = 0
    while size <= max_bytes:
        chunk = file.read(min(_UNZIP_CHUNK, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def named_documents(blob: bytes | IO[bytes], max_bytes: int = MAX_XML_BYTES, *, name: str = "the timetable file",
                    skipped: Callable[[str], None] = log_skipped) -> Iterator[tuple[str, bytes]]:
    """(name, XML) for the TransXChange in a download: one file, called
    `name`, or every .xml in a zip by its own name, unpacked one at a time.
    The download can be bytes or an open file, so a big one can wait on disk
    rather than in memory. A file over `max_bytes` is skipped, told to
    `skipped` (too_big; logged unless it says otherwise), and the rest read;
    a zip of too many files, too much in all, a file packed other than
    stored or deflated, or one unpacking to more than MAX_RATIO times its
    size, is refused."""
    source = io.BytesIO(blob) if isinstance(blob, (bytes, bytearray)) else blob
    if not zipfile.is_zipfile(source):
        if isinstance(blob, (bytes, bytearray)):
            yield name, bytes(blob)
            return
        source.seek(0)
        data = _read_up_to(source, max_bytes)
        if len(data) > max_bytes:
            skipped(too_big(name, max_bytes))
            return
        yield name, data
        return
    reads = _CountedReads(source)
    try:
        archive = bounded.open_zip(reads, MAX_ZIP_MEMBERS, MAX_ZIP_LIST_BYTES)
    except bounded.ZipTooBig:
        raise RouteSetupError(f"Timetable zip lists more than {MAX_ZIP_MEMBERS} files; refusing") from None
    with archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".xml")]
        total = 0
        for member in members:
            if member.compress_type not in _PACKINGS:
                raise _odd_packing(member.filename)
            if member.file_size > max_bytes:
                skipped(too_big(member.filename, max_bytes, member.file_size))
                continue
            if member.file_size > MAX_RATIO * max(member.compress_size, 1):
                raise _bomb(member.filename)
            data = _unpacked(archive, member, reads, max_bytes)
            total += len(data)
            if total > MAX_UNZIPPED_BYTES:
                raise RouteSetupError(f"Timetable zip unpacks to more than {MAX_UNZIPPED_BYTES} bytes; refusing")
            if len(data) > max_bytes:  # larger than it claims
                skipped(too_big(member.filename, max_bytes))
                continue
            yield member.filename, data


def timetable_documents(blob: bytes | IO[bytes], max_bytes: int = MAX_XML_BYTES, *, name: str = "the timetable file",
                        skipped: Callable[[str], None] = log_skipped) -> Iterator[bytes]:
    """named_documents without the names."""
    for _, data in named_documents(blob, max_bytes, name=name, skipped=skipped):
        yield data


def downloaded_documents(
    urls: Iterable[str], http_get: HttpGetBytes = get_bytes, announce: Callable[[str], None] = lambda url: None
) -> Iterator[bytes]:
    """The timetable documents behind each download link, fetched only when the
    previous download's documents have been used, so one is held at a time."""
    for url in urls:
        announce(url)
        yield from timetable_documents(http_get(url, MAX_DOWNLOAD_BYTES))


def find_patterns(documents: Iterable[bytes], line: str) -> list[Pattern]:
    tag = f"<LineName>{line}</LineName>".encode()
    # Skip the many files of a big operator that don't mention the line at all.
    return [p for doc in documents if tag in doc for p in patterns(doc, line)]


# BODS lists data sets 100 at a time; a big operator has more, but not endlessly many.
_DATASET_PAGE = 100
_DATASET_PAGES = 5


def dataset_urls(api_key: str, noc: str, line: str | None = None, http_get: HttpGetBytes = get_bytes) -> list[str]:
    """Download links for an operator's published timetable data sets; with
    `line`, only those that list it (BODS gives lines as "341" or "341|...")."""
    results = []
    for page in range(_DATASET_PAGES):
        query = urlencode({"noc": noc, "status": "published", "limit": _DATASET_PAGE,
                           "offset": page * _DATASET_PAGE, "api_key": api_key})
        try:
            body = http_get(f"{DEFAULT_BASE_URL}/dataset/?{query}", 5_000_000)
        except Exception as error:  # noqa: BLE001 - re-raised without the key
            raise RouteSetupError(redact_keys(str(error))) from None
        payload = json.loads(body)
        found = payload.get("results", [payload]) if isinstance(payload, dict) else payload
        results.extend(found)
        if len(found) < _DATASET_PAGE:
            break

    def has_line(result: dict) -> bool:
        listed = result.get("lines")
        return line is None or not listed or any(str(entry).split("|")[0] == line for entry in listed)

    return [r["url"] for r in results if r.get("url") and has_line(r)]


def naptan_stops(atcos: Iterable[str], http_get: HttpGetBytes = get_bytes,
                 with_indicator: bool = False) -> dict[str, tuple]:
    """Name, latitude and longitude for each stop, from the NaPTAN API (no key
    needed). The response is decoded a little at a time as it is read, never
    copied whole: as one text, read as a file, it took five times its size
    again."""
    wanted = set(atcos)
    areas = sorted({atco[:3] for atco in wanted})
    query = urlencode({"atcoAreaCodes": ",".join(areas), "dataFormat": "csv"})
    body = http_get(f"{NAPTAN_URL}?{query}", MAX_NAPTAN_BYTES)
    return _naptan_rows(io.TextIOWrapper(io.BytesIO(body), encoding="utf-8-sig", newline=""), wanted, with_indicator)


def parse_naptan(text: str, wanted: set[str], with_indicator: bool = False) -> dict[str, tuple]:
    """Name, latitude and longitude for each wanted stop; with_indicator adds
    the letter or word on its sign ("Stop A", "Opp"), "" when there's none."""
    return _naptan_rows(io.StringIO(text), wanted, with_indicator)


def _naptan_rows(lines: IO[str], wanted: set[str], with_indicator: bool) -> dict[str, tuple]:
    """parse_naptan over a file of NaPTAN's CSV, a line at a time: one longer
    than MAX_NAPTAN_LINE is left out, never held whole."""
    found = {}
    for row in csv.DictReader(bounded.lines(lines, MAX_NAPTAN_LINE)):
        if row.get("ATCOCode") in wanted:
            place = (row["CommonName"].strip(), float(row["Latitude"]), float(row["Longitude"]))
            found[row["ATCOCode"]] = (*place, (row.get("Indicator") or "").strip()) if with_indicator else place
    return found


def route_toml(line: str, label: str, atcos: list[str], stops: dict[str, tuple[str, float, float]], note: str) -> str:
    missing = [a for a in atcos if a not in stops]
    if missing:
        raise RouteSetupError(f"NaPTAN has no position for {', '.join(missing)}")
    rows = [
        f"  {{ name = {json.dumps(stops[a][0])}, lat = {stops[a][1]:.6f}, lon = {stops[a][2]:.6f} }},  # {a}"
        for a in atcos
    ]
    return "\n".join(
        [f"# {note}", f'[bus.routes."{line}"]', f"stop = {json.dumps(label)}", "stops = [", *rows, "]", ""]
    )


def naptan_stop_details(atcos: Iterable[str]) -> dict[str, tuple]:
    """naptan_stops with each stop's sign letter, as the bus timetable keeps it."""
    return naptan_stops(atcos, with_indicator=True)
