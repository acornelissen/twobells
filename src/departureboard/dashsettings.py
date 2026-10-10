"""The dashboard's password-protected settings area, under /settings.

The rest of the dashboard is read-only and open to the home network; this
part changes what it runs on, so it needs signing in. The machinery is
auth.py's, and the pages and forms formpages.py's, as on the Pi's settings
page (web.py), with the dashboard's own names so the two never share a
cookie on one host:

- The password is in web.json in the dashboard's state folder. The first
  claim, when none is set, and a forgotten one both need a one-time code.
  The dashboard has no display, so the code goes to its log (the container
  log in Docker, the Terminal on a Mac), which only the owner can read.
  No response ever holds a code. `departureboard dashboard --set-password`
  and `--reset-code` do the same from the command line, in the same folder;
  a password set there ends every session here (Sessions checks its stamp).
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

import json
import logging
import time
from importlib.resources import files
from pathlib import Path
from typing import Callable, Mapping, Protocol

from . import setupapi
from .auth import (
    FORM_PAGE_HEADERS,
    FormTokens,
    Lockout,
    PasswordStore,
    ResetCodes,
    Sessions,
    csrf_matches,
    same_origin,
    show_in_log,
)
from .formpages import (
    MAX_FORM_BODY,
    FormPages,
    Headers,
    Reply,
    SettingsPage,
    Wording,
    document,
    parse_form,
    redirect,
)
from .formpages import WIZARD_TYPES as _CONTENT_TYPES

log = logging.getLogger(__name__)

PREFIX = "/settings"
SETUP = "/settings/setup/"
SETUP_API = "/settings/setup/api/"
COOKIE_NAME = "dash_session"
FORM_COOKIE_NAME = "dash_form"
PASSWORD_FILE = "web.json"
LOG_HELP = ("In Docker: <code>docker logs departureboard-dashboard</code>, or in Container Manager, "
            "Container, departureboard-dashboard, Log. On a Mac: the Terminal window running the dashboard.")
# The dashboard has no display, so its codes go to its log.
DASHBOARD = Wording(
    title="Dashboard settings",
    claim_title="Claim this dashboard",
    claim_intro=("No password is set yet. To set one, show that you can read the dashboard's log: "
                 "ask for a code, find it in the log, then type it below with a new password."),
    reset_intro=("Ask for a code, find it in the dashboard's log, then type it below with a new password. "
                 "Every browser signed in now is signed out."),
    asked=f"A 6-digit code is in the dashboard's log now, valid for 10 minutes. {LOG_HELP}",
    ask="Put a code in the log",
    ask_again="Put a new code in the log",
    code_label="Code from the log",
    no_code=f"{LOG_HELP} A new code can be made once a minute, up to 10 times a day.",
    claim_hint="",
    back='<p class="after"><a href="/">Back to the dashboard</a></p>',
    no_code_made="That form had run out, so no code was made. Try again.",
    cant_check="The code can't be checked right now. Try again in a moment.",
    saved=("Saved. The dashboard starts again in a few seconds to use the new settings; "
           "sign in again to change anything else."),
    reset_logged="Dashboard settings password set with a reset code; every session ended",
)

_JSON = "application/json"
_SHARED = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
# Cache-Control is the server's to send (no-store), once.
SETTINGS_HEADERS = {**{k: v for k, v in FORM_PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}
WIZARD_HEADERS = {**{k: v for k, v in setupapi.PAGE_HEADERS.items() if k != "Cache-Control"}, **_SHARED}


class SetupHandlers(Protocol):
    """The wizard's handlers (setupapi.SetupApi)."""

    def handle(self, name: str, payload: object) -> tuple[int, dict]: ...


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
    return document(title, body, f"{PREFIX}/style.css")


class SettingsArea:
    """Everything under /settings, for one dashboard and its state folder."""

    def __init__(self, folder: Path | str, page: SettingsPage, setup: SetupHandlers, *,
                 stylesheet: bytes | None = None, files: dict[str, tuple[bytes, str]] | None = None,
                 clock: Callable[[], float] = time.time, codes: ResetCodes | None = None) -> None:
        folder = Path(folder)
        self.password = PasswordStore(folder / PASSWORD_FILE)
        self.sessions = Sessions(clock, cookie_name=COOKIE_NAME, cookie_path=PREFIX, password=self.password)
        self.lockout = Lockout(clock)
        self._pages = FormPages(
            page, self.password, self.sessions, self.lockout,
            codes or ResetCodes(folder, show=show_in_log),
            FormTokens(clock, cookie_name=FORM_COOKIE_NAME, cookie_path=PREFIX),
            DASHBOARD, prefix=PREFIX, shell=shell, home=lambda: PREFIX, log=log, resume_setup=SETUP, clock=clock,
        )
        self._setup = setup
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
        fields = parse_form(body)
        if path == PREFIX + "/login":
            return self._pages.sign_in(fields, headers, client)
        if path == PREFIX + "/forgot":
            return self._pages.ask_for_code(fields, headers)
        if path == PREFIX + "/reset":
            return self._pages.reset(fields, headers, client)
        if path == PREFIX + "/logout":
            return self._pages.sign_out(fields, headers)
        if path == PREFIX:
            return self._pages.save(fields, headers)
        return Reply(404, b"Not Found")

    def _token(self, headers: Headers) -> str | None:
        return self.sessions.token_from(headers.get("Cookie"))

    def _get(self, path: str, query: str, headers: Headers) -> Reply:
        if path == PREFIX:
            return self._pages.settings_page(headers)
        session = self.sessions.get(self._token(headers))
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
            if session is None:
                return redirect(PREFIX + "/login?next=setup")
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


def _json(status: int, value: dict) -> Reply:
    return Reply(status, json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii"), _JSON)


def _default_stylesheet() -> bytes:
    return files("departureboard").joinpath("web.css").read_bytes()
