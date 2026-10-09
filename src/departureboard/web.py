"""The configuration web page's server: its pages, forms and responses.

Home network only. Every request must come from a private address and name a
local host (which blocks DNS rebinding from outside websites); every change
needs a signed-in session and a matching form token; sign-in attempts are
rate limited. The password is set on the Pi itself (`web --set-password`),
never through the page, so nobody on the network can claim it first. No
JavaScript, and a strict Content-Security-Policy. The sign-in machinery and
the request checks live in auth.py, shared with the dashboard server.
"""

from __future__ import annotations

import html
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from .auth import (
    FORM_PAGE_HEADERS,
    Lockout,
    PasswordCheck,
    PasswordStore,
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


class SettingsPage(Protocol):
    """The form: drawn for a signed-in visitor, and saved from their fields."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    def save(self, fields: dict[str, str]) -> dict[str, str]: ...  # field -> error; empty when saved


def make_handler(page: SettingsPage, password: PasswordStore, sessions: Sessions, lockout: Lockout,
                 shell: Callable[[str, str], str], stylesheet: bytes):
    """`shell(title, body)` wraps page content in the HTML document."""
    checker = PasswordCheck(password, lockout)

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S  # a client that stalls mid-request lets go of its thread

        def version_string(self) -> str:
            return "departureboard"  # no Python or OS version for anyone probing

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/html; charset=utf-8",
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in {**FORM_PAGE_HEADERS, **(extra or {})}.items():
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
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
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
            path = urlparse(self.path).path
            if path == "/style.css":
                self._send(200, stylesheet, "text/css; charset=utf-8")
            elif path == "/login":
                self._login_page(200)
            elif path == "/":
                session = sessions.get(self._token())
                if session is None:
                    self._redirect("/login")
                else:
                    self._page(200, "Board settings", page.render(session.csrf))
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def _login_page(self, status: int, message: str = "") -> None:
            if not password.is_set():
                body = (
                    '<h1>Board settings</h1><p class="notice">No password is set yet. On the Pi, run '
                    "<code>sudo -u departureboard /opt/departureboard/venv/bin/departureboard web "
                    "--set-password</code>, then reload this page.</p>"
                )
                self._page(status, "Set a password first", body)
                return
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
            )
            self._page(status, "Sign in", body)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self._refuse_outsiders():
                return
            if not same_origin(self.headers):
                self._send(403, b"Forbidden: cross-site request", "text/plain; charset=utf-8")
                return
            fields = self._form()
            if fields is None:
                self._send(400, b"Bad request", "text/plain; charset=utf-8")
                return
            path = urlparse(self.path).path
            if path == "/login":
                self._sign_in(fields)
            elif path == "/logout":
                if csrf_matches(sessions.get(self._token()), fields.get("csrf", "")):
                    sessions.end(self._token())
                self._redirect("/login", {"Set-Cookie": sessions.expired_cookie()})
            elif path == "/":
                self._save(fields)
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

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
            self._redirect("/", {"Set-Cookie": sessions.cookie(token)})

        def _save(self, fields: dict[str, str]) -> None:
            session = sessions.get(self._token())
            if session is None:
                self._redirect("/login")
                return
            if not csrf_matches(session, fields.get("csrf", "")):
                self._send(403, b"Forbidden: form expired, reload the page", "text/plain; charset=utf-8")
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
