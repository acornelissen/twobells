"""Builds the data files the setup wizard reads, from NaPTAN (and TfL, for London).

    python tools/build_setup_data.py            # downloads about 130 MB
    python tools/build_setup_data.py --rail-xml rail.xml --stops-csv naptan.csv
    python tools/build_setup_data.py --trams-only --stops-csv area940.csv
    python tools/build_setup_data.py --london-only --stops-csv area940.csv --piers-csv area930.csv

Run it on a laptop now and then (stations open, stops move) and commit the
result. It writes, under src/twobells/data/:

- gb_stations.csv: every active National Rail station with its CRS code and
  position. NaPTAN's CSV has no CRS codes; its XML does, in each station's
  AnnotatedRailRef, so the rail area (910) is read as XML.
- naptan_area_grid.csv: which NaPTAN areas (the first three digits of an
  ATCO code) have bus stops in each 0.1 by 0.1 degree cell. Setup uses it
  to download only the areas near a home, without sending NaPTAN the
  home's position.
- metrolink_stops.csv: every active Manchester Metrolink stop, with its
  NaPTAN code (9400ZZMA and the three letters TfGM calls it by, like
  9400ZZMAGMX) and position, from the stop's own entry (StopType MET; its
  platforms are PLT). NaPTAN's tram and metro area is 940; --trams-only
  writes this file alone, from a CSV of that area
  (naptan.api.dft.gov.uk/v1/access-nodes?dataFormat=csv&atcoAreaCodes=940).
- london_stops.csv: every London Underground, DLR and London Trams stop,
  the cable car's two stations and the river buses' piers, by TfL's code
  for them (940GZZLUBNK, 930GWMR), with their kind (tube, dlr, tram,
  cable-car, river-bus), the TfL lines that call there and their position.
  Which stops and lines there are comes from TfL's Unified API (about 20
  calls, no key: each line's stops); names and positions from NaPTAN's
  entries for them, areas 940 and 930 (ferry). --london-only writes this
  file alone, from --stops-csv (area 940) and --piers-csv (area 930).

NaPTAN is published by the Department for Transport under the Open
Government Licence v3.0; every file says so in its header, and the London
list says it is powered by TfL Open Data too.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from twobells.core import safexml  # noqa: E402
from twobells.core.net import get_bytes, get_json_capped  # noqa: E402
from twobells.core.osgb import distance_m  # noqa: E402
from twobells.setup.lookups import BUS_STOP_TYPES, naptan_position  # noqa: E402
from twobells.timetable.routesetup import NAPTAN_URL  # noqa: E402

RAIL_URL = f"{NAPTAN_URL}?dataFormat=xml&atcoAreaCodes=910"
STOPS_URL = f"{NAPTAN_URL}?dataFormat=csv"
OUT = ROOT / "src" / "twobells" / "data"

_NS = {"n": "http://www.naptan.org.uk/"}
_CRS = re.compile(r"^[A-Z]{3}$")
# Local areas only: the 9xx areas are national (rail, air, ferry, tram), and
# their few bus bays are rail replacement stops at stations.
_AREA = re.compile(r"^[0-8]\d{2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# A Metrolink stop's own NaPTAN entry: 9400ZZMA and its three letters.
_METROLINK = re.compile(r"^9400ZZMA[A-Z]{3}$")
TRAMS_URL = f"{NAPTAN_URL}?dataFormat=csv&atcoAreaCodes=940"
PIERS_URL = f"{NAPTAN_URL}?dataFormat=csv&atcoAreaCodes=930"
# London's stops for the TfL modes the board shows (sources/tfl.py), by TfL's
# code: its stop area's, 940G for NaPTAN's 9400 entry and 930G for 9300.
TFL_URL = "https://api.tfl.gov.uk"
TFL_MODES = ("tube", "dlr", "tram", "cable-car", "river-bus")
_LONDON = re.compile(r"^(?:940GZZ(?:LU|DL|CR|AL)[A-Z]{3}|930G[A-Z]{3,4})$")
_LINE_ID = re.compile(r"^[a-z0-9-]{1,40}$")
_KINDS = {"LU": "tube", "DL": "dlr", "CR": "tram", "AL": "cable-car"}
# NaPTAN's names end with what they are, which the kind already says.
_LONDON_SUFFIX = re.compile(r"\s*(?:-Underground| Underground Station| DLR Station| Tram Stop)$")
# A station's NaPTAN entries (platform groups, low and high level) sit
# within a few hundred metres; further apart means a wrong CRS somewhere.
MAX_SPREAD_M = 2_000
# Entries NaPTAN tags with another station's CRS. Checked by hand:
# Newbury Park is a London Underground station; NRC is Newbury Racecourse.
WRONG_CRS = {"9100ILFENBP"}

LICENCE = [
    "Source: National Public Transport Access Nodes (NaPTAN), Department for Transport,",
    f"https://naptan.api.dft.gov.uk/ , downloaded {date.today().isoformat()}.",
    "Contains public sector information licensed under the Open Government Licence v3.0:",
    "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    "Built by tools/build_setup_data.py; rebuild rather than edit by hand.",
]


def _clean(text: str) -> str:
    return " ".join(_CONTROL.sub(" ", text).split())


def station_rows(xml: bytes) -> list[tuple[str, str, float, float]]:
    """(name, crs, lat, lon) for each active station, one row per CRS code."""
    root = safexml.fromstring(xml)
    by_crs = defaultdict(list)
    for point in root.iterfind(".//n:StopPoints/n:StopPoint", _NS):
        atco = point.findtext("n:AtcoCode", "", _NS).strip()
        crs = point.findtext(".//n:AnnotatedRailRef/n:CrsRef", "", _NS).strip()
        if (
            point.get("Status") != "active"
            or point.findtext("n:StopClassification/n:StopType", "", _NS) != "RLY"
            or not _CRS.match(crs)
            or crs.startswith("ZZ")  # placeholder codes for heritage lines
            or atco in WRONG_CRS
        ):
            continue
        where = point.find("n:Place/n:Location", _NS)
        if where is None:
            continue
        grid = where.find("n:Translation", _NS)
        grid = grid if grid is not None else where
        position = naptan_position({
            "Latitude": grid.findtext("n:Latitude", None, _NS),
            "Longitude": grid.findtext("n:Longitude", None, _NS),
            "Easting": grid.findtext("n:Easting", None, _NS) or where.findtext("n:Easting", None, _NS),
            "Northing": grid.findtext("n:Northing", None, _NS) or where.findtext("n:Northing", None, _NS),
        })
        name = _clean(point.findtext("n:Descriptor/n:CommonName", "", _NS))
        name = re.sub(r"\s+Rail Station$", "", name)
        if position and name:
            by_crs[crs].append((name, atco, *position))
    rows = []
    for crs, entries in sorted(by_crs.items()):
        spread = max(distance_m(a[2], a[3], b[2], b[3]) for a in entries for b in entries)
        if spread > MAX_SPREAD_M:
            listed = ", ".join(f"{e[1]} {e[0]}" for e in entries)
            raise SystemExit(f"{crs} is on entries {spread / 1000:.0f} km apart: {listed}. Check and add to WRONG_CRS.")
        # The plainest name ("Smethwick Galton Bridge", not "... High Level").
        name, _, lat, lon = min(entries, key=lambda e: (len(e[0]), e[1]))
        rows.append((name, crs, lat, lon))
    return rows


def area_grid(text: str) -> dict[tuple[int, int], set[str]]:
    """For each 0.1 degree cell, the NaPTAN areas with active bus stops in it."""
    cells = defaultdict(set)
    for row in csv.DictReader(io.StringIO(text)):
        area = (row.get("ATCOCode") or "")[:3]
        if row.get("Status") != "active" or row.get("StopType") not in BUS_STOP_TYPES or not _AREA.match(area):
            continue
        position = naptan_position(row)
        if position:
            cells[(math.floor(position[0] * 10), math.floor(position[1] * 10))].add(area)
    return cells


def tram_stops(text: str) -> list[tuple[str, str, float, float]]:
    """(name, code, lat, lon) for each active Metrolink stop, by code. NaPTAN
    names each "Deansgate-Castlefield (Manchester Metrolink)": the name is
    kept without the part in brackets."""
    rows = {}
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("ATCOCode") or "").strip()
        if row.get("Status") != "active" or row.get("StopType") != "MET" or not _METROLINK.match(code):
            continue
        position = naptan_position(row)
        name = re.sub(r"\s*\(Manchester Metrolink\)$", "", _clean(row.get("CommonName") or ""))
        if position and name:
            rows[code] = (name, code, *position)
    return sorted(rows.values(), key=lambda r: r[1])


def london_kind(code: str) -> str:
    """tube, dlr, tram, cable-car or river-bus, from a London stop's code."""
    return "river-bus" if code.startswith("930G") else _KINDS[code[6:8]]


def tfl_lines(get_json=None) -> dict[str, set[str]]:
    """{stop code: line ids} for every stop TfL's lines of TFL_MODES call at,
    from the Unified API: the lines, then each line's stops."""
    get = get_json or (lambda url: get_json_capped(url, None, 20_000_000, timeout=60))
    lines = get(f"{TFL_URL}/Line/Mode/{','.join(TFL_MODES)}")
    found: dict[str, set[str]] = defaultdict(set)
    for line in lines:
        line_id = str(line.get("id", ""))
        if not _LINE_ID.match(line_id):
            continue
        for stop in get(f"{TFL_URL}/Line/{line_id}/StopPoints"):
            code = str(stop.get("naptanId", ""))
            if _LONDON.match(code):
                found[code].add(line_id)
    return dict(found)


def london_stops(metro: str, piers: str, lines: dict[str, set[str]]) -> list[tuple[str, str, str, str, float, float]]:
    """(name, code, kind, lines, lat, lon) for each stop in `lines` (TfL's
    code: its lines) that NaPTAN has active, from its own entry in area 940
    (`metro`, MET) or 930 (`piers`, FER). `lines` is separated by spaces."""
    entries = {}
    for text, stop_type in ((metro, "MET"), (piers, "FER")):
        for row in csv.DictReader(io.StringIO(text)):
            atco = (row.get("ATCOCode") or "").strip()
            if row.get("Status") == "active" and row.get("StopType") == stop_type:
                entries[atco] = row
    rows = []
    for code, ids in sorted(lines.items()):
        row = entries.get(("9400" if code.startswith("940G") else "9300") + code[4:])
        if row is None:
            print(f"NaPTAN has no active entry for {code}; left out")
            continue
        position = naptan_position(row)
        name = _LONDON_SUFFIX.sub("", _clean(row.get("CommonName") or ""))
        if position and name:
            rows.append((name, code, london_kind(code), " ".join(sorted(ids)), *position))
    return rows


def write_london_stops(rows: list[tuple[str, str, str, str, float, float]], path: Path) -> None:
    out = io.StringIO()
    out.write("# London's Underground, DLR, tram, cable car and river bus stops, by TfL's codes for them.\n")
    out.write("# Which stops and lines: Powered by TfL Open Data. Contains OS data (c) Crown copyright and\n")
    out.write("# database rights 2016, and Geomni UK Map data (c) and database rights 2019.\n")
    out.writelines(f"# {line}\n" for line in LICENCE)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["name", "code", "kind", "lines", "lat", "lon"])
    writer.writerows((name, code, kind, lines, f"{lat:.5f}", f"{lon:.5f}")
                     for name, code, kind, lines, lat, lon in rows)
    path.write_text(out.getvalue(), encoding="utf-8")


def write_tram_stops(rows: list[tuple[str, str, float, float]], path: Path) -> None:
    out = io.StringIO()
    out.write("# Manchester Metrolink stops, with their NaPTAN codes (9400ZZMA and TfGM's three letters).\n")
    out.writelines(f"# {line}\n" for line in LICENCE)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["name", "code", "lat", "lon"])
    writer.writerows((name, code, f"{lat:.5f}", f"{lon:.5f}") for name, code, lat, lon in rows)
    path.write_text(out.getvalue(), encoding="utf-8")


def write_stations(rows: list[tuple[str, str, float, float]], path: Path) -> None:
    out = io.StringIO()
    out.write("# National Rail stations in Great Britain, with their CRS codes.\n")
    out.writelines(f"# {line}\n" for line in LICENCE)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["name", "crs", "lat", "lon"])
    writer.writerows((name, crs, f"{lat:.5f}", f"{lon:.5f}") for name, crs, lat, lon in rows)
    path.write_text(out.getvalue(), encoding="utf-8")


def write_grid(cells: dict[tuple[int, int], set[str]], path: Path) -> None:
    out = io.StringIO()
    out.write("# NaPTAN areas with bus stops in each cell of 0.1 by 0.1 degrees.\n")
    out.write("# lat10 and lon10 are the cell's south-west corner times ten, rounded down.\n")
    out.writelines(f"# {line}\n" for line in LICENCE)
    out.write("lat10,lon10,areas\n")
    for (lat10, lon10), areas in sorted(cells.items()):
        out.write(f"{lat10},{lon10},{' '.join(sorted(areas))}\n")
    path.write_text(out.getvalue(), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rail-xml", type=Path, help="NaPTAN XML for area 910, already downloaded")
    parser.add_argument("--stops-csv", type=Path, help="national NaPTAN CSV, already downloaded")
    parser.add_argument("--trams-only", action="store_true",
                        help="write metrolink_stops.csv alone, from --stops-csv or NaPTAN's area 940")
    parser.add_argument("--piers-csv", type=Path, help="NaPTAN CSV for area 930 (ferry), already downloaded")
    parser.add_argument("--london-only", action="store_true",
                        help="write london_stops.csv alone, from --stops-csv (area 940) and --piers-csv")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.london_only:
        metro = args.stops_csv.read_bytes() if args.stops_csv else get_bytes(TRAMS_URL, 50_000_000, timeout=120)
        piers = args.piers_csv.read_bytes() if args.piers_csv else get_bytes(PIERS_URL, 50_000_000, timeout=120)
        london = london_stops(metro.decode("utf-8-sig"), piers.decode("utf-8-sig"), tfl_lines())
        print(f"{len(london)} London stops")
        write_london_stops(london, args.out / "london_stops.csv")
        return

    if args.trams_only:
        blob = args.stops_csv.read_bytes() if args.stops_csv else get_bytes(TRAMS_URL, 50_000_000, timeout=120)
        trams = tram_stops(blob.decode("utf-8-sig"))
        print(f"{len(trams)} Metrolink stops")
        write_tram_stops(trams, args.out / "metrolink_stops.csv")
        return

    xml = args.rail_xml.read_bytes() if args.rail_xml else get_bytes(RAIL_URL, 200_000_000, timeout=120)
    stations = station_rows(xml)
    print(f"{len(stations)} stations")

    blob = args.stops_csv.read_bytes() if args.stops_csv else get_bytes(STOPS_URL, 500_000_000, timeout=300)
    text = blob.decode("utf-8-sig")
    cells = area_grid(text)
    print(f"{len(cells)} cells, {len({a for v in cells.values() for a in v})} areas")
    trams = tram_stops(text)
    print(f"{len(trams)} Metrolink stops")
    london = london_stops(text, text, tfl_lines())  # the national CSV has areas 940 and 930
    print(f"{len(london)} London stops")

    write_stations(stations, args.out / "gb_stations.csv")
    write_grid(cells, args.out / "naptan_area_grid.csv")
    write_tram_stops(trams, args.out / "metrolink_stops.csv")
    write_london_stops(london, args.out / "london_stops.csv")


if __name__ == "__main__":
    main()
