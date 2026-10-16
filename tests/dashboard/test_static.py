
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

from tests.contrast import assert_contrast, contrast, luminance
from tests.paths import FIXTURES, ROOT

STATIC = ROOT / "src" / "twobells" / "dashboard_static"
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


def test_the_board_look_keeps_a_long_destination_to_one_line():
    # The dot face stays at 20px (above); a destination too long for its
    # column is cut short on one line instead of wrapping the row taller.
    dest = ':root[data-theme="board"] table.board .dest'
    assert _css_value(dest, "display") == "-webkit-box"
    assert _css_value(dest, "-webkit-box-orient") == "vertical"
    assert _css_value(dest, "-webkit-line-clamp") == "1" and _css_value(dest, "line-clamp") == "1"
    assert _css_value(dest, "overflow") == "hidden"
    # Only in the Board look: in the modern one a long name wraps.
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "line-clamp" in body and ".dest" in selectors:
            assert all(s.strip().startswith(':root[data-theme="board"]') for s in selectors.split(",")), selectors


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
        ("header.js", "CONNECTION", r'\["([a-z-]+)", "'),  # [icon, words]
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


# The looks, and a wall tablet's dimmed ones for its quiet windows.
THEMES = ["modern-light", "modern-dark", "board", "kiosk-dim", "kiosk-dim-board"]


def _palettes():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    palettes = {}
    for name in THEMES:
        match = re.search(r"/\* theme: %s \*/[^{]*\{(?:[^{}]*\{)?([^{}]*)\}" % name, css)
        assert match, name
        palettes[name] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", match.group(1)))
    # The dark and board palettes override the light one; a wall tablet's
    # dimmed Board look overrides the Board look.
    base = {"kiosk-dim-board": {**palettes["modern-light"], **palettes["board"]}}
    return {name: {**base.get(name, palettes["modern-light"]), **tokens} for name, tokens in palettes.items()}


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("muted", "surface"), ("muted", "bg"), ("accent", "surface"),
    ("accent", "bg"), ("accent-fg", "accent"), ("ok-fg", "ok-bg"), ("late-fg", "late-bg"), ("bad-fg", "bad-bg"),
    ("unk-fg", "unk-bg"), ("leave-fg", "leave-bg"), ("badge-fg", "badge-bg"), ("bad-fg", "surface"),
    ("muted", "unk-bg"),
    # The boards: late words and the live mark straight on the surface, and
    # a row's text on its hover tint.
    ("late-fg", "surface"), ("ok-fg", "surface"), ("fg", "surface-2"), ("muted", "surface-2"),
    # The hero: its quiet lines, confidence words and a pressed Remind me.
    ("muted", "leave-bg"), ("ok-fg", "leave-bg"), ("late-fg", "leave-bg"), ("accent", "leave-bg"),
    # Sections sit straight on the page, not on cards: the same on its background.
    ("late-fg", "bg"), ("ok-fg", "bg"), ("bad-fg", "bg"),
]
OUTLINE_PAIRS = [("focus", "bg"), ("focus", "surface"), ("edge", "surface"), ("edge", "bg")]


@pytest.mark.parametrize("theme", THEMES)
def test_theme_contrast(theme):
    assert_contrast(theme, _palettes()[theme], TEXT_PAIRS, OUTLINE_PAIRS)


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
    "platform": Opt(str), "replacement_bus": bool, "length": Opt(integer),
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
    "quiet": bool,
    "row_strips": bool,
    "trains": Opt({
        "station": str, "filter": Opt(str), "status": STATUS, "positions": Opt(STATUS), "messages": [MESSAGE],
        "rows": [TRAIN_ROW],
    }),
    "buses": Opt({
        "status": STATUS,
        "positions": Opt(STATUS),
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


# --- The disruption summary under the hero ---


def test_feed_links_leave_without_a_referrer_or_an_opener():
    text = (STATIC / "disruptions.js").read_text(encoding="utf-8")
    assert 'rel: "noopener noreferrer"' in text
    assert "target:" not in text  # links open in the same tab, as links do
    # Only links the server already vetted are used, and only https ones.
    assert 'link.startsWith("https://")' in text


def _inside(parent_id, child_id):
    """Whether index.html has the element child_id inside parent_id."""
    stack, inside = [], False
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    class Walk(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nonlocal inside
            if tag in {"meta", "link", "br", "img", "input", "path", "circle"}:
                return
            stack.append(dict(attrs).get("id"))
            if dict(attrs).get("id") == child_id and parent_id in stack[:-1]:
                inside = True

        def handle_endtag(self, tag):
            if tag not in {"meta", "link", "br", "img", "input", "path", "circle"} and stack:
                stack.pop()

    Walk().feed(html)
    return inside


def test_the_disruption_summary_is_one_disclosure_under_the_hero():
    elements = _page().elements
    ids = {attrs.get("id"): (tag, attrs) for tag, attrs in elements}
    assert {"disruptions", "disruptions-heading", "disruptions-status", "disruptions-body"} <= set(ids)
    assert ids["disruptions-details"][0] == "details"
    # The line is the <details>' own summary; "Coming up this week" folds inside it.
    assert _inside("disruptions-details", "disruptions-summary")
    assert ids["disruptions-coming"][0] == "details" and _inside("disruptions-details", "disruptions-coming")
    assert _inside("disruptions-details", "disruptions-now")
    # The old panels are gone: rail messages join the summary.
    assert "messages" not in ids and "message-list" not in ids


def test_departures_come_straight_after_the_hero_and_summary():
    ids = [attrs.get("id") for _, attrs in _page().elements]
    order = ["leave", "disruptions", "trains", "buses", "area", "weather"]
    assert [ids.index(i) for i in order] == sorted(ids.index(i) for i in order)


# The summary's markup as index.html has it, on a fake page.
DISRUPTION_PAGE = """
import { h } from "./dom.js";
import { renderDisruptions, markBusRows } from "./disruptions.js";
document.body.append(
  h("section", { id: "disruptions" },
    h("p", { id: "disruptions-none" }, "No disruption on your journeys"),
    h("details", { id: "disruptions-details" },
      h("summary", {}, h("span", { id: "disruptions-glyph" }), " ", h("span", { id: "disruptions-summary" })),
      h("div", { id: "disruptions-body" },
        h("div", { id: "disruptions-now-part" }, h("h3", {}, "In force now"), h("ul", { id: "disruptions-now" })),
        h("details", { id: "disruptions-coming" },
          h("summary", {}, "Coming up this week (", h("span", { id: "disruptions-coming-count" }), ")"),
          h("ul", { id: "disruptions-later" })))),
    h("p", { id: "disruptions-status" })));
const $ = (id) => document.getElementById(id);
const words = (el) => spoken(el).replace(/\\s+/g, " ").trim();
const ids = (el) => elements(el).filter((li) => li.nodeName === "LI").map((li) => li.id || words(li.childNodes[0]));
// What the summary shows, or null when the whole section is hidden.
const summary = () => ($("disruptions").hidden ? null : {
  line: $("disruptions-details").hidden ? words($("disruptions-none")) : words($("disruptions-summary")),
  shown: $("disruptions-details").hidden ? null : $("disruptions-summary").childNodes[0].textContent,
  tone: ["late", "bad", "quiet"].filter((c) => $("disruptions-details").classList.contains(c)),
  now: $("disruptions-now-part").hidden ? [] : ids($("disruptions-now")),
  later: $("disruptions-coming").hidden ? [] : ids($("disruptions-later")),
});
const item = (id, relevance, extra = {}) => ({
  id, mode: "bus", title: `Notice ${id.slice(0, 1)}`, summary: "", reason: "Roadworks", planned: true, lines: [],
  stops: [], places: ["Station Road"], severity: "normal", relevance, link: null,
  periods: [{ from_at: "2026-01-01T00:00:00+00:00", to_at: null }], ...extra,
});
const at = (snapshot) => ({ generated_at: "2026-10-10T07:00:00+01:00", ...snapshot });
"""


@needs_node
def test_disruptions_are_listed_most_relevant_first():
    found = run_js(DISRUPTION_PAGE + """
        const from = (iso) => ({ periods: [{ from_at: iso, to_at: null }] });
        renderDisruptions(at({ disruptions: { status: {}, items: [
          item("aaaaaaaaaaaaaaaa", "line", from("2026-01-01T00:00:00+00:00")),
          item("bbbbbbbbbbbbbbbb", "nearby", from("2026-02-01T00:00:00+00:00")),
          item("cccccccccccccccc", "your_stop", from("2026-03-01T00:00:00+00:00")),
          item("dddddddddddddddd", "line", from("2025-12-01T00:00:00+00:00")),
        ] } }));
        console.log(JSON.stringify(elements(document.body).filter((el) => el.nodeName === "LI").map((el) => el.id)));
    """)
    assert found == ["disruption-cccccccccccccccc", "disruption-bbbbbbbbbbbbbbbb",
                     "disruption-dddddddddddddddd", "disruption-aaaaaaaaaaaaaaaa"]


@needs_node
def test_the_summary_leads_with_what_is_in_force_on_your_journeys():
    found = run_js(DISRUPTION_PAGE + """
        import { readFileSync } from "node:fs";
        import { observeSnapshot } from "./time.js";
        const snapshot = JSON.parse(readFileSync(%r, "utf-8"));
        observeSnapshot(snapshot.generated_at);
        renderDisruptions(snapshot);
        console.log(JSON.stringify(summary()));
    """ % str(FIXTURES / "dashboard_snapshot.json"))
    # The stop closure at Henry Street A, by its cause and our stop's name,
    # then the rail message, the roadworks next week and the engineering works.
    assert found["shown"] == "Maintenance work at Henry Street A (your stop) until 18:00 today · 3 more this week"
    assert found["line"] == "Maintenance work at Henry Street A (your stop) until 18:00 today. 3 more this week."
    assert found["tone"] == ["late"]
    assert found["now"] == [
        "Train: Disruption between Hadfield and Glossop until 10:00. Some trains may be cancelled or delayed.",
        "disruption-372c555cca5388f0",
    ]
    assert found["later"] == ["disruption-424e0a2ce2d843be", "disruption-55ce209470d0eb6b"]


@needs_node
def test_the_summary_is_quiet_with_nothing_on_your_journeys():
    found = run_js(DISRUPTION_PAGE + """
        const shown = [];
        // On your line, in force: listed, but not on your journeys.
        renderDisruptions(at({ disruptions: { status: {}, items: [item("aaaaaaaaaaaaaaaa", "line")] } }));
        shown.push(summary());
        // At your stop, but only next week: coming up, folded away.
        renderDisruptions(at({ disruptions: { status: {}, items: [item("bbbbbbbbbbbbbbbb", "your_stop", {
          periods: [{ from_at: "2099-01-01T00:00:00+00:00", to_at: null }] })] } }));
        shown.push(summary());
        // Nothing at all: one quiet line, nothing to open.
        renderDisruptions(at({ disruptions: { status: {}, items: [] } }));
        shown.push(summary());
        // No disruption data and no rail messages: nothing.
        renderDisruptions(at({ disruptions: null, trains: { messages: [] } }));
        shown.push(summary());
        console.log(JSON.stringify(shown));
    """)
    line, coming, empty, none = found
    assert line["line"] == "No disruption on your journeys. 1 notice this week."
    assert line["tone"] == ["quiet"] and line["now"] == ["disruption-aaaaaaaaaaaaaaaa"]
    assert coming["line"] == "No disruption on your journeys. 1 notice this week."
    assert coming["now"] == [] and coming["later"] == ["disruption-bbbbbbbbbbbbbbbb"]
    assert empty["line"] == "No disruption on your journeys" and empty["shown"] is None
    assert none is None


@needs_node
def test_rail_messages_join_the_summary_and_severe_is_red():
    found = run_js(DISRUPTION_PAGE + """
        const message = (text, severity) => ({ text, category: "train", severity, kind: "service" });
        const shown = [];
        // A rail message alone, with no disruption feed, still shows.
        renderDisruptions(at({ disruptions: null, trains: { messages: [message("Lifts out of use at Dinting.", "minor")] } }));
        shown.push(summary());
        // Severe comes first and turns the line red.
        renderDisruptions(at({
          disruptions: { status: {}, items: [item("aaaaaaaaaaaaaaaa", "nearby")] },
          trains: { messages: [message("No trains between Glossop and Manchester.", "severe")] },
        }));
        shown.push(summary());
        console.log(JSON.stringify(shown));
    """)
    alone, severe = found
    assert alone["line"] == "Lifts out of use at Dinting" and alone["tone"] == ["late"]
    assert severe["shown"] == "No trains between Glossop and Manchester · 1 more this week"
    assert severe["tone"] == ["bad"]


@needs_node
def test_feed_words_never_reach_the_lookup_tables_inherited_keys():
    # "constructor", "toString" and "__proto__" are keys every object inherits:
    # a feed that sends one must get the same as any other unknown word.
    found = run_js(DISRUPTION_PAGE + """
        import { statusKey } from "./board.js";
        const odd = ["constructor", "toString", "__proto__", "hasOwnProperty"];
        renderDisruptions(at({
          trains: { messages: [{ text: "Trains may be delayed.", kind: "service", severity: "constructor" }] },
          disruptions: { status: {}, items: [
            item("aaaaaaaaaaaaaaaa", "constructor", { severity: "toString" }),
            item("bbbbbbbbbbbbbbbb", "your_stop", { mode: "constructor" }),
            item("cccccccccccccccc", "line", { severity: "__proto__" }),
          ] },
        }));
        console.log(JSON.stringify({
          keys: odd.map(statusKey),
          now: summary().now,
          badges: byClass($("disruptions-now"), "badge-text").map((el) => el.textContent),
          modes: byClass($("disruptions-now"), "dis-mode").map((el) => el.textContent),
        }));
    """)
    assert found["keys"] == ["unknown"] * 4
    # The item with a mode the page doesn't know is left out; the others are
    # treated as having no known relevance or severity.
    assert found["now"] == ["Train: Trains may be delayed.", "disruption-aaaaaaaaaaaaaaaa", "disruption-cccccccccccccccc"]
    assert found["badges"] == ["Disruption"] * 3
    assert found["modes"] == ["Train:", "Bus:", "Bus:"]


@needs_node
def test_a_long_summary_shows_two_lines_until_more():
    found = run_js(DISRUPTION_PAGE + """
        const long = "Due to roadworks on Station Road, service 237 will divert via Woolley Bridge in both directions "
          + "until further notice.";
        const snapshot = at({ disruptions: { status: {}, items: [
          item("aaaaaaaaaaaaaaaa", "your_stop", { summary: long }), item("bbbbbbbbbbbbbbbb", "line", { summary: "Short." }),
        ] } });
        renderDisruptions(snapshot);
        const state = () => elements($("disruptions-now")).filter((el) => el.nodeName === "LI").map((li) => [
          byClass(li, "dis-summary")[0].getAttribute("class"),
          byClass(li, "dis-more").map((b) => [b.getAttribute("aria-expanded"), b.textContent]),
        ]);
        const before = state();
        const button = byClass($("disruptions-now"), "dis-more")[0];
        button.listeners.find(([type]) => type === "click")[1]({ currentTarget: button });
        const opened = state();
        renderDisruptions(snapshot); // a new snapshot keeps it open
        console.log(JSON.stringify([before, opened, state()]));
    """)
    before, opened, again = found
    assert before == [["dis-summary clamp", [["false", "More"]]], ["dis-summary", []]]
    assert opened[0] == ["dis-summary", [["true", "Less"]]]
    assert again == opened


@needs_node
def test_a_disruption_link_keeps_the_focus_across_snapshots():
    found = run_js(DISRUPTION_PAGE + """
        import { focusKey, refocus } from "./dom.js";
        globalThis.CSS = { escape: (text) => text };
        const snapshot = at({ disruptions: { status: {}, items: [
          item("aaaaaaaaaaaaaaaa", "your_stop", { link: "https://www.tfgm.com/travel-updates/aaa" }),
        ] } });
        renderDisruptions(snapshot);
        const link = () => elements($("disruptions-now")).find((el) => el.nodeName === "A");
        link().focus();
        const key = focusKey();
        renderDisruptions(snapshot);
        const fresh = link();
        refocus(key);
        console.log(JSON.stringify([key, document.activeElement === fresh]));
    """)
    assert found == ["dis-link:aaaaaaaaaaaaaaaa", True]


@needs_node
def test_the_area_map_keeps_the_focus_and_the_open_popup_across_snapshots():
    # WCAG 2.4.3: each snapshot redraws the open area map's list and markers;
    # the focused "Show on map" button and the bus's open popup must survive it.
    area = json.dumps(_fixture("dashboard_area.json"))
    found = run_js("""
        import { register } from "node:module";
        // Leaflet itself needs a real browser: a stand-in that keeps what the page asks of it.
        register("data:text/javascript," + encodeURIComponent(`
          export async function resolve(spec, context, next) {
            if (!spec.includes("leaflet-src.esm.js")) return next(spec, context);
            const names = ["map", "tileLayer", "layerGroup", "polyline", "circleMarker", "marker", "divIcon"];
            const body = names.map((n) => "export const " + n + " = (...a) => globalThis.FAKE_L." + n + "(...a);").join("");
            return { url: "data:text/javascript," + encodeURIComponent(body), shortCircuit: true };
          }`));
        const { h } = await import("./dom.js");
        const markers = [];
        const chain = (extra = {}) => { const o = { addTo: () => o, bindTooltip: () => o, ...extra }; return o; };
        globalThis.FAKE_L = {
          map: () => ({ invalidateSize() {}, fitBounds() {}, setView() {}, getZoom: () => 14 }),
          tileLayer: () => chain(), layerGroup: () => chain({ clearLayers() {} }), polyline: () => chain(),
          circleMarker: () => chain(), divIcon: () => ({}),
          marker: (at) => {
            const m = chain({ open: false, bindPopup: () => m, getLatLng: () => at,
                              openPopup() { m.open = true; return m; }, isPopupOpen: () => m.open });
            markers.push(m);
            return m;
          },
        };
        globalThis.CSS = { escape: (text) => text };
        globalThis.matchMedia = () => ({ matches: true });
        globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => (AREA),
          headers: { get: (name) => (name === "Content-Type" ? "application/json" : null) } });
        document.head = document.createElement("head");
        document.head.append = (link) => { for (const [type, fn] of link.listeners) if (type === "load") fn(); };
        const dialog = h("dialog", { id: "area-dialog" },
          h("h2", { "data-dialog-title": "" }), h("button", { "data-close": "" }), h("p", { id: "area-status" }),
          h("div", { id: "area-map" }), h("p", { id: "area-dialog-summary" }), h("ul", { id: "area-vehicles" }),
          h("ul", { id: "area-stops" }));
        dialog.showModal = () => { dialog.open = true; };
        document.body.append(dialog, h("p", { id: "osm-credit" }), h("button", { id: "area-zoom-in" }),
          h("button", { id: "area-zoom-out" }), h("button", { id: "area-fit" }),
          h("section", { id: "area" }, h("div", { id: "line-part" }), h("p", { id: "line-summary" }),
            h("ul", { id: "line-diagram" }), h("ul", { id: "timetable-changes" }),
            h("div", { id: "area-buses" }, h("p", { id: "area-summary" }),
              h("button", { id: "area-open", "data-focus-key": "area-open" }))));
        const { setUpArea, renderArea } = await import("./area.js");
        setUpArea();
        const opener = document.getElementById("area-open");
        opener.listeners.find(([type]) => type === "click")[1]({ currentTarget: opener });
        await new Promise((resolve) => setTimeout(resolve, 20));
        const buttons = () => document.getElementById("area-vehicles").querySelectorAll("button");
        const second = buttons()[1];
        second.focus();
        second.listeners.find(([type]) => type === "click")[1]({});
        const before = markers.length;
        await renderArea({ generated_at: "2026-10-09T10:15:00+01:00", buses: { rows: [] } });
        const fresh = buttons()[1];
        console.log(JSON.stringify({
          count: buttons().length, redrawn: fresh !== second, focused: document.activeElement === fresh,
          popups: markers.slice(before).map((m) => m.open),
        }));
    """.replace("(AREA)", f"({area})"))
    assert found["count"] > 1 and found["redrawn"] is True
    assert found["focused"] is True
    assert found["popups"] == [i == 1 for i in range(found["count"])]


@needs_node
def test_the_line_across_the_page_marks_each_train_and_lists_it_in_words():
    # Across a wide section the stations' own train lists are hidden: each placed
    # train is a marker on the rail (drawing only) and a line in the list under
    # it, in line order, its arrow pointing along the row the way it is going.
    snapshot = _fixture("dashboard_snapshot.json")
    rows = snapshot["trains"]["rows"]
    back = {**rows[2], "id": "back", "scheduled": "08:30", "destination": "Hadfield",
            "position": {"where": "Left Gorton 08:25, on time",
                         "strip": {"stops": ["Ashburys", "Gorton", "Guide Bridge"], "yours": 1, "train_at": 1.5}}}
    snapshot["trains"]["rows"] = [*rows, back]
    found = run_js("""
        const { h } = await import("./dom.js");
        document.body.append(h("div", { id: "line-part" }), h("p", { id: "line-summary" }),
          h("div", { id: "line-track" }, h("ol", { id: "line-diagram" }), h("div", { id: "line-rail", "aria-hidden": "true" })),
          h("ul", { id: "line-train-list" }));
        const { renderLine } = await import("./area.js");
        renderLine(SNAPSHOT);
        const $ = (id) => document.getElementById(id);
        const stations = byClass($("line-diagram"), "line-station").map(spoken);
        const prop = (el, name) => el.style.getPropertyValue(name);
        const arrows = (el) => byClass(el, "glyph").map((g) => g.childNodes[0]?.getAttribute("class") || "");
        console.log(JSON.stringify({
          stations: stations.length, first: stations[0], last: stations.at(-1),
          track: ["--stations", "--longest", "--last", "--deepest"].map((n) => prop($("line-track"), n)),
          markers: byClass($("line-rail"), "line-marker").map((m) => [spoken(m), prop(m, "--at"), prop(m, "--stack")]),
          list: [...$("line-train-list").childNodes].map(spoken),
          arrows: arrows($("line-train-list")),
          railHidden: $("line-rail").getAttribute("aria-hidden"),
        }));
    """.replace("SNAPSHOT", json.dumps(snapshot)))
    assert found["first"] == "Manchester Piccadilly" and found["last"] == "Hadfield"
    n = found["stations"]
    # The longest label is "Manchester Piccadilly" or Glossop with its "(this station)".
    assert found["track"] == [str(n), str(len("Glossop (this station)")), str(len("Hadfield")), "0"]
    assert found["markers"] == [["08:30", "2.5", "0"], ["08:12", str(n - 1.5), "0"]]
    assert found["list"] == [" 08:30 to Hadfield: Left Gorton 08:25, on time",
                             " 08:12 to Manchester Piccadilly: Left Hadfield 08:04, on time"]
    assert [("right" in a, "left" in a) for a in found["arrows"]] == [(True, False), (False, True)]
    assert found["railHidden"] == "true"


@needs_node
def test_a_row_link_opens_the_summary_at_its_item():
    found = run_js(_boards() + DISRUPTION_PAGE.replace('import { h } from "./dom.js";\n', "") + """
        renderDisruptions(snapshot);
        const rowsById = new Map(snapshot.buses.rows.map((row) => [row.id, { row, mode: "bus" }]));
        markBusRows(snapshot, rowsById);
        const link = byClass(board("buses-board"), "dis-mark")[0];
        let prevented = false;
        link.listeners.find(([type]) => type === "click")[1]({ currentTarget: link, preventDefault: () => { prevented = true; } });
        console.log(JSON.stringify([link.getAttribute("href"), $("disruptions-details").open === true,
                                    document.activeElement.id, prevented]));
    """)
    assert found == ["#disruption-372c555cca5388f0", True, "disruption-372c555cca5388f0", True]


@needs_node
def test_new_disruption_on_your_journeys_is_announced_once():
    found = run_js("""
        import { noteSnapshot } from "./live.js";
        const live = document.createElement("div");
        live.setAttribute("id", "live");
        document.body.append(live);
        globalThis.requestAnimationFrame = (fn) => fn();
        const item = (id, relevance) => ({
          id, mode: "bus", title: id, summary: "", reason: "Roadworks", planned: true, lines: [], stops: [],
          places: [`Road ${id.slice(0, 1)}`], severity: "normal", relevance, link: null,
          periods: [{ from_at: "2026-01-01T00:00:00+00:00", to_at: null }],
        });
        const snapshot = (...items) => ({ generated_at: "2026-10-10T07:00:00+01:00", disruptions: { status: {}, items } });
        const wait = () => new Promise((resolve) => setTimeout(resolve, 20));
        // What is in force when the page opens, and on your line only, is never spoken.
        noteSnapshot(snapshot(item("aaaaaaaaaaaaaaaa", "your_stop")), new Map());
        noteSnapshot(snapshot(item("aaaaaaaaaaaaaaaa", "your_stop"), item("bbbbbbbbbbbbbbbb", "line")), new Map());
        await wait();
        const quiet = live.textContent;
        const both = snapshot(item("aaaaaaaaaaaaaaaa", "your_stop"), item("cccccccccccccccc", "nearby"));
        noteSnapshot(both, new Map());
        await wait();
        const spokenOnce = live.textContent;
        // The same again, snapshot after snapshot: nothing more is queued.
        let queued = 0;
        globalThis.setTimeout = () => { queued += 1; return 1; };
        noteSnapshot(both, new Map());
        noteSnapshot(both, new Map());
        console.log(JSON.stringify([quiet, spokenOnce, queued]));
    """)
    assert found == ["", "New disruption: Roadworks at Road c (nearby).", 0]


# --- Station messages: disruption, or station information ---


def test_station_information_has_its_own_quiet_place_in_the_trains_section():
    elements = _page().elements
    ids = [attrs.get("id") for _, attrs in elements]
    assert ids.index("trains") < ids.index("station-info") < ids.index("buses")
    info = next(attrs for _, attrs in elements if attrs.get("id") == "station-info")
    assert "hidden" in info


@needs_node
def test_a_closed_ticket_office_is_station_information_not_disruption():
    found = run_js(DISRUPTION_PAGE + """
        import { renderMessages } from "./board.js";
        document.body.append(h("div", { id: "station-info" }));
        const shown = (trains) => {
          renderMessages(trains);
          renderDisruptions({ trains });
          return {
            disruption: summary() ? summary().now : [],
            info: $("station-info").hidden ? [] : $("station-info").childNodes.map((p) => spoken(p)),
            icons: byClass($("station-info"), "icon-info").length,
          };
        };
        const both = shown({ messages: [
          { text: "Trains may be delayed between Dinting and Guide Bridge.", category: "train", severity: "minor",
            kind: "service" },
          { text: "The ticket office is currently closed at this station.", category: null, severity: null,
            kind: "station" },
        ] });
        const stationOnly = shown({ messages: [{ text: "The ticket office is currently closed at this station.", kind: "station" }] });
        // An older server's plain strings are disruption, as they always were.
        const old = shown({ messages: ["Lifts out of use.", 7, { kind: "station" }] });
        console.log(JSON.stringify([both, stationOnly, old]));
    """)
    both, station_only, old = found
    assert both == {
        "disruption": ["Train: Trains may be delayed between Dinting and Guide Bridge."],
        "info": ["Station information: The ticket office is currently closed at this station."],
        "icons": 1,
    }
    # With only a station notice, there is no disruption to show.
    assert station_only["disruption"] == [] and len(station_only["info"]) == 1
    assert old == {"disruption": ["Train: Lifts out of use."], "info": [], "icons": 0}


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


# --- The header and the page's layout ---


@needs_node
def test_the_heading_is_the_place_never_cut():
    cases = [
        ["Glossop > Manchester Picc", "Glossop"],  # the trains' caption says where to
        ["Glossop>Hadfield", "Glossop"],
        ["Departures", "Glossop"],  # the default title gives way to the station
        ["", "Glossop"],
        ["Departures", ""],
        ["Our house", "Glossop"],
        ["Old Glossop, Shepley Street and the long way round to Manchester", "Glossop"],
    ]
    found = run_js(f"""
        import {{ placeName }} from "./header.js";
        console.log(JSON.stringify({json.dumps(cases)}.map(([title, station]) => placeName(title, station))));
    """)
    assert found == ["Glossop", "Glossop", "Glossop", "Glossop", "Departures", "Our house",
                     "Old Glossop, Shepley Street and the long way round to Manchester"]


@needs_node
def test_the_status_pill_follows_the_trains_and_buses_only():
    # The weather and the disruption checks say their problems in their own
    # sections: one optional feed failing all day mustn't make the page look stale.
    found = run_js("""
        import { pill } from "./header.js";
        const ok = (last) => ({ last_success: last, stale: false, error: null, note: null });
        const at = (hm) => `2026-10-10T${hm}:00+01:00`;
        const snapshot = (parts) => ({
          trains: { status: ok(at("07:01")) }, buses: { status: ok(at("07:01")) }, weather: { status: ok(at("06:49")) },
          disruptions: { status: { bus: ok(at("07:00")), train: null } }, ...parts,
        });
        const t = Date.parse(at("07:05"));
        console.log(JSON.stringify([
          pill("live", snapshot({}), t, t),
          // Buses stale since 06:40, trains failing since 06:50: the older one.
          pill("live", snapshot({ buses: { status: { ...ok(at("06:40")), stale: true } },
                                 trains: { status: { ...ok(at("06:50")), error: "timed out" } } }), t, t),
          pill("live", snapshot({ trains: { status: { ...ok(null), error: "no key" } } }), t, t),
          // The weather failing and the disruption checks stale: still live.
          pill("live", snapshot({ weather: { status: { ...ok(null), error: "timed out" } },
                                 disruptions: { status: { bus: { ...ok(at("06:30")), stale: true },
                                                          train: { ...ok(null), error: "no key" } } } }), t, t),
          pill("polling", snapshot({ weather: { status: { ...ok(at("06:00")), stale: true } } }), 0, t),
          // Losing the server comes first, with how long ago it last answered.
          pill("reconnecting", snapshot({ buses: { status: { ...ok(at("06:40")), stale: true } } }), t - 120000, t),
          pill("connecting", null, 0, t),
        ]));
    """)
    assert found == [
        ["live", "circle", "Live"],
        ["stale", "triangle-alert", "Not updated since 06:40"],
        ["stale", "triangle-alert", "Not updated yet"],
        ["live", "circle", "Live"],
        ["polling", "refresh-cw", "Updating every 30 s"],
        ["reconnecting", "circle-dashed", "Reconnecting…, last update 2 min ago"],
        ["connecting", "circle-dashed", "Connecting…"],
    ]


@needs_node
def test_the_status_pill_s_changes_are_announced_once_each():
    # WCAG 4.1.3: the pill changes without focus moving, so each new state is
    # said through the live region; its words ticking on ("2 min ago") are not.
    found = run_js("""
        import { noteConnection } from "./header.js";
        const live = document.createElement("div");
        live.setAttribute("id", "live");
        document.body.append(live);
        globalThis.requestAnimationFrame = (fn) => fn();
        const timers = [];
        globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
        const said = [];
        const step = (state, words) => {
          noteConnection(state, words);
          while (timers.length) timers.shift()();
          said.push(live.textContent);
          live.textContent = "";
        };
        step("connecting", "Connecting…");
        step("live", "Live");
        step("stale", "Not updated since 06:40");
        step("stale", "Not updated since 06:45");
        step("live", "Live");
        step("reconnecting", "Reconnecting…, last update 1 min ago");
        step("reconnecting", "Reconnecting…, last update 2 min ago");
        step("offline", "Offline, last update 3 min ago");
        step("polling", "Updating every 30 s");
        console.log(JSON.stringify(said));
    """)
    assert found == ["", "", "Not updated since 06:40.", "", "Live again.",
                     "Reconnecting, last update 1 min ago.", "", "Offline, last update 3 min ago.",
                     "Updating every 30 s."]


def test_the_page_announces_the_status_pill_s_changes():
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "noteConnection(state, words)" in main


@needs_node
def test_a_section_has_its_own_status_line_only_when_it_is_behind():
    found = run_js(_boards() + """
        import { renderWeather } from "./weather.js";
        document.body.append(h("section", { id: "weather" }, h("p", { id: "weather-status" }), h("div", { id: "weather-now" }),
          h("p", { id: "weather-rain" }), h("div", { id: "weather-hours" })));
        const line = (id) => { const el = document.getElementById(id); return el.hidden ? null : said(el); };
        renderWeather(snapshot);
        const fresh = ["trains-status", "buses-status", "weather-status"].map(line);
        const behind = structuredClone(snapshot);
        behind.buses.status = { ...behind.buses.status, stale: true };
        behind.weather.status = { ...behind.weather.status, error: "timed out" };
        behind.trains.status = { ...behind.trains.status, note: "Showing trains to Manchester only." };
        renderTrains(behind, alerts);
        renderBuses(behind, alerts);
        renderWeather(behind);
        console.log(JSON.stringify([fresh, ["trains-status", "buses-status", "weather-status"].map(line)]));
    """)
    fresh, behind = found
    # All current: the header's pill says so for every section.
    assert fresh == [None, None, None]
    assert behind == ["Showing trains to Manchester only.", "Not updated since 07:01.",
                      "Problem: timed out. Showing the forecast from 06:49."]


def test_the_board_look_keeps_the_hero_soon_and_now_edges():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    # A board-look border for every hero would outrank .leave.soon and .leave.now.
    assert not re.search(r':root\[data-theme="board"\] \.leave\s*\{', css)
    assert _css_value(':root[data-theme="board"] .leave:not(.soon):not(.now)', "border-color") == "var(--fg)"


def test_touch_targets_are_44px_on_coarse_pointers():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rules = {}
    for start in [m.end() for m in re.finditer(r"@media \(pointer: coarse\)\s*\{", css)]:
        end = css.index("\n}", start)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css[start:end]):
            for selector in selectors.split(","):
                rules[selector.strip()] = rules.get(selector.strip(), "") + body
    for selector in (".dis-mark", "button.link.dis-more", "button.open", "button.more"):
        assert "min-height: 2.75rem" in rules.get(selector, ""), selector


def test_a_record_sits_on_its_own_line():
    # Inline, it ran on from the destination or the reason before it.
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    record = re.search(r"\n\.record\s*\{([^}]*)\}", css).group(1)
    assert "display: block;" in record and "width: fit-content;" in record


def test_from_75rem_the_weather_sits_beside_the_hero_over_the_boards():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "100rem" not in css
    wide = _css_block("@media (min-width: 75rem)")
    # Not on a wall tablet, which has its own layout.
    main = wide[":root:not([data-kiosk]) main"]
    assert "display: grid" in main
    areas = re.search(r'grid-template-areas:\s*((?:"[^"]*"\s*)+);', main).group(1)
    rows = [row.split() for row in re.findall(r'"([^"]*)"', areas)]
    assert rows[0] == ["hero", "weather"]
    assert rows.index(["dis", "dis"]) < rows.index(["boards", "boards"]) < [r[0] for r in rows].index("area")
    # The wrappers step aside, so their sections are the grid's own items.
    assert "display: contents" in wide[".sections"] and "display: contents" in wide[".extras"]
    for selector, area in ((".leave", "hero"), (".weather", "weather"), (".disruptions", "dis"),
                           (".boards", "boards"), (".area", "area"), ("#alert-banner", "alert")):
        assert f"grid-area: {area};" in wide[selector], selector
    # Trains and buses side by side.
    assert "display: flex" in wide[".boards"] and "flex: 1 1 0" in wide[".boards > *"]
    # The page reads as it did: departures first, the weather, a sidebar here, last.
    ids = [attrs.get("id") for _, attrs in _page().elements]
    assert _inside("main", "trains") and ids.index("buses") < ids.index("area") < ids.index("weather")


def test_sections_are_plain_only_the_hero_and_dialogs_are_raised():
    for body in _css_rules(".panel"):
        assert "border" not in body and "background" not in body
    for body in _css_rules(".disruptions"):
        assert "border" not in body and "background" not in body and "shadow" not in body
    assert any("box-shadow: var(--shadow)" in body for body in _css_rules("dialog.dialog"))


def test_a_dialog_s_sticky_head_never_hides_the_focused_control():
    # WCAG 2.4.11: a control tabbed to under the sticky head scrolls clear of
    # it. The head is at least its padding (1rem above, 0.75rem below) and a
    # 2.75rem button tall.
    head = "".join(_css_rules(".dialog-head"))
    assert "position: sticky" in head and "padding: var(--s-4) 0 var(--s-3)" in head
    padding = [re.search(r"scroll-padding-top:\s*([\d.]+)rem", body) for body in _css_rules("dialog.dialog")]
    found = [float(m.group(1)) for m in padding if m]
    assert found and min(found) >= 1 + 0.75 + 2.75


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
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
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


@needs_node
def test_a_stop_letter_never_wraps_away_from_its_name():
    # "Henry Street" then "A" alone on the next line reads as two things.
    found = run_js("""
        import { stopLabel } from "./board.js";
        const parts = (text) => {
          const label = stopLabel(text);
          return (Array.isArray(label) ? label : [label]).map((part) =>
            typeof part === "string" ? ["text", part] : [part.getAttribute("class"), part.textContent]);
        };
        console.log(JSON.stringify([parts("Henry Street A"), parts("Henry Street (B)"), parts("Stand 12"),
          parts("Victoria Street towards Market Hall"), parts("Glossop"), parts("Glossop Brook Road Stop C")]));
    """)
    # A short stop's name stays whole on one line, so a bus row isn't three
    # lines tall for "Henry / Street A"; a long one may wrap, never before
    # its letter.
    assert found == [
        [["text", ""], ["nobreak", "Henry Street A"]],
        [["text", ""], ["nobreak", "Henry Street (B)"]],
        [["text", ""], ["nobreak", "Stand 12"]],
        [["text", "Victoria Street towards Market Hall"]],
        [["text", ""], ["nobreak", "Glossop"]],
        [["text", "Glossop Brook Road "], ["nobreak", "Stop C"]],
    ]
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.nobreak\s*\{\s*white-space:\s*nowrap;", css)


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
          calling: rows.slice(1).map((tr) => byClass(tr, "calling-at").length),
          where: rows.slice(1).map((tr) => byClass(tr, "where").map(said).join("")),
          cancelled: [rows[1].getAttribute("class"), byClass(rows[1], "reason").map(said), said(cell(rows[1], "c-status")),
                      said(cell(rows[1], "c-leave"))],
          onTime: [said(cell(rows[2], "c-status")), byClass(rows[2], "glyph").length],
          platform: [said(cell(rows[2], "c-plat")), said(cell(rows[3], "c-plat")), shown(cell(rows[3], "c-plat"))],
          walk: said(board("trains-walk")),
          caption: said(elements(board("trains-board")).find((el) => el.nodeName === "CAPTION")),
        }));
    """)
    assert found["head"] == ["Time", "Destination", "Platform", "Expected", "Leave"]
    # Where they go, in full: the filter station's name, not its code (MAN).
    assert found["caption"] == "Next trains from Glossop to Manchester Piccadilly"
    first = found["first"]
    assert first["time"] == "07:12"
    # Row 1 alone says where it calls; it says where it is, as does a later
    # row whose train has a position strip to draw (the 08:12).
    assert first["calling"][0].startswith("Calling at Dinting, Broadbottom, Hattersley")
    assert first["calling"][0].endswith("Manchester Piccadilly")
    assert first["where"] == ["Not started yet: leaves Hadfield at 07:11"]
    assert found["calling"] == [0] * 9
    assert found["where"] == ["", "Left Hadfield 08:04, on time"] + [""] * 7
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
def test_a_replacement_bus_in_the_trains_says_so_in_words():
    script = _boards().replace("observeSnapshot(snapshot.generated_at);", """
snapshot.trains.rows[0] = { ...snapshot.trains.rows[0], replacement_bus: true, platform: null, length: null };
observeSnapshot(snapshot.generated_at);""")
    assert script != _boards()
    found = run_js(script + """
        const [bus, train] = bodyRows("trains-board");
        const open = (tr) => byClass(tr, "open").map(said);
        console.log(JSON.stringify({
          platform: [said(cell(bus, "c-plat")), shown(cell(bus, "c-plat"))],
          words: byClass(bus, "replacement").map(said),
          open: open(bus),
          train: [said(cell(train, "c-plat")), byClass(train, "replacement").length, open(train)],
        }));
    """)
    assert found["platform"] == ["Bus", "PlatBus"]
    assert found["words"] == ["Replacement bus"]
    assert found["open"] == ["More about the 07:12 replacement bus to Manchester Piccadilly"]
    assert found["train"][:2] == ["1", 0]
    assert found["train"][2][0].startswith("More about the 07:42 to ")


@needs_node
def test_the_bus_board_caption_is_for_screen_readers_only():
    # The section's "Buses" heading says it on screen; the table keeps its name.
    found = run_js(_boards() + """
        const caption = (id) => elements(board(id)).find((el) => el.nodeName === "CAPTION");
        console.log(JSON.stringify([
          [said(caption("buses-board")), caption("buses-board").getAttribute("class")],
          [said(caption("trains-board")), caption("trains-board").getAttribute("class")],
        ]));
    """)
    assert found == [["Next buses", "vh"], ["Next trains from Glossop to Manchester Piccadilly", None]]


# Each row's position strip, as [drawings, hidden, vehicles drawn, words], or null.
ROW_STRIPS = """
const strips = (id) => bodyRows(id).map((tr) => {
  const svg = elements(tr).filter((el) => el.nodeName === "svg" && byClass(tr, "row-strip").includes(el));
  if (!svg.length) return null;
  return [svg.length, svg[0].getAttribute("aria-hidden"), byClass(svg[0], "strip-bus").length
    + byClass(svg[0], "strip-train").length, said(byClass(tr, "where")[0])];
});
console.log(JSON.stringify({ trains: strips("trains-board"), buses: strips("buses-board") }));
"""


@needs_node
def test_a_row_with_a_position_draws_its_strip_small_beside_its_words():
    found = run_js(_boards() + ROW_STRIPS)
    # Trains with a position strip: the 07:12 not started yet (no train
    # drawn), the 08:12 on its way. One drawing each, hidden: the words say it.
    assert found["trains"] == [[1, "true", 0, "Not started yet: leaves Hadfield at 07:11"], None,
                               [1, "true", 1, "Left Hadfield 08:04, on time"]] + [None] * 7
    # Every bus with stops to draw, live or not, as before the rows were
    # redesigned: a timetable-only bus shows its stops and yours with no bus
    # on them, and says so. The 07:05 has gone (just missed).
    assert found["buses"] == [[1, "true", 1, "2 stops away"], [1, "true", 0, "No live position"],
                              [1, "true", 1, "4 stops away"], None,
                              [1, "true", 0, "Not on the route yet"], None, None, None, None]


@needs_node
def test_a_wall_tablet_row_has_no_position_strip():
    found = run_js(KIOSK + _boards() + ROW_STRIPS)
    assert found == {"trains": [None] * 4, "buses": [None] * 4}


# The page as a snapshot saying row_strips marks it, before the boards are drawn.
def _row_strips(setting, kiosk=False):
    page = '{ dataset: { kiosk: "on" } }' if kiosk else "{ dataset: {} }"
    return (f'import {{ applyRowStrips }} from "./board.js";\ndocument.documentElement = {page};\n'
            f"applyRowStrips(document.documentElement, {json.dumps(setting)});\n")


# Each row's drawings and where-words, as [drawings, words].
ROW_WHERE = """
const where = (id) => bodyRows(id).map((tr) => [byClass(tr, "row-strip").length, byClass(tr, "where").map(said).join("")]);
console.log(JSON.stringify({ trains: where("trains-board"), buses: where("buses-board") }));
"""


@needs_node
@pytest.mark.parametrize("setting", [{"row_strips": True}, {}])
def test_rows_draw_their_strips_when_the_setting_is_on_or_unsaid(setting):
    # A snapshot from an older server has no row_strips: they are on.
    found = run_js(_row_strips(setting) + _boards() + ROW_WHERE)
    assert [n for n, _ in found["trains"]] == [1, 0, 1] + [0] * 7
    assert [n for n, _ in found["buses"]] == [1, 1, 1, 0, 1, 0, 0, 0, 0]


@needs_node
def test_with_the_setting_off_rows_draw_no_strips_and_row_1_still_says_where():
    found = run_js(_row_strips({"row_strips": False}) + _boards() + ROW_WHERE)
    # As on a wall tablet: no drawings; row 1 says where its train is in
    # words, a later train row says nothing, and a bus row keeps its words.
    assert found["trains"] == [[0, "Not started yet: leaves Hadfield at 07:11"]] + [[0, ""]] * 9
    assert found["buses"] == [[0, "2 stops away"], [0, "No live position"], [0, "4 stops away"], [0, ""],
                              [0, "Not on the route yet"], [0, ""], [0, ""], [0, ""], [0, ""]]


@needs_node
@pytest.mark.parametrize("setting", [{"row_strips": True}, {"row_strips": False}])
def test_a_wall_tablet_draws_no_strips_whatever_the_setting(setting):
    found = run_js(_row_strips(setting, kiosk=True) + _boards() + ROW_WHERE)
    assert [n for n, _ in found["trains"] + found["buses"]] == [0] * 8
    assert found["trains"][0][1] == "Not started yet: leaves Hadfield at 07:11"


@needs_node
def test_the_page_is_marked_only_while_the_setting_is_off():
    found = run_js("""
        import { applyRowStrips } from "./board.js";
        const root = { dataset: {} };
        const marks = [];
        for (const snapshot of [{ row_strips: false }, { row_strips: true }, { row_strips: false }, {},
                                { row_strips: "false" }, null]) {
          applyRowStrips(root, snapshot);
          marks.push(root.dataset.rowStrips ?? null);
        }
        console.log(JSON.stringify(marks));
    """)
    # Only a real false turns them off; anything else leaves them on.
    assert found == ["off", None, "off", None, None, None]


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
          buses: bodyRows("buses-board").map((tr) => /the (\\d\\d:\\d\\d)/.exec(said(cell(tr, "c-dest")))[1]),
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
        const redrawn = [/the (\\d\\d:\\d\\d)/.exec(said(cell(bodyRows("buses-board")[0], "c-dest")))[1], tickBoards()];
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
def test_a_bus_row_is_headed_by_its_line_and_destination_only():
    # The row header is said with every cell: what is extra about the bus
    # (where it is, no tracker, its record) belongs in a cell of its own.
    found = run_js(_boards() + """
        import { markBusRows } from "./disruptions.js";
        markBusRows(snapshot, new Map(snapshot.buses.rows.map((row) => [row.id, { row, mode: "bus" }])));
        const byId = new Map(snapshot.buses.rows.map((row) => [row.id, row]));
        const head = elements(board("buses-board")).filter((el) => el.nodeName === "TH" && el.getAttribute("scope") === "col");
        console.log(JSON.stringify({
          head: head.map(said),
          rows: bodyRows("buses-board").map((tr) => {
            const th = tr.childNodes.find((el) => el.nodeName === "TH");
            const row = byId.get(tr.dataset.id);
            return [th.getAttribute("role"), said(th), `${row.line} to ${row.destination}`, shown(th),
                    elements(th).length, byClass(cell(tr, "c-dest"), "open").length];
          }),
          extras: bodyRows("buses-board").flatMap((tr) => ["where", "record", "ghost", "dis-mark"]
            .flatMap((name) => byClass(tr, name).map((el) => [name, el.parentNode.nodeName === "TH"]))),
        }));
    """)
    assert found["head"] == ["Bus", "Destination", "Stop", "Due", "Leave"]
    for role, said, expected, shown, nodes, buttons in found["rows"]:
        # "341 to Hyde Bus Station": the line once, the destination, nothing more.
        assert (role, said, buttons) == ("rowheader", expected, 1)
        assert shown == expected.split(" to ")[0] and nodes <= 3
    extras = {name for name, _ in found["extras"]}
    assert {"where", "dis-mark"} <= extras and not any(in_th for _, in_th in found["extras"])


@needs_node
def test_a_stop_without_a_letter_keeps_its_way_on_the_quiet_line(tmp_path):
    # With no letters on the signs, two Henry Streets are told apart by where
    # they go next. The name stays one line and "towards ..." joins the walk
    # on the quiet line, so a row isn't four lines tall.
    snapshot = _fixture("dashboard_snapshot.json")
    for label, towards in (("A", "Market Hall"), ("B", "Victoria Street")):
        snapshot["buses"]["stops"][label].update(letter=None, towards=towards)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    found = run_js(BOARDS % str(path) + """
        const stop = cell(bodyRows("buses-board")[0], "c-stop");
        console.log(JSON.stringify({
          name: byClass(stop, "stop").map((el) => el.textContent),
          quiet: byClass(stop, "sub").map(shown),
          said: said(stop),
        }));
    """)
    assert found["name"] == ["Henry Street"]
    assert found["quiet"] == ["towards Market Hall · 8 min walk"]
    assert found["said"] == "Henry Street, towards Market Hall, 8 min walk"


@needs_node
def test_a_stop_and_its_walk_are_kept_apart():
    found = run_js(_boards() + """
        const stop = cell(bodyRows("buses-board")[0], "c-stop");
        const sep = byClass(stop, "walk-sep")[0];
        console.log(JSON.stringify([said(stop), sep.getAttribute("aria-hidden"), sep.textContent]));
    """)
    assert found == ["Henry Street A, 8 min walk", "true", " · "]
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    # A real node, not generated content with alt text, which older browsers don't read.
    assert "::before { content: \" · \"" not in css
    assert re.search(r"\n\.walk-sep\s*\{\s*display:\s*none;", css)
    assert re.search(r"table\.board \.c-stop \.walk-sep\s*\{\s*display:\s*inline;", css)


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
    assert rows[0][2:] == ["9 min", "9 min, live, expected 07:11", 1, "Henry Street A, 8 min walk"]
    for live, scheduled, shown, spoken, minutes, _ in rows:
        if live:
            assert minutes == 1 and "sched." not in shown and ", live, " in spoken
        else:
            assert minutes == 0 and shown == f"{scheduled}sched." and spoken == f"{scheduled}, timetable only"
    assert any(not r[0] for r in rows) and any(r[0] for r in rows)
    assert found["words"] == ["Due", "Due", "4 min"]


@needs_node
def test_a_record_shows_in_the_row_only_when_it_is_notable():
    from twobells.dashboard.history import Stats, phrase

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
def test_the_sheet_and_reminders_call_a_replacement_bus_one():
    found = run_js(_sheet() + """
        import { describe } from "./alerts.js";
        const bus = { ...train(1), replacement_bus: true, platform: null };
        openTrain(bus);
        const facts = elements(summary).filter((el) => el.nodeName === "DD" || el.nodeName === "DT").map(spoken);
        const words = byClass(summary, "replacement").map((el) => [spoken(el), el.hasAttribute("hidden")]);
        const busTitle = spoken(title);
        openTrain({ ...train(1), platform: "2" });
        console.log(JSON.stringify({
          title: busTitle, facts, words, describe: describe(bus, "train"),
          train: [spoken(title), byClass(summary, "replacement").map((el) => el.hasAttribute("hidden")),
                  describe({ ...train(1), platform: "2" }, "train")],
        }));
    """)
    assert found["title"] == "07:20 replacement bus to Manchester Piccadilly"
    assert found["words"] == [["Replacement bus, running in place of the train", False]]
    platform = found["facts"].index("Platform")
    assert found["facts"][platform + 1] == "Bus"
    assert found["describe"] == "the 07:20 replacement bus to Manchester Piccadilly"
    assert found["train"] == ["07:20 to Manchester Piccadilly", [True],
                              "the 07:20 train to Manchester Piccadilly, platform 2"]


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
def test_the_focus_stays_in_the_sheet_when_its_departure_leaves_the_board():
    found = run_js(_sheet() + """
        openTrain({ ...train(1), leave_at: "2026-10-10T07:10:00+01:00" });
        remind.focus();
        refreshTrain(new Map());
        console.log(JSON.stringify([remind.hidden, document.activeElement === title]));
    """)
    assert found == [True, True]


@needs_node
def test_the_history_never_reads_inherited_keys_as_words():
    found = run_js(_sheet() + """
        HISTORY.day_type = "constructor";
        HISTORY.days = HISTORY.days.map((d) => ({ ...d, status: "toString" }));
        openTrain(train(1));
        await new Promise((resolve) => setTimeout(resolve, 0));
        const caption = elements(history).find((el) => el.nodeName === "CAPTION");
        console.log(JSON.stringify([spoken(caption), byClass(history, "badge-text").map((el) => el.textContent)[0]]));
    """)
    assert found[0].endswith(" days, newest first") and found[1] == "Unknown"


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


# --- Next to catch ---


# The hero on a fake page, with the live region. The clock starts at 07:00:00
# and `clock("07:01:30")` moves it on (never back); it doesn't move by itself. `bus(n, scheduled, leave)`
# and `train(...)` make rows; `rows(...)` indexes them as main.js does; `draw`
# renders the hero with your stops' names and `rain`. `announced` collects
# every announcement the live region makes.
HERO = """
import { h } from "./dom.js";
import { observe } from "./time.js";
import { renderHero, setUpHero } from "./hero.js";
const p = (id) => h("p", { id });
const hero = h("section", { id: "leave" }, h("h2", { id: "leave-heading" }), p("leave-time"), p("leave-what"),
  p("leave-sure"), p("leave-then"), p("leave-rain"), h("button", { id: "leave-remind" }), p("notify-note"));
const live = h("div", { id: "live" });
document.body.append(hero, live);
const announced = [];
globalThis.requestAnimationFrame = (fn) => { fn(); announced.push(live.textContent); };
const at = (hms) => `2026-10-10T${hms}+01:00`;
// The page's clock stands still between moves, so a time on a whole minute
// stays on it (real milliseconds passing would make 10 min read as 9).
const stopped = Date.now();
Date.now = () => stopped;
const clock = (hms) => observe(Date.parse(at(hms)));
clock("07:00:00");
const id = (n) => String(n).padStart(16, "0");
const bus = (n, scheduled, leave, extra = {}) => ({
  id: id(n), scheduled, line: "237", destination: "Ashton-under-Lyne Interchange", stop: "A", walk_min: 5,
  leave_at: leave && at(`${leave}:00`), scheduled_at: at(`${scheduled}:00`), status: "unknown", live: false,
  ghost: false, ...extra,
});
const train = (n, scheduled, leave, extra = {}) => ({
  id: id(n), scheduled, destination: "Manchester Piccadilly", platform: "1", walk_min: 12,
  leave_at: leave && at(`${leave}:00`), scheduled_at: at(`${scheduled}:00`), status: "on_time", ...extra,
});
const rows = (...list) => new Map(list.map((row) => [row.id, { row, mode: "platform" in row ? "train" : "bus" }]));
const ctx = { stops: { A: "Henry Street A" }, rain: "" };
const draw = (map, more = {}) => renderHero(map, { ...ctx, ...more });
const $ = (name) => document.getElementById(name);
// What shows on screen (skipping what only screen readers get) and what is said.
const shown = (el) => (el.nodeType === 3 ? el.nodeValue
  : (el.getAttribute("class") || "").split(" ").includes("vh") ? "" : el.childNodes.map(shown).join(""));
const said = (el) => spoken(el).replace(/\\s+/g, " ").trim();
const wait = () => new Promise((resolve) => setTimeout(resolve, 20));
"""


@needs_node
def test_the_hero_counts_seconds_only_under_two_minutes():
    found = run_js(HERO + """
        import { leaveWords } from "./hero.js";
        const s = 1000, min = 60 * s;
        const words = [64 * min + 30 * s, 6 * min + 59 * s, 2 * min, 2 * min - 1, 113.9 * s, 61 * s, 60 * s, 5 * s]
          .map(leaveWords);
        const map = rows(bus(1, "07:20", "07:15"));
        const state = (time) => {
          clock(time);
          draw(map);
          return [shown($("leave-time")), said($("leave-time")),
                  ["soon", "now"].filter((name) => hero.classList.contains(name))];
        };
        console.log(JSON.stringify({
          words, states: ["07:05:00", "07:12:00", "07:13:07", "07:14:15", "07:14:59"].map(state),
        }));
    """)
    assert found["words"] == [
        ["Leave in 1 h 4 min", "Leave in 1 h 4 min"],
        ["Leave in 6 min", "Leave in 6 min"],  # rounded down: never more time than there is
        ["Leave in 2 min", "Leave in 2 min"],
        ["Leave in 1:59", "Leave in 1 min 59 s"],
        ["Leave in 1:53", "Leave in 1 min 53 s"],
        ["Leave in 1:01", "Leave in 1 min 1 s"],
        ["Leave now", "Leave now"],
        ["Leave now", "Leave now"],
    ]
    assert found["states"] == [
        ["Leave in 10 min", "Leave in 10 min", []],
        ["Leave in 3 min", "Leave in 3 min", ["soon"]],  # amber from 3 minutes
        ["Leave in 1:53", "Leave in 1 min 53 s", ["soon"]],
        ["Leave now", "Leave now", ["now"]],  # the last minute
        ["Leave now", "Leave now", ["now"]],
    ]


@needs_node
def test_the_hero_keeps_a_departure_as_long_as_its_row_says_go_now():
    found = run_js(HERO + """
        const map = rows(bus(1, "07:20", "07:15"), bus(2, "07:30", "07:25"));
        const state = (time) => { clock(time); draw(map); return [shown($("leave-time")), shown($("leave-what")).slice(0, 5)]; };
        console.log(JSON.stringify(["07:14:30", "07:16:00", "07:16:59", "07:17:01"].map(state)));
    """)
    assert found == [
        ["Leave now", "07:20"],
        ["Leave now", "07:20"],  # its row says "Go now" until 2 min after its leaving time
        ["Leave now", "07:20"],
        ["Leave in 7 min", "07:30"],  # then the row drops, and so does the hero
    ]


@needs_node
def test_the_hero_says_how_sure_its_departure_is():
    found = run_js(HERO + """
        const sure = (row) => { draw(rows(row)); return said($("leave-sure")); };
        console.log(JSON.stringify([
          sure(bus(1, "07:20", "07:15", { live: true, status: "on_time" })),
          sure(bus(2, "07:20", "07:15")),
          sure(bus(3, "07:20", "07:15", { ghost: true })),
          sure(train(4, "07:20", "07:08")),
          sure(train(5, "07:20", "07:08", { status: "late", expected_at: at("07:24:00") })),
        ]));
    """)
    assert found == ["Live", "Timetable only", "May not be running", "Live", "Live"]


@needs_node
def test_the_hero_names_its_departure_on_one_line():
    found = run_js(HERO + """
        const line = (row) => { draw(rows(row)); return [shown($("leave-what")), said($("leave-what"))]; };
        console.log(JSON.stringify([
          line(bus(1, "17:55", "07:15")),
          line(train(2, "18:22", "07:15")),
          line(train(3, "18:22", "07:15", { platform: null, walk_min: null })),
        ]));
    """)
    assert found == [
        ["17:55 237 Ashton-under-Lyne Interchange · Henry Street A · 5 min walk",
         "17:55 237 bus to Ashton-under-Lyne Interchange, Henry Street A, 5 min walk"],
        ["18:22 train to Manchester Piccadilly · Platform 1 · 12 min walk",
         "18:22 train to Manchester Piccadilly, Platform 1, 12 min walk"],
        ["18:22 train to Manchester Piccadilly", "18:22 train to Manchester Piccadilly"],
    ]


@needs_node
def test_then_offers_the_next_train_and_the_next_bus():
    found = run_js(HERO + """
        const then = (...list) => {
          draw(rows(...list));
          const el = $("leave-then");
          return [el.hidden === true, shown(el), said(el), shown($("leave-what")).slice(0, 5)];
        };
        console.log(JSON.stringify({
          both: then(
            train(1, "07:05", "06:55", { status: "cancelled", leave_at: null }),  // cancelled: never the one
            bus(2, "07:12", null),  // no walk time: never the one
            bus(3, "07:15", "07:10"),
            train(4, "07:25", "07:13"),
            bus(5, "07:27", "07:22", { line: "341" }),
            bus(6, "07:40", "07:35"),
            train(7, "07:55", "07:43"),
          ),
          buses: then(bus(3, "07:15", "07:10"), bus(5, "07:27", "07:22", { line: "341" }), bus(6, "07:40", "07:35")),
          trainFirst: then(train(4, "07:25", "07:13"), bus(5, "07:27", "07:22", { line: "341" })),
          none: then(bus(3, "07:15", "07:10")),
        }));
    """)
    assert found["both"] == [False, "Then: 07:25 train (leave 07:13) · 07:27 341 (leave 07:22)",
                             "Then: 07:25 train (leave 07:13), 07:27 341 bus (leave 07:22)", "07:15"]
    assert found["buses"][:2] == [False, "Then: 07:27 341 (leave 07:22)"]
    assert found["trainFirst"] == [False, "Then: 07:27 341 (leave 07:22)", "Then: 07:27 341 bus (leave 07:22)",
                                   "07:25"]
    assert found["none"][:2] == [True, ""]


@needs_node
def test_the_hero_calls_a_replacement_bus_one():
    found = run_js(HERO + """
        const replacement = { replacement_bus: true, platform: null };
        draw(rows(train(1, "07:25", "07:13", replacement), train(2, "07:55", "07:43", replacement)));
        console.log(JSON.stringify([shown($("leave-what")), shown($("leave-then"))]));
    """)
    assert found == ["07:25 replacement bus to Manchester Piccadilly · 12 min walk",
                     "Then: 07:55 replacement bus (leave 07:43)"]


@needs_node
def test_a_bus_that_may_not_be_running_gives_way_to_a_live_one_soon_after():
    found = run_js(HERO + """
        import { nextCatchable } from "./hero.js";
        const ghost = bus(1, "07:15", "07:10", { ghost: true });
        const pick = (...list) => nextCatchable(rows(...list)).row.id.slice(-1);
        console.log(JSON.stringify([
          pick(ghost, bus(2, "07:19", "07:14", { live: true })),  // live, 4 min after: take it
          pick(ghost, bus(2, "07:20", "07:15", { live: true })),  // 5 min after: still
          pick(ghost, bus(2, "07:21", "07:16", { live: true })),  // 6 min after: the ghost stays
          pick(ghost, bus(2, "07:16", "07:11"), bus(3, "07:18", "07:13", { live: true })),  // timetable only doesn't count
          pick(ghost, train(4, "07:25", "07:13")),  // a train is live
          pick(bus(2, "07:15", "07:10"), bus(3, "07:16", "07:11", { live: true })),  // only a ghost gives way
          pick(ghost),
        ]));
    """)
    assert found == ["2", "2", "1", "3", "4", "2", "1"]


@needs_node
def test_the_hero_announces_a_new_departure_once_it_holds_and_never_the_countdown():
    found = run_js(HERO + """
        const map = rows(bus(1, "07:10", "07:05"), train(2, "07:20", "07:08"));
        draw(map);  // the first draw only notes what was there
        for (const time of ["07:01:00", "07:02:00", "07:04:30", "07:06:59"]) { clock(time); draw(map); }
        await wait();
        const ticking = [...announced];
        clock("07:07:01");  // the bus's "Go now" is over: the train is next
        draw(map);
        clock("07:07:20");
        draw(map);
        await wait();
        const early = [...announced];
        clock("07:07:31");  // it has held for 30 s
        draw(map);
        clock("07:07:40");
        draw(map);
        await wait();
        console.log(JSON.stringify({ ticking, early, after: announced, region: live.textContent,
                                     hero: said($("leave-what")) }));
    """)
    assert found["ticking"] == []
    assert found["early"] == []
    assert found["after"] == ["Next to catch is now the 07:20 train to Manchester Piccadilly."]
    assert found["region"] == "Next to catch is now the 07:20 train to Manchester Piccadilly."
    assert found["hero"].startswith("07:20 train to Manchester Piccadilly")


@needs_node
def test_the_hero_never_announces_a_departure_that_flips_back():
    # A bus whose tracker comes and goes gives way to a live one and takes the
    # place back, snapshot after snapshot: only a change that holds is spoken.
    found = run_js(HERO + """
        const timers = [];
        const realTimeout = globalThis.setTimeout;
        const ghostly = (ghost) => rows(bus(1, "07:40", "07:35", { ghost }), bus(2, "07:42", "07:37", { live: true }));
        const pointsAt = () => said($("leave-what")).slice(0, 5);
        const flips = [];
        draw(ghostly(false));
        // Flipping every 10 s for a minute and a half: never said.
        for (let s = 10; s <= 90; s += 10) {
          clock(`07:0${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);
          draw(ghostly(s % 20 === 10));
          flips.push(pointsAt());
        }
        await wait();
        const flipping = [...announced];
        // The live bus holds for 30 s: said once.
        clock("07:02:00"); draw(ghostly(true));
        clock("07:02:31"); draw(ghostly(true));
        await wait();
        const held = [...announced];
        // The first bus comes back and holds, but its words wait their turn
        // behind the last; the live bus is back before they are spoken.
        globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
        clock("07:03:00"); draw(ghostly(false));
        clock("07:03:31"); draw(ghostly(false));
        const waiting = timers.length;
        clock("07:03:40"); draw(ghostly(true));
        for (const fn of timers) fn();
        globalThis.setTimeout = realTimeout;
        await wait();
        console.log(JSON.stringify({ flips, flipping, held, waiting, after: announced }));
    """)
    assert found["flips"] == ["07:42", "07:40"] * 4 + ["07:42"]
    assert found["flipping"] == []
    assert found["held"] == ["Next to catch is now the 07:42 237 bus to Ashton-under-Lyne Interchange."]
    assert found["waiting"] == 1
    # The live region last said the live bus, and it is the one again: nothing more.
    assert found["after"] == found["held"]


@needs_node
def test_the_hero_has_its_own_remind_me_and_the_rain():
    found = run_js(HERO + """
        let changed = 0;
        setUpHero(() => { changed += 1; });
        const map = rows(bus(1, "07:20", "07:15"));
        draw(map, { rain: "Rain from 08:00 (70%)" });
        const button = $("leave-remind");
        const note = $("notify-note");
        const state = () => [button.getAttribute("aria-pressed"), said(button), note.hidden, said(note)];
        const before = state();
        const click = button.listeners.find(([type]) => type === "click")[1];
        click();
        const on = state();
        click();
        const off = state();
        const rain = [$("leave-rain").hidden, said($("leave-rain"))];
        draw(map);
        console.log(JSON.stringify({ before, on, off, changed, rain, dry: $("leave-rain").hidden }));
    """)
    # It says which departure it is for, as the sheet's title does there.
    assert found["before"] == ["false", "Remind me for the 07:20 237 bus to Ashton-under-Lyne Interchange", True, ""]
    # The same honest words as the sheet's: on plain http it shows only on the page.
    assert found["on"] == ["true", "Remind me for the 07:20 237 bus to Ashton-under-Lyne Interchange", False,
                           "The reminder shows only on this page, while it is open."]
    assert found["off"] == found["before"]
    assert found["changed"] == 2
    assert found["rain"] == [False, "Rain from 08:00 (70%)"]
    assert found["dry"] is True


@needs_node
def test_a_focused_remind_me_hidden_hands_the_focus_to_the_hero_s_heading():
    # WCAG 2.4.3: with nothing left to leave for, Remind me hides; if it had the
    # focus, the focus goes to the hero's heading rather than to nowhere.
    found = run_js(HERO + """
        setUpHero(() => {});
        draw(rows(bus(1, "07:20", "07:15")));
        $("leave-remind").focus();
        draw(rows());
        const gone = [$("leave-remind").hidden, document.activeElement.id];
        // Not focused: hiding it leaves the focus alone.
        draw(rows(bus(2, "07:30", "07:25")));
        $("leave-time").focus();
        draw(rows());
        console.log(JSON.stringify({ gone, elsewhere: document.activeElement.id }));
    """)
    assert found == {"gone": [True, "leave-heading"], "elsewhere": "leave-time"}


# --- Reminders while a dialog is open ---


@needs_node
def test_a_reminder_due_while_a_dialog_is_open_is_shown_and_said_in_it():
    # The page behind a modal dialog is inert: an alert or announcement made
    # there is neither seen nor heard.
    found = run_js("""
        import { h, openDialog, setUpDialog } from "./dom.js";
        import { observe } from "./time.js";
        import { tickAlerts, toggle } from "./alerts.js";
        observe(Date.parse("2026-10-10T07:00:00+01:00"));
        const heading = h("h2", { id: "leave-heading" });
        const banner = h("div", { id: "alert-banner", role: "alert" });
        const live = h("div", { id: "live", "aria-live": "polite" });
        const title = h("h2", { "data-dialog-title": "" });
        const inDialog = h("div", { "data-alerts": "", role: "alert" });
        const dialog = h("dialog", { id: "sheet" }, title, h("button", { "data-close": "" }), inDialog);
        dialog.showModal = () => { dialog.open = true; dialog.setAttribute("open", ""); };
        const closed = () => dialog.listeners.filter(([type]) => type === "close").map(([, fn]) => fn);
        dialog.close = () => { dialog.open = false; dialog.removeAttribute("open"); closed().forEach((fn) => fn()); };
        document.body.append(h("main", { id: "main" }, heading, banner), live, dialog);
        setUpDialog(dialog);
        const row = { id: "0123456789abcdef", scheduled: "07:20", destination: "Manchester Piccadilly",
                      leave_at: "2026-10-10T07:00:30+01:00", scheduled_at: "2026-10-10T07:20:00+01:00" };
        toggle(row.id, row, () => {});
        openDialog(dialog, null);
        const liveIn = live.parentNode === dialog;
        tickAlerts(new Map([[row.id, { row, mode: "train" }]]));
        const said = (el) => spoken(el).replace(/\\s+/g, " ").trim();
        const fired = [said(inDialog), said(banner)];
        // Dismissed in the dialog: gone from both, and the focus stays in the dialog.
        const dismiss = elements(inDialog).find((el) => el.nodeName === "BUTTON");
        dismiss.focus();
        dismiss.listeners.find(([type]) => type === "click")[1]();
        const dismissed = [said(inDialog), said(banner), document.activeElement === title];
        dialog.close();
        console.log(JSON.stringify({ liveIn, fired, dismissed, liveBack: live.parentNode === document.body }));
    """)
    alert = "Time to leave. Leave now for the 07:20 train to Manchester Piccadilly.Dismiss"
    assert found["liveIn"] is True
    assert found["fired"] == [alert, alert]
    assert found["dismissed"] == ["", "", True]
    assert found["liveBack"] is True


def test_every_dialog_has_a_place_for_alerts():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    dialogs = re.findall(r"<dialog.*?</dialog>", html, re.S)
    assert len(dialogs) == 2
    for dialog in dialogs:
        assert '<div class="alert-banner" data-alerts role="alert"></div>' in dialog


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
    from twobells.web.server import VERSION_SLOT

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
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
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
        assert contrast(fg, bg) >= need, f"{fg} on {bg} is {contrast(fg, bg):.2f}:1"


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
    # The hour it is now (07:01) is "Now".
    assert found["first"] == [
        "Now, light rain, 11 degrees, 70% chance of rain",
        "08:00, light rain, 11 degrees, 70% chance of rain",
        "09:00, rain, 12 degrees, 85% chance of rain",
        "10:00, rain showers, 12 degrees, 60% chance of rain",
    ]
    assert found["summary"] == "More hours"
    assert [s[:5] for s in found["later"]] == ["11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
    # Named regions; a tab stop only while they overflow (see below), which a
    # page without layout never does.
    assert found["regions"] == [["region", None, "weather-hours-heading"], ["region", None, "Later hours"]]
    # The fixture's 07:01 is before sunrise, so sunrise is the one shown.
    assert found["now"] == ("11° Light rain"
                            "Feels like 9 degrees. Wind 12 mph from the west-south-west, gusts 25 mph. Sunrise 07:24.")


@needs_node
def test_the_weather_calls_the_hour_it_is_now_now():
    found = run_js(_weather_panel() + """
        const label = () => byClass(hours, "wx-hour").map((el) => el.textContent).slice(0, 2);
        const said = () => items(hours).map((li) => spoken(li).split(",")[0]).slice(0, 2);
        renderWeather(snapshot);
        const current = [label(), said()];
        // A forecast that starts with an hour still to come has no "Now".
        const later = structuredClone(snapshot);
        later.weather.hours = later.weather.hours.slice(1);
        renderWeather(later);
        console.log(JSON.stringify([current, [label(), said()]]));
    """)
    # At 07:01 the 07:00 hour is the one it is now.
    assert found == [[["Now", "08:00"], ["Now", "08:00"]], [["08:00", "09:00"], ["08:00", "09:00"]]]


@needs_node
def test_a_weather_strip_is_a_tab_stop_only_while_it_overflows():
    # Keyboard users need to reach a strip to scroll it, and only then.
    found = run_js(_weather_panel() + """
        renderWeather(snapshot);
        const [first] = byClass(hours, "wx-strip");
        const stop = () => first.getAttribute("tabindex");
        const fits = stop();
        first.scrollWidth = 480;
        first.clientWidth = 320;
        renderWeather(snapshot);  // a new snapshot, or the window resized
        const overflows = [byClass(hours, "wx-strip")[0] === first, stop()];
        first.clientWidth = 480;
        renderWeather(snapshot);
        console.log(JSON.stringify([fits, overflows, stop()]));
    """)
    assert found == [None, [True, "0"], None]


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


# --- Kiosk mode: a wall tablet at /?kiosk ---

# Every test page below is a wall tablet's: theme.js has marked <html>.
KIOSK = 'document.documentElement = { dataset: { kiosk: "on" } };\n'


@needs_node
def test_kiosk_mode_and_its_board_look_are_set_before_the_first_paint_from_the_address():
    found = run_js("""
        import { readFileSync } from "node:fs";
        const source = readFileSync("theme.js", "utf-8");
        const run = (search, saved) => {
          const root = { dataset: { theme: "modern" } };
          document.documentElement = root;
          const storage = { getItem: () => { if (saved === "blocked") throw new Error("blocked"); return saved; } };
          for (const [name, value] of [["location", { search }], ["localStorage", storage]]) {
            Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
          }
          new Function(source)();
          return [root.dataset.kiosk ?? null, root.dataset.theme];
        };
        console.log(JSON.stringify([
          run("", null), run("", "board"), run("?kiosk", null), run("?kiosk", "modern"), run("?kiosk", "board"),
          run("?kiosk", "blocked"), run("?from=home&kiosk=1", null), run("?kiosks", null),
        ]));
    """)
    assert found == [
        [None, "modern"],  # the page as ever
        [None, "board"],
        ["on", "board"],  # a wall tablet takes the Board look...
        ["on", "modern"],  # ...unless this browser chose a look
        ["on", "board"],
        ["on", "board"],  # storage blocked: still a wall tablet
        ["on", "board"],
        [None, "modern"],
    ]
    # theme.js is a classic script in <head>, so it runs before the page is
    # drawn; the modules run after parsing.
    tags = [tag for tag, _ in _page().elements]
    scripts = [attrs for tag, attrs in _page().elements if tag == "script"]
    assert scripts[0] == {"src": "/static/theme.js"}
    assert tags.index("script") < tags.index("body")


class _Tree(HTMLParser):
    """index.html's elements, each with the (tag, attrs) of its ancestors."""

    VOID = frozenset({"meta", "link", "br", "img", "input", "hr", "source"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.found = []  # (tag, attrs, ancestors)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.found.append((tag, attrs, list(self.stack)))
        if tag not in self.VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break


def _kiosk_hidden():
    """The selectors app.css hides on a wall tablet, without their :root[data-kiosk] part."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
    hidden = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if re.search(r"(?:^|;)\s*display:\s*none\s*(?:;|$)", body.strip()):
            for selector in selectors.split(","):
                selector = selector.strip()
                # Elements only: a hidden ::marker hides no control.
                if selector.startswith(":root[data-kiosk] ") and "::" not in selector:
                    hidden.append(selector.removeprefix(":root[data-kiosk] "))
    return hidden


def _compound(selector, tag, attrs):
    """Whether a compound selector of a tag, an #id and .classes matches."""
    parts = re.findall(r"^[a-z0-9]+|#[\w-]+|\.[\w-]+", selector)
    assert "".join(parts) == selector, f"can't read {selector}"
    classes = attrs.get("class", "").split()

    def one(part):
        if part[0] == "#":
            return attrs.get("id") == part[1:]
        return part[1:] in classes if part[0] == "." else part == tag

    return all(one(part) for part in parts)


def _matches(selector, path):
    """Whether a selector of compounds joined by spaces (descendants) matches
    the last of path, a list of (tag, attrs) from the root down."""
    *outer, last = selector.split()
    if not _compound(last, *path[-1]):
        return False
    for tag, attrs in reversed(path[:-1]):
        if outer and _compound(outer[-1], tag, attrs):
            outer.pop()
    return not outer


def test_a_wall_tablet_page_has_nothing_to_focus_or_press():
    # Everything in index.html that takes a click or the focus is hidden by
    # app.css on a wall tablet, itself or by a part it is in. The one left is
    # the disruption line's <summary>, which disruptions.js takes out of the
    # tab order (tested below). Dialogs are closed, and nothing opens them.
    tree = _Tree()
    tree.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    hidden = _kiosk_hidden()
    left = []
    for tag, attrs, ancestors in tree.found:
        focusable = (tag in ("button", "summary", "input", "select", "textarea") or (tag == "a" and "href" in attrs)
                     or attrs.get("tabindex", "-1") != "-1")
        if not focusable or any(t == "dialog" for t, _ in ancestors):
            continue
        path = [*ancestors, (tag, attrs)]
        if not any(_matches(s, path[:i + 1]) for s in hidden for i in range(len(path))):
            left.append(attrs.get("id") or attrs.get("class") or tag)
    assert left == ["dis-line"]
    # And a click on a row opens nothing on a wall tablet.
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "if (kiosk) return; // nothing on a wall tablet opens" in main


def _kiosk_rule(selector):
    """The declarations app.css gives exactly this :root[data-kiosk] selector, wherever they are."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)
    return " ".join(body for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
                    if f":root[data-kiosk] {selector}" in [s.strip() for s in selectors.split(",")])


def test_a_wall_tablet_header_and_rows_keep_to_their_lines():
    # The header is one line: the place gives way (cut short), never the clock or the status.
    assert "flex-wrap: nowrap" in _kiosk_rule(".top") and "width: 100%" in _kiosk_rule(".top")
    assert "white-space: nowrap" in _kiosk_rule(".top h1") and "min-width: 0" in _kiosk_rule(".top h1")
    assert "flex: none" in _kiosk_rule(".top .connection") and "flex: none" in _kiosk_rule(".top .clock")
    # Rows: the stop never wraps, so the destination does; the leave time's
    # second line goes, and the captions are for screen readers only.
    assert "white-space: nowrap" in _kiosk_rule("table.board .c-stop")
    assert "display: none" in _kiosk_rule("table.board .leave-at")
    caption = _kiosk_rule("table.board caption")
    assert "clip-path: inset(50%)" in caption and "display: none" not in caption


@needs_node
def test_a_wall_tablet_board_has_four_rows_and_no_controls():
    found = run_js(KIOSK + _boards() + """
        import { markBusRows } from "./disruptions.js";
        const rowsById = new Map(snapshot.buses.rows.map((row) => [row.id, { row, mode: "bus" }]));
        markBusRows(snapshot, rowsById);
        const controls = ["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"];
        const focusable = (el) => elements(el).filter((e) => controls.includes(e.nodeName)
          || (e.hasAttribute("tabindex") && e.getAttribute("tabindex") !== "-1"));
        const rows = [...bodyRows("trains-board"), ...bodyRows("buses-board")];
        console.log(JSON.stringify({
          rows: [bodyRows("trains-board").length, bodyRows("buses-board").length],
          hidden: rows.filter((tr) => tr.hasAttribute("hidden")).length,
          focusable: [...focusable(board("trains-board")), ...focusable(board("buses-board"))].map((e) => e.nodeName),
          dest: said(byClass(bodyRows("trains-board")[0], "dest")[0]),
        }));
    """)
    # The fixture has 10 trains and 9 buses still to catch: 4 of each, no "Show later departures".
    assert found["rows"] == [4, 4] and found["hidden"] == 0
    # No open buttons, no disruption links: the destination is plain text.
    assert found["focusable"] == []
    assert found["dest"] == "Manchester Piccadilly"


@needs_node
def test_a_wall_tablet_hero_has_no_remind_me():
    found = run_js(HERO + KIOSK + """
        draw(rows(bus(1, "07:20", "07:10")));
        console.log(JSON.stringify([$("leave-remind").hidden, shown($("leave-time"))]));
    """)
    assert found == [True, "Leave in 10 min"]


@needs_node
def test_a_wall_tablet_disruption_line_is_its_summary_only():
    found = run_js(DISRUPTION_PAGE + KIOSK + """
        $("disruptions-details").open = true;
        renderDisruptions(at({ disruptions: { status: {}, items: [item("a000000000000001", "your_stop")] } }));
        const line = $("disruptions-details").childNodes[0];
        console.log(JSON.stringify([words($("disruptions-summary")), $("disruptions-details").open,
                                    line.nodeName, line.getAttribute("tabindex")]));
    """)
    assert found == ["Roadworks at Station Road (your stop)", False, "SUMMARY", "-1"]
    # What would open it is hidden too.
    assert ".dis-body" in _kiosk_hidden()


@needs_node
def test_a_wall_tablet_weather_is_now_and_four_hours_with_nothing_to_focus():
    found = run_js(KIOSK + _weather_panel() + """
        renderWeather(snapshot);
        console.log(JSON.stringify({
          parts: hours.childNodes.map((el) => el.nodeName),
          hours: items(hours).length,
          focus: elements(hours).filter((el) => el.hasAttribute("tabindex") || el.hasAttribute("role")).length,
          more: document.getElementById("weather-more"),
        }));
    """)
    assert found == {"parts": ["DIV"], "hours": 4, "focus": 0, "more": None}


@needs_node
def test_the_wall_tablet_shifts_a_pixel_or_two_and_never_far():
    found = run_js("""
        import { h } from "./dom.js";
        import { SHIFTS, SHIFT_MS, applyShift, shiftAt } from "./kiosk.js";
        const root = h("html", {});
        const applied = [0, 1, 2, SHIFTS.length, SHIFTS.length + 1].map((step) => {
          applyShift(root, step);
          return [root.style.getPropertyValue("--shift-x"), root.style.getPropertyValue("--shift-y")];
        });
        console.log(JSON.stringify({ shifts: SHIFTS, every: SHIFT_MS, applied, wraps: shiftAt(SHIFTS.length) === SHIFTS[0] }));
    """)
    shifts = found["shifts"]
    assert shifts[0] == [0, 0] and len({tuple(s) for s in shifts}) == len(shifts) > 4
    assert all(abs(x) <= 2 and abs(y) <= 2 for x, y in shifts)
    # Each step, the last back to the first included, moves the page 1 or 2 px.
    for (x1, y1), (x2, y2) in zip(shifts, shifts[1:] + shifts[:1], strict=True):
        assert 1 <= max(abs(x2 - x1), abs(y2 - y1)) <= 2
    assert 60_000 <= found["every"] <= 10 * 60_000  # every few minutes
    assert found["wraps"] is True
    assert found["applied"] == [[f"{x}px", f"{y}px"] for x, y in (shifts[0], shifts[1], shifts[2], shifts[0], shifts[1])]
    # The page moves by the property; a jump, so there is no transition to reduce.
    body = " ".join(_css_rules(":root[data-kiosk] body"))
    assert "translate(var(--shift-x, 0px), var(--shift-y, 0px))" in body and "transition" not in body


@needs_node
def test_a_wall_tablet_dims_while_the_snapshot_says_a_quiet_window_is_on():
    found = run_js("""
        import { applyDim } from "./kiosk.js";
        const root = { dataset: { kiosk: "on" } };
        const states = [{ quiet: true }, { quiet: false }, { quiet: true }, {}, { quiet: "yes" }].map((snapshot) => {
          applyDim(root, snapshot);
          return root.dataset.dim ?? null;
        });
        console.log(JSON.stringify(states));
    """)
    assert found == ["on", None, "on", None, None]
    # Dimmed is darker: black, with the words at most 60% of the light they
    # have in the look's own dark palette. test_theme_contrast keeps them legible.
    palettes = _palettes()
    for dim, look in (("kiosk-dim", "modern-dark"), ("kiosk-dim-board", "board")):
        assert luminance(palettes[dim]["fg"]) <= 0.6 * luminance(palettes[look]["fg"])
        assert palettes[dim]["bg"] == "#000000"
    # Only on a wall tablet, in a quiet window.
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ':root[data-kiosk][data-dim]:not([data-theme="board"]) {' in css
    assert ':root[data-kiosk][data-dim][data-theme="board"] {' in css


STALE_BANNER = """
import { h } from "./dom.js";
import { STATE } from "./api.js";
import { pill } from "./header.js";
import { drawBanner } from "./kiosk.js";
const banner = h("p", { id: "stale-banner", hidden: true });
document.body.append(banner);
const status = (extra) => ({ last_success: "2026-10-10T17:42:00+01:00", stale: false, error: null, note: null, ...extra });
const snapshot = (trains) => ({ trains: { status: status(trains) }, buses: { status: status() } });
const show = (state, data) => {
  const [kind, , words] = pill(state, data, 0, Date.now());
  drawBanner(kind, words);
  return banner.hidden ? null : spoken(banner).trim();
};
"""


@needs_node
def test_a_wall_tablet_says_across_the_room_when_it_is_behind():
    found = run_js(STALE_BANNER + """
        console.log(JSON.stringify([
          show(STATE.live, snapshot({})),
          show(STATE.live, snapshot({ stale: true })),
          show(STATE.polling, snapshot({ error: "timed out" })),
          show(STATE.live, snapshot({})),
          show(STATE.offline, snapshot({})),
          show(STATE.reconnecting, snapshot({})),
        ]));
    """)
    assert found == [
        None,
        "Not updated since 17:42",
        "Not updated since 17:42",
        None,  # and gone again once it catches up
        "Offline",  # the server can't be reached
        None,  # a moment's reconnecting is only the pill's
    ]
    # Full width, outside the page's parts, and large: to be seen from 2 m.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'</header>\s*<p id="stale-banner" class="stale-banner" hidden></p>\s*<main', html)
    rule = " ".join(_css_rules(".stale-banner"))
    assert "font-size: var(--t-xl);" in rule and "background: var(--late-fg);" in rule


@needs_node
def test_a_wall_tablet_reloads_once_a_day_at_half_three_when_the_server_answers():
    found = run_js("""
        import { RELOAD_AT, RETRY_MS, msUntil, scheduleReload } from "./kiosk.js";
        const local = (d, h, m) => new Date(2026, 9, d, h, m).getTime();
        const timers = [];
        let reloads = 0;
        const answers = [false, "throws", true];
        const reachable = async () => {
          const next = answers.shift();
          if (next === "throws") throw new Error("down");
          return next;
        };
        scheduleReload({ now: () => local(10, 14, 0), setTimer: (fn, ms) => timers.push([fn, ms]), reachable,
                         reload: () => { reloads += 1; } });
        const delays = [timers[0][1]];
        for (let i = 0; i < 3; i++) {
          await timers[i][0]();
          delays.push(timers[i + 1] ? timers[i + 1][1] : null);
        }
        console.log(JSON.stringify({
          at: RELOAD_AT, retry: RETRY_MS, delays, reloads,
          until: [msUntil(RELOAD_AT, local(10, 2, 0)), msUntil(RELOAD_AT, local(10, 3, 30)),
                  msUntil(RELOAD_AT, local(10, 3, 31))],
        }));
    """)
    assert found["at"] == [3, 30]
    hour = 3_600_000
    assert found["until"] == [1.5 * hour, 24 * hour, 24 * hour - 60_000]
    # At 14:00 the first try is 13.5 h off; the server down twice, it tries
    # again every 5 min, then reloads, once.
    assert found["delays"] == [13.5 * hour, found["retry"], found["retry"], None]
    assert found["retry"] == 5 * 60_000 and found["reloads"] == 1
