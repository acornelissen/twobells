"""Static checks on the dashboard's browser files and its contract fixtures.

The page runs under `script-src 'self'; style-src 'self'`, sets feed text only
as text, and ships a vendored Leaflet pinned by hash. These tests keep it that
way without a browser.
"""

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "departureboard" / "dashboard_static"
VENDOR = STATIC / "vendor"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

OWN_FILES = [p for p in STATIC.rglob("*") if p.is_file() and VENDOR not in p.parents]
OWN_JS = [p for p in OWN_FILES if p.suffix == ".js"]


# --- index.html ---


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []  # (tag, attrs)
        self.inline_script = False
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script and data.strip():
            self.inline_script = True


def _page():
    parser = _Page()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser


def test_index_has_no_inline_script_style_or_handlers():
    page = _page()
    assert not page.inline_script
    tags = [tag for tag, _ in page.elements]
    assert "style" not in tags
    for tag, attrs in page.elements:
        assert "style" not in attrs, f"<{tag}> has a style attribute"
        handlers = [name for name in attrs if name.startswith("on")]
        assert not handlers, f"<{tag}> has {handlers}"
        if tag == "script":
            assert attrs.get("src"), "every script must be an external file"


def test_index_is_british_english():
    html = [attrs for tag, attrs in _page().elements if tag == "html"]
    assert html == [{"lang": "en-GB", "data-theme": "modern"}]


def test_index_has_one_polite_live_region():
    regions = [attrs for _, attrs in _page().elements if "aria-live" in attrs]
    assert [r["aria-live"] for r in regions] == ["polite"]


# --- References resolve to shipped files ---


def _served(url):
    """The file the server answers a same-origin URL with, per the contract."""
    if url == "/manifest.webmanifest":
        return STATIC / "manifest.webmanifest"
    assert url.startswith("/static/"), f"unexpected path {url}"
    return STATIC / url.removeprefix("/static/")


def test_index_references_resolve():
    refs = []
    for tag, attrs in _page().elements:
        for name in ("href", "src"):
            value = attrs.get(name)
            if value and not value.startswith(("#", "https://")):
                refs.append(value)
    assert "/static/main.js" in refs
    for ref in refs:
        assert _served(ref).is_file(), ref


IMPORT = re.compile(
    r"""(?:\bimport\s*\(\s*|\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?|\bexport\s+[\w*{}\s,$]+\s+from\s+)["']([^"']+)["']"""
)


def test_js_imports_resolve():
    seen = 0
    for path in OWN_JS:
        for spec in IMPORT.findall(path.read_text(encoding="utf-8")):
            assert spec.startswith("./"), f"{path.name}: bare or absolute import {spec!r}"
            target = (path.parent / spec).resolve()
            assert target.is_file(), f"{path.name}: {spec} does not exist"
            seen += 1
    assert seen > 10  # the pattern really is finding imports


def test_leaflet_loads_only_by_dynamic_import():
    for path in OWN_JS:
        text = path.read_text(encoding="utf-8")
        static = re.findall(r"""^\s*import\s+(?:[^"'();]*?\s+from\s+)?["']([^"']+)["']""", text, re.M)
        if path.name == "map.js":
            assert "./dom.js" in static  # the pattern really is finding imports
        assert not [spec for spec in static if "leaflet" in spec], f"{path.name} imports Leaflet up front"
    assert 'import("./vendor/leaflet-1.9.4/leaflet-src.esm.js")' in (STATIC / "map.js").read_text()


# --- No HTML injection sinks in our code ---

BANNED = [
    (re.compile(r"\binnerHTML\b"), "innerHTML"),
    (re.compile(r"\bouterHTML\b"), "outerHTML"),
    (re.compile(r"\binsertAdjacentHTML\b"), "insertAdjacentHTML"),
    (re.compile(r"\bdocument\.write"), "document.write"),
    (re.compile(r"\beval\s*\("), "eval("),
    (re.compile(r"\bnew\s+Function\b"), "new Function"),
    (re.compile(r"""setAttribute\(\s*["'`]style"""), "setAttribute('style'"),
    (re.compile(r"""setTimeout\(\s*["'`]"""), "string setTimeout"),
    (re.compile(r"""setInterval\(\s*["'`]"""), "string setInterval"),
]


@pytest.mark.parametrize("path", [p for p in OWN_FILES if p.suffix in {".js", ".html", ".css"}], ids=lambda p: p.name)
def test_no_html_sinks_outside_vendor(path):
    text = path.read_text(encoding="utf-8")
    found = [name for pattern, name in BANNED if pattern.search(text)]
    assert not found, f"{path.name} uses {found}"


def test_no_dev_files_shipped():
    assert not (STATIC / "dev").exists()
    assert not list(STATIC.rglob("*.map"))


# --- Manifest and icons ---


def test_manifest_is_valid_and_its_icons_exist():
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "/"
    sizes = {}
    for icon in manifest["icons"]:
        path = _served(icon["src"])
        assert path.is_file(), icon["src"]
        if icon["type"] == "image/png":
            with Image.open(path) as image:
                assert image.format == "PNG"
                assert f"{image.width}x{image.height}" == icon["sizes"]
                sizes[icon["sizes"]] = path
    assert set(sizes) == {"192x192", "512x512"}


def test_apple_touch_icon_is_180():
    with Image.open(STATIC / "icons" / "apple-touch-icon.png") as image:
        assert (image.format, image.size) == ("PNG", (180, 180))
        assert image.mode == "RGB"  # iOS shows transparency as black


# --- Vendored Leaflet ---


def test_vendor_hashes_match():
    leaflet = VENDOR / "leaflet-1.9.4"
    listed = {}
    for line in (leaflet / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name.lstrip("*")] = digest
    assert set(listed) == {"LICENSE", "leaflet-src.esm.js", "leaflet.css"}
    for name, digest in listed.items():
        assert hashlib.sha256((leaflet / name).read_bytes()).hexdigest() == digest, name
    shipped = {p.name for p in leaflet.iterdir()}
    assert shipped == set(listed) | {"SHA256SUMS"}, "a vendored file is not pinned"


# --- Colour contrast in every theme ---


def _palettes():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    palettes = {}
    for name in ("modern-light", "modern-dark", "board"):
        match = re.search(r"/\* theme: %s \*/[^{]*\{(?:[^{}]*\{)?([^{}]*)\}" % name, css)
        assert match, name
        palettes[name] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", match.group(1)))
    # The dark and board palettes override the light one.
    return {name: {**palettes["modern-light"], **tokens} for name, tokens in palettes.items()}


def _luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("muted", "surface"), ("muted", "bg"), ("accent", "surface"),
    ("accent", "bg"), ("accent-fg", "accent"), ("ok-fg", "ok-bg"), ("late-fg", "late-bg"), ("bad-fg", "bad-bg"),
    ("unk-fg", "unk-bg"), ("leave-fg", "leave-bg"), ("badge-fg", "badge-bg"), ("bad-fg", "surface"),
    ("muted", "unk-bg"),
]
OUTLINE_PAIRS = [("focus", "bg"), ("focus", "surface"), ("edge", "surface"), ("edge", "bg")]


@pytest.mark.parametrize("theme", ["modern-light", "modern-dark", "board"])
def test_theme_contrast(theme):
    tokens = _palettes()[theme]
    for pairs, need in ((TEXT_PAIRS, 4.5), (OUTLINE_PAIRS, 3.0)):
        for fg, bg in pairs:
            ratio = _contrast(tokens[fg], tokens[bg])
            assert ratio >= need, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1"


# --- The contract fixtures (docs/dashboard-api.md), for both sides to share ---


class Opt:
    """A value that may also be null."""

    def __init__(self, spec):
        self.spec = spec


class MapOf:
    """An object with free-form keys, each value matching spec."""

    def __init__(self, spec):
        self.spec = spec


ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?[+-]\d\d:\d\d$")
HHMM = re.compile(r"^\d\d:\d\d$")
ID = re.compile(r"^[0-9a-f]{16}$")
DAY = re.compile(r"^\d{4}-\d\d-\d\d$")


def at(value):
    return isinstance(value, str) and bool(ISO.match(value))


def hhmm(value):
    return isinstance(value, str) and bool(HHMM.match(value))


def row_id(value):
    return isinstance(value, str) and bool(ID.match(value))


def one_of(*choices):
    return lambda value: value in choices


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check(value, spec, path="$"):
    """Asserts value matches spec: a dict of keys, a one-item list, Opt, MapOf,
    a type, or a predicate."""
    if isinstance(spec, Opt):
        if value is not None:
            check(value, spec.spec, path)
    elif isinstance(spec, MapOf):
        assert isinstance(value, dict), f"{path} is not an object"
        for key, item in value.items():
            check(item, spec.spec, f"{path}.{key}")
    elif isinstance(spec, dict):
        assert isinstance(value, dict), f"{path} is not an object"
        assert set(value) == set(spec), f"{path} keys differ: {sorted(set(value) ^ set(spec))}"
        for key, item_spec in spec.items():
            check(value[key], item_spec, f"{path}.{key}")
    elif isinstance(spec, list):
        assert isinstance(value, list), f"{path} is not a list"
        for i, item in enumerate(value):
            check(item, spec[0], f"{path}[{i}]")
    elif isinstance(spec, type):
        assert isinstance(value, spec) and not (spec is int and isinstance(value, bool)), f"{path} is not {spec.__name__}"
    else:
        assert spec(value), f"{path} = {value!r} is not valid"


STATUS = {"last_success": Opt(at), "stale": bool, "error": Opt(str), "note": Opt(str)}
ROW_STATUS = one_of("on_time", "late", "cancelled", "unknown")
HISTORY = {"phrase": Opt(str)}
BUS_HISTORY = {"phrase": Opt(str), "no_show_phrase": Opt(str)}
# Row extras (enrich.py): when it gets there, and the bus coming to work a departure.
ARRIVES = {"name": str, "at": hhmm, "at_iso": at, "minutes": integer}
INBOUND = {"from": Opt(str), "due": hhmm, "late_min": integer, "where": str, "text": str}

CALLING_POINT = {
    "name": str, "crs": Opt(str), "scheduled": Opt(hhmm), "expected": Opt(hhmm),
    "actual": Opt(hhmm), "cancelled": bool,
}
TRAIN_ROW = {
    "id": row_id, "service": str, "destination": str, "origin": Opt(str),
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "minutes_late": Opt(integer),
    "platform": Opt(str), "length": Opt(integer),
    "cancel_reason": Opt(str), "delay_reason": Opt(str), "leave_at": Opt(at),
    "calling_points": [CALLING_POINT],
    "previous_points": [CALLING_POINT],
    "position": Opt({"where": str, "strip": {"stops": [str], "yours": integer, "train_at": Opt(number)}}),
    "history": HISTORY,
    "arrives": Opt(ARRIVES),
}
BUS_ROW = {
    "id": row_id, "line": str, "destination": str, "stop": str,
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "minutes_late": Opt(integer),
    "live": bool, "leave_at": Opt(at),
    "vehicle": Opt({"age_s": integer, "recorded_at": at, "bearing": Opt(number)}),
    "strip": Opt({"stops": [str], "yours": integer, "bus_at": Opt(number), "where": str}),
    "history": BUS_HISTORY,
    "arrives": Opt(ARRIVES),
    "inbound": Opt(INBOUND),
    "ghost": bool,
}
WEATHER = {
    "status": STATUS,
    "rain": Opt(str),
    "now": Opt({
        "temperature": number, "feels_like": Opt(number), "code": Opt(integer), "text": str,
        "wind": Opt(number), "gust": Opt(number), "direction": Opt(number), "is_day": bool,
    }),
    "hours": [{
        "start": at, "temperature": number, "probability": Opt(number), "precipitation": Opt(number),
        "code": Opt(integer), "text": str, "wind": Opt(number), "is_day": bool,
    }],
    "sun": Opt({"sunrise": at, "sunset": at}),
}
PERIOD = {"from_at": at, "to_at": Opt(at)}
DISRUPTION = {
    "id": row_id, "mode": one_of("bus", "train"), "title": str, "summary": str, "reason": Opt(str),
    "from_at": at, "to_at": Opt(at), "periods": [PERIOD], "planned": bool,
    "lines": [str], "stops": [str], "places": [str],
    "severity": one_of("slight", "normal", "severe", "unknown"),
    "link": Opt(lambda v: isinstance(v, str) and v.startswith("https://")),
}
DISRUPTIONS = {"status": {"bus": Opt(STATUS), "train": Opt(STATUS)}, "items": [DISRUPTION]}
SNAPSHOT = {
    "version": integer,
    "generated_at": at,
    "title": str,
    "walk": {"train": Opt(integer), "bus": MapOf(integer)},
    "trains": Opt({
        "station": str, "filter": Opt(str), "status": STATUS, "messages": [str], "rows": [TRAIN_ROW],
    }),
    "buses": Opt({
        "status": STATUS,
        "timetable": {
            "state": one_of("ready", "building", "missing"), "built": Opt(at),
            "valid_until": Opt(lambda v: isinstance(v, str) and bool(DAY.match(v))), "journeys": Opt(integer),
        },
        "stops": MapOf(str),
        "rows": [BUS_ROW],
    }),
    "weather": Opt(WEATHER),
    "disruptions": Opt(DISRUPTIONS),
}
BUS_DETAIL = {
    "id": row_id, "line": str, "destination": str, "stop": str,
    "stops": [{"name": str, "lat": number, "lon": number, "time": Opt(hhmm), "yours": bool}],
    "vehicle": Opt({"lat": number, "lon": number, "bearing": Opt(number), "recorded_at": at, "age_s": integer}),
    "where": Opt(str),
}
HISTORY_DETAIL = {
    "id": row_id, "mode": one_of("train", "bus"), "label": str,
    "day_type": one_of("weekday", "saturday", "sunday"), "phrase": Opt(str),
    "days": [{
        "day": lambda v: isinstance(v, str) and bool(DAY.match(v)), "status": ROW_STATUS,
        "delay_min": Opt(integer), "live": bool, "reason": Opt(str),
    }],
}


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_snapshot_fixture_follows_the_contract():
    snapshot = _fixture("dashboard_snapshot.json")
    check(snapshot, SNAPSHOT)
    trains, buses = snapshot["trains"]["rows"], snapshot["buses"]["rows"]
    assert len(trains) == 10 and len(buses) == 10
    # The cases the page has to draw are all here.
    assert any(r["status"] == "cancelled" and r["cancel_reason"] and r["leave_at"] is None for r in trains)
    assert any(r["status"] == "late" and r["delay_reason"] for r in trains)
    assert snapshot["trains"]["messages"]
    assert any(r["live"] and r["strip"] and r["strip"]["bus_at"] is not None for r in buses)
    assert any(not r["live"] and r["vehicle"] is None and r["status_text"] == "Timetable only" for r in buses)
    assert len({r["id"] for r in trains + buses}) == 20
    for row in trains + buses:
        if row["minutes_late"] is not None:
            assert row["status"] == "late"
    for row in buses:
        if row["strip"]:
            assert 0 <= row["strip"]["yours"] < len(row["strip"]["stops"])
    # Row extras: arrivals, an inbound bus, and a ghost with its history.
    assert any(r["arrives"] is None for r in trains) and any(r["arrives"] for r in trains)
    for row in trains:
        if row["status"] == "cancelled":
            assert row["arrives"] is None
    for row in trains + buses:
        if row["arrives"]:
            assert row["arrives"]["minutes"] > 0
            assert row["arrives"]["at_iso"][11:16] == row["arrives"]["at"]
    assert any(r["inbound"] for r in buses)
    assert any(r["ghost"] and r["history"]["no_show_phrase"] for r in buses)
    for row in buses:
        if row["ghost"]:
            assert not row["live"] and row["status_text"] == "Timetable only"
    # Trains: the first few have a position; the rest don't.
    assert [bool(r["position"]) for r in trains[:3]] == [True, False, True]  # the second is cancelled
    assert any(r["position"]["strip"]["train_at"] is not None for r in trains if r["position"])
    assert any(r["previous_points"] for r in trains) and not any(r["previous_points"] for r in trains[3:])
    for row in trains:
        if row["position"]:
            strip = row["position"]["strip"]
            assert 0 <= strip["yours"] < len(strip["stops"])
            if strip["train_at"] is not None:
                assert 0 <= strip["train_at"] <= len(strip["stops"]) - 1


def test_disruptions_in_the_fixture_cover_what_the_page_draws():
    snapshot = _fixture("dashboard_snapshot.json")
    items = snapshot["disruptions"]["items"]
    assert {i["mode"] for i in items} == {"bus", "train"}
    for item in items:
        assert item["periods"][0] == {"from_at": item["from_at"], "to_at": item["to_at"]}
    generated = snapshot["generated_at"]
    assert any(i["from_at"] <= generated for i in items) and any(i["from_at"] > generated for i in items)
    # A bus row the page marks: its line and stop are named by one in force at its time.
    rows = snapshot["buses"]["rows"]
    assert any(r["line"] in i["lines"] and r["stop"] in i["stops"] for i in items for r in rows)
    assert len({i["id"] for i in items}) == len(items)


def test_bus_fixture_follows_the_contract():
    bus = _fixture("dashboard_bus.json")
    check(bus, BUS_DETAIL)
    assert sum(stop["yours"] for stop in bus["stops"]) == 1
    assert bus["id"] in {r["id"] for r in _fixture("dashboard_snapshot.json")["buses"]["rows"]}


def test_history_fixture_follows_the_contract():
    history = _fixture("dashboard_history.json")
    check(history, HISTORY_DETAIL)
    days = [d["day"] for d in history["days"]]
    assert days == sorted(days, reverse=True) and len(days) <= 90


def test_shape_checker_rejects_wrong_shapes():
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["trains"]["rows"][0]["id"] = "../../etc"
    with pytest.raises(AssertionError, match="id"):
        check(snapshot, SNAPSHOT)
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["buses"]["rows"][0]["extra"] = 1
    with pytest.raises(AssertionError, match="keys differ"):
        check(snapshot, SNAPSHOT)
    snapshot = _fixture("dashboard_snapshot.json")
    snapshot["version"] = True
    with pytest.raises(AssertionError, match="version"):
        check(snapshot, SNAPSHOT)
