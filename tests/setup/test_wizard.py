"""Static checks on the setup wizard's browser files, as for the dashboard's.

The page runs under setupapi.PAGE_HEADERS (`script-src 'self'; style-src
'self'`), sets server text only as text, and borrows the dashboard's dom.js,
icons.js, vendored Leaflet and Inter through the paths setupapi.static_files()
serves. These tests keep it that way without a browser.

The script is wizard.js and the modules beside it, one per step and a few
they share; the checks on a step's code read that step's module.
"""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from urllib.parse import urljoin

import pytest

from tests.contrast import assert_contrast
from tests.paths import FIXTURES, ROOT
from twobells.settings.schema import keys
from twobells.setup import keytest
from twobells.setup.api import PAGE_HEADERS, static_files

DASHBOARD = ROOT / "src" / "twobells" / "dashboard_static"

# The wizard offers only the keys every view uses; each view's own are on its settings tab.
KEYS = keys("general")

STATIC = ROOT / "src" / "twobells" / "wizard_static"
OWN = sorted(p for p in STATIC.rglob("*") if p.is_file())
PREFIX = "http://pi.local/setup/"  # any prefix ending in a slash
STEPS = ("home", "station", "stops", "walk", "keys", "views", "finish", "done")
SCRIPTS = ("api.js", "finish.js", "home.js", "keys.js", "maps.js", "roads.js", "say.js", "state.js", "station.js",
           "steps.js", "stops.js", "views.js", "walk.js", "wizard.js")


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


def _js(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _all_js():
    """Every module's text, for checks on the script as a whole."""
    return "\n".join(_js(name) for name in SCRIPTS)


def _served(url):
    """The file a host answers a URL under the prefix with, per static_files()."""
    assert url.startswith(PREFIX), f"{url} is outside the wizard's prefix"
    return static_files().get(url.removeprefix(PREFIX))


def test_only_the_listed_files_are_shipped_and_each_is_served():
    assert [p.name for p in OWN] == sorted(["index.html", "wizard.css", *SCRIPTS])
    served = {path for path in static_files() if path.startswith("wizard_static/")}
    assert served == {f"wizard_static/{p.name}" for p in OWN if p.name != "index.html"}


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


@pytest.mark.parametrize("name", SCRIPTS)
def test_js_imports_resolve_to_served_files(name):
    js_url = urljoin(PREFIX, f"wizard_static/{name}")
    specs = IMPORT.findall(_js(name))
    assert "../dashboard_static/dom.js" in specs
    for spec in specs:
        assert spec.startswith(("./", "../")), f"bare or absolute import {spec!r}"
        assert (STATIC / spec).resolve().is_file(), spec  # in the source tree
        found = _served(urljoin(js_url, spec))  # and where a host serves it
        assert found is not None and found.resolve() == (STATIC / spec).resolve(), spec
    # The dashboard's modules the wizard imports may import nothing the host doesn't serve.
    for borrowed in ("dom.js", "icons.js"):
        url = urljoin(PREFIX, f"dashboard_static/{borrowed}")
        for spec in IMPORT.findall((DASHBOARD / borrowed).read_text(encoding="utf-8")):
            found = _served(urljoin(url, spec))
            assert found is not None and found.resolve() == (DASHBOARD / spec).resolve(), (borrowed, spec)


def test_leaflet_css_resolves_to_a_served_file():
    base = re.search(r'const LEAFLET = "([^"]+)"', _js("maps.js")).group(1)
    url = urljoin(urljoin(PREFIX, "wizard_static/maps.js"), f"{base}leaflet.css")
    assert _served(url) is not None


def test_inter_resolves_to_the_dashboards_served_copy():
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    [src] = re.findall(r'src: url\("([^"]+)"\) format\("woff2"\)', css)
    found = _served(urljoin(urljoin(PREFIX, "wizard_static/wizard.css"), src))
    assert found is not None and found.resolve() == (DASHBOARD / "vendor/inter-4.1/InterVariable.woff2").resolve()
    assert re.search(r'--font: "Inter", system-ui', css)


def _icon_names():
    return set(re.findall(r'^  "([a-z-]+)": \[', (DASHBOARD / "icons.js").read_text(encoding="utf-8"), re.M))


def test_every_icon_the_wizard_asks_for_is_in_the_set():
    asked = set()
    for name in SCRIPTS:
        for call in re.findall(r"\bicon\(([^()]*)\)", _js(name)):
            asked.update(re.findall(r'"([a-z-]+)"', call))
    assert asked and asked <= _icon_names(), asked - _icon_names()


PICTOGRAPHS = re.compile("[←-⇿⌀-⏿①-⓿■-➿⬀-⯿︎️]")


def test_no_text_glyphs_stand_in_for_icons():
    # They fall back to whatever font has them, so they come out at odd sizes.
    for path in OWN:
        found = PICTOGRAPHS.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name}: {found}"


def test_a_done_step_shows_a_tick_and_says_so():
    steps = _between(_js("steps.js"), "export function numberSteps", "export function go")
    assert 'item.append(icon("check"), h("span", { class: "vh" }, " (done)"))' in steps
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    assert ".progress li:not(.done) .icon, .progress li:not(.done) .vh { display: none; }" in css


STATIC_IMPORT = re.compile(r"""^\s*import\s+(?:[^"'();]*?\s+from\s+)?["']([^"']+)["']""", re.M)


def test_leaflet_loads_only_when_a_map_opens():
    assert STATIC_IMPORT.findall(_js("wizard.js")) == [
        "../dashboard_static/dom.js", "./api.js", "./finish.js", "./home.js", "./keys.js", "./say.js", "./state.js",
        "./station.js", "./steps.js", "./stops.js", "./views.js", "./walk.js",
    ]
    # No module loads Leaflet up front: maps.js imports it when the first map opens.
    for name in SCRIPTS:
        assert not [spec for spec in STATIC_IMPORT.findall(_js(name)) if "leaflet" in spec], name
    assert [name for name in SCRIPTS if "leaflet-src" in _js(name)] == ["maps.js"]
    assert 'import("../dashboard_static/vendor/leaflet-1.9.4/leaflet-src.esm.js")' in _js("maps.js")


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
    text = _all_js()
    calls = re.findall(r"\.bind(?:Popup|Tooltip)\(\s*([^,)]+)", text)
    assert calls
    for call in calls:
        assert call.strip().startswith("h("), call
    icons = re.findall(r"divIcon\(\{\s*html:\s*([^,]+)", text)
    assert icons and all(icon.strip().startswith("h(") for icon in icons), icons
    assert "attributionControl: false" in text  # the credit is our own text under each map
    assert "keyboard: false" in text  # markers repeat the list, so they aren't extra tab stops


def test_the_api_is_called_by_relative_url_with_the_csrf_token():
    text = _js("api.js")
    assert "fetch(`api/${name}`" in text and 'fetch("api/session"' in text
    assert '"X-CSRF-Token": state.csrf' in text and 'credentials: "same-origin"' in text
    assert [name for name in SCRIPTS if "fetch(" in _js(name)] == ["api.js"]
    assert not re.search(r"""fetch\(\s*["'`](/|https?:)""", _all_js())


def test_key_inputs_are_write_only():
    text = _js("keys.js")
    draw = text[text.index("function drawKeys"):text.index("function enterKeys")]
    assert 'autocomplete: "off"' in draw and "value:" not in draw  # never filled in from the server
    assert "keyTestButton(key.name, input)" in draw  # the hook for "Test this key" (bead 0fm.8)


def _between(text, start, end):
    return text[text.index(start):text.index(end)]


def test_key_test_says_its_result_politely_in_words():
    text = _js("keys.js")
    hook = _between(text, "function keyTestButton", "function signUp")
    assert 'type: "button"' in hook and '"Test this key"' in hook
    assert 'role: "status", "aria-live": "polite"' in hook
    # Every state the host can answer has an icon and words; the icon is
    # hidden from screen readers (icons.js), which read the words.
    found = re.findall(r'(\w+): \["([^"]+)", "([^"]+)"\]', _between(text, "const RESULTS", "function showResult"))
    results = {state: (shape, words) for state, shape, words in found}
    assert set(keytest.STATES) <= set(results)
    assert all(shape in _icon_names() and words for shape, words in results.values())
    assert 'fill(region, icon(shape), h("strong", {}, `${words}:`)' in text
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    for state in keytest.STATES:
        assert re.search(r"\.key-result-%s\b[^{]*\{ color: var\(--(ok-fg|bad-fg|late-fg)\); \}" % state, css), state


def test_the_wizard_says_nothing_of_the_keys_on_the_views_tabs():
    # TransportAPI's and the dashboard's own keys are signed up for and tested on their tabs.
    text = _all_js()
    for gone in ("TAPI", "TransportAPI", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY", "LOOKUP_WARNING", "confirm("):
        assert gone not in text, gone
    assert 'call("testkey", body)' in _between(_js("keys.js"), "async function testKey", "function keyTestButton")


def test_every_key_on_the_page_says_how_to_get_it():
    text = _js("keys.js")
    sign_up = _between(text, "const SIGN_UP", "const TESTED")
    assert re.findall(r"^  (\w+): \[$", sign_up, re.MULTILINE) == [name for name, _, _ in KEYS]
    assert "The full guide (opens in a new tab)" in text and 'rel: "noopener noreferrer"' in text


def test_every_key_the_wizard_offers_has_a_test_button():
    text = _js("keys.js")
    tested = set(re.findall(r'"([A-Z_]+)"', _between(text, "const TESTED", "const RESULTS")))
    assert tested == {name for name, _, _ in KEYS}
    assert tested <= set(keytest.KEY_NAMES)


# Accessibility.


def test_page_is_british_english_and_says_what_it_is():
    page = _page()
    assert [attrs for tag, attrs in page.elements if tag == "html"] == [{"lang": "en-GB"}]
    # The page is the Pi's and the dashboard's: each host names it in begin's
    # answer, set as text, so until then it says only what it is for.
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<title>Setup</title>" in html and '<h1 id="title">Setup</h1>' in html
    assert "board" not in _between(html, "<title>", "<main")
    begin = _between(_js("wizard.js"), "async function begin", "begin();")
    assert 'state.title = str(data.title).slice(0, 60) || state.title;' in begin
    assert 'fill($("title"), state.title);' in begin
    assert "document.title = `${TITLES[step]}: ${state.title}`;" in _js("steps.js")


BOARD = re.compile(r"\bboards?\b", re.IGNORECASE)
PRODUCTS = re.compile(r"Live (Fastest )?Departure Boards?")  # Rail Data Marketplace's names, not the host


def test_the_script_says_the_hosts_noun_never_board():
    # The dashboard's wizard says "dashboard": every "board" the person reads
    # comes from begin's noun, and "board" is only the default until then.
    text = _all_js()
    code = re.sub(r"(^\s*|\s)//.*$", "", text, flags=re.MULTILINE)  # comments aren't read by anyone
    lines = [line.strip() for line in PRODUCTS.sub("", code).splitlines() if BOARD.search(line)]
    assert lines == ['noun: "board",']
    for words in ("Label on the ${state.noun}", "The ${state.noun} didn't answer",
                  "the ${state.noun} has nothing to show"):
        assert words in text, words


def test_the_page_says_the_hosts_noun_never_board():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nouns = re.findall(r"<span data-noun>board</span>", html)
    assert len(nouns) == 4
    assert not BOARD.search(html.replace("<span data-noun>board</span>", "")), "a board not set from begin's noun"
    begin = _between(_js("wizard.js"), "async function begin", "begin();")
    # Only a plain word, and set as text.
    assert "if (NOUN.test(str(data.noun))) state.noun = data.noun;" in begin
    assert 'for (const el of document.querySelectorAll("[data-noun]")) el.textContent = state.noun;' in begin


def test_each_step_is_a_section_with_a_focusable_heading():
    ids = _ids()
    for step in STEPS:
        tag, attrs = ids[f"step-{step}"]
        assert tag == "section" and attrs["aria-labelledby"] == f"{step}-heading" and "hidden" in attrs
        tag, attrs = ids[f"{step}-heading"]
        assert tag == "h2" and attrs.get("tabindex") == "-1"
    assert "$(`${step}-heading`).focus()" in _js("steps.js")


def test_the_steps_are_numbered_for_those_shown():
    # Six steps, or seven where there are views to choose: the headings and
    # the progress list are numbered for the steps shown.
    ids = _ids()
    numbered = [step for step in STEPS if step != "done"]
    assert [attrs["data-step"] for tag, attrs in _page().elements if tag == "li" and "data-step" in attrs] == numbered
    for step in numbered:
        assert ids[f"{step}-number"][0] == "span"
    text = _js("steps.js")
    number = _between(text, "function numberSteps", "// One action at a time")
    assert 'STEPS.filter((step) => step !== "views" || state.askViews)' in text
    assert "item.hidden = !counted.includes(item.dataset.step);" in number
    assert "fill($(`${step}-number`), `Step ${i + 1} of ${counted.length}:`)" in number
    # Each Back goes to the step before among those shown: past the views step
    # when it isn't asked (see the behaviour tests below).
    assert "back.dataset.back = i ? counted[i - 1] : LEAVE;" in number
    assert ids["finish-back"][1]["data-back"] == "keys"
    begin = _between(_js("wizard.js"), "async function begin", "begin();")
    assert begin.index("numberSteps();") < begin.index('go("home"')


def test_the_views_step_is_a_group_of_labelled_checkboxes_with_its_error_described():
    ids = _ids()
    tag, attrs = ids["views-set"]
    assert tag == "fieldset" and attrs["aria-describedby"].split() == ["views-hint", "views-error"]
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    fieldset = page[page.index('<fieldset id="views-set"'):]
    fieldset = fieldset[:fieldset.index("</fieldset>")]
    assert "<legend>Show the departures on</legend>" in fieldset and 'id="views-list"' in fieldset
    assert ids["views-error"][1]["class"] == "error" and "hidden" in ids["views-error"][1]
    assert ids["views-next"][0] == "button"
    draw = _between(_js("views.js"), "function drawViews", "const firstView")
    # A real checkbox each, with its own label and the view's hint and the error described.
    assert 'type: "checkbox", id, value: view.name' in draw
    assert 'h("label", { for: id }, view.label)' in draw
    assert '"aria-describedby": view.hint ? `${id}-hint views-error` : "views-error"' in draw
    assert 'h("p", { class: "hint", id: `${id}-hint` }, view.hint)' in draw
    # Text from the server goes in as text only.
    assert "view.label" in draw and "innerHTML" not in draw


def test_the_views_step_asks_only_when_there_is_a_choice_and_needs_one_ticked():
    begin = _between(_js("wizard.js"), "async function begin", "begin();")
    assert "state.askViews = state.views.length > 1;" in begin
    # One view offered: sent as chosen, without asking.
    assert "state.chosenViews = state.views.length === 1 ? [state.views[0].name] : [];" in begin
    assert "VIEW.test(str(v.name))" in begin
    keys, views = _js("keys.js"), _js("views.js")
    leave_keys = keys[keys.index("function leaveKeys"):]  # the last in keys.js
    assert leave_keys.count("if (state.askViews) enterViews();") == 1
    leave = views[views.index("function leaveViews"):]  # the last in views.js
    assert 'refuse("views", "Tick at least one view, or the departures show nowhere.", firstView())' in leave
    assert leave.index("if (!chosen.length)") < leave.index("enterFinish();")
    # Those on now ticked; with none on, the first offered (the board, where there is one).
    assert "const ticked = state.viewsOn.length ? state.viewsOn : [state.views[0].name];" in views
    payload = _between(_js("finish.js"), "function payload", "async function save")
    assert "if (state.chosenViews.length) body.views = state.chosenViews;" in payload


def test_the_done_links_come_from_finish():
    text = _js("finish.js")
    done = _between(text, "function finished", "function setUpFinish")
    assert "list(data.links).map(obj).filter((l) => l && str(l.label) && LINK.test(str(l.href)))" in done
    assert 'h("a", { href: l.href }, l.label)' in done


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
    for name in ("station", "stops", "views"):
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


def _palette(path, name):
    css = path.read_text(encoding="utf-8")
    match = re.search(r"/\* theme: %s \*/[^{]*\{(?:[^{}]*\{)?([^{}]*)\}" % name, css)
    assert match, name
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", match.group(1)))


def _palettes():
    light, dark = (_palette(STATIC / "wizard.css", name) for name in ("light", "dark"))
    return {"light": light, "dark": {**light, **dark}}


def test_the_colours_are_the_dashboards():
    # The same names mean the same colours as on the dashboard's modern look;
    # only the map pins' are the wizard's own.
    dashboard = {theme: _palette(DASHBOARD / "app.css", f"modern-{theme}") for theme in ("light", "dark")}
    for theme, tokens in _palettes().items():
        own = {name: value for name, value in tokens.items() if not name.startswith("pin-")}
        assert own == {name: dashboard[theme][name] for name in own}, theme


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("muted", "surface"), ("muted", "bg"), ("accent", "surface"),
    ("accent-fg", "accent"), ("bad-fg", "surface"), ("ok-fg", "surface"), ("late-fg", "late-bg"),
    ("late-fg", "surface"),
    # Notes, the chosen place and tags on their quiet tint.
    ("fg", "surface-2"), ("ok-fg", "surface-2"), ("muted", "surface-2"),
    ("pin-fg", "pin-bg"), ("pin-fg", "pin-chosen-bg"), ("pin-fg", "pin-home-bg"),
]
OUTLINE_PAIRS = [("focus", "bg"), ("focus", "surface"), ("edge", "surface"), ("edge", "bg"), ("bad-fg", "surface")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_contrast(theme):
    assert_contrast(theme, _palettes()[theme], TEXT_PAIRS, OUTLINE_PAIRS)


def test_targets_are_big_enough():
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    button = re.search(r"\nbutton \{([^}]*)\}", css).group(1)
    assert "min-height: 2.75rem" in button and "min-width: 2.75rem" in button
    assert re.search(r"\.option \{[^}]*min-height: 2\.75rem", css)
    assert re.search(r"\.option input \{[^}]*width: 1\.5rem; height: 1\.5rem", css)  # 24 px


def test_page_headers_allow_only_what_the_page_needs():
    csp = PAGE_HEADERS["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src 'self';" in csp and "style-src 'self';" in csp
    assert "font-src 'self';" in csp
    assert "unsafe" not in csp and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "img-src 'self' data: https://tile.openstreetmap.org" in csp
    assert PAGE_HEADERS["X-Content-Type-Options"] == "nosniff" and PAGE_HEADERS["Cache-Control"] == "no-store"


# The roads for the bus maps, found after the search: a progress bar and a
# choice to wait for them or carry on.


def test_the_roads_have_a_named_progress_bar_their_count_beside_it_and_two_buttons():
    ids = _ids()
    assert "hidden" in ids["roads-box"][1]
    tag, bar = ids["roads-progress"]
    assert tag == "progress" and "roads-count" in bar.get("aria-describedby", "").split()
    assert ids["roads-intro"][1].get("tabindex") == "-1"  # focus goes there when the choice appears
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<label for="roads-progress" class="vh">Roads for the bus maps</label>' in html  # its name
    assert re.search(r'<button type="button" id="roads-wait"[^>]*>Wait for the roads</button>', html)
    assert re.search(r'<button type="button" id="roads-on"[^>]*>Carry on</button>', html)
    # In the finish step, after the search's box and before Save.
    finish = _between(html, '<section id="step-finish"', '<section id="step-done"')
    assert finish.index('id="job-box"') < finish.index('id="roads-box"') < finish.index('id="finish-actions"')


def test_saving_waits_on_the_roads_and_the_done_step_says_if_they_are_still_being_found():
    text = _js("finish.js")
    save = _between(text, "async function save", "function finished")
    assert save.index("await search()") < save.index("await followRoads(job)") < save.index('call("finish"')
    assert 'roads === "found" ? "Found the roads for the bus maps. Saving…" : "Saving…"' in save
    done = _between(text, "function finished", "function setUpFinish")
    assert 'roads === "carried"' in done and "still being found" in done


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node isn't installed: the page's behaviour tests need it")

# The roads' part of the finish step on a fake page, and an api/job that
# answers each of STATUSES in turn (the last again once they run out).
# Only roads.js and what it imports load, so only what the test calls runs.
ROADS_PAGE = """
import "FAKE_DOM";
const proto = Object.getPrototypeOf(document.createElement("p"));
proto.focus = function () { document.activeElement = this; };
for (const id of ["job-box", "roads-box", "roads-intro", "roads-progress", "roads-count", "roads-wait",
                  "roads-on", "live", "alert"]) {
  const el = document.createElement(id === "roads-progress" ? "progress" : "div");
  el.setAttribute("id", id);
  if (id === "roads-box") el.hidden = true;
  document.body.append(el);
}
const said = [];
Object.defineProperty(document.getElementById("live"), "textContent", {
  set(text) { if (text) said.push(text); }, get() { return ""; } });
const statuses = STATUSES;
let asked = 0;
globalThis.fetch = (url) => {
  if (url !== "api/job") return new Promise(() => {});
  const job = statuses[Math.min(asked++, statuses.length - 1)];
  return Promise.resolve({ ok: true, status: 200, headers: { get: () => "application/json" },
                           json: async () => ({ job }) });
};
const $ = (id) => document.getElementById(id);
const click = (id) => { for (const [type, fn] of $(id).listeners) if (type === "click") fn({ currentTarget: $(id) }); };
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });
const { followRoads, setUpRoads } = await import("./roads.js");
setUpRoads();
"""


def run_roads(statuses, script):
    prelude = ROADS_PAGE.replace("FAKE_DOM", (FIXTURES / "fake_dom.mjs").as_uri())
    prelude = prelude.replace("STATUSES", json.dumps(statuses))
    done = subprocess.run([NODE, "--input-type=module", "-e", prelude + script], cwd=STATIC,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _roads(state, done, total=140):
    return {"state": "done", "step": "", "done": 0, "total": 0, "message": "Found 1",
            "found": {"operators": ["BNML"]}, "roads": {"state": state, "done": done, "total": total}}


@needs_node
def test_waiting_for_the_roads_follows_them_to_the_end_saying_so_now_and_then():
    statuses = [_roads("running", n) for n in (20, 40, 70, 100, 140)] + [_roads("done", 140)]
    found = run_roads(statuses, """
        const pending = followRoads(START, 1);
        const shown = { box: $("roads-box").hidden, jobBox: $("job-box").hidden, focus: document.activeElement.id,
                        count: $("roads-count").textContent, max: $("roads-progress").max,
                        value: $("roads-progress").value };
        click("roads-wait");
        const outcome = await pending;
        await sleep(120);
        console.log(JSON.stringify({ outcome, shown, said, asked, wait: $("roads-wait").getAttribute("aria-disabled"),
                                     box: $("roads-box").hidden, count: $("roads-count").textContent,
                                     max: $("roads-progress").max, value: $("roads-progress").value }));
    """.replace("START", json.dumps(_roads("running", 0))))
    assert found["outcome"] == "found" and found["asked"] == 6
    assert found["shown"] == {"box": False, "jobBox": True, "focus": "roads-intro",
                              "count": "Finding the roads for the bus maps: 0 of 140", "max": 140, "value": 0}
    # Said at the start, when waiting, and at each quarter: never every road.
    assert found["said"] == [
        "The timetable is built. Finding the roads for the bus maps: 0 of 140. Wait for them, or carry on.",
        "Waiting for the roads. Setup saves once they are found.",
        "Finding the roads for the bus maps: 40 of 140",
        "Finding the roads for the bus maps: 70 of 140",
    ]
    assert found["wait"] == "true" and found["box"] is True
    assert (found["count"], found["max"], found["value"]) == ("Finding the roads for the bus maps: 140 of 140", 140, 140)


@needs_node
def test_carrying_on_leaves_the_roads_to_finish_on_their_own_at_once():
    found = run_roads([_roads("running", 10)], """
        const start = Date.now();
        const pending = followRoads({ ...statuses[0] }, 5000);
        click("roads-on");
        const outcome = await pending;
        console.log(JSON.stringify({ outcome, asked, quick: Date.now() - start < 1000, box: $("roads-box").hidden }));
    """)
    assert found == {"outcome": "carried", "asked": 0, "quick": True, "box": True}


@needs_node
@pytest.mark.parametrize("statuses", [
    [_roads("cancelled", 12)],  # stopped: the next build finds the rest
    [{"state": "failed", "message": "Gone"}],  # lost track of them: they finish anyway
])
def test_roads_that_stop_or_are_lost_are_left_to_finish_on_their_own(statuses):
    found = run_roads(statuses, """
        console.log(JSON.stringify(await followRoads(START, 1)));
    """.replace("START", json.dumps(_roads("running", 0))))
    assert found == "carried"


@needs_node
def test_a_search_with_no_roads_to_find_moves_straight_on():
    found = run_roads([], """
        const outcome = await followRoads({ state: "done", found: {} }, 1);
        const done = await followRoads({ state: "done", roads: { state: "done", done: 3, total: 3 } }, 1);
        console.log(JSON.stringify({ outcome, done, asked, box: $("roads-box").hidden, said }));
    """)
    assert found == {"outcome": "none", "done": "none", "asked": 0, "box": True, "said": []}


# The whole page on a fake DOM built from index.html, with an API that answers
# from ANSWERS, at a URL with SEARCH: how it starts, and the shortcuts
# ("Change station only", "Change stops only") from start to save.

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Tree(HTMLParser):
    """index.html's body as nested [tag, attrs, children], text as strings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["body", {}, []]
        self.open = [self.root]
        self.inside = False

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.inside = True
            return
        if not self.inside:
            return
        node = [tag, {name: value or "" for name, value in attrs}, []]
        self.open[-1][2].append(node)
        if tag not in VOID:
            self.open.append(node)

    def handle_endtag(self, tag):
        if self.inside and tag != "body" and tag not in VOID and len(self.open) > 1:
            self.open.pop()

    def handle_data(self, data):
        if self.inside and data.strip():
            self.open[-1][2].append(data)


def _body_tree():
    parser = _Tree()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser.root[2]


WIZARD_PAGE = """
import "FAKE_DOM";
const build = (node) => {
  if (typeof node === "string") return document.createTextNode(node);
  const [tag, attrs, children] = node;
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === "hidden") el.hidden = true;
    else el.setAttribute(name, value);
    if (name.startsWith("data-")) el.dataset[name.slice(5).replace(/-(\\w)/g, (_, c) => c.toUpperCase())] = value;
  }
  el.append(...children.map(build));
  return el;
};
document.body.append(...TREE.map(build));
const assigned = [];
globalThis.window = { location: { search: SEARCH, assign: (url) => { assigned.push(url); } } };
const answers = ANSWERS;
const sent = [];
globalThis.fetch = async (url, init) => {
  const name = url.replace(/^api\\//, "");
  if (init && init.body) sent.push([name, JSON.parse(init.body)]);
  const body = name === "session" ? { csrf: "token" } : answers[name];
  const json = async () => (body === undefined ? { error: "No such setup step" } : body);
  return { ok: body !== undefined, status: body === undefined ? 404 : 200,
           headers: { get: () => "application/json" }, json };
};
const $ = (id) => document.getElementById(id);
const click = (id) => { for (const [type, fn] of $(id).listeners) if (type === "click") fn({ currentTarget: $(id) }); };
const settle = () => new Promise((resolve) => { setTimeout(resolve, 100); });
const shown = () => [...document.querySelectorAll("section")].filter((s) => !s.hidden).map((s) => s.id);
const progress = () => $("progress").children.filter((li) => !li.hidden).map((li) => li.dataset.step);
const back = (step) => {
  const button = $(`step-${step}`).querySelector("[data-back]");
  return { to: button.dataset.back, text: button.textContent, hidden: button.hidden === true };
};
const ticked = () => [...$("stops-list").querySelectorAll('input[type="checkbox"]')]
  .filter((box) => box.checked).map((box) => box.value);
const asked = (name) => sent.filter(([n]) => n === name).map(([, body]) => body);
const { state } = await import("./state.js");
await import("./wizard.js");
await settle();
"""

KEYS_SET = [{"name": "RDM_API_KEY", "label": "Train key", "hint": "", "set": True, "doc": ""},
            {"name": "BODS_API_KEY", "label": "Bus key", "hint": "", "set": True, "doc": ""}]
BEGIN = {"title": "Set up your departures", "noun": "board", "keys": KEYS_SET,
         "views": {"offered": [{"name": "board", "label": "The board"}, {"name": "dashboard", "label": "The dashboard"}],
                   "on": ["board"]},
         "links": [], "back": "/settings", "job": {"state": "idle"}}
SAVED_NOW = {
    "home": {"lat": 53.46, "lon": -1.97},
    "station": {"crs": "GLO", "name": "Glossop", "walk": 6},
    "filter": {"crs": "MAN", "name": "Manchester Piccadilly"},
    "stops": [{"label": "A", "atco": "1800SB12341", "name": "Station Road", "indicator": "Stop A", "walk": 3},
              {"label": "Z", "atco": "1800SB99999", "name": "", "indicator": "", "walk": None}],
}
NEAR_STOPS = [
    {"atco": "1800SB12341", "name": "Station Road", "indicator": "Stop A", "bearing": "N", "lat": 53.4615,
     "lon": -1.9662, "distance_m": 64, "label": "A"},
    {"atco": "1800SB12342", "name": "Station Road", "indicator": "Stop B", "bearing": "S", "lat": 53.4614,
     "lon": -1.9665, "distance_m": 85, "label": "B"},
]
NEAR_STATIONS = [{"crs": "HDF", "name": "Hadfield", "lat": 53.46, "lon": -1.96, "distance_m": 600},
                 {"crs": "GLO", "name": "Glossop", "lat": 53.44, "lon": -1.95, "distance_m": 2000}]


def run_wizard(script, search="", **answers):
    """The page at ?SEARCH, its API answering from BEGIN, SAVED_NOW and `answers`."""
    answers = {"begin": BEGIN, "saved": SAVED_NOW, "stops": {"stops": NEAR_STOPS},
               "stations": {"stations": NEAR_STATIONS}, "walk": {"walk": {}},
               "finish": {"saved": True, "message": "Saved.", "links": []}, **answers}
    prelude = WIZARD_PAGE.replace("FAKE_DOM", (FIXTURES / "fake_dom.mjs").as_uri())
    prelude = prelude.replace("TREE", json.dumps(_body_tree())).replace("SEARCH", json.dumps(search))
    prelude = prelude.replace("ANSWERS", json.dumps({k: v for k, v in answers.items() if v is not None}))
    done = subprocess.run([NODE, "--input-type=module", "-e", prelude + script], cwd=STATIC,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


START = """
console.log(JSON.stringify({ shown: shown(), focus: document.activeElement && document.activeElement.id,
                             title: $("title").textContent, page: document.title, progress: progress(),
                             only: state.only, asked: sent.map(([name]) => name) }));
"""


@needs_node
@pytest.mark.parametrize("search", ["", "?step=home", "?step=keys", "?step=finish", "?step=STOPS", "?step=__proto__",
                                    "?step=constructor", "?step=stops%00", "?other=stops"])
def test_only_the_two_shortcuts_start_part_way_anything_else_starts_at_home(search):
    found = run_wizard(START, search)
    assert found["shown"] == ["step-home"] and found["focus"] is None  # as ever: focus isn't moved on load
    assert found["only"] == "" and found["title"] == "Set up your departures"
    assert found["progress"] == ["home", "station", "stops", "walk", "keys", "views", "finish"]
    assert found["asked"] == ["begin", "saved"]


@needs_node
@pytest.mark.parametrize("search, step, title, progress", [
    ("?step=stops", "stops", "Change your bus stops", ["stops", "walk", "finish"]),
    ("?step=station", "station", "Change your station", ["station", "walk", "finish"]),
])
def test_a_shortcut_starts_at_its_step_with_focus_on_its_heading(search, step, title, progress):
    found = run_wizard(START, search)
    assert found["shown"] == [f"step-{step}"] and found["focus"] == f"{step}-heading"
    assert found["title"] == title and found["page"].endswith(f": {title}")
    assert found["progress"] == progress


@needs_node
def test_a_shortcut_shows_the_keys_step_only_while_its_key_is_not_set():
    keys = [dict(KEYS_SET[0], set=False), KEYS_SET[1]]
    assert run_wizard(START, "?step=station", begin={**BEGIN, "keys": keys})["progress"] == [
        "station", "walk", "keys", "finish"]
    assert run_wizard(START, "?step=stops", begin={**BEGIN, "keys": keys})["progress"] == ["stops", "walk", "finish"]


@needs_node
def test_back_from_a_shortcuts_first_step_returns_to_the_settings():
    found = run_wizard("""
        const first = back("stops"), walk = back("walk"), finish = back("finish");
        for (const [type, fn] of $("step-stops").querySelector("[data-back]").listeners) if (type === "click") fn();
        console.log(JSON.stringify({ first, walk, finish, assigned, shown: shown() }));
    """, "?step=stops")
    assert found["first"] == {"to": "settings", "text": "Back to settings", "hidden": False}
    assert found["walk"] == {"to": "stops", "text": "Back", "hidden": False}
    assert found["finish"] == {"to": "walk", "text": "Back", "hidden": False}
    assert found["assigned"] == ["/settings"] and found["shown"] == ["step-stops"]


@needs_node
@pytest.mark.parametrize("given", ["", "//evil.example.com/", "https://evil.example.com/", "javascript:alert(1)"])
def test_without_a_plain_path_to_go_back_to_a_shortcuts_first_step_has_no_back(given):
    found = run_wizard("""
        console.log(JSON.stringify({ first: back("station"), back: state.back }));
    """, "?step=station", begin={**BEGIN, "back": given})
    assert found == {"first": {"to": "settings", "text": "Back to settings", "hidden": True}, "back": ""}


@needs_node
def test_the_whole_wizards_back_buttons_go_one_step_back_past_the_views_when_not_asked():
    script = """
        console.log(JSON.stringify(["station", "stops", "walk", "keys", "views", "finish"].map((s) => back(s).to)));
    """
    assert run_wizard(script) == ["home", "station", "stops", "walk", "keys", "views"]
    one_view = {**BEGIN, "views": {"offered": [{"name": "dashboard", "label": "The dashboard"}], "on": []}}
    assert run_wizard(script, begin=one_view)[-1] == "keys"


@needs_node
def test_the_stops_shortcut_ticks_todays_stops_and_saves_only_them():
    found = run_wizard("""
        const start = { ticked: ticked(), near: $("stops-near").hidden, asked: asked("stops"),
                        labels: [...$("stops-list").querySelectorAll('input[type="text"]')].map((i) => i.value),
                        listed: $("stops-list").textContent };
        click("stops-next");
        await settle();
        const walk = { shown: shown(), asked: asked("walk"), values: ["walk-0", "walk-1"].map((id) => $(id).value),
                       hints: $("walk-list").textContent };
        click("walk-next");
        await settle();
        const finish = { shown: shown(), only: $("only-hint").hidden,
                         summary: [...$("summary").querySelectorAll("dt")].map((dt) => dt.textContent),
                         routes: $("routes-box").hidden };
        click("save");
        await settle();
        console.log(JSON.stringify({ start, walk, finish, saved: asked("finish"), started: asked("start"),
                                     done: shown() }));
    """, "?step=stops", walk={"walk": {"1800SB99999": {"minutes": 4, "source": "route"}}})
    start, walk, finish = found["start"], found["walk"], found["finish"]
    # Found near where home roughly is, today's ticked by their labels; one not
    # found nearby is listed all the same, so it can be kept.
    assert start["asked"] == [{"lat": 53.46, "lon": -1.97}] and start["near"] is False
    assert start["ticked"] == ["1800SB12341", "1800SB99999"]
    assert start["labels"] == ["A", "B", "Z"] and "Stop code 1800SB99999" in start["listed"]
    # Today's walk kept; the other worked out, and said to be rough.
    assert walk["shown"] == ["step-walk"] and walk["asked"] == [
        {"home": {"lat": 53.46, "lon": -1.97}, "station_crs": None, "stop_atcos": ["1800SB99999"]}]
    assert walk["values"] == ["3", "4"] and "Saved" in walk["hints"] and "roughly where home is" in walk["hints"]
    assert "Glossop" not in walk["hints"]  # the station isn't this shortcut's
    # Straight to saving: the keys are set. Only the stops, and no search for the same stops.
    assert finish["shown"] == ["step-finish"] and finish["only"] is False and finish["routes"] is True
    assert finish["summary"] == ["Bus stops", "Keys to save"]
    assert found["saved"] == [{"only": "stops", "stops": {"A": "1800SB12341", "Z": "1800SB99999"},
                               "walk": {"1800SB12341": 3, "1800SB99999": 4}}]
    assert found["started"] == [] and found["done"] == ["step-done"]


@needs_node
def test_the_station_shortcut_picks_todays_station_and_saves_only_it():
    found = run_wizard("""
        const start = { glo: $("station-GLO").checked, hdf: $("station-HDF").checked, filter: $("filter").value };
        click("station-next");
        await settle();
        const walk = { shown: shown(), values: [$("walk-0").value], extra: $("walk-1"), asked: asked("walk") };
        click("walk-next");
        await settle();
        click("save");
        await settle();
        console.log(JSON.stringify({ start, walk, saved: asked("finish"), stops: asked("stops") }));
    """, "?step=station", station={"station": {"crs": "MAN", "name": "Manchester Piccadilly", "lat": 53.47,
                                                 "lon": -2.23, "distance_m": 0}})
    assert found["start"] == {"glo": True, "hdf": False, "filter": "MAN"}
    assert found["walk"] == {"shown": ["step-walk"], "values": ["6"], "extra": None, "asked": []}
    assert found["saved"] == [{"only": "station", "station_crs": "GLO", "filter_crs": "MAN", "walk": {"GLO": 6}}]
    assert found["stops"] == []  # the stops stay as they are


@needs_node
def test_the_station_shortcut_types_todays_code_when_it_isnt_near_home():
    found = run_wizard("""
        console.log(JSON.stringify({ other: $("station-other").checked, code: $("station-code").value,
                                     box: $("station-code-box").hidden }));
    """, "?step=station", stations={"stations": NEAR_STATIONS[:1]})
    assert found == {"other": True, "code": "GLO", "box": False}


@needs_node
def test_the_station_shortcut_wont_leave_nothing_to_show():
    found = run_wizard("""
        $("station-GLO").checked = false;  // one radio of a group at a time, as a browser does
        $("station-none").checked = true;
        $("filter").value = "";
        click("station-next");
        await settle();
        console.log(JSON.stringify({ shown: shown(), error: $("station-error").textContent,
                                     focus: document.activeElement.id }))
    """, "?step=station", saved={**SAVED_NOW, "stops": []})
    assert found["shown"] == ["step-station"] and found["focus"] == "station-none"
    assert "nothing to show" in found["error"]


@needs_node
def test_with_no_home_saved_a_shortcut_lists_only_todays_choices():
    found = run_wizard("""
        console.log(JSON.stringify({ ticked: ticked(), status: $("stops-status").textContent,
                                     near: $("stops-near").hidden, asked: sent.map(([name]) => name) }));
    """, "?step=stops", saved={**SAVED_NOW, "home": None})
    assert found["ticked"] == ["1800SB12341", "1800SB99999"] and found["near"] is True
    assert "Home isn't saved" in found["status"] and found["asked"] == ["begin", "saved"]


@needs_node
def test_the_whole_wizard_starts_from_todays_station_and_stops_where_they_are_near():
    found = run_wizard("""
        state.home = { lat: 53.462, lon: -1.966, label: "SK13 1AH", country: "" };
        click("home-next");
        await settle();
        const station = { glo: $("station-GLO").checked, filter: $("filter").value };
        click("station-next");
        await settle();
        console.log(JSON.stringify({ station, ticked: ticked(), listed: $("stops-list").textContent }));
    """, station={"station": {"crs": "MAN", "name": "Manchester Piccadilly", "lat": 53.47, "lon": -2.23,
                               "distance_m": 0}})
    assert found["station"] == {"glo": True, "filter": "MAN"}
    # Only those found near home: today's stop elsewhere isn't added.
    assert found["ticked"] == ["1800SB12341"] and "1800SB99999" not in found["listed"]


# Errors are said with the control that takes the focus, and taken back with them.

DESCRIBED = """
const described = (el) => (el.getAttribute("aria-describedby") || "").split(" ").map((id) => $(id))
  .filter((p) => p && !p.hidden).map((p) => p.textContent).join(" ");
"""


@needs_node
def test_next_with_no_home_says_why_with_the_postcode_box():
    # WCAG 3.3.1: the postcode box takes the focus; its description says what is wrong.
    found = run_wizard(DESCRIBED + """
        click("home-next");
        await settle();
        console.log(JSON.stringify({ focus: document.activeElement.id, said: described($("postcode")) }));
    """, saved={**SAVED_NOW, "home": None})
    assert found["focus"] == "postcode"
    assert "Find your postcode or address, or pick a point on the map, first." in found["said"]


@needs_node
def test_a_station_refused_is_marked_invalid_until_the_error_goes():
    # WCAG 4.1.2: aria-invalid goes when the error does, not only when the same control is passed.
    found = run_wizard("""
        $("station-GLO").checked = false;
        $("station-none").checked = true;
        $("filter").value = "";
        click("station-next");
        await settle();
        const refused = $("station-none").getAttribute("aria-invalid");
        $("station-none").checked = false;
        $("station-GLO").checked = true;
        click("station-next");
        await settle();
        console.log(JSON.stringify({ refused, after: $("station-none").getAttribute("aria-invalid"),
                                     error: $("station-error").hidden }));
    """, "?step=station", saved={**SAVED_NOW, "stops": []})
    assert found == {"refused": "true", "after": None, "error": True}


@needs_node
def test_stops_refused_are_marked_invalid_until_the_error_goes():
    found = run_wizard("""
        state.home = { lat: 53.462, lon: -1.966, label: "SK13 1AH", country: "" };
        click("home-next");
        await settle();
        $("station-GLO").checked = false;
        $("station-none").checked = true;
        $("filter").value = "";
        click("station-next");
        await settle();
        for (const box of $("stops-list").querySelectorAll('input[type="checkbox"]')) box.checked = false;
        state.labels.clear();
        click("stops-next");
        const first = $("stops-list").querySelector('input[type="checkbox"]');
        const refused = [document.activeElement === first, first.getAttribute("aria-invalid")];
        first.checked = true;
        for (const [type, fn] of first.listeners) if (type === "change") fn({ currentTarget: first });
        console.log(JSON.stringify({ refused, after: first.getAttribute("aria-invalid"),
                                     error: $("stops-error").hidden }));
    """, station={"station": {"crs": "MAN", "name": "Manchester Piccadilly", "lat": 53.47, "lon": -2.23,
                               "distance_m": 0}})
    assert found == {"refused": [True, "true"], "after": None, "error": True}
