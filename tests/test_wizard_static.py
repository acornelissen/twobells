"""Static checks on the setup wizard's browser files, as for the dashboard's.

The page runs under setupapi.PAGE_HEADERS (`script-src 'self'; style-src
'self'`), sets server text only as text, and borrows the dashboard's dom.js
and vendored Leaflet through the paths setupapi.static_files() serves. These
tests keep it that way without a browser.
"""

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import pytest

from departureboard.settings.schema import keys
from departureboard.setup import keytest
from departureboard.setup.api import PAGE_HEADERS, static_files

# The wizard offers only the keys every view uses; each view's own are on its settings tab.
KEYS = keys("general")

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "departureboard" / "wizard_static"
OWN = sorted(p for p in STATIC.rglob("*") if p.is_file())
PREFIX = "http://pi.local/setup/"  # any prefix ending in a slash
STEPS = ("home", "station", "stops", "walk", "keys", "views", "finish", "done")


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
    found = re.findall(r'(\w+): \["([^"]+)", "([^"]+)"\]', _between(text, "const RESULTS", "function showResult"))
    results = {state: (glyph, words) for state, glyph, words in found}
    assert set(keytest.STATES) <= set(results)
    assert all(glyph and words for glyph, words in results.values())
    assert '"aria-hidden": "true" }, glyph), h("strong", {}, `${words}:`)' in text
    css = (STATIC / "wizard.css").read_text(encoding="utf-8")
    for state in keytest.STATES:
        assert re.search(r"\.key-result-%s\b[^{]*\{ color: var\(--(ok-fg|error|warn-fg)\); \}" % state, css), state


def test_the_wizard_says_nothing_of_the_keys_on_the_views_tabs():
    # TransportAPI's and the dashboard's own keys are signed up for and tested on their tabs.
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    for gone in ("TAPI", "TransportAPI", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY", "LOOKUP_WARNING", "confirm("):
        assert gone not in text, gone
    assert 'call("testkey", body)' in _between(text, "async function testKey", "function keyTestButton")


def test_every_key_on_the_page_says_how_to_get_it():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    sign_up = _between(text, "const SIGN_UP", "const TESTED")
    assert re.findall(r"^  (\w+): \[$", sign_up, re.MULTILINE) == [name for name, _, _ in KEYS]
    assert "The full guide (opens in a new tab)" in text and 'rel: "noopener noreferrer"' in text


def test_every_key_the_wizard_offers_has_a_test_button():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
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
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    begin = _between(text, "async function begin", "begin();")
    assert 'state.title = str(data.title).slice(0, 60) || state.title;' in begin
    assert 'fill($("title"), state.title);' in begin
    assert "document.title = `${TITLES[step]}: ${state.title}`;" in text


BOARD = re.compile(r"\bboards?\b", re.IGNORECASE)
PRODUCTS = re.compile(r"Live (Fastest )?Departure Boards?")  # Rail Data Marketplace's names, not the host


def test_the_script_says_the_hosts_noun_never_board():
    # The dashboard's wizard says "dashboard": every "board" the person reads
    # comes from begin's noun, and "board" is only the default until then.
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
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
    begin = _between((STATIC / "wizard.js").read_text(encoding="utf-8"), "async function begin", "begin();")
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
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    assert "$(`${step}-heading`).focus()" in text


def test_the_steps_are_numbered_for_those_shown():
    # Six steps, or seven where there are views to choose: the headings and
    # the progress list are numbered for the steps shown.
    ids = _ids()
    numbered = [step for step in STEPS if step != "done"]
    assert [attrs["data-step"] for tag, attrs in _page().elements if tag == "li" and "data-step" in attrs] == numbered
    for step in numbered:
        assert ids[f"{step}-number"][0] == "span"
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    number = _between(text, "function numberSteps", "// One action at a time")
    assert 'STEPS.filter((step) => step !== "views" || state.askViews)' in text
    assert "item.hidden = !counted.includes(item.dataset.step);" in number
    assert "fill($(`${step}-number`), `Step ${i + 1} of ${counted.length}:`)" in number
    # Past the views step when it isn't asked, both ways.
    assert '$("finish-back").dataset.back = state.askViews ? "views" : "keys";' in number
    assert ids["finish-back"][1]["data-back"] == "keys"
    begin = _between(text, "async function begin", "begin();")
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
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    draw = _between(text, "function drawViews", "const firstView")
    # A real checkbox each, with its own label and the view's hint and the error described.
    assert 'type: "checkbox", id, value: view.name' in draw
    assert 'h("label", { for: id }, view.label)' in draw
    assert '"aria-describedby": view.hint ? `${id}-hint views-error` : "views-error"' in draw
    assert 'h("p", { class: "hint", id: `${id}-hint` }, view.hint)' in draw
    # Text from the server goes in as text only.
    assert "view.label" in draw and "innerHTML" not in draw


def test_the_views_step_asks_only_when_there_is_a_choice_and_needs_one_ticked():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    begin = _between(text, "async function begin", "begin();")
    assert "state.askViews = state.views.length > 1;" in begin
    # One view offered: sent as chosen, without asking.
    assert "state.chosenViews = state.views.length === 1 ? [state.views[0].name] : [];" in begin
    assert "VIEW.test(str(v.name))" in begin
    assert _between(text, "function leaveKeys", "// Step 6").count("if (state.askViews) enterViews();") == 1
    leave = _between(text, "function leaveViews", "// Step 7")
    assert 'refuse("views", "Tick at least one view, or the departures show nowhere.", firstView())' in leave
    assert leave.index("if (!chosen.length)") < leave.index("enterFinish();")
    # Those on now ticked; with none on, the first offered (the board, where there is one).
    assert "const ticked = state.viewsOn.length ? state.viewsOn : [state.views[0].name];" in text
    assert "if (state.chosenViews.length) body.views = state.chosenViews;" in _between(text, "function payload", "async function save")


def test_the_done_links_come_from_finish():
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
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
    text = (STATIC / "wizard.js").read_text(encoding="utf-8")
    save = _between(text, "async function save", "function finished")
    assert save.index("await search()") < save.index("await followRoads(job)") < save.index('call("finish"')
    assert 'roads === "found" ? "Found the roads for the bus maps. Saving…" : "Saving…"' in save
    done = _between(text, "function finished", "function setUpFinish")
    assert 'roads === "carried"' in done and "still being found" in done


NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node isn't installed: the page's behaviour tests need it")
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# The roads' part of the finish step on a fake page, and an api/job that
# answers each of STATUSES in turn (the last again once they run out).
# begin's sign-in never answers, so only what the test calls runs.
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
const { followRoads, setUpRoads } = await import("./wizard.js");
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
