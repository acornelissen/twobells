"""The configuration web page's server: its pages, forms and responses.

Home network only. Every request must come from a private address and name a
local host (which blocks DNS rebinding from outside websites); every change
needs a signed-in session and a matching form token; sign-in attempts are
rate limited. No JavaScript, and a strict Content-Security-Policy. The
sign-in machinery and the request checks live in auth.py, and the pages and
forms in formpages.py, shared with the dashboard's settings area.

The password is set on the Pi (`web --set-password`), or on the page with a
one-time code read off the board: the first claim when there is no password
yet, and a forgotten one. Being on the network is never enough. The forms
for that come before any session, so they carry a signed double-submit form
token (auth.FormTokens) instead of a session's. No response or log line
ever holds a code; the board's display (or `web --reset-code`) is the only
place one is shown.

The setup wizard (setupapi.py) is mounted under /setup/, behind the same
sign-in. It is the one part with JavaScript, so only its responses carry
setupapi.PAGE_HEADERS, whose policy allows the page's own scripts; every
other page keeps the script-free policy. Its files are a fixed list read at
start, each with its content type set here; its API takes JSON, only from a
signed-in session, with the session's form token in an X-CSRF-Token header,
from this same origin, and no bigger than setupapi.MAX_BODY. Until the board
is set up, claiming the page or signing in opens the wizard.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from . import setupapi
from .auth import (
    FORM_PAGE_HEADERS,
    FormTokens,
    Lockout,
    PasswordStore,
    ResetCodes,
    Sessions,
    outsider,
    same_origin,
)
from .formpages import (
    MAX_FORM_BODY,
    WIZARD_TYPES,
    WRONG_CODE,  # noqa: F401 - its old home
    FormPages,
    Reply,
    SettingsPage,
    SetupSteps,
    Wording,
    document,
    parse_form,
)

# Their home before auth.py; the old names still work.
from .auth import FAILURE_WINDOW_S, MAX_FAILURES, SESSION_SECONDS, local_host, private_address  # noqa: F401
from .auth import Lockout as SignInLimiter  # noqa: F401
from .auth import FORM_PAGE_HEADERS as HEADERS  # noqa: F401
from .auth import PasswordStore as Password  # noqa: F401

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
MAX_BODY = MAX_FORM_BODY
REQUEST_TIMEOUT_S = 10
# How to get a code when the board can't show one, as the service user.
RESET_CODE_COMMAND = "sudo -u departureboard /opt/departureboard/venv/bin/departureboard web --reset-code"
SET_PASSWORD_COMMAND = "sudo -u departureboard /opt/departureboard/venv/bin/departureboard web --set-password"
# The Pi's codes show on the board's display.
BOARD = Wording(
    title="Board settings",
    claim_title="Claim this board",
    claim_intro=("No password is set yet. To set one here, show that you can see the board: "
                 "ask for a code on its display, then type it below with a new password."),
    reset_intro=("Ask for a code on the board's display, then type it below with a new password. "
                 "Every browser signed in now is signed out."),
    asked="Look at the board: a 6-digit code shows there for 10 minutes. E-paper can take a minute to change.",
    ask="Show a code on the board",
    ask_again="Show a new code",
    code_label="Code from the board",
    no_code=(f"No code? Look at the board (or run <code>{html.escape(RESET_CODE_COMMAND)}</code> on the device "
             "to print it). A new code can be shown once a minute, up to 10 times a day."),
    claim_hint=f'<p class="hint">Or set the password on the Pi: <code>{html.escape(SET_PASSWORD_COMMAND)}</code></p>',
    back="",
    no_code_made="That form had run out, so no code was shown. Try again.",
    cant_check="The code can't be checked right now. Set the password on the Pi instead.",
    saved="Saved. The board shows it within a few minutes; the screen within seconds.",
    reset_logged="Settings page password set with a reset code; every session ended",
    wrong_step_token="This page has run out. Reload it and try again.",
)


SETUP = "/setup/"
SETUP_API = SETUP + "api/"
_SETUP_STEP = re.compile(r"^[a-z]{1,20}$")
SETUP_TYPES = WIZARD_TYPES
JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"


def _json_reply(status: int, value: dict) -> Reply:
    return Reply(status, json.dumps(value).encode(), JSON)


@dataclass(frozen=True)
class Wizard:
    """The setup wizard as this server mounts it: its API, its files (bytes
    and content type, by path under SETUP; "" is the page), and whether the
    board still needs setting up, which sends a new sign-in there."""

    api: SetupSteps
    files: Mapping[str, tuple[bytes, str]]
    needed: Callable[[], bool] = field(default=lambda: False)

    @classmethod
    def load(cls, api: SetupSteps, needed: Callable[[], bool] = lambda: False,
             paths: Mapping[str, Path] | None = None) -> "Wizard":
        """Reads setupapi.static_files() (or `paths`) once. A file of a kind
        not in SETUP_TYPES is refused, so nothing goes out with a guessed type."""
        files = {}
        for relative, path in (setupapi.static_files() if paths is None else paths).items():
            content_type = SETUP_TYPES.get(Path(path).suffix.lower())
            if content_type is None:
                raise ValueError(f"No content type for the setup page's file {relative!r}")
            files[relative] = (Path(path).read_bytes(), content_type)
        return cls(api, files, needed)


def make_handler(page: SettingsPage, password: PasswordStore, sessions: Sessions, lockout: Lockout,
                 shell: Callable[[str, str], str], stylesheet: bytes, codes: ResetCodes | None = None,
                 clock: Callable[[], float] = time.time, wizard: Wizard | None = None):
    """`shell(title, body)` wraps page content in the HTML document. `codes`
    are the reset codes, by default beside the password file, shown on the
    board. `wizard`, if given, is mounted under SETUP."""

    def home() -> str:
        """Where a new session starts: the wizard while the board isn't set up."""
        if wizard is None:
            return "/"
        try:
            return SETUP if wizard.needed() else "/"
        except Exception as error:  # noqa: BLE001 - the settings page will say what is wrong
            log.warning("Can't tell whether the board is set up: %s", type(error).__name__)
            return "/"

    pages = FormPages(page, password, sessions, lockout, codes or ResetCodes(password.path.parent),
                      FormTokens(clock), BOARD, prefix="", shell=shell, home=home, log=log,
                      setup=wizard.api if wizard is not None else None, json_reply=_json_reply, clock=clock)

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S  # a client that stalls mid-request lets go of its thread

        def version_string(self) -> str:
            return "departureboard"  # no Python or OS version for anyone probing

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/html; charset=utf-8",
                  extra: dict[str, str] | None = None, headers: Mapping[str, str] = FORM_PAGE_HEADERS) -> None:
            self.send_response(status)
            for name, value in {**headers, **(extra or {})}.items():
                self.send_header(name, value)
            if body:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _reply(self, reply: Reply) -> None:
            self._send(reply.status, reply.body, reply.content_type, extra=reply.headers)

        def _redirect(self, where: str, extra: dict[str, str] | None = None) -> None:
            self._send(303, extra={"Location": where, **(extra or {})})

        def _token(self) -> str | None:
            return sessions.token_from(self.headers.get("Cookie"))

        def _refuse_outsiders(self) -> bool:
            if outsider(self.client_address[0], self.headers.get("Host")):
                self._send(403, b"Forbidden", TEXT)
                return True
            return False

        def _form(self) -> dict[str, str] | None:
            if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if not 0 <= length <= MAX_BODY:
                return None
            return parse_form(self.rfile.read(length))

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self._refuse_outsiders():
                return
            url = urlparse(self.path)
            path = url.path
            if path == "/style.css":
                self._send(200, stylesheet, "text/css; charset=utf-8")
            elif path == "/login":
                self._reply(pages.login_page(200, self.headers))
            elif path == "/reset":
                self._reply(pages.reset_page(200, self.headers, asked=url.query == "asked"))
            elif path == "/":
                self._reply(pages.settings_page(self.headers))
            elif wizard is not None and path == SETUP.rstrip("/"):
                self._redirect(SETUP)
            elif wizard is not None and path.startswith(SETUP):
                self._setup_file(path.removeprefix(SETUP))
            else:
                self._send(404, b"Not found", TEXT)

        do_HEAD = do_GET

        # The setup wizard.

        def _setup_send(self, status: int, body: bytes, content_type: str) -> None:
            self._send(status, body, content_type, headers=setupapi.PAGE_HEADERS)

        def _setup_reply(self, reply: Reply) -> None:
            self._setup_send(reply.status, reply.body, reply.content_type)

        def _setup_file(self, relative: str) -> None:
            """The wizard's page, a file on its list, or its session's form token."""
            if relative == "api/session":
                self._setup_reply(pages.wizard_session(self.headers))
                return
            if sessions.get(self._token()) is None:
                if relative == "":
                    self._redirect("/login")
                else:
                    self._setup_send(401, b"Sign in first", TEXT)
                return
            found = wizard.files.get(relative)
            if found is None:
                self._setup_send(404, b"Not found", TEXT)
            else:
                self._setup_send(200, *found)

        def _setup_step(self, name: str) -> None:
            """One wizard step: signed in, with the session's token in the
            header, JSON of a sensible size. Same-origin is checked before this,
            and the size before the body is read."""
            refused = pages.wizard_refusal(self.headers)
            if refused is not None:
                self._setup_reply(refused)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._setup_reply(_json_reply(411, {"error": "Say how long the request is"}))
                return
            if not 0 <= length <= setupapi.MAX_BODY:
                self._setup_reply(_json_reply(413, {"error": "That request is too big"}))
                return
            if not _SETUP_STEP.match(name):
                self._setup_reply(_json_reply(404, {"error": "No such setup step"}))
                return
            self._setup_reply(pages.wizard_step(name, self.rfile.read(length)))

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self._refuse_outsiders():
                return
            if not same_origin(self.headers):
                self._send(403, b"Forbidden: cross-site request", TEXT)
                return
            path = urlparse(self.path).path
            if wizard is not None and path.startswith(SETUP_API):
                self._setup_step(path.removeprefix(SETUP_API))
                return
            fields = self._form()
            if fields is None:
                self._send(400, b"Bad request", TEXT)
                return
            client = self.client_address[0]
            if path == "/login":
                self._reply(pages.sign_in(fields, self.headers, client))
            elif path == "/forgot":
                self._reply(pages.ask_for_code(fields, self.headers))
            elif path == "/reset":
                self._reply(pages.reset(fields, self.headers, client))
            elif path == "/logout":
                self._reply(pages.sign_out(fields, self.headers))
            elif path == "/":
                self._reply(pages.save(fields, self.headers))
            else:
                self._send(404, b"Not found", TEXT)

        def log_message(self, format, *args) -> None:  # noqa: A002 - no request lines, no secrets in logs
            pass

    return Handler


def shell(title: str, body: str) -> str:
    """The HTML document around a page's content."""
    return document(title, body, "/style.css")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError):
            return  # the browser went away mid-reply; nothing to report
        log.warning("Settings page request failed: %s", type(error).__name__)


def make_server(handler, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return _Server((host, port), handler)
