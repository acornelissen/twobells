"""Static checks on the dashboard's browser files and its contract fixtures.

The page runs under `script-src 'self'; style-src 'self'`, sets feed text only
as text, and ships a vendored Leaflet pinned by hash. These tests keep it that
way without a browser.
"""

import hashlib
import json
import re
import shutil
import subprocess
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

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node isn't installed: the page's behaviour tests need it")


def run_js(script):
    """Runs script as an ES module under node, in the static folder, with
    tests/fixtures/fake_dom.mjs giving it a document; returns what it prints
    with console.log, read as JSON."""
    prelude = f'import {{ spoken, elements, byClass }} from "{(FIXTURES / "fake_dom.mjs").as_uri()}";\n'
    done = subprocess.run([NODE, "--input-type=module", "-e", prelude + script], cwd=STATIC,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


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
    for _tag, attrs in _page().elements:
        for name in ("href", "src"):
            value = attrs.get(name)
            # /settings is the server's own settings area, not a file.
            if value and not value.startswith(("#", "https://")) and value != "/settings":
                refs.append(value)
    assert "/static/main.js" in refs
    for ref in refs:
        assert _served(ref).is_file(), ref


def test_the_header_links_to_the_settings():
    page = _page()
    header = [i for i, (tag, attrs) in enumerate(page.elements) if tag == "header"]
    main = [i for i, (tag, _) in enumerate(page.elements) if tag == "main"]
    links = [attrs for tag, attrs in page.elements[header[0]:main[0]] if tag == "a"]
    assert {"class": "top-link", "href": "/settings"} in links


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
    "id": row_id, "line": str, "destination": str, "stop": str, "stop_name": str,
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
    "timetable_changes": Opt([{"line": str, "operator": str, "from": lambda v: isinstance(v, str) and bool(DAY.match(v))}]),  # noqa: E501
}
def lat_lon(value):
    return isinstance(value, list) and len(value) == 2 and all(number(x) for x in value)


BUS_DETAIL = {
    "id": row_id, "line": str, "destination": str, "stop": str, "stop_name": str,
    "stops": [{"name": str, "lat": number, "lon": number, "time": Opt(hhmm), "yours": bool}],
    "path": [lat_lon],
    "vehicle": Opt({"lat": number, "lon": number, "bearing": Opt(number), "recorded_at": at, "age_s": integer}),
    "where": Opt(str),
}
AREA = {
    "fetched_at": at,
    "box": Opt(lambda v: isinstance(v, list) and len(v) == 4 and all(number(x) for x in v)),
    "vehicles": [{
        "line": str, "destination": str, "lat": number, "lon": number, "bearing": Opt(number),
        "recorded_at": Opt(at), "age_s": Opt(integer), "next_stop": Opt(str),
    }],
    "stops": [{"label": str, "name": str, "lat": number, "lon": number}],
    "routes": [{"line": str, "destination": str, "path": [lat_lon]}],
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
    # The road runs from the first stop to the last, through each in turn.
    stops = [[stop["lat"], stop["lon"]] for stop in bus["stops"]]
    assert bus["path"][0] == stops[0] and bus["path"][-1] == stops[-1]
    assert [point for point in bus["path"] if point in stops] == stops
    assert len(bus["path"]) > len(stops)  # and bends between them
    assert bus["id"] in {r["id"] for r in _fixture("dashboard_snapshot.json")["buses"]["rows"]}


def test_area_fixture_follows_the_contract():
    area = _fixture("dashboard_area.json")
    check(area, AREA)
    vehicles = area["vehicles"]
    assert any(v["next_stop"] for v in vehicles) and any(v["next_stop"] is None for v in vehicles)
    assert any(v["bearing"] is None for v in vehicles)
    assert {s["label"] for s in area["stops"]} == set(_fixture("dashboard_snapshot.json")["buses"]["stops"])
    ours = [[s["lat"], s["lon"]] for s in area["stops"]]
    for route in area["routes"]:  # at least two points, through one of our stops
        assert len(route["path"]) >= 2 and any(point in ours for point in route["path"])
    assert len({len(r["path"]) for r in area["routes"]}) > 1  # one follows the roads, one is straight


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
    for point in ([53.4, "-1.9"], [53.4, -1.9, 0], [53.4], {"lat": 53.4, "lon": -1.9}):
        bus = _fixture("dashboard_bus.json")
        bus["path"][1] = point
        with pytest.raises(AssertionError, match=r"path\[1\]"):
            check(bus, BUS_DETAIL)


# --- Disruptions panel ---


def test_feed_links_leave_without_a_referrer_or_an_opener():
    text = (STATIC / "disruptions.js").read_text(encoding="utf-8")
    assert 'rel: "noopener noreferrer"' in text
    assert "target:" not in text  # links open in the same tab, as links do
    # Only links the server already vetted are used, and only https ones.
    assert 'link.startsWith("https://")' in text


def test_the_disruptions_panel_is_on_the_page():
    ids = {attrs.get("id") for _, attrs in _page().elements}
    assert {"disruptions", "disruptions-heading", "disruptions-status", "disruptions-body"} <= ids


# --- The train dialog's journey line ---


def _css_rules(selector_part):
    """The declaration blocks of app.css rules whose selector mentions selector_part."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    return [body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css) if selector_part in selectors]


def test_the_train_dialog_draws_the_journey_as_a_route_line():
    js = (STATIC / "train.js").read_text(encoding="utf-8")
    assert 'class: ["calling", "journey"]' in js and _css_rules(".calling.journey")
    for cls in ("cp-rail", "cp-train", "cp-tag"):
        assert f'"{cls}"' in js and _css_rules(f".{cls}"), cls
    # The drawing is hidden from screen readers; the words carry it.
    assert '{ class: "cp-rail", "aria-hidden": "true" }' in js
    assert '{ class: "cp-train", "aria-hidden": "true" }' in js
    assert "Train is between" in js and "vh(" in js  # and at a stop, its tag (see the test below)
    assert 'passed: "passed", next: "next stop"' in js
    # The train's place along a gap goes through the CSSOM, never a style attribute.
    assert 'style.setProperty("--part"' in js
    # It doesn't move by itself.
    for body in _css_rules(".cp-") + _css_rules(".journey"):
        assert "animation" not in body and "transition" not in body


def _css_block(at_rule):
    """The rules inside an at-rule of app.css, as {selector: declarations}."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    start = css.index("{", css.index(at_rule)) + 1
    depth, end = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(css[end], 0)
        end += 1
    rules = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css[start:end - 1]):
        for selector in selectors.split(","):
            rules[selector.strip()] = rules.get(selector.strip(), "") + body
    return rules


def test_the_journey_line_keeps_its_shapes_in_forced_colours():
    # Forced colours (Windows High Contrast) drop backgrounds, which draw the
    # filled dots, the solid rail and the train; system colours draw them then.
    rules = _css_block("@media (forced-colors: active)")
    for selector in (".journey li::before", ".cp-rail", ".cp-train"):
        assert "forced-color-adjust: none" in rules[selector], selector
    hollow = rules[".journey li::before"]
    assert "background: Canvas;" in hollow and "border-color: CanvasText" in hollow
    for filled in (".journey li.cp-passed::before", ".journey li.cp-here::before"):
        assert "background: CanvasText" in rules[filled] and "border-color: CanvasText" in rules[filled]
    assert "background: Highlight" in rules[".journey li.yours::before"]  # this station
    assert "background: CanvasText" in rules[".cp-rail::before"]  # solid, against the dashed rest
    assert "border-left-color: CanvasText" in rules[".cp-rail"]
    assert "background: CanvasText" in rules[".cp-train"]
    assert "background: Highlight" in rules[".line-station.yours::after"]  # the line's own station dot


def test_the_journey_dots_follow_the_line_height():
    # Text spacing (WCAG 1.4.12) can widen the lines: a dot sits half a line
    # down wherever the browser knows the lh unit.
    assert "0.5lh" in _css_block("@supports (top: 1lh)")[".calling.journey"]


# Where the train is along its journey: the stops before this station (0, 1),
# this station (2), and the stops after (3, 4).
BEFORE = '[{ name: "Hadfield" }, { name: "Dinting" }]'
AFTER = '[{ name: "Broadbottom" }, { name: "Manchester Piccadilly" }]'
STRIP = '["Dinting", "Glossop", "Broadbottom"]'  # the strip starts one stop before this station


@needs_node
@pytest.mark.parametrize(
    "train_at, before, place",
    [
        (1, BEFORE, {"at": 2}),  # at this station
        (0, BEFORE, {"at": 1}),
        (0.5, BEFORE, {"gap": 1, "part": 0.5}),  # between Dinting and here
        (1.25, BEFORE, {"gap": 2, "part": 0.25}),
        (0.01, BEFORE, {"at": 1}),  # as good as at a stop
        (0.99, BEFORE, {"at": 2}),
        (-3, BEFORE, {"at": 1}),  # off the strip: held to its ends
        (9, BEFORE, {"at": 3}),
        (None, BEFORE, None),  # not placed
        (1, '[{ name: "Dinting" }]', {"at": 1}),  # a shorter journey before: the strip lines up on this station
        (0, "[]", None),  # the strip's first stop isn't in the journey
    ],
)
def test_the_train_is_placed_on_its_journey_from_the_strip(train_at, before, place):
    train_at = "null" if train_at is None else train_at
    found = run_js(f"""
        import {{ placeOnJourney }} from "./train.js";
        const row = {{ position: {{ where: "x", strip: {{ stops: {STRIP}, yours: 1, train_at: {train_at} }} }} }};
        console.log(JSON.stringify(placeOnJourney(row, {before}, {AFTER})));
    """)
    assert found == place


@needs_node
def test_actual_times_place_the_train_when_the_strip_cannot_or_lags():
    found = run_js("""
        import { placeOnJourney } from "./train.js";
        const before = [{ name: "Hadfield", actual: "07:01" }, { name: "Dinting", actual: "07:04" }];
        const after = [{ name: "Broadbottom", actual: "07:12" }, { name: "Manchester Piccadilly" }];
        const strip = (at) => ({ position: { strip: { stops: ["Dinting", "Glossop", "Broadbottom"], yours: 1,
                                                       train_at: at } } });
        console.log(JSON.stringify([
          placeOnJourney({}, before, []),  // no strip: just past the last stop it left
          placeOnJourney(strip(0), before, after),  // the strip lags the actual times
          placeOnJourney(strip(2), before, after),  // the strip agrees with them
          placeOnJourney({}, before, [{ name: "Broadbottom", actual: "07:12" }]),  // there: at the last stop
          placeOnJourney({}, [{ name: "Hadfield" }], [{ name: "Broadbottom", cancelled: true }]),  // nothing to go on
        ]));
    """)
    assert found == [{"gap": 1, "part": 0.5}, {"gap": 3, "part": 0.5}, {"at": 3}, {"at": 3}, None]


# The train dialog on a fake page, and a train for it: `train(at)` is the
# 07:20 from Glossop with its strip's train_at (null: not placed).
TRAIN_DIALOG = """
import { openTrain, refreshTrain } from "./train.js";
const dialog = document.createElement("dialog");
dialog.setAttribute("id", "train-dialog");
dialog.showModal = () => { dialog.open = true; };
dialog.querySelector = () => ({ focus() {} });
const title = document.createElement("h2");
title.setAttribute("id", "train-title");
const body = document.createElement("div");
body.setAttribute("id", "train-body");
dialog.append(title, body);
document.body.append(dialog);
const train = (at) => ({
  id: "0123456789abcdef", service: "Northern", scheduled: "07:20", destination: "Manchester Piccadilly",
  origin: "Hadfield", status: "on_time", status_text: "On time",
  previous_points: [{ name: "Hadfield", scheduled: "07:14", actual: "07:14" }, { name: "Dinting", scheduled: "07:17" }],
  calling_points: [{ name: "Broadbottom", scheduled: "07:24" }, { name: "Manchester Piccadilly", scheduled: "07:50" }],
  position: { where: "Left Hadfield 07:14, on time", strip: { stops: ["Dinting", "Glossop", "Broadbottom"], yours: 1, train_at: at } },
});
const journey = () => elements(body).find((el) => el.nodeName === "OL");
const said = () => journey().childNodes.map((li) => spoken(li).replace(/\\s+/g, " ").trim());
"""


@needs_node
def test_the_journey_says_where_the_train_is_once():
    found = run_js(TRAIN_DIALOG + """
        openTrain(train(1), null, "Glossop");
        const atGlossop = said();
        openTrain(train(0.5), null, "Glossop");
        console.log(JSON.stringify([atGlossop, said()]));
    """)
    at_glossop, between = found
    assert at_glossop == [
        "07:14 Hadfield passed actual 07:14",
        "07:17 Dinting passed",
        "07:20 Glossop (this station) train here on time",
        "07:24 Broadbottom",
        "07:50 Manchester Piccadilly",
    ]
    # Between stops no tag says so, so a sentence does, with where it is.
    assert between[1] == ("07:17 Dinting passed. Train is between Dinting and Glossop "
                          "(Left Hadfield 07:14, on time).")
    assert between[2] == "07:20 Glossop (this station) next stop on time"


@needs_node
def test_a_new_snapshot_updates_the_open_dialog_in_place():
    # Rebuilding it would move a screen reader's place: what's there stays,
    # and only what changed is changed.
    found = run_js(TRAIN_DIALOG + """
        openTrain(train(0.5), null, "Glossop");
        const parts = [...body.childNodes];
        const items = [...journey().childNodes];
        const first = elements(items[0]);
        const was = { part: items[1].style.getPropertyValue("--part"), said: said() };
        const moved = { ...train(1), delay_reason: "A late crew" };
        refreshTrain(new Map([[moved.id, { row: moved }]]), "Glossop");
        console.log(JSON.stringify({
          was,
          now: said(),
          part: items[1].style.getPropertyValue("--part"),
          sameParts: parts.filter((node) => body.childNodes.includes(node)).length,
          parts: parts.length,
          partsNow: body.childNodes.length,
          sameItems: journey().childNodes.every((li, i) => li === items[i]),
          firstKept: elements(journey().childNodes[0]).every((el, i) => el === first[i]),
          reasons: byClass(body, "reason").map((p) => [p.hasAttribute("hidden"), spoken(p)]),
        }));
    """)
    assert found["was"]["part"] == "0.500" and found["part"] == ""
    assert found["was"]["said"][1].startswith("07:17 Dinting passed. Train is between")
    assert found["now"][1:3] == ["07:17 Dinting passed", "07:20 Glossop (this station) train here on time"]
    assert found["sameItems"] and found["firstKept"]
    # The delay reason has its place kept for it, so nothing after it moves.
    assert found["sameParts"] == found["parts"] == found["partsNow"]
    assert found["reasons"] == [[True, ""], [False, "A late crew"]]


@needs_node
def test_patch_keeps_matching_nodes_and_replaces_the_rest():
    found = run_js("""
        import { h, patch } from "./dom.js";
        const el = h("div", {}, h("p", { class: "a", title: "old" }, "one"), h("span", {}, "two"), h("em", {}, "three"));
        const [p, span] = el.childNodes;
        const text = p.firstChild;
        patch(el, h("p", { class: "b" }, "uno"), h("ul", {}, "dos"));
        console.log(JSON.stringify([
          el.childNodes[0] === p, p.firstChild === text, text.nodeValue,
          p.getAttribute("class"), p.hasAttribute("title"),
          el.childNodes[1] !== span, el.childNodes[1].nodeName, el.childNodes.length,
        ]));
    """)
    assert found == [True, True, "uno", "b", False, True, "UL", 2]


@needs_node
def test_the_open_dialog_says_when_its_train_has_gone():
    found = run_js(TRAIN_DIALOG + """
        openTrain(train(1), null, "Glossop");
        refreshTrain(new Map(), "Glossop");
        const gone = spoken(body);
        openTrain(train(1), null, "Glossop");
        console.log(JSON.stringify([gone, said().length]));
    """)
    assert found == ["This train has left the board.", 5]


# --- Around here: the line and the area map ---


def test_the_area_map_has_real_buttons_a_credit_and_a_text_list():
    page = _page().elements
    ids = {attrs.get("id"): (tag, attrs) for tag, attrs in page}
    for button in ("area-open", "area-zoom-in", "area-zoom-out", "area-fit"):
        assert ids[button][0] == "button" and ids[button][1].get("type") == "button"
    assert ids["area-dialog"][0] == "dialog" and ids["area-map"][1].get("role") == "region"
    assert ids["area-vehicles"][0] == "ul" and ids["line-diagram"][0] == "ol"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    dialog = html[html.index('id="area-dialog"'):]
    assert "OpenStreetMap contributors" in dialog[:dialog.index("</dialog>")]


def test_every_map_credit_links_to_fix_the_map():
    # The road router's policy (routing.openstreetmap.de/about.html) asks for the
    # OpenStreetMap credit and a link to "fix the map" wherever its roads are drawn.
    # Both are plain links that open in the same tab.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    credits = re.findall(r"<p[^>]*>Map data ©.*?</p>", html)
    assert len(credits) == 3  # the page footer, the bus map and the area map
    for credit in credits:
        assert re.findall(r"<a ([^>]*)>([^<]*)</a>", credit) == [
            ('href="https://www.openstreetmap.org/copyright"', "OpenStreetMap contributors"),
            ('href="https://www.openstreetmap.org/fixthemap"', "Fix the map"),
        ]


@needs_node
def test_maps_draw_along_the_roads_or_else_straight_between_stops():
    found = run_js("""
        import { roadLine } from "./map.js";
        const stops = [[53.4438, -1.949], [53.4434, -1.9554]];
        const road = [[53.4438, -1.949], [53.4436, -1.952], [53.4434, -1.9554]];
        console.log(JSON.stringify([
          roadLine(road, stops),
          roadLine([[53.4438, -1.949], "x", [53.4436, NaN], [53.4434], null, { lat: 1, lon: 2 }, [53.4434, -1.9554]], stops),
          roadLine([[53.4438, -1.949]], stops),  // one point is no road
          roadLine("junk", stops),
          roadLine(undefined, stops),
          roadLine([[53.4438, "-1.949"], [53.4434, -1.9554]]),  // no stops to fall back on
        ]));
    """)
    stops = [[53.4438, -1.949], [53.4434, -1.9554]]
    assert found == [[[53.4438, -1.949], [53.4436, -1.952], [53.4434, -1.9554]], stops, stops, stops, stops, []]
    # Both maps draw their lines through it.
    assert "roadLine(data.path, points)" in (STATIC / "map.js").read_text(encoding="utf-8")
    assert "roadLine(route.path" in (STATIC / "area.js").read_text(encoding="utf-8")


def test_the_area_map_gives_leaflet_nodes_never_strings():
    text = (STATIC / "area.js").read_text(encoding="utf-8")
    # Leaflet parses a string as HTML: popups, tooltips and icons get DOM nodes from h().
    for call in re.findall(r"\.bind(?:Popup|Tooltip)\(\s*([^,)]+)", text):
        assert call.strip().startswith("h("), call
    # The icon is map.js's bus marker, a DOM node built with h() (checked below).
    assert re.search(r"divIcon\(\{\s*html: busPin\(", text)
    assert "attributionControl: false" in text  # the credit is our own text under the map


def test_both_maps_draw_a_bus_built_as_svg_nodes():
    map_js = (STATIC / "map.js").read_text(encoding="utf-8")
    area_js = (STATIC / "area.js").read_text(encoding="utf-8")
    # svg() is dom.js's s(), which builds with createElementNS.
    assert "document.createElementNS(SVG_NS, tag)" in (STATIC / "dom.js").read_text(encoding="utf-8")
    assert 'import { $, h, s as svg,' in map_js
    pin = map_js[map_js.index("export function busPin("):]
    pin = pin[:pin.index("\n}\n")]
    assert 'svg("svg", { class: "bus-heading"' in pin and 'return h("span", { class: "bus-pin" }' in pin
    glyph = map_js[map_js.index("function busGlyph("):map_js.index("export function busPin(")]
    assert 'svg("svg", { class: "bus-glyph"' in glyph and glyph.count('class: "bus-body"') >= 1
    # Only the bearing chevron turns, through the CSSOM; the bus stays upright.
    assert "heading.style.transform = `rotate(${bearing}deg)`" in pin
    assert pin.count("style.transform") == 1
    assert re.search(r"divIcon\(\{\s*html: busPin\(", map_js)
    assert "busPin(num(vehicle.bearing), str(vehicle.line))" in area_js
    # The old arrows are gone.
    for old in ("bus-arrow", "area-arrow", "area-pin"):
        assert old not in map_js + area_js + (STATIC / "app.css").read_text(encoding="utf-8"), old


def _css_value(selector, prop):
    """The value of prop in the app.css rule for exactly this selector."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in [s.strip() for s in selectors.split(",")]:
            found = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), body)
            if found:
                return found.group(1).strip()
    raise AssertionError(f"no {prop} for {selector}")


OSM_LAND = "#f2efe9"  # the light background of OpenStreetMap's standard tiles


def test_bus_marker_colours_stand_out_on_the_tiles():
    disc = _css_value(".bus-pin", "background")
    edge = _css_value(".bus-pin", "border").split()[-1]
    pairs = [
        (disc, OSM_LAND, 3.0),  # the disc on the map
        (edge, disc, 3.0),  # its white edge, for dark map areas
        (_css_value(".bus-body", "fill"), disc, 3.0),  # the bus on its disc
        (_css_value(".bus-dark", "fill"), _css_value(".bus-body", "fill"), 3.0),  # windows on the bus
        (_css_value(".bus-heading", "fill"), OSM_LAND, 3.0),  # the bearing chevron
        (_css_value(".bus-heading", "stroke"), _css_value(".bus-heading", "fill"), 3.0),
        (_css_value(".bus-pin-line", "color"), _css_value(".bus-pin-line", "background"), 4.5),  # the line number
    ]
    for fg, bg, need in pairs:
        assert _contrast(fg, bg) >= need, f"{fg} on {bg} is {_contrast(fg, bg):.2f}:1"
