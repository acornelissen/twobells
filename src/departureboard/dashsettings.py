"""The dashboard's password-protected settings area, under /settings.

The rest of the dashboard is read-only and open to the home network; this
part changes what it runs on, so it needs signing in. The machinery is
auth.py's, as on the Pi's settings page (web.py), with the dashboard's own
names so the two never share a cookie on one host:

- The password is in web.json in the dashboard's state folder. The first
  claim, when none is set, and a forgotten one both need a one-time code.
  The dashboard has no display, so the code goes to its log (the container
  log in Docker, the Terminal on a Mac), which only the owner can read.
  No response ever holds a code.
- A session is a random token in an HttpOnly, SameSite=Strict cookie named
  dash_session with Path=/settings; pre-session forms carry a signed
  double-submit token (dash_form). Every POST must be same-origin and carry
  the session's (or the form's) token.
- Pages are drawn on the server with no JavaScript, under a CSP allowing
  form-action 'self'. The setup wizard under /settings/setup/ is the one
  place with scripts: its own files only, from an allowlist read at start,
  under setupapi.PAGE_HEADERS, its API needing the session and its CSRF
  token in X-CSRF-Token.

Framework-free: the dashboard server reads the request (its body capped by
max_body) and hands it to SettingsArea.handle, which returns a Reply; the
server adds headers_for(path) and its own limits, the same as for the rest.
"""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qs

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
    same_origin,
    show_in_log,
)

log = logging.getLogger(__name__)

PREFIX = "/settings"
SETUP = "/settings/setup/"
SETUP_API = "/settings/setup/api/"
COOKIE_NAME = "dash_session"
FORM_COOKIE_NAME = "dash_form"
PASSWORD_FILE = "web.json"
MAX_FORM_BODY = 64 * 1024
LOG_HELP = ("In Docker: <code>docker logs departureboard-dashboard</code>, or in Container Manager, "
            "Container, departureboard-dashboard, Log. On a Mac: the Terminal window running the dashboard.")
# The same words whether the code was wrong, used, run out, voided or never
# issued, so a guesser learns nothing about whether there is one.
WRONG_CODE = "That code isn't right, or it has run out. Ask for a new code and try again."

_TEXT = "text/plain; charset=utf-8"
_HTML = "text/html; charset=utf-8"
_JSON = "application/json"
_CONTENT_TYPES = {".html": _HTML, ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
_SHARED = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# Cache-Control is the server's to send (no-store), once.
SETTINGS_HEADERS = {**{k: v for k, v in FORM_PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}
WIZARD_HEADERS = {**{k: v for k, v in setupapi.PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}


class SettingsPage(Protocol):
    """The everyday form (dashform.DashboardForm)."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    def save(self, fields: dict[str, str]) -> dict[str, str]: ...


class SetupHandlers(Protocol):
    """The wizard's handlers (setupapi.SetupApi)."""

    def handle(self, name: str, payload: object) -> tuple[int, dict]: ...


class Headers(Protocol):
    """A request's headers, as http.server gives them."""

    def get(self, name: str, default: str | None = None) -> str | None: ...

    def get_content_type(self) -> str: ...


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    content_type: str = _TEXT
    headers: dict[str, str] = field(default_factory=dict)  # Location, Set-Cookie


def owns(path: str) -> bool:
    """Whether a request path is the settings area's."""
    return path == PREFIX or path.startswith(PREFIX + "/")


def headers_for(path: str) -> dict[str, str]:
    """The security headers for a path: scripts only on the wizard's."""
    return WIZARD_HEADERS if path == SETUP[:-1] or path.startswith(SETUP) else SETTINGS_HEADERS


def max_body(path: str) -> int:
    """The largest request body a POST to `path` may have."""
    return setupapi.MAX_BODY if path.startswith(SETUP_API) else MAX_FORM_BODY


def wizard_files(found: Mapping[str, Path] | None = None) -> dict[str, tuple[bytes, str]]:
    """The wizard's files by path under /settings/setup/, read once: only
    those setupapi lists, with a type we know."""
    out = {}
    for relative, path in (setupapi.static_files() if found is None else found).items():
        content_type = _CONTENT_TYPES.get(Path(path).suffix.lower())
        if content_type is None:
            continue
        try:
            out[relative] = (Path(path).read_bytes(), content_type)
        except OSError as error:
            log.warning("Setup page file %s unreadable: %s", relative or "index.html", type(error).__name__)
    return out


def shell(title: str, body: str) -> str:
    """The HTML document around a page's content."""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="{PREFIX}/style.css"></head>'
        f"<body><main>{body}</main></body></html>"
    )


class SettingsArea:
    """Everything under /settings, for one dashboard and its state folder."""

    def __init__(self, folder: Path | str, page: SettingsPage, setup: SetupHandlers, *,
                 stylesheet: bytes | None = None, files: dict[str, tuple[bytes, str]] | None = None,
                 clock: Callable[[], float] = time.time, codes: ResetCodes | None = None) -> None:
        folder = Path(folder)
        self.password = PasswordStore(folder / PASSWORD_FILE)
        self.sessions = Sessions(clock, cookie_name=COOKIE_NAME, cookie_path=PREFIX, password=self.password)
        self.lockout = Lockout(clock)
        self._checker = PasswordCheck(self.password, self.lockout)
        self._codes = codes or ResetCodes(folder, show=show_in_log)
        self._forms = FormTokens(clock, cookie_name=FORM_COOKIE_NAME, cookie_path=PREFIX)
        self._page = page
        self._setup = setup
        self._clock = clock
        self._stylesheet = _default_stylesheet() if stylesheet is None else stylesheet
        self._files = wizard_files() if files is None else files

    def handle(self, method: str, path: str, query: str, headers: Headers, client: str, body: bytes) -> Reply:
        """The reply to one request the server has already let in: from the home
        network, naming a local host, with a path under /settings."""
        if path.startswith(SETUP_API) and (headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return _json(403, {"error": "Not from another site"})
        if method in ("GET", "HEAD"):
            return self._get(path, query, headers)
        if method != "POST":
            return Reply(405, b"Method Not Allowed", headers={"Allow": "GET, HEAD, POST"})
        if not same_origin(headers):
            return Reply(403, b"Forbidden: cross-site request")
        if path.startswith(SETUP_API):
            return self._wizard_api(path[len(SETUP_API):], headers, body)
        if headers.get_content_type() != "application/x-www-form-urlencoded":
            return Reply(400, b"Bad request")
        parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=300)
        fields = {key: values[-1] for key, values in parsed.items()}
        if path == PREFIX + "/login":
            return self._sign_in(fields, headers, client)
        if path == PREFIX + "/forgot":
            return self._ask_for_code(fields, headers)
        if path == PREFIX + "/reset":
            return self._reset(fields, headers, client)
        if path == PREFIX + "/logout":
            token = self._token(headers)
            if csrf_matches(self.sessions.get(token), fields.get("csrf", "")):
                self.sessions.end(token)
            return _redirect(PREFIX + "/login", {"Set-Cookie": self.sessions.expired_cookie()})
        if path == PREFIX:
            return self._save(fields, headers)
        return Reply(404, b"Not Found")

    # Reading

    def _token(self, headers: Headers) -> str | None:
        return self.sessions.token_from(headers.get("Cookie"))

    def _get(self, path: str, query: str, headers: Headers) -> Reply:
        session = self.sessions.get(self._token(headers))
        if path == PREFIX:
            if session is None:
                return _redirect(PREFIX + "/login")
            return self._html(200, "Dashboard settings", self._page.render(session.csrf))
        if path == PREFIX + "/":
            return _redirect(PREFIX)
        if path == PREFIX + "/login":
            return self._login_page(200, headers, to_setup=query == "next=setup")
        if path == PREFIX + "/reset":
            return self._reset_page(200, headers, asked=query == "asked")
        if path == PREFIX + "/style.css":
            return Reply(200, self._stylesheet, "text/css; charset=utf-8")
        if path == SETUP[:-1]:
            return _redirect(SETUP)
        if path == SETUP:
            if session is None:
                return _redirect(PREFIX + "/login?next=setup")
            body, content_type = self._files.get("", (b"", None))
            return Reply(200, body, content_type) if content_type else Reply(404, b"Not Found")
        if path == SETUP_API + "session":
            if session is None:
                return _json(401, {"error": "Sign in first"})
            return _json(200, {"csrf": session.csrf})
        found = self._files.get(path[len(SETUP):]) if path.startswith(SETUP) else None
        if found is not None:
            return Reply(200, found[0], found[1])
        return Reply(404, b"Not Found")

    def _html(self, status: int, title: str, body: str, extra: dict[str, str] | None = None) -> Reply:
        return Reply(status, shell(title, body).encode(), _HTML, extra or {})

    def _form_token(self, headers: Headers) -> tuple[str, dict[str, str]]:
        """A pre-session form's hidden field, and the header that sets its cookie."""
        nonce = self._forms.nonce(headers.get("Cookie"))
        return html.escape(self._forms.form_value(nonce)), {"Set-Cookie": self._forms.cookie(nonce)}

    def _form_token_ok(self, fields: dict[str, str], headers: Headers) -> bool:
        return self._forms.check(headers.get("Cookie"), fields.get("form", ""))

    def _login_page(self, status: int, headers: Headers, message: str = "", to_setup: bool = False) -> Reply:
        if not self.password.is_set():
            return self._reset_page(status, headers, message)  # the first claim
        token, cookie = self._form_token(headers)
        error = (f'<p class="error" role="alert" id="login-error">{html.escape(message, quote=False)}</p>'
                 if message else "")
        described = ' aria-describedby="login-error" aria-invalid="true"' if message else ""
        then = '<input type="hidden" name="next" value="setup">' if to_setup else ""
        body = (
            "<h1>Dashboard settings</h1>"
            f"{error}"
            f'<form method="post" action="{PREFIX}/login" class="signin">{then}'
            '<label for="password">Password</label>'
            '<input id="password" name="password" type="password" autocomplete="current-password" '
            f"required autofocus{described}>"
            '<button type="submit">Sign in</button></form>'
            f'<form method="post" action="{PREFIX}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            '<button type="submit" class="quiet">Forgot password?</button></form>'
            '<p class="after"><a href="/">Back to the dashboard</a></p>'
        )
        return self._html(status, "Sign in", body, cookie)

    def _reset_page(self, status: int, headers: Headers, message: str = "", invalid: str = "",
                    asked: bool = False) -> Reply:
        """Ask for a code in the log, then type it with a new password: for a
        forgotten password, and for the first claim when there is none.
        `invalid` names the field `message` is about."""
        claim = not self.password.is_set()
        token, cookie = self._form_token(headers)
        title = "Claim this dashboard" if claim else "Set a new password"
        if claim:
            intro = ("No password is set yet. To set one, show that you can read the dashboard's log: "
                     "ask for a code, find it in the log, then type it below with a new password.")
        else:
            intro = ("Ask for a code, find it in the dashboard's log, then type it below with a new password. "
                     "Every browser signed in now is signed out.")
        parts = [f"<h1>{title}</h1>", f"<p>{intro}</p>"]
        if asked:
            parts.append('<p class="notice" role="status">A 6-digit code is in the dashboard\'s log now, '
                         f"valid for 10 minutes. {LOG_HELP}</p>")
        if message:
            parts.append(f'<p class="error" role="alert" id="reset-error">{html.escape(message, quote=False)}</p>')

        def flagged(name: str, hint: str = "") -> str:
            """A field's description, and the error when it is about this field."""
            wrong = bool(message) and invalid == name
            described = " ".join(part for part in (hint, "reset-error" if wrong else "") if part)
            attributes = f' aria-describedby="{described}"' if described else ""
            return attributes + (' aria-invalid="true"' if wrong else "")

        ask = "Put a new code in the log" if asked else "Put a code in the log"
        parts.append(
            f'<form method="post" action="{PREFIX}/forgot" class="forgot">'
            f'<input type="hidden" name="form" value="{token}">'
            f'<button type="submit" class="quiet">{ask}</button></form>'
            f'<form method="post" action="{PREFIX}/reset" class="signin">'
            f'<input type="hidden" name="form" value="{token}">'
            '<label for="code">Code from the log</label>'
            '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7" '
            f'required{" autofocus" if asked or invalid == "code" else ""}{flagged("code")}>'
            '<label for="new-password">New password</label>'
            f'<p class="hint" id="new-password-hint">At least {MIN_PASSWORD} characters.</p>'
            '<input id="new-password" name="password" type="password" autocomplete="new-password" '
            f'minlength="{MIN_PASSWORD}" required{flagged("password", "new-password-hint")}>'
            '<label for="again">New password again</label>'
            '<input id="again" name="again" type="password" autocomplete="new-password" '
            f'required{flagged("again")}>'
            f'<button type="submit">{"Set password" if claim else "Set new password"} and sign in</button></form>'
            f'<p class="hint after">Where to find the log: {LOG_HELP} '
            "A new code can be made once a minute, up to 10 times a day.</p>"
        )
        if not claim:
            parts.append(f'<p class="after"><a href="{PREFIX}/login">Back to sign in</a></p>')
        parts.append('<p class="after"><a href="/">Back to the dashboard</a></p>')
        return self._html(status, title, "".join(parts), cookie)

    # Changing

    def _signed_in(self, fields: dict[str, str]) -> Reply:
        token, _ = self.sessions.create()
        where = SETUP if fields.get("next") == "setup" else PREFIX
        return _redirect(where, {"Set-Cookie": self.sessions.cookie(token)})

    def _sign_in(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        if not self.password.is_set():
            return self._login_page(403, headers)
        to_setup = fields.get("next") == "setup"
        result = self._checker.sign_in(client, fields.get("password", ""))
        if result is SignInResult.BUSY:
            return self._login_page(429, headers, "Busy, try again in a moment.", to_setup)
        if result is SignInResult.LOCKED:
            return self._login_page(429, headers, "Too many attempts. Try again in 15 minutes.", to_setup)
        if result is SignInResult.WRONG:
            return self._login_page(401, headers, "That password isn't right.", to_setup)
        return self._signed_in(fields)

    def _ask_for_code(self, fields: dict[str, str], headers: Headers) -> Reply:
        if not self._form_token_ok(fields, headers):
            return self._reset_page(403, headers, "That form had run out, so no code was made. Try again.")
        try:
            self._codes.issue(self._clock())
        except ResetLimited:
            pass  # the code already in the log stands; the page reads the same either way
        except OSError as error:
            log.warning("Can't issue a settings reset code: %s", type(error).__name__)
        return _redirect(PREFIX + "/reset?asked")

    def _reset(self, fields: dict[str, str], headers: Headers, client: str) -> Reply:
        if not self._form_token_ok(fields, headers):
            return self._reset_page(403, headers, "That form had run out, so nothing was changed. Try again.")
        new = fields.get("password", "")
        # Checked before the code, so a typo here doesn't use the code up.
        if len(new) < MIN_PASSWORD:
            return self._reset_page(422, headers, f"Use at least {MIN_PASSWORD} characters for the new password.",
                                    "password")
        if new != fields.get("again", ""):
            return self._reset_page(422, headers, "The two new passwords don't match.", "again")
        try:
            right = self._codes.verify(fields.get("code", ""), self._clock())
        except OSError as error:
            log.warning("Can't check a settings reset code: %s", type(error).__name__)
            return self._reset_page(503, headers, "The code can't be checked right now. Try again in a moment.")
        if not right:
            return self._reset_page(401, headers, WRONG_CODE, "code")
        self.password.set(new)
        log.info("Dashboard settings password set with a reset code; every session ended")
        self.sessions.end_all()
        self.lockout.succeeded(client)
        return self._signed_in({})

    def _save(self, fields: dict[str, str], headers: Headers) -> Reply:
        session = self.sessions.get(self._token(headers))
        if session is None:
            return _redirect(PREFIX + "/login")
        if not csrf_matches(session, fields.get("csrf", "")):
            return Reply(403, b"Forbidden: form expired, reload the page")
        fields.pop("csrf", None)
        errors = self._page.save(fields)
        if errors:
            return self._html(422, "Dashboard settings: fix the errors",
                              self._page.render(session.csrf, errors=errors, values=fields))
        message = ("Saved. The dashboard restarts in a few seconds to use the new settings; "
                   "sign in again to change anything else.")
        return self._html(200, "Saved – Dashboard settings", self._page.render(session.csrf, message=message))

    def _wizard_api(self, name: str, headers: Headers, body: bytes) -> Reply:
        session = self.sessions.get(self._token(headers))
        if session is None:
            return _json(401, {"error": "You've been signed out. Reload the page and sign in again."})
        if not csrf_matches(session, headers.get("X-CSRF-Token") or ""):
            return _json(403, {"error": "That request had no valid form token. Reload the page."})
        if headers.get_content_type() != _JSON:
            return _json(415, {"error": "Send JSON"})
        try:
            payload = setupapi.decode(body)
        except setupapi.Invalid as error:
            return _json(400, {"error": str(error)})
        status, reply = self._setup.handle(name, payload)
        return _json(status, reply)


def _redirect(where: str, extra: dict[str, str] | None = None) -> Reply:
    return Reply(303, b"", _TEXT, {"Location": where, **(extra or {})})


def _json(status: int, value: dict) -> Reply:
    return Reply(status, json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii"), _JSON)


def _default_stylesheet() -> bytes:
    return files("departureboard").joinpath("web.css").read_bytes()
