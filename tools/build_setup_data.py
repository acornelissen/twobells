"""Builds the three data files the setup wizard reads, from NaPTAN.

    python tools/build_setup_data.py            # downloads about 130 MB
    python tools/build_setup_data.py --rail-xml rail.xml --stops-csv naptan.csv
    python tools/build_setup_data.py --trams-only --stops-csv area940.csv

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

NaPTAN is published by the Department for Transport under the Open
Government Licence v3.0; both files say so in their header.
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
from twobells.core.net import get_bytes  # noqa: E402
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
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

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

    write_stations(stations, args.out / "gb_stations.csv")
    write_grid(cells, args.out / "naptan_area_grid.csv")
    write_tram_stops(trams, args.out / "metrolink_stops.csv")


if __name__ == "__main__":
    main()
