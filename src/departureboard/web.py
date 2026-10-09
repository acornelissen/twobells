"""The configuration web page's server: its pages, forms and responses.

Home network only. Every request must come from a private address and name a
local host (which blocks DNS rebinding from outside websites); every change
needs a signed-in session and a matching form token; sign-in attempts are
rate limited. No JavaScript, and a strict Content-Security-Policy. The
sign-in machinery and the request checks live in auth.py, shared with the
dashboard server.

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
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qs, urlparse

from . import setupapi
from .auth import (
    FORM_PAGE_HEADERS,
    MIN_PASSWORD,
    FormTokens,
    Lockout,
    PasswordCheck,
    PasswordStore,
    ResetCodes,
    ResetLimited,
    Sessions,
    SignInResult,
    csrf_matches,
    outsider,
    same_origin,
)

# Their home before auth.py; the old names still work.
from .auth import FAILURE_WINDOW_S, MAX_FAILURES, SESSION_SECONDS, local_host, private_address  # noqa: F401
from .auth import Lockout as SignInLimiter  # noqa: F401
from .auth import FORM_PAGE_HEADERS as HEADERS  # noqa: F401
from .auth import PasswordStore as Password  # noqa: F401

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
MAX_BODY = 64 * 1024
REQUEST_TIMEOUT_S = 10
# How to get a code when the board can't show one, as the service user.
RESET_CODE_COMMAND = "sudo -u departureboard /opt/departureboard/venv/bin/departureboard web --reset-code"
SET_PASSWORD_COMMAND = "sudo -u departureboard /opt/departureboard/venv/bin/departureboard web --set-password"
# The same words whether the code was wrong, used, run out, voided or never
# issued, so a guesser learns nothing about whether there is one.
WRONG_CODE = "That code isn't right, or it has run out. Ask for a new code and try again."


SETUP = "/setup/"
SETUP_API = SETUP + "api/"
_SETUP_STEP = re.compile(r"^[a-z]{1,20}$")
# The only kinds of file the wizard serves, each with its type said outright.
SETUP_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"


class SettingsPage(Protocol):
    """The form: drawn for a signed-in visitor, and saved from their fields."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    def save(self, fields: dict[str, str]) -> dict[str, str]: ...  # field -> error; empty when saved


class SetupSteps(Protocol):
    """setupapi.SetupApi: a step's (status, JSON-ready reply) for its decoded body."""

    def handle(self, name: str, payload: object) -> tuple[int, dict]: ...


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
    checker = PasswordCheck(password, lockout)
    codes = codes or ResetCodes(password.path.parent)
    forms = FormTokens(clock)

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

        def _page(self, status: int, title: str, body: str, extra: dict[str, str] | None = None) -> None:
            self._send(status, shell(title, body).encode(), extra=extra)

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
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=300)
            return {key: values[-1] for key, values in parsed.items()}

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self._refuse_outsiders():
                return
            url = urlparse(self.path)
            path = url.path
            if path == "/style.css":
                self._send(200, stylesheet, "text/css; charset=utf-8")
            elif path == "/login":
                self._login_page(200)
            elif path == "/reset":
                self._reset_page(200, asked=url.query == "asked")
            elif path == "/":
                session = sessions.get(self._token())
                if session is None:
                    self._redirect("/login")
                else:
                    self._page(200, "Board settings", page.render(session.csrf))
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

        def _json(self, status: int, reply: dict) -> None:
            self._setup_send(status, json.dumps(reply).encode(), JSON)

        def _setup_file(self, relative: str) -> None:
            """The wizard's page, a file on its list, or its session's form token."""
            session = sessions.get(self._token())
            if relative == "api/session":
                if session is None:
                    self._json(401, {"error": "Sign in first"})
                else:
                    self._json(200, {"csrf": session.csrf})
                return
            if session is None:
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
            header, JSON of a sensible size. Same-origin is checked before this."""
            session = sessions.get(self._token())
            if session is None:
                self._json(401, {"error": "You've been signed out. Reload the page and sign in again."})
                return
            if not csrf_matches(session, self.headers.get("X-CSRF-Token", "")):
                self._json(403, {"error": "This page has run out. Reload it and try again."})
                return
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "Send JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"error": "Say how long the request is"})
                return
            if not 0 <= length <= setupapi.MAX_BODY:
                self._json(413, {"error": "That request is too big"})
                return
            if not _SETUP_STEP.match(name):
                self._json(404, {"error": "No such setup step"})
                return
            try:
                payload = setupapi.decode(self.rfile.read(length))
            except setupapi.Invalid as error:
                self._json(400, {"error": str(error)})
                return
            self._json(*wizard.api.handle(name, payload))

        def _home(self) -> str:
            """Where a new session starts: the wizard while the board isn't set up."""
            if wizard is None:
                return "/"
            try:
                return SETUP if wizard.needed() else "/"
            except Exception as error:  # noqa: BLE001 - the settings page will say what is wrong
                log.warning("Can't tell whether the board is set up: %s", type(error).__name__)
                return "/"

        def _form_token(self) -> tuple[str, dict[str, str]]:
            """A pre-session form's hidden field, and the header that sets its cookie."""
            nonce = forms.nonce(self.headers.get("Cookie"))
            return html.escape(forms.form_value(nonce)), {"Set-Cookie": forms.cookie(nonce)}

        def _form_token_ok(self, fields: dict[str, str]) -> bool:
            return forms.check(self.headers.get("Cookie"), fields.get("form", ""))

        def _login_page(self, status: int, message: str = "") -> None:
            if not password.is_set():
                self._reset_page(status, message)  # the first claim
                return
            token, cookie = self._form_token()
            error = f'<p class="error" role="alert" id="login-error">{html.escape(message, quote=False)}</p>' if message else ""
            described = ' aria-describedby="login-error" aria-invalid="true"' if message else ""
            body = (
                "<h1>Board settings</h1>"
                f"{error}"
                '<form method="post" action="/login" class="signin">'
                '<label for="password">Password</label>'
                f'<input id="password" name="password" type="password" autocomplete="current-password" '
                f'required autofocus{described}>'
                '<button type="submit">Sign in</button></form>'
                '<form method="post" action="/forgot" class="forgot">'
                f'<input type="hidden" name="form" value="{token}">'
                '<button type="submit" class="quiet">Forgot password?</button></form>'
            )
            self._page(status, "Sign in", body, cookie)

        def _reset_page(self, status: int, message: str = "", invalid: str = "", asked: bool = False) -> None:
            """Ask for a code on the board, then type it with a new password: for
            a forgotten password, and for the first claim when there is none.
            `invalid` names the field `message` is about."""
            claim = not password.is_set()
            token, cookie = self._form_token()
            title = "Claim this board" if claim else "Set a new password"
            if claim:
                intro = ("No password is set yet. To set one here, show that you can see the board: "
                         "ask for a code on its display, then type it below with a new password.")
            else:
                intro = ("Ask for a code on the board's display, then type it below with a new password. "
                         "Every browser signed in now is signed out.")
            parts = [f"<h1>{title}</h1>", f"<p>{intro}</p>"]
            if asked:
                parts.append('<p class="notice" role="status">Look at the board: a 6-digit code shows there for '
                             "10 minutes. E-paper can take a minute to change.</p>")
            if message:
                parts.append(f'<p class="error" role="alert" id="reset-error">{html.escape(message, quote=False)}</p>')

            def flagged(name: str, hint: str = "") -> str:
                """A field's description, and the error when it is about this field."""
                wrong = bool(message) and invalid == name
                described = " ".join(part for part in (hint, "reset-error" if wrong else "") if part)
                attributes = f' aria-describedby="{described}"' if described else ""
                return attributes + (' aria-invalid="true"' if wrong else "")

            ask = "Show a new code" if asked else "Show a code on the board"
            parts.append(
                '<form method="post" action="/forgot" class="forgot">'
                f'<input type="hidden" name="form" value="{token}">'
                f'<button type="submit" class="quiet">{ask}</button></form>'
                '<form method="post" action="/reset" class="signin">'
                f'<input type="hidden" name="form" value="{token}">'
                '<label for="code">Code from the board</label>'
                '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" '
                f'required{" autofocus" if asked or invalid == "code" else ""}{flagged("code")}>'
                '<label for="new-password">New password</label>'
                f'<p class="hint" id="new-password-hint">At least {MIN_PASSWORD} characters.</p>'
                '<input id="new-password" name="password" type="password" autocomplete="new-password" '
                f'minlength="{MIN_PASSWORD}" required{flagged("password", "new-password-hint")}>'
                '<label for="again">New password again</label>'
                f'<input id="again" name="again" type="password" autocomplete="new-password" required{flagged("again")}>'
                f'<button type="submit">{"Set password" if claim else "Set new password"} and sign in</button></form>'
                '<p class="hint after">No code? Look at the board (or run '
                f"<code>{html.escape(RESET_CODE_COMMAND)}</code> on the device to print it). "
                "A new code can be shown once a minute, up to 10 times a day.</p>"
            )
            if claim:
                parts.append(f'<p class="hint">Or set the password on the Pi: <code>{html.escape(SET_PASSWORD_COMMAND)}</code></p>')
            else:
                parts.append('<p class="after"><a href="/login">Back to sign in</a></p>')
            self._page(status, title, "".join(parts), cookie)

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
            if path == "/login":
                self._sign_in(fields)
            elif path == "/forgot":
                self._ask_for_code(fields)
            elif path == "/reset":
                self._reset(fields)
            elif path == "/logout":
                if csrf_matches(sessions.get(self._token()), fields.get("csrf", "")):
                    sessions.end(self._token())
                self._redirect("/login", {"Set-Cookie": sessions.expired_cookie()})
            elif path == "/":
                self._save(fields)
            else:
                self._send(404, b"Not found", TEXT)

        def _sign_in(self, fields: dict[str, str]) -> None:
            if not password.is_set():
                self._login_page(403)
                return
            result = checker.sign_in(self.client_address[0], fields.get("password", ""))
            if result is SignInResult.BUSY:
                self._login_page(429, "Busy, try again in a moment.")
                return
            if result is SignInResult.LOCKED:
                self._login_page(429, "Too many attempts. Try again in 15 minutes.")
                return
            if result is SignInResult.WRONG:
                self._login_page(401, "That password isn't right.")
                return
            token, _ = sessions.create()
            self._redirect(self._home(), {"Set-Cookie": sessions.cookie(token)})

        def _ask_for_code(self, fields: dict[str, str]) -> None:
            if not self._form_token_ok(fields):
                self._reset_page(403, "That form had run out, so no code was shown. Try again.")
                return
            try:
                codes.issue(clock())
            except ResetLimited:
                pass  # the code already on the board stands; the page reads the same either way
            except OSError as error:
                log.warning("Can't issue a settings reset code: %s", type(error).__name__)
            self._redirect("/reset?asked")

        def _reset(self, fields: dict[str, str]) -> None:
            if not self._form_token_ok(fields):
                self._reset_page(403, "That form had run out, so nothing was changed. Try again.")
                return
            new = fields.get("password", "")
            # Checked before the code, so a typo here doesn't use the code up.
            if len(new) < MIN_PASSWORD:
                self._reset_page(422, f"Use at least {MIN_PASSWORD} characters for the new password.", "password")
                return
            if new != fields.get("again", ""):
                self._reset_page(422, "The two new passwords don't match.", "again")
                return
            try:
                right = codes.verify(fields.get("code", ""), clock())
            except OSError as error:
                log.warning("Can't check a settings reset code: %s", type(error).__name__)
                self._reset_page(503, "The code can't be checked right now. Set the password on the Pi instead.")
                return
            if not right:
                self._reset_page(401, WRONG_CODE, "code")
                return
            password.set(new)
            log.info("Settings page password set with a reset code; every session ended")
            sessions.end_all()
            lockout.succeeded(self.client_address[0])
            token, _ = sessions.create()
            self._redirect(self._home(), {"Set-Cookie": sessions.cookie(token)})

        def _save(self, fields: dict[str, str]) -> None:
            session = sessions.get(self._token())
            if session is None:
                self._redirect("/login")
                return
            if not csrf_matches(session, fields.get("csrf", "")):
                self._send(403, b"Forbidden: form expired, reload the page", TEXT)
                return
            fields.pop("csrf", None)
            errors = page.save(fields)
            if errors:
                body = page.render(session.csrf, errors=errors, values=fields)
                self._page(422, "Board settings: fix the errors", body)
            else:
                message = "Saved. The board shows it within a few minutes; the screen within seconds."
                self._page(200, "Saved – Board settings", page.render(session.csrf, message=message))

        def log_message(self, format, *args) -> None:  # noqa: A002 - no request lines, no secrets in logs
            pass

    return Handler


def shell(title: str, body: str) -> str:
    """The HTML document around a page's content."""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="stylesheet" href="/style.css"></head>'
        f"<body><main>{body}</main></body></html>"
    )


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError):
            return  # the browser went away mid-reply; nothing to report
        log.warning("Settings page request failed: %s", type(error).__name__)


def make_server(handler, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return _Server((host, port), handler)
