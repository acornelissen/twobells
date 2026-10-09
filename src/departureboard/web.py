"""The configuration web page's server: sign-in, sessions and request checks.

Home network only. Every request must come from a private address and name a
local host (which blocks DNS rebinding from outside websites); every change
needs a signed-in session and a matching form token; sign-in attempts are
rate limited. The password is set on the Pi itself (`web --set-password`),
never through the page, so nobody on the network can claim it first. No
JavaScript, and a strict Content-Security-Policy.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

DEFAULT_PORT = 8080
MIN_PASSWORD = 10
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 20
MAX_BODY = 64 * 1024
# Sign-in: this many failures from one address in the window locks it out for the window.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Password:
    """A scrypt hash in a file only the service user can read."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def is_set(self) -> bool:
        return self._path.exists()

    def set(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
        record = {"salt": salt.hex(), "hash": digest.hex(), **_SCRYPT}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(tmp, self._path)

    def check(self, password: str) -> bool:
        try:
            record = json.loads(self._path.read_text())
            digest = hashlib.scrypt(
                password.encode(),
                salt=bytes.fromhex(record["salt"]),
                dklen=32,
                n=record["n"],
                r=record["r"],
                p=record["p"],
            )
            return hmac.compare_digest(digest.hex(), record["hash"])
        except (OSError, ValueError, KeyError):
            return False


@dataclass
class Session:
    expires: float
    csrf: str


@dataclass
class Sessions:
    """Server-side sessions: the cookie holds only a random token."""

    clock: Callable[[], float] = time.time
    _live: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self) -> tuple[str, Session]:
        with self._lock:
            return self._create()

    def _create(self) -> tuple[str, Session]:
        now = self.clock()
        self._live = {t: s for t, s in self._live.items() if s.expires > now}
        while len(self._live) >= MAX_SESSIONS:
            self._live.pop(min(self._live, key=lambda t: self._live[t].expires))
        token = secrets.token_urlsafe(32)
        session = Session(now + SESSION_SECONDS, secrets.token_urlsafe(32))
        self._live[token] = session
        return token, session

    def get(self, token: str | None) -> Session | None:
        with self._lock:
            session = self._live.get(token or "")
            if session is None or session.expires <= self.clock():
                self._live.pop(token or "", None)
                return None
            return session

    def end(self, token: str | None) -> None:
        with self._lock:
            self._live.pop(token or "", None)


@dataclass
class SignInLimiter:
    clock: Callable[[], float] = time.time
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, address: str) -> list[float]:
        cutoff = self.clock() - FAILURE_WINDOW_S
        recent = [t for t in self._failures.get(address, []) if t > cutoff]
        self._failures[address] = recent
        return recent

    def allowed(self, address: str) -> bool:
        with self._lock:
            return len(self._recent(address)) < MAX_FAILURES

    def failed(self, address: str) -> None:
        with self._lock:
            self._recent(address).append(self.clock())

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


def private_address(address: str) -> bool:
    """Loopback, link-local or a private range: the home network, not the internet."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def local_host(host: str | None) -> bool:
    """A Host header naming this machine on the home network: a private IP, or a
    name with no dots or ending .local, .lan or .home.arpa. Anything else is how
    a DNS-rebinding page would reach us."""
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("["):  # [IPv6]:port
        name = name[1 : name.find("]")] if "]" in name else ""
    elif name.count(":") == 1:
        name = name.split(":")[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        pass
    else:
        return private_address(name)
    if "." not in name:
        return name.replace("-", "").isalnum()
    return name.endswith((".local", ".lan", ".home.arpa")) and all(
        part and part.replace("-", "").isalnum() for part in name.split(".")
    )


class SettingsPage(Protocol):
    """The form: drawn for a signed-in visitor, and saved from their fields."""

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str: ...

    def save(self, fields: dict[str, str]) -> dict[str, str]: ...  # field -> error; empty when saved


def make_handler(page: SettingsPage, password: Password, sessions: Sessions, limiter: SignInLimiter,
                 shell: Callable[[str, str], str], stylesheet: bytes):
    """`shell(title, body)` wraps page content in the HTML document."""

    class Handler(BaseHTTPRequestHandler):
        def version_string(self) -> str:
            return "departureboard"  # no Python or OS version for anyone probing

        def _send(self, status: int, body: bytes = b"", content_type: str = "text/html; charset=utf-8",
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in {**HEADERS, **(extra or {})}.items():
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
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:  # noqa: BLE001 - a malformed cookie is no cookie
                return None
            morsel = cookie.get("session")
            return morsel.value if morsel else None

        def _refuse_outsiders(self) -> bool:
            if not private_address(self.client_address[0]) or not local_host(self.headers.get("Host")):
                self._send(403, b"Forbidden", "text/plain; charset=utf-8")
                return True
            return False

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            if not origin:
                return False
            parsed = urlparse(origin)
            return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host")

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
            if not self._same_origin():
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
                session = sessions.get(self._token())
                if session and hmac.compare_digest(fields.get("csrf", ""), session.csrf):
                    sessions.end(self._token())
                self._redirect("/login", {"Set-Cookie": "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            elif path == "/":
                self._save(fields)
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")

        def _sign_in(self, fields: dict[str, str]) -> None:
            address = self.client_address[0]
            if not password.is_set():
                self._login_page(403)
                return
            if not limiter.allowed(address):
                self._login_page(429, "Too many attempts. Try again in 15 minutes.")
                return
            if not password.check(fields.get("password", "")):
                limiter.failed(address)
                self._login_page(401, "That password isn't right.")
                return
            limiter.succeeded(address)
            token, _ = sessions.create()
            cookie = f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
            self._redirect("/", {"Set-Cookie": cookie})

        def _save(self, fields: dict[str, str]) -> None:
            session = sessions.get(self._token())
            if session is None:
                self._redirect("/login")
                return
            if not hmac.compare_digest(fields.get("csrf", ""), session.csrf):
                self._send(403, b"Forbidden: form expired, reload the page", "text/plain; charset=utf-8")
                return
            fields.pop("csrf", None)
            errors = page.save(fields)
            if errors:
                body = page.render(session.csrf, errors=errors, values=fields)
                self._page(422, "Board settings: fix the errors", body)
            else:
                self._page(200, "Board settings", page.render(session.csrf, message="Saved. The board picks this up within a minute."))

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
