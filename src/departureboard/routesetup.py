"""Builds a route strip for config.toml from a BODS timetable and NaPTAN.

Run once on a laptop (`departureboard route`), not on the Pi: timetables are
large. TransXChange gives the order a line calls at its stops and the run time
between them; NaPTAN gives each stop's name and position.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
from urllib.parse import urlencode
from xml.etree import ElementTree

from .bods import DEFAULT_BASE_URL, redact_key
from .http import get_bytes

NAPTAN_URL = "https://naptan.api.dft.gov.uk/v1/access-nodes"
MAX_DOWNLOAD_BYTES = 300_000_000
MAX_XML_BYTES = 200_000_000  # one TransXChange file, uncompressed
MAX_ZIP_MEMBERS = 2_000
MAX_STOPS = 8  # including yours; more and the names get unreadable
EXTRA_MINUTES = 5  # beyond your walk, so the bus shows up before you need to leave
_DEFAULT_RUN_S = 60  # when a timing link has no run time

_NS = {"t": "http://www.transxchange.org.uk/"}
_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

HttpGetBytes = Callable[[str, int], bytes]


class RouteSetupError(Exception):
    pass


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


def _parse(xml: bytes):
    if b"<!DOCTYPE" in xml[:4096].upper():
        raise RouteSetupError("Timetable file has a DOCTYPE; refusing to parse it")
    return ElementTree.fromstring(xml)


def patterns(xml: bytes, line: str) -> list[Pattern]:
    """Every journey pattern of `line` in one TransXChange document."""
    root = _parse(xml)
    line_ids = {
        node.get("id")
        for node in root.iterfind(".//t:Services/t:Service/t:Lines/t:Line", _NS)
        if (node.findtext("t:LineName", "", _NS) or "").strip() == line
    }
    if not line_ids:
        return []
    sections = {}
    for section in root.iterfind(".//t:JourneyPatternSections/t:JourneyPatternSection", _NS):
        links = []
        for link in section.iterfind("t:JourneyPatternTimingLink", _NS):
            links.append(
                (
                    link.findtext("t:From/t:StopPointRef", "", _NS).strip(),
                    link.findtext("t:To/t:StopPointRef", "", _NS).strip(),
                    _seconds(link.findtext("t:RunTime", None, _NS)),
                )
            )
        sections[section.get("id")] = links
    journey_patterns = {}
    for service in root.iterfind(".//t:Services/t:Service", _NS):
        service_lines = {n.get("id") for n in service.iterfind("t:Lines/t:Line", _NS)}
        if not service_lines & line_ids:
            continue
        for pattern in service.iterfind(".//t:StandardService/t:JourneyPattern", _NS):
            refs = [r.text.strip() for r in pattern.iterfind("t:JourneyPatternSectionRefs", _NS) if r.text]
            journey_patterns[pattern.get("id")] = refs
    uses = Counter(
        journey.findtext("t:JourneyPatternRef", "", _NS).strip()
        for journey in root.iterfind(".//t:VehicleJourneys/t:VehicleJourney", _NS)
        if journey.findtext("t:LineRef", "", _NS).strip() in line_ids
    )
    found = []
    for pattern_id, refs in journey_patterns.items():
        links = [link for ref in refs for link in sections.get(ref, [])]
        if not links or not uses[pattern_id]:
            continue
        stops = (links[0][0],) + tuple(link[1] for link in links)
        found.append(Pattern(stops, tuple(link[2] for link in links), uses[pattern_id]))
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


def timetable_documents(blob: bytes) -> Iterator[bytes]:
    """The TransXChange XML in a download: one file, or every .xml in a zip."""
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        yield blob
        return
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".xml")]
        if len(members) > MAX_ZIP_MEMBERS:
            raise RouteSetupError(f"Timetable zip has {len(members)} files; refusing more than {MAX_ZIP_MEMBERS}")
        for member in members:
            if member.file_size > MAX_XML_BYTES:
                raise RouteSetupError(f"{member.filename} unpacks to {member.file_size} bytes; refusing")
            with archive.open(member) as handle:
                data = handle.read(MAX_XML_BYTES + 1)
            if len(data) > MAX_XML_BYTES:
                raise RouteSetupError(f"{member.filename} is larger than it claims; refusing")
            yield data


def find_patterns(documents: Iterable[bytes], line: str) -> list[Pattern]:
    tag = f"<LineName>{line}</LineName>".encode()
    # Skip the many files of a big operator that don't mention the line at all.
    return [p for doc in documents if tag in doc for p in patterns(doc, line)]


def dataset_urls(api_key: str, noc: str, line: str | None = None, http_get: HttpGetBytes = get_bytes) -> list[str]:
    """Download links for an operator's published timetable data sets; with
    `line`, only those that list it (BODS gives lines as "341" or "341|...")."""
    query = urlencode({"noc": noc, "status": "published", "limit": 100, "api_key": api_key})
    try:
        body = http_get(f"{DEFAULT_BASE_URL}/dataset/?{query}", 5_000_000)
    except Exception as error:  # noqa: BLE001 - re-raised without the key
        raise RouteSetupError(redact_key(str(error))) from None
    payload = json.loads(body)
    results = payload.get("results", [payload]) if isinstance(payload, dict) else payload
    def has_line(result: dict) -> bool:
        listed = result.get("lines")
        return line is None or not listed or any(str(entry).split("|")[0] == line for entry in listed)

    return [r["url"] for r in results if r.get("url") and has_line(r)]


def naptan_stops(atcos: Iterable[str], http_get: HttpGetBytes = get_bytes) -> dict[str, tuple[str, float, float]]:
    """Name, latitude and longitude for each stop, from the NaPTAN API (no key needed)."""
    wanted = set(atcos)
    areas = sorted({atco[:3] for atco in wanted})
    query = urlencode({"atcoAreaCodes": ",".join(areas), "dataFormat": "csv"})
    text = http_get(f"{NAPTAN_URL}?{query}", 100_000_000).decode("utf-8-sig")
    return parse_naptan(text, wanted)


def parse_naptan(text: str, wanted: set[str]) -> dict[str, tuple[str, float, float]]:
    found = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("ATCOCode") in wanted:
            found[row["ATCOCode"]] = (row["CommonName"].strip(), float(row["Latitude"]), float(row["Longitude"]))
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
