"""The settings site: everything under /settings, on every install.

The one server (web/server.py) hands it every request under /settings,
and the old addresses that moved here, once it has let the request in
(from the home network, naming a local host). The rest of the server is
the read-only dashboard; this part changes what the board and the
dashboard run on, so it needs signing in. The machinery is web/auth.py's, and
the pages and forms web/formpages.py's:

- The password is web.json in the store, one per install. The first
  claim, when none is set, and a forgotten one both need a one-time code:
  shown on the board while the board view is on, else put in the log
  (the journal on a Pi, the container log in Docker, the Terminal on a
  desktop), which only the owner can read. No response ever holds a code.
  `twobells serve --set-password` and `--reset-code` do the same
  from the command line, in the same folder; a password set there ends
  every session here (Sessions checks its stamp).
- A session is a random token in an HttpOnly, SameSite=Strict cookie
  named settings_session with Path=/settings; the forms before a session
  carry a signed double-submit token (settings_form). The names are new:
  the old board page's `session` cookie, at Path=/, reaches /settings too,
  and must never be mistaken for one. Every POST must be same-origin and
  carry the session's (or the form's) token.
- The settings are in pages by task, one tab each (schema.PAGES): the
  Overview at /settings, which only shows, then Trains and buses, Board
  and Dashboard while their view is on (a page whose view is off is not
  found) and General, each a groupform.GroupForm of its own fields, and
  Keys, a keyspage.KeysPage with a small form per key. A save answers with
  a redirect to the page's ?saved, which says what changed. Each key's form
  saves that key alone, and can test it as saved: checked as a save is,
  answered with a redirect too (to ?tested after a test alone), and run by
  the wizard's API (SetupApi.test_key), so the wizard's limit and, on a Pi,
  its runner apply. Pages are drawn on the server and work without
  JavaScript, under a CSP allowing form-action 'self'. A signed-in
  visitor's pages (the tabs) run one script of the site's own,
  /settings/settings.js, which only marks unsaved changes and asks before
  leaving with them: their CSP adds script-src 'self' (PAGE_HEADERS); the
  sign-in and reset pages, and everything else here, allow no script.
  The setup wizard under /settings/setup/ has scripts of its own: its own
  files only, from an allowlist read at start and served without a
  session, its page and API needing the session, and its API the
  session's token in X-CSRF-Token, under setupapi.PAGE_HEADERS.

Framework-free: handle() takes the request with its body unread and checks
the origin before reading it (one plain length, capped by max_body), and
returns a Reply; the server adds headers_for(path) and its own limits.
"""

from __future__ import annotations

import html
import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPStatus
from importlib.resources import files
from io import BufferedIOBase
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from ..board.state import StateStore
from ..settings import keyresults, schema
from ..settings.config import ConfigError, needs_setup
from ..settings.schema import BOARD_DEFAULTS
from ..setup import api as setupapi
from ..setup.keytest import KeyResult
from ..setup.lookups import SetupError
from ..sources.holidays import DIVISIONS, ENGLAND_AND_WALES, read_cache
from ..version import about, package_version
from . import keystate
from . import overview as view
from .auth import (
    FORM_PAGE_HEADERS,
    FormTokens,
    Lockout,
    PasswordStore,
    ResetCodes,
    Sessions,
    same_origin,
)
from .formpages import (
    MAX_FORM_BODY,
    FormPages,
    Headers,
    Reply,
    SettingsPage,
    SetupSteps,
    Wording,
    compact_json,
    document,
    parse_form,
    read_body,
    redirect,
)
from .formpages import WIZARD_TYPES as _CONTENT_TYPES
from .groupform import GroupForm
from .keyspage import KeysPage

if TYPE_CHECKING:
    from ..dashboard.snapshot import SnapshotSource
    from ..settings.installs import Install
    from ..settings.store import Store
    from .netguard import Guard

log = logging.getLogger(__name__)

PREFIX = "/settings"
SETUP = "/settings/setup/"
SETUP_API = "/settings/setup/api/"
COOKIE_NAME = "settings_session"
FORM_COOKIE_NAME = "settings_form"
# Each page, in the tabs' order: its address and its name. The board's and
# the dashboard's are there while their view is on.
TABS = schema.PAGES
TAB_PATHS = {
    "overview": PREFIX,
    "trains_buses": PREFIX + "/trains-and-buses",
    "board": PREFIX + "/board",
    "dashboard": PREFIX + "/dashboard",
    "keys": PREFIX + "/keys",
    "general": PREFIX + "/general",
}
TAB_NAMES = {"overview": "Overview", "trains_buses": "Trains and buses", "board": "Board",
             "dashboard": "Dashboard", "keys": "Keys", "general": "General"}
_VIEWS = {"board", "dashboard"}
# A wizard step's body refused unread, by formpages.read_body's status.
_STEP_BODY_REFUSED = {
    400: "Send the request with one plain length",
    411: "Say how long the request is",
    413: "That request is too big",
}
# The wizard's shortcuts (?step=), which a sign-in started there goes back to.
SETUP_STEPS = ("stops", "station")
# The largest snapshot the overview reads, as a Pi's handoff allows.
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
# Where the board's old settings page had these, until the first public release.
_MOVED = {"/login": PREFIX + "/login", "/reset": PREFIX + "/reset", "/setup": SETUP}

_SHARED = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# Cache-Control is the server's to send (no-store), once.
SETTINGS_HEADERS = {**{k: v for k, v in FORM_PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}
# The tabs' pages: the same, but for running settings.js.
PAGE_HEADERS = {**SETTINGS_HEADERS, "Content-Security-Policy": SETTINGS_HEADERS["Content-Security-Policy"].replace(
    "default-src 'none'; ", "default-src 'none'; script-src 'self'; ", 1)}
SCRIPT = PREFIX + "/settings.js"
WIZARD_HEADERS = {**{k: v for k, v in setupapi.PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}


def moved(path: str) -> str | None:
    """Where an old address of the board's settings site is now, or None."""
    if path.startswith("/setup/"):
        return SETUP
    return _MOVED.get(path)


def owns(path: str) -> bool:
    """Whether a request path is the settings site's: under /settings, or an old address."""
    return path == PREFIX or path.startswith(PREFIX + "/") or moved(path) is not None


def headers_for(path: str) -> dict[str, str]:
    """The security headers for a path: scripts only on the wizard's and the
    tabs' pages (settings.js), none on any other, sign-in included."""
    if path == SETUP[:-1] or path.startswith(SETUP):
        return WIZARD_HEADERS
    return PAGE_HEADERS if path in TAB_PATHS.values() else SETTINGS_HEADERS


def max_body(path: str) -> int:
    """The largest request body a POST to `path` may have."""
    return setupapi.MAX_BODY if path.startswith(SETUP_API) else MAX_FORM_BODY


def wizard_files(found: Mapping[str, Path] | None = None) -> dict[str, tuple[bytes, str]]:
    """The wizard's files by path under /settings/setup/, read once: only
    those setupapi lists, with a type we know. Any other is left out, and the log says so."""
    out = {}
    for relative, path in (setupapi.static_files() if found is None else found).items():
        content_type = _CONTENT_TYPES.get(Path(path).suffix.lower())
        if content_type is None:
            log.warning("Setup page file %s left out: not a page, script, style or font", relative or "index.html")
            continue
        try:
            out[relative] = (Path(path).read_bytes(), content_type)
        except OSError as error:
            log.warning("Setup page file %s unreadable: %s", relative or "index.html", type(error).__name__)
    return out


def shell(title: str, body: str) -> str:
    """The HTML document around a page's content: with no script, for the sign-in and reset pages."""
    return document(title, body, f"{PREFIX}/style.css")


def page_shell(title: str, body: str) -> str:
    """The HTML document around a signed-in visitor's settings page, with settings.js."""
    return document(title, body, f"{PREFIX}/style.css", SCRIPT)


def applies(install: Install) -> str:
    """When saved changes take effect on `install`, said before saving."""
    if install.fetch == "process":
        return "Saving starts the dashboard again, which takes a few seconds."
    return "Changes show on the board and the dashboard within a few minutes; a screen within seconds."


def place_name(data: dict, noun: str) -> str:
    """The place the settings are for, as the dashboard's heading says it:
    the title's part before ">", unless that is the default title; else what
    shows the departures, like "Board"."""
    title = str(data.get("title", "")).strip()
    place = title.partition(">")[0].strip()
    return place if place and place != BOARD_DEFAULTS["title"] else noun.capitalize()


def wording(install: Install, to_board: bool, dashboard_on: bool) -> Wording:
    """The pages' words on `install`: codes shown on the board (`to_board`)
    or put in the log, and a way back to the dashboard while it runs."""
    noun = install.noun
    e = html.escape
    if to_board:
        claim_intro = ("No password is set yet. To set one here, show that you can see the board: "
                       "ask for a code on its display, then type it below with a new password.")
        reset_intro = ("Ask for a code on the board's display, then type it below with a new password. "
                       "Every browser signed in now is signed out.")
        asked = ("Look at the board: a 6-digit code shows there for 10 minutes from when it was first asked for. "
                 "Asking again shows the same code until it is used or runs out. "
                 "E-paper can take a minute to change.")
        ask, ask_again, code_label = "Show a code on the board", "Show the code again", "Code from the board"
        no_code = (f"No code? Look at the board (or run <code>{e(install.reset_command)}</code> on the device "
                   "to print it). "
                   "A new code can be shown once the last is used or runs out, "
                   "at most once a minute and 10 times a day.")
    else:
        claim_intro = (f"No password is set yet. To set one, show that you can read the {noun}'s log: "
                       "ask for a code, find it in the log, then type it below with a new password.")
        reset_intro = (f"Ask for a code, find it in the {noun}'s log, then type it below with a new password. "
                       "Every browser signed in now is signed out.")
        asked = ("A 6-digit code is in the log, valid for 10 minutes from when it was first asked for. "
                 f"Asking again keeps the same code until it is used or runs out. {install.log_help}")
        ask, ask_again, code_label = "Put a code in the log", "Ask for the code again", "Code from the log"
        no_code = (f"{install.log_help} A new code can be made once the last is used or runs out, "
                   "at most once a minute and 10 times a day.")
    if install.fetch == "process":  # serve starts again with the new settings (cli/serve.py, _SettingsWatch)
        saved = "The dashboard starts again in a few seconds to use them."
    else:
        saved = "The board shows them within a few minutes, a screen within seconds."
    return Wording(
        title=f"{noun.capitalize()} settings",
        claim_title=f"Claim this {noun}",
        claim_intro=claim_intro,
        reset_intro=reset_intro,
        asked=asked,
        ask=ask,
        ask_again=ask_again,
        code_label=code_label,
        no_code=no_code,
        claim_hint=f'<p class="hint">Or set the password with <code>{e(install.password_command)}</code></p>',
        back='<p class="after"><a href="/">Back to the dashboard</a></p>' if dashboard_on else "",
        no_code_made="That form had run out, so no code was made. Try again.",
        cant_check="The code can't be checked right now. Try again in a moment.",
        saved=saved,
        reset_logged="Settings password set with a reset code; every session ended",
        wrong_step_token="This page has run out. Reload it and try again.",  # noqa: S106 - a message
    )


class SettingsSite:
    """Everything under /settings, for one install and its store.

    `setup` runs the wizard's steps. `dashboard_on()` says whether the
    dashboard is running, for the links to it. `problem` is why the
    dashboard couldn't start, if it couldn't: the Overview says so, and
    `version` (by default the package's) is what it says is running.
    `hub` is where the dashboard's snapshot is (serve sets it, with
    `healthy()` saying whether its pollers are working), for the
    Overview's health; and on a Pi, `network_guard` says whether the server
    can connect out (a netguard.Guard serve checked at start, which the
    Overview asks again), None until it has looked."""

    def __init__(self, store: Store, install: Install, setup: SetupSteps, *,
                 dashboard_on: Callable[[], bool] = lambda: False, stylesheet: bytes | None = None,
                 script: bytes | None = None,
                 files: dict[str, tuple[bytes, str]] | None = None, clock: Callable[[], float] = time.time,
                 codes: ResetCodes | None = None, version: str | None = None) -> None:
        self.store = store
        self.version = package_version() if version is None else version
        self.install = install
        self.problem = ""
        self.hub: SnapshotSource | None = None
        self.healthy: Callable[[], bool] = lambda: True
        self.network_guard: Guard | None = None
        self.setup = setup
        self._dashboard_on = dashboard_on
        self.password = PasswordStore(store.password_file)
        self.sessions = Sessions(clock, cookie_name=COOKIE_NAME, cookie_path=PREFIX, password=self.password,
                                 file=store.sessions_file)
        self.lockout = Lockout(clock)
        self._forms: dict[str, SettingsPage] = {
            tab: GroupForm(store, (tab,), TAB_PATHS[tab], store.env, install=install, noun=install.noun,
                           header=lambda csrf, tab=tab: self._header(tab, csrf), footer=self._footer,
                           applies=applies(install), setup=SETUP, overview=PREFIX)
            for tab in TABS[1:] if tab != "keys"
        }
        self._forms["overview"] = Overview(self)
        self._keys = KeysPage(store, TAB_PATHS["keys"], store.env, install=install,
                              header=lambda csrf: self._header("keys", csrf), footer=self._footer,
                              applies=applies(install), test_key=self._test_key, clock=clock)
        self._pages = FormPages(
            self._forms["overview"], self.password, self.sessions, self.lockout,
            codes or ResetCodes(store.folder, to_board=self.to_board, zone=store.zone),
            FormTokens(clock, cookie_name=FORM_COOKIE_NAME, cookie_path=PREFIX, key_file=store.form_key_file),
            lambda: wording(install, self.to_board(), self._running()), prefix=PREFIX, shell=shell,
            page_shell=page_shell,
            home=self._home, log=log, resume_setup=SETUP, setup_steps=SETUP_STEPS, setup=setup, clock=clock,
        )
        self._stylesheet = _default_stylesheet() if stylesheet is None else stylesheet
        self._script = _default_script() if script is None else script
        self._files = wizard_files() if files is None else files

    # What is on

    def views(self) -> tuple[str, ...]:
        """The views on now; the install's defaults if the settings can't be read."""
        try:
            return schema.views(self.store.data(), self.install)
        except (OSError, ConfigError):
            return self.install.default_views

    def to_board(self) -> bool:
        """Whether a reset code goes to the board (its view is on), not the log."""
        return "board" in self.views()

    def tabs(self) -> tuple[str, ...]:
        """The pages shown, as tabs: every one but a view's while it is off."""
        on = self.views()
        return tuple(tab for tab in TABS if tab not in _VIEWS or tab in on)

    def _running(self) -> bool:
        try:
            return bool(self._dashboard_on())
        except Exception as error:  # noqa: BLE001 - the settings matter more than a link
            log.warning("Can't tell whether the dashboard is running: %s", type(error).__name__)
            return False

    def _test_key(self, name: str, value: str | None, app_id: str | None) -> KeyResult:
        """A tab's key test, through the wizard's API: one limit per key with
        the wizard's, and on a Pi its runner, so this process never connects.
        A test that can't run says why, never with a key."""
        try:
            return self.setup.test_key(name, value, app_id)
        except setupapi.TooSoon as error:
            return KeyResult("not_tested", str(error))
        except SetupError as error:  # from the runner, already plain words
            return KeyResult("not_tested", str(error))
        except Exception as error:  # noqa: BLE001 - a fault, said plainly, never with its text
            log.warning("Testing %s from the settings failed: %s", name, type(error).__name__)
            return KeyResult("not_tested", "Something went wrong while testing the key. Try again.")

    def _home(self) -> str:
        """Where a new session starts: the wizard until there is something to show."""
        try:
            return SETUP if needs_setup(self.store) else PREFIX
        except Exception as error:  # noqa: BLE001 - the settings site will say what is wrong
            log.warning("Can't tell whether the settings are set up: %s", type(error).__name__)
            return PREFIX

    # Requests

    def handle(self, method: str, path: str, query: str, headers: Headers, client: str,
               stream: BufferedIOBase) -> Reply:
        """The reply to one request the server has already let in: from the home
        network, naming a local host, with a path the site owns. A POST's
        body is read from `stream` only once its origin is known to be ours."""
        where = moved(path)
        if where is not None:
            if method in ("GET", "HEAD"):
                return redirect(where)
            return Reply(405, b"Method Not Allowed", headers={"Allow": "GET, HEAD"})
        if path.startswith(SETUP_API):
            refused = self._pages.wizard_cross_site(headers)
            if refused is not None:
                return refused
        if method in ("GET", "HEAD"):
            return self._get(path, query, headers)
        if method != "POST":
            return Reply(405, b"Method Not Allowed", headers={"Allow": "GET, HEAD, POST"})
        if not same_origin(headers):
            return Reply(403, b"Forbidden: cross-site request")
        if path.startswith(SETUP_API):
            unknown = self._pages.wizard_unknown_step(path[len(SETUP_API):])
            if unknown is not None:
                return unknown
        body = read_body(headers, stream, max_body(path))
        if isinstance(body, int):  # refused unread, so the connection can't carry on
            if path.startswith(SETUP_API):  # in JSON, which the wizard's page reads
                refused = compact_json(body, {"error": _STEP_BODY_REFUSED[body]})
                return Reply(refused.status, refused.body, refused.content_type, {"Connection": "close"})
            return Reply(body, HTTPStatus(body).phrase.encode(), headers={"Connection": "close"})
        if path.startswith(SETUP_API):
            refused = self._pages.wizard_refusal(headers)
            if refused is not None:
                return refused
            return self._pages.wizard_step(path[len(SETUP_API):], body)
        if headers.get_content_type() != "application/x-www-form-urlencoded":
            return Reply(400, b"Bad request")
        try:
            fields = parse_form(body)
        except ValueError:  # more fields than any of our forms has
            return Reply(413, b"Too many fields")
        if path == PREFIX + "/login":
            return self._pages.sign_in(fields, headers, client)
        if path == PREFIX + "/forgot":
            return self._pages.ask_for_code(fields, headers, client)
        if path == PREFIX + "/reset":
            return self._pages.reset(fields, headers, client)
        if path == PREFIX + "/logout":
            return self._pages.sign_out(fields, headers)
        tab = self._tab(path)
        if tab == "overview":  # it only shows
            return Reply(405, b"Method Not Allowed", headers={"Allow": "GET, HEAD"})
        if tab == "keys":
            return self._pages.key_form(fields, headers, self._keys, self._title(tab), TAB_PATHS[tab])
        if tab is not None:
            return self._pages.save(fields, headers, self._forms[tab], self._title(tab), TAB_PATHS[tab])
        return Reply(404, b"Not Found")

    def _tab(self, path: str) -> str | None:
        """The tab at `path`, if it is shown."""
        for tab in self.tabs():
            if TAB_PATHS[tab] == path:
                return tab
        return None

    def _get(self, path: str, query: str, headers: Headers) -> Reply:
        tab = self._tab(path)
        if tab == "overview":  # drawn for this request: the wall tablet's address is the one the browser used
            return self._pages.settings_page(headers, Overview(self, headers.get("Host")), self._title(tab))
        if tab is not None:
            page = self._keys if tab == "keys" else self._forms[tab]
            return self._pages.settings_page(headers, page, self._title(tab), saved=query in ("saved", "tested"))
        if path == PREFIX + "/":
            return redirect(PREFIX)
        if path == PREFIX + "/login":
            asked = _query(query)
            to_setup = asked.get("next") == "setup"
            step = asked.get("step", "") if to_setup and asked.get("step") in SETUP_STEPS else ""
            return self._pages.login_page(200, headers, to_setup=to_setup, step=step)
        if path == PREFIX + "/reset":
            return self._pages.reset_page(200, headers, asked=query == "asked")
        if path == PREFIX + "/style.css":
            return Reply(200, self._stylesheet, "text/css; charset=utf-8")
        if path == SCRIPT:
            return Reply(200, self._script, "text/javascript; charset=utf-8")
        if path == SETUP[:-1]:
            return redirect(SETUP)
        if path == SETUP:
            if self.sessions.get(self.sessions.token_from(headers.get("Cookie"))) is None:
                step = _query(query).get("step")
                return redirect(PREFIX + "/login?next=setup" + (f"&step={step}" if step in SETUP_STEPS else ""))
            body, content_type = self._files.get("", (b"", None))
            return Reply(200, body, content_type) if content_type else Reply(404, b"Not Found")
        if path == SETUP_API + "session":
            return self._pages.wizard_session(headers)
        # The wizard's own files need no session: the page and its API do.
        found = self._files.get(path[len(SETUP):]) if path.startswith(SETUP) else None
        if found is not None:
            return Reply(200, found[0], found[1])
        return Reply(404, b"Not Found")

    # Drawing

    def _title(self, tab: str) -> str:
        """A page's title: its name, then the site's, like "Board - Glossop settings" (with an en dash)."""
        try:
            data = self.store.data()
        except (OSError, ConfigError):
            data = {}
        return f"{TAB_NAMES[tab]} – {place_name(data, self.install.noun)} settings"

    def _header(self, tab: str, csrf: str) -> str:
        """What goes before a page's form: the way back to the dashboard while
        it runs, the page's name, signing out, then the tabs."""
        e = html.escape
        items = []
        for shown in self.tabs():
            current = ' aria-current="page"' if shown == tab else ""
            items.append(f'<li><a href="{TAB_PATHS[shown]}"{current}>{TAB_NAMES[shown]}</a></li>')
        back = '<p class="to-dashboard"><a href="/">Back to the dashboard</a></p>' if self._running() else ""
        return (
            f'<header class="top">{back}<div class="bar"><h1>{TAB_NAMES[tab]}</h1>'
            f'<form method="post" action="{PREFIX}/logout">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            '<button type="submit">Sign out</button></form></div></header>'
            f'<nav aria-label="Settings" class="tabs"><ul>{"".join(items)}</ul></nav>'
        )

    def _footer(self) -> str:
        return f'<footer class="foot"><p>Running {html.escape(about(self.version))}.</p></footer>'

    def overview(self, csrf: str, host: str | None = None) -> str:
        """The overview: why the dashboard isn't running, if it isn't; how
        each feed is doing; your place in names; the keys' state; and the
        ways to change things, open the dashboard and reach it from a wall
        tablet, at `host` (the Host the browser used)."""
        e = html.escape
        try:
            data = self.store.data()
        except (OSError, ConfigError):
            data = {}
        zone = schema.zone(data)
        now = datetime.now(zone).replace(tzinfo=None)
        views = self.views()
        running = self._running()
        out = [self._header("overview", csrf)]
        if self.problem:
            out.append(
                '<div class="summary" role="alert"><h2>The dashboard isn\'t running</h2>'
                f"<p>{e(self.problem)}</p>"
                "<p>Fix the settings here, or config.toml; it starts once they load.</p></div>"
            )
        groups = []
        note = ""
        if "board" in views and "board" in self.install.offered:
            holidays = self._holidays(data)
            groups.append(("On the board", view.board_feeds(data, self._board_state(), now, holidays)))
            if data.get("bank_holidays", True) is True and now.date() in holidays:
                note = "Today is a bank holiday, so the board checks at its Sunday times."
        if running:
            groups.append(("On the dashboard", view.dashboard_feeds(self._snapshot(), now, zone, self._updating())))
        if self.network_guard is not None:
            # One send that never waits, at most once a minute: Wi-Fi may have
            # had no route at start, or the rules been flushed since.
            found, at = self.network_guard.latest()
            checked = datetime.fromtimestamp(at, zone).replace(tzinfo=None)
            groups.append(("On this device", [view.guard_feed(found, checked, now)]))
        out.append(view.render_health(groups, now, PREFIX, problems=1 if self.problem else 0, note=note))
        out.append(view.render_place(data, self.store.folder, now, zone))
        out.append(view.render_keys(data, views, self._secrets(), TAB_PATHS["keys"], self._key_tests(zone)))
        out.append(view.render_actions(data, SETUP, running, host))
        out.append(self._footer())
        return "".join(out)

    def _board_state(self) -> dict | None:
        """The board's state file, read only (the board alone writes it); None if it can't be read."""
        try:
            return StateStore(self.store.state_file).load()
        except OSError:
            return None

    def _holidays(self, data: dict) -> frozenset:
        division = data.get("bank_holiday_division", ENGLAND_AND_WALES)
        return read_cache(self.store.folder, division if division in DIVISIONS else ENGLAND_AND_WALES)

    def _snapshot(self) -> dict | None:
        """The dashboard's snapshot as its browsers get it, or None."""
        if self.hub is None:
            return None
        try:
            _, body = self.hub.current()
            if len(body) > MAX_SNAPSHOT_BYTES:
                return None
            found = json.loads(body)
        except Exception as error:  # noqa: BLE001 - the overview can do without it
            log.warning("Can't read the dashboard's snapshot for the overview: %s", type(error).__name__)
            return None
        return found if isinstance(found, dict) else None

    def _updating(self) -> bool:
        try:
            return bool(self.healthy())
        except Exception as error:  # noqa: BLE001 - said as not updating
            log.warning("Can't tell whether the dashboard is updating: %s", type(error).__name__)
            return False

    def _secrets(self) -> dict[str, str]:
        """The keys saved, only to say which are set; never shown."""
        try:
            return dict(self.store.env())
        except (OSError, ConfigError):
            return {}

    def _key_tests(self, zone: ZoneInfo) -> dict[str, view.KeyTest]:
        """Each key's last test the Keys page kept, by key, when in `zone`:
        TransportAPI's app ID and key share theirs."""
        boxes = {form.name: form.boxes for form in keystate.FORMS}
        found = {}
        for name, last in keyresults.load(self.store.folder).items():
            test = view.KeyTest(last.state, datetime.fromtimestamp(last.time, zone).replace(tzinfo=None))
            found |= dict.fromkeys(boxes.get(name, (name,)), test)
        return found


def _query(query: str) -> dict[str, str]:
    """A query string's fields, the first of each; {} if it doesn't read."""
    try:
        return {name: values[0] for name, values in parse_qs(query, max_num_fields=10).items()}
    except ValueError:
        return {}


class Overview:
    """The overview page, drawn by the site for one request (`host` is its
    Host header). It has no form: nothing is saved from it."""

    def __init__(self, site: SettingsSite, host: str | None = None) -> None:
        self._site = site
        self._host = host

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str:
        return self._site.overview(csrf, self._host)

    def save(self, fields: dict[str, str], changed: list[str] | None = None) -> dict[str, str]:
        return {"_form": "Nothing is saved from the overview"}


def _default_stylesheet() -> bytes:
    return files("twobells.web").joinpath("web.css").read_bytes()


def _default_script() -> bytes:
    return files("twobells.web").joinpath("settings.js").read_bytes()
