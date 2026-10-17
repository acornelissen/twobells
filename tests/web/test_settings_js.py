"""settings.js on the settings pages as the server draws them, run under
node on a fake DOM (tests/fixtures/fake_dom.mjs): changed fields are marked
in words, the save bar counts them once typing stops, and leaving asks first
until the form is sent. The static checks (served, CSP, loaded only on a
signed-in visitor's pages, no sinks) are test_settingsite.py's."""

import json
import shutil
import subprocess

import pytest

from tests.paths import FIXTURES, ROOT
from tests.setup.test_wizard import _Tree
from tests.web.test_groupform import fields_of
from tests.web.test_groupform_board import CONFIG, ENV, board_form
from twobells.settings.installs import install_named
from twobells.settings.store import Store
from twobells.setup.keytest import KeyResult
from twobells.web.keyspage import KeysPage

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node isn't installed: settings.js's behaviour tests need it")
SCRIPT = ROOT / "src" / "twobells" / "web" / "settings.js"

# The page's body on a fake DOM, a window that keeps its listeners, timers
# run only by flush(), then settings.js.
PAGE = """
import "FAKE_DOM";
const build = (node) => {
  if (typeof node === "string") return document.createTextNode(node);
  const [tag, attrs, children] = node;
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
  el.append(...children.map(build));
  return el;
};
document.body.append(...TREE.map(build));
const listeners = [];
globalThis.window = {
  addEventListener: (type, fn) => listeners.push([type, fn]),
  removeEventListener: (type, fn) => {
    const at = listeners.findIndex(([t, f]) => t === type && f === fn);
    if (at >= 0) listeners.splice(at, 1);
  },
};
const timers = new Map();
let next = 0;
globalThis.setTimeout = (fn) => { timers.set(++next, fn); return next; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const flush = () => { const due = [...timers.values()]; timers.clear(); for (const fn of due) fn(); };
const $ = (id) => document.getElementById(id);
const fire = (el, type) => { for (const [t, fn] of el.listeners) if (t === type) fn({ target: el }); };
const typed = (id, value) => { $(id).value = value; fire($(id).closest("form"), "input"); };
const ticked = (id, on) => { $(id).checked = on; fire($(id).closest("form"), "change"); };
const leaving = () => listeners.filter(([type]) => type === "beforeunload").length;
const said = (form) => (form || document).querySelector(".unsaved").textContent;
const changed = () => document.querySelectorAll(".is-changed").map((el) => el.id || el.querySelector("legend, label").textContent);
const tags = () => document.querySelectorAll(".changed-tag").map((tag) => tag.parentNode.nodeName + ":" + tag.textContent);
await import("SCRIPT");
"""


def tree(page):
    parser = _Tree()
    parser.feed(f"<body>{page}</body>")
    return parser.root[2]


def run(page, script):
    prelude = PAGE.replace("FAKE_DOM", (FIXTURES / "fake_dom.mjs").as_uri()).replace("SCRIPT", SCRIPT.as_uri())
    prelude = prelude.replace("TREE", json.dumps(tree(page)))
    done = subprocess.run([NODE, "--input-type=module", "-e", prelude + script], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def board_page(tmp_path):
    """The board's pages (trains and buses, board, general) as one form, on a Pi."""
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=tmp_path / "state.json"))
    return board_form(config, lambda: dict(ENV)).render("tok")


@pytest.fixture
def keys_page(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('title = "Glossop"\n[train]\ncrs = "GLO"\n[bus]\nsource = "bods"\n[bus.stops]\nA = "1000DGHS0900"\n')
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RDM_API_KEY=rail-key-1234567\n")
    store = Store.open(config, secrets, state_dir=tmp_path / "store", install="docker", environ={})
    store.make()
    page = KeysPage(store, "/settings/keys", store.env, install=install_named("docker"),
                    test_key=lambda name, value, app_id: KeyResult("works", "It works."))
    return page.render("tok")


def test_nothing_is_marked_or_said_until_something_changes(board_page):
    found = run(board_page, """
        const region = document.querySelector(".unsaved");
        const beside = region.parentNode.children;
        console.log(JSON.stringify({ changed: changed(), tags: tags(), said: said(), leaving: leaving(),
                                     region: region.getAttribute("role"),
                                     where: region.parentNode.getAttribute("class"),
                                     next: beside[beside.indexOf(region) + 1].textContent === "Save changes" }));
    """)
    assert found == {"changed": [], "tags": [], "said": "", "leaving": 0, "region": "status", "where": "savebar",
                     "next": True}


def test_a_text_box_changed_is_marked_in_words_and_unmarked_when_put_back(board_page):
    found = run(board_page, """
        typed("train_crs", "MAN");
        const marked = { changed: changed(), tags: tags(), field: $("train_crs").closest(".field").getAttribute("class") };
        typed("train_crs", "GLO");
        console.log(JSON.stringify({ marked, after: { changed: changed(), tags: tags() } }));
    """)
    assert found["marked"]["field"] == "field is-changed"
    assert found["marked"]["tags"] == ["LABEL:Changed"]  # in the label, said with it
    assert found["after"] == {"changed": [], "tags": []}


def test_a_label_shown_only_to_screen_readers_still_gets_a_visible_tag(board_page):
    found = run(board_page, """
        typed("title", "Glossop");
        const tag = document.querySelector(".changed-tag");
        console.log(JSON.stringify({ tags: tags(), before: tag.parentNode.getAttribute("class"),
                                     label: document.querySelector('label[for="title"]').textContent }));
    """)
    assert found["tags"] == ["DIV:Changed"] and found["before"] == "field is-changed"
    assert found["label"] == "Title"  # the hidden label is left alone


def test_radios_a_tick_box_and_a_list_are_each_one_change(board_page):
    found = run(board_page, """
        ticked("theme", false); ticked("theme-1", true);  // a browser unticks the other radio
        ticked("view_dashboard", !$("view_dashboard").checked);
        $("text_size").selectedIndex = $("text_size").selectedIndex + 1; fire($("text_size").closest("form"), "change");
        $("bus_stops").value = "A = 1000DGHS0900"; fire($("bus_stops").closest("form"), "input");
        flush();
        const marked = tags().length;
        ticked("theme-1", false); ticked("theme", true);
        flush();
        console.log(JSON.stringify({ marked, said: said(), now: tags().length }));
    """)
    assert found == {"marked": 4, "said": "3 unsaved changes", "now": 3}


def test_a_list_drawn_with_nothing_selected_is_unchanged_on_its_first_option():
    page = ('<form class="settings-form"><div class="field"><label for="pick">Pick</label>'
            '<select id="pick" name="pick"><option value="a">A</option><option value="b">B</option></select></div>'
            '<div class="savebar"><button type="submit">Save changes</button></div></form>')
    found = run(page, """
        const first = changed().length;
        $("pick").selectedIndex = 1; fire($("pick").closest("form"), "change");
        console.log(JSON.stringify({ first, then: changed().length }));
    """)
    assert found == {"first": 0, "then": 1}


def test_a_time_window_row_is_one_change_marked_by_its_legend(board_page):
    found = run(board_page, """
        ticked("train_windows-1-sat", true);
        typed("train_windows-1-from", "07:00");
        flush();
        const row = $("train_windows-1-sat").closest(".window");
        console.log(JSON.stringify({ said: said(), row: row.getAttribute("class"), tags: tags(),
                                     legend: row.querySelector("legend").textContent }));
    """)
    assert found["said"] == "1 unsaved change" and found["row"] == "window is-changed"
    assert found["tags"] == ["LEGEND:Changed"] and found["legend"] == "Checking time 1 Changed"


def test_the_count_is_said_once_typing_stops_not_on_every_key(board_page):
    found = run(board_page, """
        const region = document.querySelector(".unsaved");
        const sets = [];
        let text = "";
        Object.defineProperty(region, "textContent", { get: () => text, set: (t) => { sets.push(t); text = t; } });
        for (const value of ["G", "Gl", "Glo", "Glos"]) typed("title", value);
        const before = { said: said(), timers: timers.size };
        flush();
        typed("train_crs", "MAN");
        flush();
        flush();  // nothing new: said no more
        console.log(JSON.stringify({ before, sets }));
    """)
    assert found["before"] == {"said": "", "timers": 1}  # one wait, put back by each key
    assert found["sets"] == ["1 unsaved change", "2 unsaved changes"]


def test_leaving_asks_first_while_there_are_changes_and_not_once_they_are_sent(board_page):
    found = run(board_page, """
        const at = {};
        typed("title", "Glossop");
        at.changed = leaving();
        const asked = { prevented: false };
        listeners.find(([type]) => type === "beforeunload")[1]({ preventDefault() { asked.prevented = true; } });
        typed("title", "Glossop > Manchester Picc");
        at.putBack = leaving();
        typed("title", "Glossop");
        typed("train_crs", "MAN");
        at.twice = leaving();
        fire(document.querySelector("form.settings-form"), "submit");
        at.sent = leaving();
        for (const [type, fn] of listeners.filter(([type]) => type === "pageshow")) fn({ persisted: true });
        at.back = leaving();
        console.log(JSON.stringify({ at, asked }));
    """)
    assert found["at"] == {"changed": 1, "putBack": 0, "twice": 1, "sent": 0, "back": 1}
    assert found["asked"]["prevented"] is True


def test_a_refused_save_s_page_counts_what_was_typed_and_asks_before_leaving(tmp_path):
    # Drawn again after a refused save, the boxes hold what was typed: still
    # not saved, so marked, counted and guarded as if typed on this page.
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=tmp_path / "state.json"))
    form = board_form(config, lambda: dict(ENV))
    sent = fields_of(form.render("tok")) | {"title": "Home", "min_redraw_minutes": "0"}
    errors = form.save(sent)
    assert errors
    found = run(form.render("tok", errors=errors, values=sent), """
        const first = { said: said(), leaving: leaving(), changed: changed().length };
        typed("title", "Elsewhere");
        flush();
        console.log(JSON.stringify({ first, then: { said: said(), changed: changed().length } }));
    """)
    assert found["first"] == {"said": "2 unsaved changes", "leaving": 1, "changed": 2}
    assert found["then"] == {"said": "2 unsaved changes", "changed": 2}


def test_save_is_never_disabled(board_page):
    found = run(board_page, """
        typed("title", "Glossop");
        flush();
        const save = document.querySelector(".savebar button");
        console.log(JSON.stringify({ disabled: save.hasAttribute("disabled") || save.disabled === true,
                                     aria: save.getAttribute("aria-disabled") }));
    """)
    assert found == {"disabled": False, "aria": None}


def test_day_buttons_tick_a_rows_days_and_count_as_its_change(board_page):
    found = run(board_page, """
        const row = $("train_windows-1-mon").closest(".window");
        const buttons = row.querySelectorAll(".presets button");
        const named = buttons.map((b) => [b.textContent, b.getAttribute("type")]);
        const days = () => row.querySelectorAll('input[type="checkbox"]').filter((b) => b.checked).map((b) => b.id.slice(-3));
        fire(buttons[1], "click");
        const weekends = days();
        fire(buttons[2], "click");
        const every = days();
        fire(buttons[0], "click");
        const weekdays = days();
        flush();
        console.log(JSON.stringify({ named, weekends, every, weekdays, said: said(),
                                     rows: document.querySelectorAll(".presets").length,
                                     windows: document.querySelectorAll(".window").length }));
    """)
    assert found["named"] == [["Weekdays", "button"], ["Weekends", "button"], ["Every day", "button"]]
    assert found["weekends"] == ["sat", "sun"]
    assert found["every"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert found["weekdays"] == ["mon", "tue", "wed", "thu", "fri"]
    assert found["said"] == ""  # back to the days it was saved with: Mon-Fri
    assert found["rows"] == found["windows"] > 0


def test_a_show_list_s_presets_become_buttons_that_tick_its_boxes(board_page):
    found = run(board_page, """
        const list = document.querySelector(".show-list");
        const choice = list.querySelector(".show-presets");
        const buttons = list.querySelectorAll(".show-preset-buttons button");
        const named = buttons.map((b) => [b.textContent, b.getAttribute("type")]);
        const ticks = () => list.querySelectorAll('input[type="checkbox"]').filter((b) => b.checked).map((b) => b.id.slice(11));
        const before = ticks();
        fire(buttons[0], "click");
        const times = ticks();
        fire(buttons[2], "click");
        const everything = ticks();
        flush();
        const counted = said();
        fire(buttons[1], "click");
        const usual = ticks();
        flush();
        console.log(JSON.stringify({
          named, before, times, everything, usual, counted, after: said(),
          legend: choice.querySelector("legend").textContent,
          // The buttons sit under the choice's legend; its radios are hidden, left on None.
          inside: buttons[0].closest(".show-presets") === choice,
          hidden: choice.querySelector(".options").hidden === true,
          none: list.querySelector('input[type="radio"]:checked').getAttribute("value"),
        }));
    """)
    assert found["named"] == [["Just the times", "button"], ["The usual", "button"], ["Everything", "button"]]
    usual = ["platforms", "where", "disruption", "rain", "last_bus", "bus_page"]
    leave = ["leave-train", "leave-bus"]  # When to leave: every preset ticks each mode
    assert found["before"] == [*leave, *usual, "calling_at"]  # no list saved: all but the clock page
    assert found["times"] == leave
    assert found["everything"] == [*leave, *usual, "calling_at", "clock_page"]
    assert found["usual"] == [*leave, *usual]
    assert found["counted"] == "1 unsaved change"  # the clock page's box
    assert found["after"] == "1 unsaved change"  # the calling-at box
    assert found["legend"] == "Fill in from a preset" and found["inside"] and found["hidden"]
    assert found["none"] == ""


def test_each_key_form_counts_and_is_sent_on_its_own(keys_page):
    found = run(keys_page, """
        const forms = document.querySelectorAll("form.keyform");
        const form = (name) => forms.find((f) => f.querySelector('input[name="key"]').getAttribute("value") === name);
        typed("RDM_API_KEY", "a-new-rail-key-0001");
        typed("kb_incidents_url", "https://example.com/feed");
        flush();
        const at = { rail: said(form("RDM_API_KEY")), works: said(form("RDM_KB_API_KEY")),
                     buses: said(form("BODS_API_KEY")), leaving: leaving(),
                     where: form("RDM_API_KEY").querySelector(".unsaved").parentNode.getAttribute("class") };
        fire(form("RDM_API_KEY"), "submit");
        at.oneSent = leaving();  // the engineering works address is still not saved
        fire(form("RDM_KB_API_KEY"), "submit");
        at.bothSent = leaving();
        console.log(JSON.stringify(at));
    """)
    assert found == {"rail": "1 unsaved change", "works": "1 unsaved change", "buses": "", "leaving": 1,
                     "where": "key-actions", "oneSent": 1, "bothSent": 0}


def test_a_key_box_is_read_only_to_tell_whether_it_is_empty(keys_page):
    # The key is an object that says so if it is ever turned into text: compared
    # with "" it isn't, which is all the script may ask of it.
    found = run(keys_page, """
        const uses = [];
        const key = { toString() { uses.push("toString"); return "LEAKED"; },
                      valueOf() { uses.push("valueOf"); return "LEAKED"; },
                      [Symbol.toPrimitive](hint) { uses.push(hint); return "LEAKED"; } };
        $("BODS_API_KEY").value = key;
        fire($("BODS_API_KEY").closest("form"), "input");
        flush();
        const marked = $("BODS_API_KEY").closest(".field").getAttribute("class");
        const everything = document.querySelectorAll("*").map((el) => el.textContent + JSON.stringify(el.attributes));
        $("BODS_API_KEY").value = "";
        fire($("BODS_API_KEY").closest("form"), "input");
        flush();
        console.log(JSON.stringify({ uses, marked, leaked: everything.some((text) => text.includes("LEAKED")),
                                     after: $("BODS_API_KEY").closest(".field").getAttribute("class") }));
    """)
    assert found == {"uses": [], "marked": "field is-changed", "leaked": False, "after": "field"}


def test_a_page_with_no_settings_form_is_left_alone():
    page = ('<header><form method="post" action="/settings/logout"><input type="hidden" name="csrf" value="t">'
            '<button type="submit">Sign out</button></form></header><section class="card"><h2>Health</h2></section>')
    found = run(page, """
        console.log(JSON.stringify({ leaving: leaving(), regions: document.querySelectorAll(".unsaved").length,
                                     listeners: listeners.length }));
    """)
    assert found == {"leaving": 0, "regions": 0, "listeners": 0}
