"""Static checks on the setup wizard's browser files, as for the dashboard's.

The page runs under setupapi.PAGE_HEADERS (`script-src 'self'; style-src
'self'`), sets server text only as text, and borrows the dashboard's dom.js
and vendored Leaflet through the paths setupapi.static_files() serves. These
tests keep it that way without a browser.
"""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pytest

from departureboard import keytest
from departureboard.settingsform import KEYS
from departureboard.setupapi import PAGE_HEADERS, static_files

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "departureboard" / "wizard_static"
OWN = sorted(p for p in STATIC.rglob("*") if p.is_file())
PREFIX = "http://pi.local/setup/"  # any prefix ending in a slash
STEPS = ("home", "station", "stops", "walk", "keys", "finish", "done")


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


def _ids():
    return {attrs["id"]: (tag, attrs) for tag, attrs in _page().elements if "id" in attrs}


def _served(url):
    """The file a host answers a URL under the prefix with, per static_files()."""
    assert url.startswith(PREFIX), f"{url} is outside the wizard's prefix"
    return static_files().get(url.removeprefix(PREFIX))


def test_only_the_three_files_are_shipped():
    assert [p.name for p in OWN] == ["index.html", "wizard.css", "wizard.js"]


def test_every_served_file_exists():
    for path in static_files().values():
        assert path.is_file(), path


def test_page_has_no_inline_script_style_or_handlers():
    page = _page()
    assert not page.inline_script
    assert "style" not in [tag for tag, _ in page.elements]
    for tag, attrs in page.elements:
        assert "style" not in attrs, f"<{tag}> has a style attribute"
        assert not [name for name in attrs if name.startswith("on")], f"<{tag}> has a handler attribute"
        if tag == "script":
            assert attrs.get("src") and attrs.get("type") == "module"


def test_page_references_resolve_to_served_files():
    refs = [attrs.get(name) for _, attrs in _page().elements for name in ("href", "src")]
    refs = [ref for ref in refs if ref and not ref.startswith(("#", "https://"))]
    assert "wizard_static/wizard.js" in refs
    for ref in refs:
        assert not ref.startswith("/"), f"{ref}: relative, so any prefix works"
        found = _served(urljoin(PREFIX, ref))
        assert found is not None and found.is_file(), ref


IMPORT = re.compile(
    r"""(?:\bimport\s*\(\s*|\bimport\s+(?:[\w*{}\s,$]+\s+from\s+)?|\bexport\s+[\w*{}\s,$]+\s+from\s+)["']([^"']+)["']"""
)


def test_js_imports_resolve_to_served_files():
    js_url = urljoin(PREFIX, "wizard_static/wizard.js")
    specs = IMPORT.findall((STATIC / "wizard.js").read_text(encoding="utf-8"))
    assert "../dashboard_static/dom.js" in specs
    for spec in specs:
        assert spec.startswith(("./", "../")), f"bare or absolute import {spec!r}"
        assert (STATIC / spec).resolve().is_file(), spec  # in the source tree
        found = _served(urljoin(js_url, spec))  # and where a host serves it
        assert found is not None and found.resolve() == (STATIC / spec).resolve(), spec
    # The modules wizard.js imports may import nothing the host doesn't serve.
    dom = (STATIC / "../dashboard_static/dom.js").read_text(encoding="utf-8")
    assert not IMPORT.findall(dom)


def test_leaflet_css_resolves_to_a_served_file():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    base = re.search(r'const LEAFLET = "([^"]+)"', text).group(1)
    url = urljoin(urljoin(PREFIX, "wizard_static/wizard.js"), f"{base}leaflet.css")
    assert _served(url) is not None


def test_leaflet_loads_only_when_a_map_opens():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    static = re.findall(r"""^\s*import\s+(?:[^"'();]*?\s+from\s+)?["']([^"']+)["']""", text, re.M)
    assert static == ["../dashboard_static/dom.js"]
    assert 'import("../dashboard_static/vendor/leaflet-1.9.4/leaflet-src.esm.js")' in text


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
    (re.compile(r"\blocalStorage\b|\bsessionStorage\b|\bindexedDB\b"), "browser storage"),
    (re.compile(r"\bconsole\.\w+\("), "console logging"),
]


@pytest.mark.parametrize("path", OWN, ids=lambda p: p.name)
def test_no_html_sinks_storage_or_logging(path):
    text = path.read_text(encoding="utf-8")
    found = [name for pattern, name in BANNED if pattern.search(text)]
    assert not found, f"{path.name} uses {found}"


def test_leaflet_gets_nodes_never_strings():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    calls = re.findall(r"\.bind(?:Popup|Tooltip)\(\s*([^,)]+)", text)
    assert calls
    for call in calls:
        assert call.strip().startswith("h("), call
    icons = re.findall(r"divIcon\(\{\s*html:\s*([^,]+)", text)
    assert icons and all(icon.strip().startswith("h(") for icon in icons), icons
    assert "attributionControl: false" in text  # the credit is our own text under each map
    assert "keyboard: false" in text  # markers repeat the list, so they aren't extra tab stops


def test_the_api_is_called_by_relative_url_with_the_csrf_token():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    assert "fetch(`api/${name}`" in text and 'fetch("api/session"' in text
    assert '"X-CSRF-Token": state.csrf' in text and 'credentials: "same-origin"' in text
    assert not re.search(r"""fetch\(\s*["'`](/|https?:)""", text)


def test_key_inputs_are_write_only():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    draw = text[text.index("function drawKeys"):text.index("function enterKeys")]
    assert 'autocomplete: "off"' in draw and "value:" not in draw  # never filled in from the server
    assert "keyTestButton(key.name, input)" in draw  # the hook for "Test this key" (bead 0fm.8)


def _between(text, start, end):
    return text[text.index(start):text.index(end)]


def test_key_test_says_its_result_politely_in_words():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    hook = _between(text, "function keyTestButton", "function signUp")
    assert 'type: "button"' in hook and '"Test this key"' in hook
    assert 'role: "status", "aria-live": "polite"' in hook
    # Every state the host can answer has a glyph and words; the glyph is
    # hidden from screen readers, which read the words.
    found = re.findall(r'(\w+): \["([^"]+)", "([^"]+)"\]', _between(text, "const RESULTS", "const LOOKUP"))
    results = {state: (glyph, words) for state, glyph, words in found}
    assert set(keytest.STATES) <= set(results)
    assert all(glyph and words for glyph, words in results.values())
    assert '"aria-hidden": "true" }, glyph), h("strong", {}, `${words}:`)' in text
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    for state in keytest.STATES:
        assert re.search(r"\.key-result-%s\b[^{]*\{ color: var\(--(ok-fg|error|warn-fg)\); \}" % state, css), state


def test_key_test_asks_before_spending_a_transportapi_lookup():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    test = _between(text, "async function testKey", "function keyTestButton")
    assert 'call("testkey", body)' in test
    assert test.index("window.confirm(LOOKUP_WARNING)") < test.index('call("testkey"')
    assert 'name === "TAPI_APP_KEY" && !window.confirm(LOOKUP_WARNING)' in test
    assert "1 of today's 30" in _between(text, "const LOOKUP_WARNING", "function showResult")


def test_every_key_on_the_page_says_how_to_get_it():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    sign_up = _between(text, "const SIGN_UP", "const TESTED")
    for name, _, _ in KEYS:
        assert re.search(r"\b%s: \[\s*\"" % name, sign_up), name
    assert "The full guide (opens in a new tab)" in text and 'rel: "noopener noreferrer"' in text


# Accessibility.


def test_page_is_british_english_and_says_what_it_is():
    page = _page()
    assert [attrs for tag, attrs in page.elements if tag == "html"] == [{"lang": "en-GB"}]
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<title>Set up your board</title>" in html


def test_each_step_is_a_section_with_a_focusable_heading():
    ids = _ids()
    for step in STEPS:
        tag, attrs = ids[f"step-{step}"]
        assert tag == "section" and attrs["aria-labelledby"] == f"{step}-heading" and "hidden" in attrs
        tag, attrs = ids[f"{step}-heading"]
        assert tag == "h2" and attrs.get("tabindex") == "-1"
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    assert "$(`${step}-heading`).focus()" in text


def test_announcements_have_one_polite_region_one_alert_and_the_job_status():
    elements = _page().elements
    live = sorted((attrs["id"], attrs["aria-live"]) for _, attrs in elements if "aria-live" in attrs)
    assert live == [("job-status", "polite"), ("live", "polite")]
    assert [attrs["id"] for _, attrs in elements if attrs.get("role") == "alert"] == ["alert"]
    ids = _ids()
    assert ids["job-progress"][0] == "progress"


def test_every_control_has_a_label_and_its_error_is_described():
    elements = _page().elements
    labelled = {attrs["for"] for tag, attrs in elements if tag == "label"}
    ids = _ids()
    for tag, attrs in elements:
        if tag == "input":
            assert attrs["id"] in labelled, attrs["id"]
            error = f"{attrs['id']}-error"
            if error in ids:
                assert error in attrs.get("aria-describedby", "").split(), attrs["id"]
        if tag == "button":
            assert attrs.get("type") in ("button", "submit"), attrs
    for name in ("station", "stops"):
        assert f"{name}-error" in ids[f"{name}-set"][1]["aria-describedby"]


def test_map_toggles_say_whether_they_are_open():
    ids = _ids()
    for name in ("home", "station", "stops"):
        tag, attrs = ids[f"{name}-map-toggle"]
        assert tag == "button" and attrs["aria-expanded"] == "false" and attrs["aria-controls"] == f"{name}-map-box"
        assert "hidden" in ids[f"{name}-map-box"][1]
        assert ids[f"{name}-map"][1].get("role") == "region"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.count("OpenStreetMap contributors") == 3


def _palettes():
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    palettes = {}
    for name in ("light", "dark"):
        match = re.search(r"/\* theme: %s \*/[^{]*\{(?:[^{}]*\{)?([^{}]*)\}" % name, css)
        assert match, name
        palettes[name] = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", match.group(1)))
    return {"light": palettes["light"], "dark": {**palettes["light"], **palettes["dark"]}}


def _luminance(colour):
    channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("muted", "surface"), ("muted", "bg"), ("accent", "surface"),
    ("accent-fg", "accent"), ("error", "surface"), ("ok-fg", "ok-bg"), ("ok-fg", "surface"),
    ("warn-fg", "warn-bg"), ("warn-fg", "surface"), ("tag-fg", "tag-bg"), ("pin-fg", "pin-bg"),
    ("pin-fg", "pin-chosen-bg"),
]
OUTLINE_PAIRS = [("focus", "bg"), ("focus", "surface"), ("edge", "surface"), ("edge", "bg"), ("error", "surface")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_contrast(theme):
    tokens = _palettes()[theme]
    for pairs, need in ((TEXT_PAIRS, 4.5), (OUTLINE_PAIRS, 3.0)):
        for fg, bg in pairs:
            ratio = _contrast(tokens[fg], tokens[bg])
            assert ratio >= need, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1"


def test_targets_are_big_enough():
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    button = re.search(r"\nbutton \{([^}]*)\}", css).group(1)
    assert "min-height: 2.75rem" in button and "min-width: 2.75rem" in button
    assert re.search(r"\.option \{[^}]*min-height: 2\.75rem", css)
    assert re.search(r"\.option input \{[^}]*width: 1\.5rem; height: 1\.5rem", css)  # 24 px


def test_page_headers_allow_only_what_the_page_needs():
    csp = PAGE_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self';" in csp and "style-src 'self';" in csp
    assert "unsafe" not in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "img-src 'self' data: https://tile.openstreetmap.org" in csp
    assert PAGE_HEADERS["X-Content-Type-Options"] == "nosniff" and PAGE_HEADERS["Cache-Control"] == "no-store"
