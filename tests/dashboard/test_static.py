
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

import pytest
from PIL import Image

from tests.paths import FIXTURES, ROOT

STATIC = ROOT / "src" / "departureboard" / "dashboard_static"
VENDOR = STATIC / "vendor"

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


def test_vendored_inter_is_pinned_and_licensed():
    inter = VENDOR / "inter-4.1"
    listed = {}
    for line in (inter / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name] = digest
    assert set(listed) == {"InterVariable.woff2", "LICENSE"}
    for name, digest in listed.items():
        assert hashlib.sha256((inter / name).read_bytes()).hexdigest() == digest, name
    assert {p.name for p in inter.iterdir()} == set(listed) | {"SHA256SUMS"}, "a vendored file is not pinned"
    assert "SIL Open Font License, Version 1.1" in (inter / "LICENSE").read_text()


def test_the_page_uses_the_vendored_inter_with_tabular_times():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    face = re.search(r"@font-face\s*\{([^}]*)\}", css)
    assert face, "no @font-face"
    assert 'font-family: "Inter"' in face.group(1) and "font-display: swap" in face.group(1)
    assert 'url("/static/vendor/inter-4.1/InterVariable.woff2") format("woff2")' in face.group(1)
    assert _served("/static/vendor/inter-4.1/InterVariable.woff2").is_file()
    assert re.search(r'--font:\s*"Inter"', css)
    assert re.search(r"--tnum:\s*tabular-nums", css) or "font-variant-numeric: tabular-nums" in css


# The type scale's sizes of 20px and up: --t-lg is 1.25rem, the others larger.
LARGE_SIZES = {"var(--t-lg)", "var(--t-xl)", "var(--t-hero)"}


def test_the_board_look_uses_the_dot_face_only_at_20px_and_up():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    dots = [face for face in faces if 'font-family: "Departureboard Dots"' in face]
    assert len(dots) == 1 and "font-display: swap" in dots[0]
    assert 'url("/static/fonts/DepartureboardDots.woff2") format("woff2")' in dots[0]
    assert _served("/static/fonts/DepartureboardDots.woff2").is_file()
    assert re.search(r'--dots:\s*"Departureboard Dots", "Inter"', css)
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*var\(--dots\)[^{}]*)\}", bare)
    assert len(rules) >= 3
    for selectors, body in rules:
        assert all(s.strip().startswith(':root[data-theme="board"]') for s in selectors.split(",")), selectors
        size = re.search(r"font-size:\s*([^;]+);", body)
        assert size and size.group(1).strip() in LARGE_SIZES, selectors
    assert re.search(r"--font:\s*\"Inter\"", css) and "monospace" not in css


def test_the_board_look_has_no_fake_led_effects():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    board = "".join(re.findall(r':root\[data-theme="board"\][^{]*\{[^}]*\}', css))
    assert board
    for effect in ("radial-gradient", "letter-spacing: 0", "text-shadow", "animation", "repeating-linear-gradient"):
        assert effect not in board, effect


# --- Icons ---


@needs_node
def test_icons_are_hidden_svg_drawn_in_the_text_colour():
    found = run_js("""
        import { icon, ICONS } from "./icons.js";
        import { weatherIcon } from "./weather.js";
        const drawn = ICONS.map((name) => {
          const el = icon(name);
          return [el.nodeName, el.getAttribute("aria-hidden"), el.getAttribute("stroke"), el.childNodes.length];
        });
        const weather = [];
        for (let code = 0; code < 100; code++) for (const day of [true, false]) weather.push(weatherIcon(code, day));
        console.log(JSON.stringify({ names: ICONS, drawn, weather, missing: icon("no-such-icon") }));
    """)
    assert len(found["names"]) >= 20 and found["missing"] is None
    assert all(d[:3] == ["svg", "true", "currentColor"] and d[3] > 0 for d in found["drawn"])
    assert {w for w in found["weather"] if w is not None} <= set(found["names"])
    assert "sun" in found["weather"] and "moon" in found["weather"] and "cloud-rain" in found["weather"]


@needs_node
def test_a_status_shown_short_is_said_in_full():
    found = run_js("""
        import { statusBadge } from "./board.js";
        const shown = (node) => byClass(node, "badge-text").map((el) => el.childNodes.map((c) => c.nodeValue).join(""));
        const badges = [statusBadge("late", "Exp 07:19", "Expected 07:19"), statusBadge("on_time", "On time", "On time"),
                        statusBadge("late", "Delayed")];
        console.log(JSON.stringify(badges.map((b) => [shown(b), spoken(b)])));
    """)
    assert found == [
        [["Exp 07:19"], "Expected 07:19"],
        [["On time"], "On time"],
        [["Delayed"], "Delayed"],
    ]


def test_every_icon_the_page_asks_for_is_in_the_set():
    names = set(re.findall(r'^  "([a-z-]+)": \[', (STATIC / "icons.js").read_text(encoding="utf-8"), re.M))
    asked = set()
    for path in OWN_JS:
        text = path.read_text(encoding="utf-8")
        for call in re.findall(r"\bicon\(([^()]*)\)", text):
            asked.update(re.findall(r'"([a-z-]+)"', call))
    # The status, connection and severity tables name their icons too.
    js = {path.name: path.read_text(encoding="utf-8") for path in OWN_JS}
    tables = [
        ("board.js", "ICON", r': "([a-z-]+)"'),  # status -> icon
        ("main.js", "CONNECTION", r'\["([a-z-]+)", "'),  # [icon, words]
        ("disruptions.js", "SEVERITY", r', "([a-z-]+)"\],'),  # [words, class, icon]
    ]
    for name, table, entry in tables:
        body = re.search(r"const %s = \{(.*?)\};" % table, js[name], re.S).group(1)
        found = re.findall(entry, body)
        assert found, table
        asked.update(found)
    assert asked and asked <= names, asked - names


PICTOGRAPHS = re.compile("[←-⇿⌀-⏿①-⓿■-➿⬀-⯿︎️]")


def test_no_text_glyphs_stand_in_for_icons():
    # They fall back to whatever font has them, so they come out at odd sizes.
    for path in [*OWN_JS, STATIC / "index.html"]:
        found = PICTOGRAPHS.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name}: {found}"


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
    # The boards: late words and the live mark straight on the surface, and
    # a row's text on its hover tint.
    ("late-fg", "surface"), ("ok-fg", "surface"), ("fg", "surface-2"), ("muted", "surface-2"),
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
# Row extras (dashboard/enrich.py): when it gets there, and the bus coming to work a departure.
ARRIVES = {"name": str, "at": hhmm, "at_iso": at, "minutes": integer}
INBOUND = {"from": Opt(str), "due": hhmm, "late_min": integer, "where": str, "text": str}

CALLING_POINT = {
    "name": str, "crs": Opt(str), "scheduled": Opt(hhmm), "expected": Opt(hhmm),
    "actual": Opt(hhmm), "cancelled": bool,
}
TRAIN_ROW = {
    "id": row_id, "service": str, "destination": str, "origin": Opt(str),
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "status_spoken": str, "minutes_late": Opt(integer),
    "platform": Opt(str), "length": Opt(integer),
    "cancel_reason": Opt(str), "delay_reason": Opt(str), "walk_min": Opt(integer), "leave_at": Opt(at),
    "calling_points": [CALLING_POINT],
    "previous_points": [CALLING_POINT],
    "position": Opt({"where": str, "strip": {"stops": [str], "yours": integer, "train_at": Opt(number)}}),
    "history": HISTORY,
    "arrives": Opt(ARRIVES),
}
# One of your stops, by label: what tells it from another of the same name, and its walk.
BUS_STOP = {"name": Opt(str), "letter": Opt(str), "towards": Opt(str), "walk_min": Opt(integer)}
BUS_ROW = {
    "id": row_id, "line": str, "destination": str, "stop": str, "stop_name": str,
    "scheduled": hhmm, "scheduled_at": at, "expected": Opt(hhmm), "expected_at": Opt(at),
    "status": ROW_STATUS, "status_text": str, "status_spoken": str, "minutes_late": Opt(integer),
    "live": bool, "walk_min": Opt(integer), "leave_at": Opt(at),
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
MESSAGE = {
    "text": str,
    "category": Opt(one_of("train", "station", "connections", "system", "misc", "prior_trains", "prior_other")),
    "severity": Opt(one_of("normal", "minor", "major", "severe")),
    "kind": one_of("station", "service"),
}
DISRUPTION = {
    "id": row_id, "mode": one_of("bus", "train"), "title": str, "summary": str, "reason": Opt(str),
    "from_at": at, "to_at": Opt(at), "periods": [PERIOD], "planned": bool,
    "lines": [str], "stops": [str], "places": [str],
    "severity": one_of("slight", "normal", "severe", "unknown"),
    "relevance": one_of("your_stop", "nearby", "line"),
    "link": Opt(lambda v: isinstance(v, str) and v.startswith("https://")),
}
DISRUPTIONS = {"status": {"bus": Opt(STATUS), "train": Opt(STATUS)}, "items": [DISRUPTION]}
SNAPSHOT = {
    "version": integer,
    "generated_at": at,
    "title": str,
    "walk": {"train": Opt(integer), "bus": MapOf(integer)},
    "trains": Opt({
        "station": str, "filter": Opt(str), "status": STATUS, "messages": [MESSAGE], "rows": [TRAIN_ROW],
    }),
    "buses": Opt({
        "status": STATUS,
        "timetable": {
            "state": one_of("ready", "building", "missing"), "built": Opt(at),
            "valid_until": Opt(lambda v: isinstance(v, str) and bool(DAY.match(v))), "journeys": Opt(integer),
        },
        "stops": MapOf(BUS_STOP),
        "rows": [BUS_ROW],
    }),
    "weather": Opt(WEATHER),
    "disruptions": Opt(DISRUPTIONS),
    "timetable_changes": Opt([{"line": str, "operator": str, "from": lambda v: isinstance(v, str) and bool(DAY.match(v))}]),
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
    # A disruption message and a station notice, which the page shows differently.
    assert {m["kind"] for m in snapshot["trains"]["messages"]} == {"service", "station"}
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
    # Every row has its walk; the two Henry Street stops are told apart.
    stops = snapshot["buses"]["stops"]
    assert all(r["walk_min"] == snapshot["walk"]["train"] for r in trains)
    assert all(r["walk_min"] == stops[r["stop"]]["walk_min"] for r in buses)
    assert stops["A"]["name"] == stops["B"]["name"] and stops["A"]["letter"] != stops["B"]["letter"]
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
    assert {i["relevance"] for i in items} >= {"your_stop", "line"}


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


@needs_node
def test_disruptions_are_listed_most_relevant_first():
    found = run_js("""
        import { renderDisruptions } from "./disruptions.js";
        for (const id of ["disruptions", "disruptions-status", "disruptions-body"]) {
          const el = document.createElement("div");
          el.setAttribute("id", id);
          document.body.append(el);
        }
        const item = (id, relevance, from) => ({
          id, mode: "bus", title: id, summary: "", reason: null, planned: true, lines: [], stops: [], places: [],
          severity: "normal", relevance, link: null, periods: [{ from_at: from, to_at: null }],
        });
        renderDisruptions({ generated_at: "2026-10-10T07:00:00+01:00", disruptions: { status: {}, items: [
          item("aaaaaaaaaaaaaaaa", "line", "2026-01-01T00:00:00+00:00"),
          item("bbbbbbbbbbbbbbbb", "nearby", "2026-02-01T00:00:00+00:00"),
          item("cccccccccccccccc", "your_stop", "2026-03-01T00:00:00+00:00"),
          item("dddddddddddddddd", "line", "2025-12-01T00:00:00+00:00"),
        ] } });
        console.log(JSON.stringify(elements(document.body).filter((el) => el.nodeName === "LI").map((el) => el.id)));
    """)
    assert found == ["disruption-cccccccccccccccc", "disruption-bbbbbbbbbbbbbbbb",
                     "disruption-dddddddddddddddd", "disruption-aaaaaaaaaaaaaaaa"]


# --- Station messages: disruption, or station information ---


def test_station_information_has_its_own_quiet_place_in_the_trains_section():
    elements = _page().elements
    ids = [attrs.get("id") for _, attrs in elements]
    assert ids.index("trains") < ids.index("station-info") < ids.index("buses")
    info = next(attrs for _, attrs in elements if attrs.get("id") == "station-info")
    assert "hidden" in info


MESSAGES_PAGE = """
import { renderMessages } from "./board.js";
for (const [tag, id] of [["section", "messages"], ["ul", "message-list"], ["div", "station-info"]]) {
  const el = document.createElement(tag);
  el.setAttribute("id", id);
  document.body.append(el);
}
const $ = (id) => document.getElementById(id);
const shown = () => ({
  disruption: $("messages").hidden ? [] : $("message-list").childNodes.map((li) => spoken(li)),
  info: $("station-info").hidden ? [] : $("station-info").childNodes.map((p) => spoken(p)),
  icons: byClass($("station-info"), "icon-info").length,
});
"""


@needs_node
def test_a_closed_ticket_office_is_station_information_not_disruption():
    found = run_js(MESSAGES_PAGE + """
        renderMessages({ messages: [
          { text: "Trains may be delayed between Dinting and Guide Bridge.", category: "train", severity: "minor",
            kind: "service" },
          { text: "The ticket office is currently closed at this station.", category: null, severity: null,
            kind: "station" },
        ] });
        const both = shown();
        renderMessages({ messages: [{ text: "The ticket office is currently closed at this station.", kind: "station" }] });
        const stationOnly = shown();
        // An older server's plain strings are disruption, as they always were.
        renderMessages({ messages: ["Lifts out of use.", 7, { kind: "station" }] });
        console.log(JSON.stringify([both, stationOnly, shown()]));
    """)
    both, station_only, old = found
    assert both == {
        "disruption": ["Trains may be delayed between Dinting and Guide Bridge."],
        "info": ["Station information: The ticket office is currently closed at this station."],
        "icons": 1,
    }
    # With only a station notice, the "Disruption" panel stays hidden.
    assert station_only["disruption"] == [] and len(station_only["info"]) == 1
    assert old == {"disruption": ["Lifts out of use."], "info": [], "icons": 0}


@needs_node
def test_station_information_is_never_announced():
    found = run_js("""
        import { noteSnapshot } from "./live.js";
        const live = document.createElement("div");
        live.setAttribute("id", "live");
        document.body.append(live);
        globalThis.requestAnimationFrame = (fn) => fn();
        const snapshot = (messages) => ({ trains: { messages } });
        noteSnapshot(snapshot([]), new Map());
        noteSnapshot(snapshot([
          { text: "The ticket office is currently closed at this station.", category: null, severity: null,
            kind: "station" },
        ]), new Map());
        await new Promise((resolve) => setTimeout(resolve, 20));
        const quiet = live.textContent;
        noteSnapshot(snapshot([
          { text: "The ticket office is currently closed at this station.", kind: "station" },
          { text: "Trains may be cancelled between Glossop and Hadfield.", category: "train", severity: "major",
            kind: "service" },
        ]), new Map());
        await new Promise((resolve) => setTimeout(resolve, 20));
        console.log(JSON.stringify([quiet, live.textContent]));
    """)
    assert found == ["", "New disruption: Trains may be cancelled between Glossop and Hadfield."]


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


def _stop(name, walk, letter=None, towards=None):
    return {"name": name, "letter": letter, "towards": towards, "walk_min": walk}


@needs_node
@pytest.mark.parametrize(
    "stops, names",
    [
        # Two stops on one street, lettered on the sign: the letter tells them apart.
        ({"A": _stop("Henry Street", 5, "A"), "B": _stop("Henry Street", 10, "B")},
         {"A": "Henry Street A", "B": "Henry Street B"}),
        # No letters: the stop buses go on to tells them apart.
        ({"A": _stop("Victoria Street", 5, towards="Howard Arms"), "C": _stop("Victoria Street", 5, towards="Market Hall"),
          "B": _stop("Henry Street", 10, "B")},
         {"A": "Victoria Street towards Howard Arms", "C": "Victoria Street towards Market Hall", "B": "Henry Street B"}),
        # Nothing tells them apart: the board's labels do.
        ({"A": _stop("Victoria Street", 5), "B": _stop("Victoria Street", 6)},
         {"A": "Victoria Street (A)", "B": "Victoria Street (B)"}),
        ({"A": _stop("Victoria Street", 5, towards="Glossop"), "B": _stop("Victoria Street", 6, towards="Glossop")},
         {"A": "Victoria Street (A)", "B": "Victoria Street (B)"}),
        # A name of its own needs nothing more; no stop data says the label.
        ({"A": _stop("Market Hall", 3, towards="Howard Arms"), "B": _stop(None, 4)},
         {"A": "Market Hall", "B": "Stop B"}),
        # A stop without a walk is still named, and still shares the name.
        ({"A": _stop("Henry Street", 5, towards="Howard Arms"), "B": _stop("Henry Street", None, towards="Market Hall")},
         {"A": "Henry Street towards Howard Arms", "B": "Henry Street towards Market Hall"}),
        ({"A": _stop("Henry Street", None)}, {"A": "Henry Street"}),
        ({}, {}),
    ],
)
def test_each_stop_is_named_and_namesakes_told_apart(stops, names):
    # The bus board's Stop column (the walk sentence it replaces said them all in one line).
    found = run_js(f"""
        import {{ stopNames }} from "./board.js";
        console.log(JSON.stringify(stopNames({json.dumps(stops)})));
    """)
    assert found == names


@needs_node
def test_the_fixture_stops_are_named_by_their_letters():
    stops = _fixture("dashboard_snapshot.json")["buses"]["stops"]
    found = run_js(f"""
        import {{ stopNames }} from "./board.js";
        console.log(JSON.stringify(stopNames({json.dumps(stops)})));
    """)
    assert found == {"A": "Henry Street A", "B": "Henry Street B"}


# --- The boards ---

# Both boards on a fake page, drawn from the contract fixture at its own time
# (07:01:30). `bodyRows(id)` are a board's departure rows, `said(el)` what a
# screen reader says for an element, `cell(tr, cls)` one cell of a row.
BOARDS = """
import { readFileSync } from "node:fs";
import { h } from "./dom.js";
import { observeSnapshot } from "./time.js";
import { renderBuses, renderTrains, toggleMore } from "./board.js";
const status = (id) => h("span", { id });
document.body.append(
  h("section", { id: "messages" }, h("ul", { id: "message-list" })),
  h("section", { id: "trains" }, h("span", { id: "trains-walk" }), status("trains-status"), h("div", { id: "station-info" }),
    h("div", { id: "trains-board" })),
  h("section", { id: "buses" }, status("buses-status"), h("div", { id: "buses-board" })));
const snapshot = JSON.parse(readFileSync(%r, "utf-8"));
observeSnapshot(snapshot.generated_at);
const alerts = { isOn: () => false };
renderTrains(snapshot, alerts);
renderBuses(snapshot, alerts);
const board = (id) => document.getElementById(id);
const bodyRows = (id) => elements(board(id)).filter((el) => el.nodeName === "TR" && el.parentNode.nodeName === "TBODY");
const said = (el) => spoken(el).replace(/\\s+/g, " ").trim();
// What shows on screen: the text, skipping what only screen readers get.
const shown = (el) => (el.nodeType === 3 ? el.nodeValue
  : (el.getAttribute("class") || "").split(" ").includes("vh") ? "" : el.childNodes.map(shown).join(""));
const cell = (tr, cls) => byClass(tr, cls)[0];
"""


def _boards():
    return BOARDS % str(FIXTURES / "dashboard_snapshot.json")


@needs_node
def test_the_train_board_reads_like_a_station_screen():
    found = run_js(_boards() + """
        const rows = bodyRows("trains-board");
        const head = elements(board("trains-board")).filter((el) => el.nodeName === "TH" && el.getAttribute("scope") === "col");
        console.log(JSON.stringify({
          head: head.map(said),
          first: {
            time: said(rows[0].childNodes[0]),
            calling: byClass(rows[0], "calling-at").map(said),
            where: byClass(rows[0], "where").map(said),
            status: [said(cell(rows[0], "c-status")), cell(rows[0], "badge-text").textContent],
            record: byClass(rows[0], "record").map((el) => el.childNodes[0].textContent),
          },
          calling: rows.slice(1).map((tr) => byClass(tr, "calling-at").length + byClass(tr, "where").length),
          cancelled: [rows[1].getAttribute("class"), byClass(rows[1], "reason").map(said), said(cell(rows[1], "c-status")),
                      said(cell(rows[1], "c-leave"))],
          onTime: [said(cell(rows[2], "c-status")), byClass(rows[2], "glyph").length],
          platform: [said(cell(rows[2], "c-plat")), said(cell(rows[3], "c-plat")), shown(cell(rows[3], "c-plat"))],
          walk: said(board("trains-walk")),
        }));
    """)
    assert found["head"] == ["Time", "Destination", "Platform", "Expected", "Leave"]
    first = found["first"]
    assert first["time"] == "07:12"
    # Row 1 alone says where it calls and where it is.
    assert first["calling"][0].startswith("Calling at Dinting, Broadbottom, Hattersley")
    assert first["calling"][0].endswith("Manchester Piccadilly")
    assert first["where"] == ["Not started yet: leaves Hadfield at 07:11"]
    assert found["calling"] == [0] * 9
    assert first["status"] == ["Expected 07:19", "Exp 07:19"]
    assert first["record"] == ["Often late: 4 of 10"]
    # Cancelled: struck through by its class, reversed out, with its reason.
    assert "row-cancelled" in found["cancelled"][0].split()
    assert found["cancelled"][1:] == [["This train has been cancelled because of a shortage of train crew"],
                                      "Cancelled", "No need to leave"]
    # On time is plain words, with no icon.
    assert found["onTime"] == ["On time", 0]
    # "Plat" labels the cell on narrow screens, where the header is hidden; a
    # screen reader has the header.
    assert found["platform"] == ["1", "not yet known", "Plat–"]
    assert found["walk"] == "10 min walk to Glossop."


@needs_node
def test_a_row_drops_once_its_leaving_time_is_two_minutes_gone():
    found = run_js(_boards() + """
        import { arrange, leaveText } from "./board.js";
        const at = (hm) => `2026-10-10T${hm}:00+01:00`;
        const ms = (hms) => Date.parse(`2026-10-10T${hms}+01:00`);
        const rows = [
          { id: "a", leave_at: at("07:00"), scheduled_at: at("07:10") },
          { id: "b", leave_at: null, status: "cancelled", scheduled_at: at("07:05") },
          { id: "c", leave_at: at("07:01"), scheduled_at: at("07:09") },
          { id: "d", leave_at: at("07:20"), scheduled_at: at("07:30") },
        ];
        const ids = (hms) => {
          const { shown, missed } = arrange(rows, ms(hms));
          return [shown.map((r) => r.id).join(""), missed && missed.id];
        };
        console.log(JSON.stringify({
          arranged: ["07:01:59", "07:02:01", "07:03:01", "07:09:01", "07:10:01"].map(ids),
          words: [leaveText(26 * 60_000 + 5_000), leaveText(60_000), leaveText(59_000), leaveText(-119_000)],
          // The fixture's 07:05 bus left its walk at 06:59: at 07:01:30 it is gone, just missed.
          buses: bodyRows("buses-board").map((tr) => /the (\\d\\d:\\d\\d)/.exec(said(tr.childNodes[0]))[1]),
          missed: byClass(board("buses-board"), "just-missed").map(said),
          trainsMissed: byClass(board("trains-board"), "just-missed").length,
        }));
    """)
    assert found["arranged"] == [
        ["abcd", None],  # Go now still
        ["bcd", "a"],  # dropped, just missed
        ["bd", "c"],  # the latest one missed
        ["bd", "a"],  # the 07:09 has gone; the 07:10 hasn't
        ["bd", None],  # both gone; the cancelled row stays
    ]
    assert found["words"] == ["in 26 min", "in 1 min", "Go now", "Go now"]
    assert "07:05" not in found["buses"] and found["buses"][0] == "07:09"
    assert found["missed"] == ["Just missed 07:05 237 to Ashton Bus Station"]
    assert found["trainsMissed"] == 0


@needs_node
def test_the_tick_says_go_now_then_asks_for_the_row_to_drop():
    # Between snapshots: the 07:09 bus's walk starts at 07:03.
    found = run_js(_boards() + """
        import { observe } from "./time.js";
        import { tickBoards } from "./board.js";
        const generated = Date.parse(snapshot.generated_at);
        const countdown = () => byClass(bodyRows("buses-board")[0], "countdown")[0];
        const at = (minutes) => {
          observe(generated + minutes * 60_000);  // the server's clock, moved on
          const redraw = tickBoards();
          return [shown(countdown()), countdown().classList.contains("go"), redraw];
        };
        const ticks = [at(0), at(1), at(3), at(4)];
        // Drawn again, it has gone; and a board whose section goes away asks for nothing more.
        renderBuses(snapshot, alerts);
        const redrawn = [/the (\\d\\d:\\d\\d)/.exec(said(bodyRows("buses-board")[0].childNodes[0]))[1], tickBoards()];
        renderBuses({ ...snapshot, buses: null }, alerts);
        renderTrains({ ...snapshot, trains: null }, alerts);
        observe(generated + 60 * 60_000);
        console.log(JSON.stringify({ ticks, redrawn, gone: tickBoards() }));
    """)
    assert found["ticks"] == [
        ["in 1 min", False, False],  # 07:01:30
        ["Go now", True, False],  # 07:02:30
        ["Go now", True, False],  # 07:04:30, still within 2 min of 07:03
        ["Go now", True, True],  # 07:05:30: it has to go
    ]
    assert found["redrawn"] == ["07:20", False]
    assert found["gone"] is False


@needs_node
def test_five_rows_show_then_show_later_departures():
    found = run_js(_boards() + """
        const state = (id) => bodyRows(id).map((tr) => tr.hasAttribute("hidden"));
        const more = (id) => byClass(board(id), "more")[0];
        const button = (id) => [said(more(id)), more(id).getAttribute("aria-expanded"), more(id).getAttribute("aria-controls")];
        const before = { trains: state("trains-board"), buses: state("buses-board"), button: button("trains-board"),
                         table: elements(board("trains-board")).find((el) => el.nodeName === "TABLE").id };
        toggleMore(more("trains-board"));
        const opened = [bodyRows("trains-board").map((tr) => tr.hidden !== false && tr.hasAttribute("hidden")),
                        more("trains-board").getAttribute("aria-expanded")];
        renderTrains(snapshot, alerts);  // a new snapshot keeps them open
        console.log(JSON.stringify({ before, opened, redrawn: [state("trains-board"), button("trains-board")[1]] }));
    """)
    before = found["before"]
    assert before["trains"] == [False] * 5 + [True] * 5
    assert before["buses"] == [False] * 5 + [True] * 4  # one has dropped out
    assert before["button"] == ["Show later departures", "false", "trains-table"] and before["table"] == "trains-table"
    assert found["opened"] == [[False] * 10, "true"]
    assert found["redrawn"] == [[False] * 10, "true"]


@needs_node
def test_each_row_is_one_tab_stop_that_names_its_departure():
    found = run_js(_boards() + """
        const focusable = (tr) => elements(tr).filter((el) => ["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA"].includes(el.nodeName)
          || (el.hasAttribute("tabindex") && el.getAttribute("tabindex") !== "-1"));
        const rows = [...bodyRows("trains-board"), ...bodyRows("buses-board")];
        console.log(JSON.stringify({
          stops: rows.map((tr) => focusable(tr).map((el) => [el.nodeName, el.dataset.action, el.dataset.focusKey === `open:${tr.dataset.id}`])),
          names: [bodyRows("trains-board")[0], bodyRows("buses-board")[0]].map((tr) => said(focusable(tr)[0])),
        }));
    """)
    assert found["stops"] == [[["BUTTON", "open", True]]] * 19
    assert found["names"] == ["More about the 07:12 to Manchester Piccadilly", "More about the 07:09 341 to Hyde Bus Station"]


@needs_node
def test_a_bus_row_with_a_disruption_in_force_links_to_it():
    found = run_js(_boards() + """
        import { markBusRows } from "./disruptions.js";
        const rowsById = new Map(snapshot.buses.rows.map((row) => [row.id, { row, mode: "bus" }]));
        markBusRows(snapshot, rowsById);
        console.log(JSON.stringify(bodyRows("buses-board").flatMap((tr) => byClass(tr, "dis-mark").map((a) =>
          [a.parentNode.getAttribute("class"), a.getAttribute("href").startsWith("#disruption-"), shown(a)]))));
    """)
    # Under the destination, where the narrow layout has room for it.
    assert found and all(mark == ["c-dest", True, "Disruption"] for mark in found)


@needs_node
def test_a_timetable_only_bus_shows_its_clock_time_never_minutes():
    found = run_js(_boards() + """
        import { dueText } from "./board.js";
        const byId = new Map(snapshot.buses.rows.map((row) => [row.id, row]));
        console.log(JSON.stringify({
          rows: bodyRows("buses-board").map((tr) => {
            const due = cell(tr, "c-due");
            return [byId.get(tr.dataset.id).live, byId.get(tr.dataset.id).scheduled, shown(due), said(due),
                    byClass(due, "due").length, said(cell(tr, "c-stop"))];
          }),
          words: [dueText(30_000), dueText(-5_000), dueText(4 * 60_000 + 59_000)],
        }));
    """)
    rows = found["rows"]
    # The 07:09 is live and 2 min late: it counts down from 07:11, the time the
    # tracker gives, with a quiet mark (an icon) that screen readers hear as "live".
    assert rows[0][2:] == ["9 min", "9 min, live, expected 07:11", 1, "Henry Street A8 min walk"]
    for live, scheduled, shown, spoken, minutes, _ in rows:
        if live:
            assert minutes == 1 and "sched." not in shown and ", live, " in spoken
        else:
            assert minutes == 0 and shown == f"{scheduled}sched." and spoken == f"{scheduled}, timetable only"
    assert any(not r[0] for r in rows) and any(r[0] for r in rows)
    assert found["words"] == ["Due", "Due", "4 min"]


@needs_node
def test_a_record_shows_in_the_row_only_when_it_is_notable():
    from departureboard.dashboard.history import Stats, phrase

    def said(late, cancelled, days, day_type="weekday"):
        return phrase(Stats(days, late, cancelled, 0, days - late - cancelled, 5.0 if late else None, 9), day_type)

    phrases = [said(4, 0, 10), said(3, 0, 10), said(3, 0, 8), said(0, 2, 10), said(0, 3, 9, "saturday"),
               said(3, 1, 10, "sunday"), said(0, 0, 10), said(2, 0, 3), None]
    found = run_js(f"""
        import {{ recordNote }} from "./board.js";
        console.log(JSON.stringify({json.dumps(phrases)}.map(recordNote)));
    """)
    assert found == ["Often late: 4 of 10", "", "Often late: 3 of 8", "", "Often cancelled: 3 of 9",
                     "Often late or cancelled: 4 of 10", "", "", ""]


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


# The departure sheet on a fake page, as index.html has it, with the history
# API answering from its fixture; and a train for it: `train(at)` is the 07:20
# from Glossop with its strip's train_at (null: not placed). `body` is the
# train's journey part, `summary` its status and facts.
SHEET = """
import { readFileSync } from "node:fs";
import { h } from "./dom.js";
import { openSheet, refreshSheet, setUpSheet } from "./sheet.js";
const HISTORY = JSON.parse(readFileSync(%r, "utf-8"));
globalThis.fetch = async () => ({
  status: 200, ok: true, json: async () => HISTORY,
  headers: { get: (name) => (name === "Content-Type" ? "application/json" : null) },
});
const dialog = h("dialog", { id: "sheet" });
dialog.showModal = () => { dialog.open = true; };
dialog.querySelector = () => ({ focus() {}, addEventListener() {} });
dialog.addEventListener = () => {};
const title = h("h2", { id: "sheet-title" });
const summary = h("div", { id: "sheet-summary" });
const remind = h("button", { id: "sheet-remind" });
const note = h("p", { id: "remind-note" });
const body = h("div", { id: "sheet-train" });
const history = h("div", { id: "history-body" });
dialog.append(title, summary, remind, note, body, h("div", { id: "sheet-bus" }), history);
document.body.append(dialog);
const ctx = { station: "Glossop", stops: {}, generatedMs: null };
const openTrain = (row) => openSheet({ row, mode: "train" }, null, ctx);
const refreshTrain = (rows) => refreshSheet(rows, ctx);
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


def _sheet():
    return SHEET % str(FIXTURES / "dashboard_history.json")


@needs_node
def test_the_journey_says_where_the_train_is_once():
    found = run_js(_sheet() + """
        openTrain(train(1));
        const atGlossop = said();
        openTrain(train(0.5));
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
def test_a_new_snapshot_updates_the_open_sheet_in_place():
    # Rebuilding it would move a screen reader's place: what's there stays,
    # and only what changed is changed.
    found = run_js(_sheet() + """
        openTrain(train(0.5));
        const now = () => [...summary.childNodes, ...body.childNodes];
        const parts = now();
        const items = [...journey().childNodes];
        const first = elements(items[0]);
        const was = { part: items[1].style.getPropertyValue("--part"), said: said() };
        const moved = { ...train(1), delay_reason: "A late crew" };
        refreshTrain(new Map([[moved.id, { row: moved, mode: "train" }]]));
        console.log(JSON.stringify({
          was,
          now: said(),
          part: items[1].style.getPropertyValue("--part"),
          sameParts: parts.filter((node) => now().includes(node)).length,
          parts: parts.length,
          partsNow: now().length,
          sameItems: journey().childNodes.every((li, i) => li === items[i]),
          firstKept: elements(journey().childNodes[0]).every((el, i) => el === first[i]),
          reasons: byClass(summary, "reason").map((p) => [p.hasAttribute("hidden"), spoken(p)]),
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
def test_the_open_sheet_says_when_its_train_has_gone():
    found = run_js(_sheet() + """
        openTrain(train(1));
        refreshTrain(new Map());
        const gone = [spoken(summary), body.childNodes.length, remind.hidden];
        openTrain(train(1));
        console.log(JSON.stringify([gone, said().length]));
    """)
    assert found == [["This train has left the board.", 0, True], 5]


@needs_node
def test_the_sheet_holds_the_history_and_remind_me():
    # One sheet, not three dialogs: the punctuality loads into it on opening,
    # and Remind me is in it, saying honestly that on the LAN's plain http the
    # reminder can only show on the page.
    found = run_js(_sheet() + """
        setUpSheet(() => {});
        const row = { ...train(1), leave_at: "2026-10-10T07:10:00+01:00", scheduled_at: "2026-10-10T07:20:00+01:00" };
        openTrain(row);
        const before = [remind.hidden, remind.getAttribute("aria-pressed"), spoken(remind), spoken(note)];
        await new Promise((resolve) => setTimeout(resolve, 0));
        const phrase = byClass(history, "phrase").map(spoken);
        const days = elements(history).filter((el) => el.nodeName === "TR").length;
        const click = remind.listeners.find(([type]) => type === "click")[1];
        click();
        const on = remind.getAttribute("aria-pressed");
        click();
        const off = remind.getAttribute("aria-pressed");
        openTrain({ ...row, status: "cancelled", leave_at: null });
        console.log(JSON.stringify({
          title: spoken(title), before, on, off, phrase, days, cancelled: [remind.hidden, spoken(note)],
        }));
    """)
    assert found["title"] == "07:20 to Manchester Piccadilly"
    assert found["before"] == [False, "false", "Remind me", "The reminder shows only on this page, while it is open."]
    assert (found["on"], found["off"]) == ("true", "false")
    history = _fixture("dashboard_history.json")
    assert found["phrase"] == [history["phrase"]]
    assert found["days"] == len(history["days"]) + 1  # and the header row
    # A cancelled train has nothing to leave for, so no reminder.
    assert found["cancelled"] == [True, ""]


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


def test_the_footer_has_one_place_for_the_version_the_server_fills_in():
    from departureboard.web.server import VERSION_SLOT

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    footer = html[html.index('<footer class="foot">'):html.index("</footer>")]
    assert html.count(VERSION_SLOT.decode()) == 1 and VERSION_SLOT.decode() in footer


def test_every_map_credit_links_to_fix_the_map():
    # The road router's policy (routing.openstreetmap.de/about.html) asks for the
    # OpenStreetMap credit and a link to "fix the map" wherever its roads are drawn.
    # Both are plain links that open in the same tab.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    map_credits = re.findall(r"<p[^>]*>Map data ©.*?</p>", html)
    assert len(map_credits) == 3  # the page footer, the bus map and the area map
    for credit in map_credits:
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


# --- Weather ---


# The weather panel on a fake page, at the fixture's time (07:01): the hours
# from 07:00 to 18:00 are the next 12.
WEATHER_PANEL = """
import { readFileSync } from "node:fs";
import { h } from "./dom.js";
import { observeSnapshot } from "./time.js";
import { renderWeather } from "./weather.js";
const status = h("span", { id: "weather-status" });
const hours = h("div", { id: "weather-hours" });
document.body.append(h("section", { id: "weather" }, status, h("div", { id: "weather-now" }),
                       h("p", { id: "weather-rain" }), hours));
const snapshot = JSON.parse(readFileSync(%r, "utf-8"));
observeSnapshot(snapshot.generated_at);
const items = (node) => elements(node).filter((el) => el.nodeName === "LI");
"""


def _weather_panel():
    return WEATHER_PANEL % str(FIXTURES / "dashboard_snapshot.json")


@needs_node
def test_the_weather_shows_four_hours_and_the_rest_behind_more_hours():
    found = run_js(_weather_panel() + """
        renderWeather(snapshot);
        const [first, more] = hours.childNodes;
        const said = (node) => items(node).map((li) => spoken(li));
        console.log(JSON.stringify({
          parts: hours.childNodes.map((el) => el.nodeName),
          first: said(first),
          summary: spoken(more.childNodes[0]),
          later: said(more),
          regions: byClass(hours, "wx-strip").map((el) =>
            [el.getAttribute("role"), el.getAttribute("tabindex"),
             el.getAttribute("aria-labelledby") || el.getAttribute("aria-label")]),
          now: spoken(document.getElementById("weather-now")),
        }));
    """)
    assert found["parts"] == ["DIV", "DETAILS"]
    # Each hour is one sentence for a screen reader; the drawing is hidden.
    assert found["first"] == [
        "07:00, light rain, 11 degrees, 70% chance of rain",
        "08:00, light rain, 11 degrees, 70% chance of rain",
        "09:00, rain, 12 degrees, 85% chance of rain",
        "10:00, rain showers, 12 degrees, 60% chance of rain",
    ]
    assert found["summary"] == "More hours"
    assert [s[:5] for s in found["later"]] == ["11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
    # Both strips can scroll sideways, so keyboard users can reach them by name.
    assert found["regions"] == [["region", "0", "weather-hours-heading"], ["region", "0", "Later hours"]]
    # The fixture's 07:01 is before sunrise, so sunrise is the one shown.
    assert found["now"] == ("11° Light rain"
                            "Feels like 9 degrees. Wind 12 mph from the west-south-west, gusts 25 mph. Sunrise 07:24.")


@needs_node
def test_the_weather_writes_the_chance_of_rain_from_30_percent():
    found = run_js(_weather_panel() + """
        renderWeather(snapshot);
        console.log(JSON.stringify(items(hours).map((li) => {
          const bar = byClass(li, "wx-bar-fill")[0];
          return [byClass(li, "wx-pct").map((el) => el.textContent).join(""),
                  bar.style.getPropertyValue("--rain"), byClass(li, "wx-rain")[0].getAttribute("aria-hidden")];
        })));
    """)
    written = [pct for pct, _, _ in found]
    assert written == ["70%", "70%", "85%", "60%", "30%", "", "", "", "", "", "", ""]
    # Every hour has its bar, drawn to scale and hidden from screen readers.
    assert [bar for _, bar, _ in found] == ["70%", "70%", "85%", "60%", "30%", "20%", "10%", "10%", "5%", "0%", "0%",
                                            "5%"]
    assert all(hidden == "true" for _, _, hidden in found)


@needs_node
def test_more_hours_stays_open_across_a_new_snapshot():
    found = run_js(_weather_panel() + """
        renderWeather(snapshot);
        const more = document.getElementById("weather-more");
        more.setAttribute("open", "");
        more.open = true;
        renderWeather(snapshot);
        console.log(JSON.stringify([document.getElementById("weather-more") === more, more.hasAttribute("open")]));
    """)
    assert found == [True, True]


def test_the_weather_hours_are_one_row_not_a_wrapping_grid():
    for body in _css_rules(".wx-hours"):
        assert "auto-fill" not in body and "auto-fit" not in body and "wrap" not in body
    assert _css_value(".wx-hours", "display") == "flex"
    strip = " ".join(_css_rules(".wx-strip"))
    assert "overflow-x: auto" in strip and "scroll-snap-type: x" in strip
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<h3 id="weather-hours-heading">Next hours</h3>' in html
