"""The settings site: everything under /settings, on every install.

The one server (dashserver.py) hands it every request under /settings,
and the old addresses that moved here, once it has let the request in
(from the home network, naming a local host). The rest of the server is
the read-only dashboard; this part changes what the board and the
dashboard run on, so it needs signing in. The machinery is auth.py's, and
the pages and forms formpages.py's:

- The password is web.json in the store, one per install. The first
  claim, when none is set, and a forgotten one both need a one-time code:
  shown on the board while the board view is on, else put in the log
  (the journal on a Pi, the container log in Docker, the Terminal on a
  desktop), which only the owner can read. No response ever holds a code.
  `departureboard serve --set-password` and `--reset-code` do the same
  from the command line, in the same folder; a password set there ends
  every session here (Sessions checks its stamp).
- A session is a random token in an HttpOnly, SameSite=Strict cookie
  named settings_session with Path=/settings; the forms before a session
  carry a signed double-submit token (settings_form). The names are new:
  the old board page's `session` cookie, at Path=/, reaches /settings too,
  and must never be mistaken for one. Every POST must be same-origin and
  carry the session's (or the form's) token.
- The settings are in tabs, each a groupform.GroupForm of one group:
  General at /settings, and Board and Dashboard while their view is on (a
  tab whose view is off is not found). Pages are drawn on the server with
  no JavaScript, under a CSP allowing form-action 'self'. The setup wizard
  under /settings/setup/ is the one place with scripts: its own files
  only, from an allowlist read at start and served without a session, its
  page and API needing the session, and its API the session's token in
  X-CSRF-Token, under setupapi.PAGE_HEADERS.

Framework-free: handle() takes the request with its body unread and checks
the origin before reading it (one plain length, capped by max_body), and
returns a Reply; the server adds headers_for(path) and its own limits.
"""

from __future__ import annotations

import html
import logging
import time
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Callable, Mapping

from . import schema, setupapi
from .auth import (
    FORM_PAGE_HEADERS,
    FormTokens,
    Lockout,
    PasswordStore,
    ResetCodes,
    Sessions,
    same_origin,
)
from .config import ConfigError, needs_setup
from .formpages import (
    MAX_FORM_BODY,
    FormPages,
    Headers,
    Reply,
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

if TYPE_CHECKING:
    from .installs import Install
    from .store import Store

log = logging.getLogger(__name__)

PREFIX = "/settings"
SETUP = "/settings/setup/"
SETUP_API = "/settings/setup/api/"
COOKIE_NAME = "settings_session"
FORM_COOKIE_NAME = "settings_form"
# Each tab: its address, its name in the tabs, and its fieldsets' names (None is the first).
TABS = ("general", "board", "dashboard")
TAB_PATHS = {"general": PREFIX, "board": PREFIX + "/board", "dashboard": PREFIX + "/dashboard"}
TAB_NAMES = {"general": "General", "board": "Board", "dashboard": "Dashboard"}
_LEGENDS: dict[str, dict[str | None, str]] = {
    "general": {None: "General"},
    "board": {None: "Board"},
    "dashboard": {None: "Dashboard"},
}
# A wizard step's body refused unread, by formpages.read_body's status.
_STEP_BODY_REFUSED = {
    400: "Send the request with one plain length",
    411: "Say how long the request is",
    413: "That request is too big",
}
# Where the board's old settings page had these, until the first public release.
_MOVED = {"/login": PREFIX + "/login", "/reset": PREFIX + "/reset", "/setup": SETUP}

_SHARED = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# Cache-Control is the server's to send (no-store), once.
SETTINGS_HEADERS = {**{k: v for k, v in FORM_PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}
WIZARD_HEADERS = {**{k: v for k, v in setupapi.PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}


def moved(path: str) -> str | None:
    """Where an old address of the board's settings page is now, or None."""
    if path.startswith("/setup/"):
        return SETUP
    return _MOVED.get(path)


def owns(path: str) -> bool:
    """Whether a request path is the settings site's: under /settings, or an old address."""
    return path == PREFIX or path.startswith(PREFIX + "/") or moved(path) is not None


def headers_for(path: str) -> dict[str, str]:
    """The security headers for a path: scripts only on the wizard's."""
    return WIZARD_HEADERS if path == SETUP[:-1] or path.startswith(SETUP) else SETTINGS_HEADERS


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
            log.warning("Setup page file %s left out: not a page, script or style", relative or "index.html")
            continue
        try:
            out[relative] = (Path(path).read_bytes(), content_type)
        except OSError as error:
            log.warning("Setup page file %s unreadable: %s", relative or "index.html", type(error).__name__)
    return out


def shell(title: str, body: str) -> str:
    """The HTML document around a page's content."""
    return document(title, body, f"{PREFIX}/style.css")


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
        asked = "Look at the board: a 6-digit code shows there for 10 minutes. E-paper can take a minute to change."
        ask, ask_again, code_label = "Show a code on the board", "Show a new code", "Code from the board"
        no_code = (f"No code? Look at the board (or run <code>{e(install.reset_command)}</code> on the device "
                   "to print it). "
                   "A new code can be shown once a minute, up to 10 times a day.")
    else:
        claim_intro = (f"No password is set yet. To set one, show that you can read the {noun}'s log: "
                       "ask for a code, find it in the log, then type it below with a new password.")
        reset_intro = (f"Ask for a code, find it in the {noun}'s log, then type it below with a new password. "
                       "Every browser signed in now is signed out.")
        asked = f"A 6-digit code is in the log now, valid for 10 minutes. {install.log_help}"
        ask, ask_again, code_label = "Put a code in the log", "Put a new code in the log", "Code from the log"
        no_code = f"{install.log_help} A new code can be made once a minute, up to 10 times a day."
    if install.fetch == "process":  # serve starts again with the new settings (cli._SettingsWatch)
        saved = ("Saved. The dashboard starts again in a few seconds to use the new settings; "
                 "sign in again to change anything else.")
    else:
        saved = "Saved. The board shows it within a few minutes; the screen within seconds."
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
        wrong_step_token="This page has run out. Reload it and try again.",
    )


class SettingsSite:
    """Everything under /settings, for one install and its store.

    `setup` runs the wizard's steps. `dashboard_on()` says whether the
    dashboard is running, for the links to it. `problem` is why the
    dashboard couldn't start, if it couldn't: the General tab says so."""

    def __init__(self, store: Store, install: Install, setup: SetupSteps, *,
                 dashboard_on: Callable[[], bool] = lambda: False, stylesheet: bytes | None = None,
                 files: dict[str, tuple[bytes, str]] | None = None, clock: Callable[[], float] = time.time,
                 codes: ResetCodes | None = None) -> None:
        self.store = store
        self.install = install
        self.problem = ""
        self.setup = setup
        self._dashboard_on = dashboard_on
        self.password = PasswordStore(store.password_file)
        self.sessions = Sessions(clock, cookie_name=COOKIE_NAME, cookie_path=PREFIX, password=self.password)
        self.lockout = Lockout(clock)
        self._forms = {
            tab: GroupForm(store, (tab,), TAB_PATHS[tab], store.env, install=install, noun=install.noun,
                           header=lambda csrf, tab=tab: self._header(tab, csrf), legends=_LEGENDS[tab])
            for tab in TABS
        }
        self._pages = FormPages(
            self._forms["general"], self.password, self.sessions, self.lockout,
            codes or ResetCodes(store.folder, to_board=self.to_board),
            FormTokens(clock, cookie_name=FORM_COOKIE_NAME, cookie_path=PREFIX),
            lambda: wording(install, self.to_board(), self._running()), prefix=PREFIX, shell=shell,
            home=self._home, log=log, resume_setup=SETUP, setup=setup, clock=clock,
        )
        self._stylesheet = _default_stylesheet() if stylesheet is None else stylesheet
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
        """The tabs shown: General, and each view's while it is on."""
        on = self.views()
        return tuple(tab for tab in TABS if tab == "general" or tab in on)

    def _running(self) -> bool:
        try:
            return bool(self._dashboard_on())
        except Exception as error:  # noqa: BLE001 - the settings matter more than a link
            log.warning("Can't tell whether the dashboard is running: %s", type(error).__name__)
            return False

    def _home(self) -> str:
        """Where a new session starts: the wizard until there is something to show."""
        try:
            return SETUP if needs_setup(self.store) else PREFIX
        except Exception as error:  # noqa: BLE001 - the settings page will say what is wrong
            log.warning("Can't tell whether the settings are set up: %s", type(error).__name__)
            return PREFIX

    # Requests

    def handle(self, method: str, path: str, query: str, headers: Headers, client: str, stream: BinaryIO) -> Reply:
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
            return self._pages.ask_for_code(fields, headers)
        if path == PREFIX + "/reset":
            return self._pages.reset(fields, headers, client)
        if path == PREFIX + "/logout":
            return self._pages.sign_out(fields, headers)
        tab = self._tab(path)
        if tab is not None:
            return self._pages.save(fields, headers, self._forms[tab], self._title(tab))
        return Reply(404, b"Not Found")

    def _tab(self, path: str) -> str | None:
        """The tab at `path`, if it is shown."""
        for tab in self.tabs():
            if TAB_PATHS[tab] == path:
                return tab
        return None

    def _get(self, path: str, query: str, headers: Headers) -> Reply:
        tab = self._tab(path)
        if tab is not None:
            return self._pages.settings_page(headers, self._forms[tab], self._title(tab))
        if path == PREFIX + "/":
            return redirect(PREFIX)
        if path == PREFIX + "/login":
            return self._pages.login_page(200, headers, to_setup=query == "next=setup")
        if path == PREFIX + "/reset":
            return self._pages.reset_page(200, headers, asked=query == "asked")
        if path == PREFIX + "/style.css":
            return Reply(200, self._stylesheet, "text/css; charset=utf-8")
        if path == SETUP[:-1]:
            return redirect(SETUP)
        if path == SETUP:
            if self.sessions.get(self.sessions.token_from(headers.get("Cookie"))) is None:
                return redirect(PREFIX + "/login?next=setup")
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

    @staticmethod
    def _title(tab: str) -> str:
        return f"{TAB_NAMES[tab]} settings"

    def _header(self, tab: str, csrf: str) -> str:
        """What goes before a tab's form: its heading, signing out, the tabs,
        and the tab's own notes."""
        e = html.escape
        items = []
        for shown in self.tabs():
            current = ' aria-current="page"' if shown == tab else ""
            items.append(f'<li><a href="{TAB_PATHS[shown]}"{current}>{TAB_NAMES[shown]}</a></li>')
        out = [
            f'<header class="bar"><h1>{self._title(tab)}</h1>'
            f'<form method="post" action="{PREFIX}/logout">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            '<button type="submit" class="quiet">Sign out</button></form></header>',
            f'<nav aria-label="Settings" class="tabs"><ul>{"".join(items)}</ul></nav>',
        ]
        if self._running():
            out.append('<p class="to-dashboard"><a href="/">Back to the dashboard</a></p>')
        if tab == "general":
            if self.problem:
                out.append(
                    '<div class="summary" role="alert"><h2>The dashboard isn\'t running</h2>'
                    f"<p>{e(self.problem)}</p>"
                    "<p>Fix the settings here, or config.toml; it starts once they load.</p></div>"
                )
            out.append(self._place())
        return "".join(out)

    def _place(self) -> str:
        """A summary of where the departures are for, and the way to change it with setup."""
        try:
            data = self.store.data()
        except (OSError, ConfigError):
            data = {}
        train, bus, weather = (data.get(name) for name in ("train", "bus", "weather"))
        station = str(train.get("crs", "")).strip() if isinstance(train, dict) else ""
        stops = bus.get("stops") if isinstance(bus, dict) else None
        stops = ", ".join(str(label) for label in stops) if isinstance(stops, dict) else ""
        home = isinstance(weather, dict) and "lat" in weather and "lon" in weather

        def off(section) -> str:
            return " (switched off)" if isinstance(section, dict) and section.get("enabled") is False else ""

        e = html.escape
        rows = [
            ("Station", e(station) + off(train) if station else "Not set up"),
            ("Bus stops", e(stops) + off(bus) if stops else "Not set up"),
            ("Home", "Set, for the weather" if home else "Not set"),
        ]
        listed = "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in rows)
        return (
            '<section class="place" aria-labelledby="place-title"><h2 id="place-title">Your place</h2>'
            f"<dl>{listed}</dl>"
            f'<p><a href="{SETUP}">Change with setup</a></p>'
            '<p class="hint">Setup finds home, your station and stops step by step, '
            "with walk times and the bus companies.</p></section>"
        )


def _default_stylesheet() -> bytes:
    return files("departureboard").joinpath("web.css").read_bytes()
