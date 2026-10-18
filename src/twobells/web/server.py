"""The one web server on every install: the settings site, and the dashboard's
page, its static files and the read-only API in docs/dashboard-api.md.

Home network only. Every request must come from a private address and name
a local host (or one in allowed_hosts), in one Host header, which blocks
DNS rebinding from outside websites; API calls from another site's page
are refused too. Connections are capped, in all and from any one
address, and so are event streams; a request's line, headers and body
must all arrive within REQUEST_DEADLINE_S. So a misbehaving client, or
one sending its request a byte at a time, can't use up the threads.

Everything under /settings goes
to the settings site (web/settingsite.py): it signs the owner in, takes POSTs
(same-origin, with a CSRF token, bodies capped) and has its own security
headers, with scripts only on the setup wizard's pages.

The dashboard is the rest, while it is on (DashboardApp.dashboard_on): read
only, with no password: only GET and HEAD, only files from an allowlist read
into memory at start. Its two pages, / and /performance, are files from
it. While it is off (its view is off, the settings aren't set up or don't
load), / goes to the settings, /healthz is still answered, and its pages,
files and API are not found. Every response, errors
included, carries the security headers: the dashboard's while it is on,
else the settings site's.
"""

from __future__ import annotations

import errno
import hashlib
import html
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from ..dashboard.snapshot import SnapshotSource
from ..version import about
from . import settingsite
from .auth import local_host, private_address

if TYPE_CHECKING:
    from ..dashboard.performance import Performance
    from .settingsite import SettingsSite

log = logging.getLogger(__name__)

DEFAULT_PORT = 8090
MAX_CONNECTIONS = 48
# Open connections from one address. Above two browsers' six each (an event stream
# among them), as Docker Desktop and rootless Podman show a household as one address.
MAX_PER_CLIENT = 16
MAX_STREAMS = 12
REQUEST_TIMEOUT_S = 10  # the longest one read waits
REQUEST_DEADLINE_S = 15  # the longest a request's line, headers and body take all told
MAX_PATH = 512
HEARTBEAT_S = 15.0
STREAM_SECONDS = 30 * 60
RETRY_MS = 5000
MAX_STATIC_BYTES = 5_000_000

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",  # frame-ancestors for browsers that don't read the CSP's
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

_TEXT = "text/plain; charset=utf-8"
_JSON = "application/json"
# Encoded dots, slashes and backslashes have no business in our paths.
_BAD_PATH = ("..", "%2e", "%2f", "%5c", "%00", "\\")
_ID = re.compile(r"[0-9a-f]{16}")
_STATIC_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
_EVENT_ID = re.compile(r"[0-9]{1,15}")
# A browser that went away or stalled: nothing to report. Other OSErrors are faults.
_GONE = (ConnectionError, TimeoutError)

# Where each page's footer says which version is running, filled in at start.
VERSION_SLOT = b"<!-- version -->"
PAGES = ("index.html", "performance.html")

NOT_BUILT = (
    b'<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><title>Departures</title></head>'
    b"<body><main><h1>Front end not built</h1><p>The dashboard's server is running, but its page "
    b"isn't installed (src/twobells/dashboard_static is missing).</p></main></body></html>"
)


@dataclass(frozen=True)
class StaticFile:
    body: bytes
    content_type: str
    etag: str


@dataclass(frozen=True)
class StaticFiles:
    """The page's files, read once at start. Only these are ever served."""

    files: dict[str, StaticFile] = field(default_factory=dict)
    present: bool = False  # False when the folder is missing: the front end isn't built

    @classmethod
    def load(cls, folder: Path | str | None, version: str | None = None) -> StaticFiles:
        """Every file under `folder` with a known extension, skipping hidden
        files, symlinks, odd names and anything too big. With `version`,
        each page's VERSION_SLOT says it, escaped, in a line of its own."""
        if folder is None or not Path(folder).is_dir():
            return cls({}, False)
        root = Path(folder)
        found = {}
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = sorted(d for d in subdirectories if not d.startswith("."))
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                content_type = CONTENT_TYPES.get(path.suffix.lower())
                if content_type is None or not _STATIC_NAME.fullmatch(relative) or path.is_symlink():
                    continue
                if path.stat().st_size > MAX_STATIC_BYTES:
                    log.warning("Not serving %s: larger than %d bytes", relative, MAX_STATIC_BYTES)
                    continue
                body = path.read_bytes()
                if relative in PAGES and version is not None:
                    line = f'<p class="version">{html.escape(about(version))}</p>'
                    body = body.replace(VERSION_SLOT, line.encode("utf-8"))
                found[relative] = StaticFile(body, content_type, f'"{hashlib.sha256(body).hexdigest()}"')
        return cls(found, True)

    def get(self, relative: str) -> StaticFile | None:
        return self.files.get(relative)


def default_static_dir() -> Path:
    return Path(str(files("twobells") / "dashboard_static"))


Recent = Callable[[str, str, str, int], list]  # (mode, route, scheduled, days) -> History.recent


@dataclass
class DashboardApp:
    """What the handlers serve, and the limits they keep to. Without a hub
    the dashboard isn't running; with one, it is on while dashboard_on().
    The hub is the pollers' own (snapshot.Hub) or, on a Pi, a mirror of the
    fetch service's (handoff.SnapshotMirror)."""

    hub: SnapshotSource | None
    static: StaticFiles
    healthy: Callable[[], bool]
    settings: SettingsSite
    dashboard_on: Callable[[], bool] = lambda: True
    recent: Recent | None = None  # History.recent, or None without a history
    performance: Performance | None = None  # the Performance page's figures, or None without a history
    history_days: int = 90
    allowed_hosts: frozenset[str] = frozenset()
    client_allowed: Callable[[str], bool] = private_address
    heartbeat: float = HEARTBEAT_S
    stream_seconds: float = STREAM_SECONDS


def _host_name(host: str) -> str:
    """The name in a Host header, without its port; "" for an IPv6 literal,
    which allowed_hosts never holds."""
    name = host.strip().lower()
    if name.startswith("["):
        return ""
    return name.rsplit(":", 1)[0] if name.count(":") == 1 else name


def _matches(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match header names `etag` (weakly, as the spec says)."""
    if not header:
        return False
    tags = [tag.strip() for tag in header.split(",")]
    return "*" in tags or etag in tags or f"W/{etag}" in tags


class _Reads(io.RawIOBase):
    """A connection's reads, none waiting past its request's deadline, so a
    client sending its request a byte at a time lets go of its thread then.
    Only reads: an event stream, which only writes, runs on."""

    def __init__(self, connection: socket.socket, handler: _Handler, timeout: float) -> None:
        super().__init__()
        self._connection = connection
        self._handler = handler
        self._timeout = timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        left = self._handler.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request not in by its deadline")
        self._connection.settimeout(min(self._timeout, left))
        try:
            return self._connection.recv_into(buffer)
        finally:
            self._connection.settimeout(self._timeout)  # writes keep the usual timeout


class _Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S  # a client that stalls lets go of its thread
    server: DashboardServer
    deadline = 0.0  # when this request must be in by, on time.monotonic()
    _security: dict[str, str] | None = None  # this request's security headers, once its path is known

    def setup(self) -> None:
        super().setup()
        self.rfile.close()  # http.server's own reader, in place of ours
        self.rfile = io.BufferedReader(_Reads(self.connection, self, REQUEST_TIMEOUT_S))

    def handle_one_request(self) -> None:
        # http.server reads the request line and headers here, and the
        # handler any body; past the deadline a read raises TimeoutError,
        # on which http.server closes the connection unanswered.
        self.deadline = time.monotonic() + self.server.request_seconds
        super().handle_one_request()

    def version_string(self) -> str:
        return "twobells"  # no Python or OS version for anyone probing

    def log_message(self, format, *args) -> None:  # noqa: A002 - no request lines in the logs
        pass

    @property
    def app(self) -> DashboardApp:
        return self.server.app

    @property
    def hub(self) -> SnapshotSource:
        """The running dashboard's hub; the dashboard's routes are only taken once _on() has found one."""
        if self.app.hub is None:
            raise RuntimeError("The dashboard isn't running")
        return self.app.hub

    # Responses

    def _on(self) -> bool:
        """Whether the dashboard is on: running, and its view on now. Off if that can't be told."""
        if self.app.hub is None:
            return False
        try:
            return bool(self.app.dashboard_on())
        except Exception as error:  # noqa: BLE001 - the settings still answer
            log.warning("Can't tell whether the dashboard is on: %s", type(error).__name__)
            return False

    def _security_for(self, path: str | None) -> dict[str, str]:
        """The security headers for `path` (None: not known): the settings
        site's for its own paths and while the dashboard is off, so scripts
        run only on the wizard's pages then; else the dashboard's."""
        if path is not None and settingsite.owns(path):
            return settingsite.headers_for(path)
        return SECURITY_HEADERS if self._on() else settingsite.SETTINGS_HEADERS

    def _head(self, status: int, content_type: str | None, length: int | None, cache: str = "no-store",
              extra: dict[str, str] | None = None, security: dict[str, str] | None = None) -> None:
        if security is None:
            security = self._security if self._security is not None else self._security_for(None)
        self.send_response(status)
        for name, value in security.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", cache)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _send(self, status: int, body: bytes, content_type: str = _TEXT, cache: str = "no-store",
              extra: dict[str, str] | None = None, security: dict[str, str] | None = None) -> None:
        self._head(status, content_type, len(body), cache, extra, security)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value) -> None:
        self._send(200, json.dumps(value, separators=(",", ":"), allow_nan=False).encode("ascii"), _JSON)

    def _error(self, status: int, extra: dict[str, str] | None = None) -> None:
        self._send(status, HTTPStatus(status).phrase.encode(), extra=extra)

    def _not_modified(self, etag: str, cache: str) -> None:
        self._head(304, None, None, cache, {"ETag": etag})

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """http.server's own errors (a bad request line, say), with our headers and no HTML."""
        self.close_connection = True
        # A request line too broken to read leaves http.server on HTTP/0.9, which
        # would send no status line or headers (on 3.12; 3.13 sends them). Answer
        # as HTTP/1.0 on every Python, so our security headers always go.
        self.request_version = "HTTP/1.0"
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "Error"
        # The request's path, if it got that far, may be a previous request's.
        self._send(code, phrase.encode(), security=self._security_for(None))

    # Requests

    def __getattr__(self, name: str):
        # http.server calls do_<METHOD>; every method but GET and HEAD lands here.
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def _outsider(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or not self.app.client_allowed(self.client_address[0]):
            return True
        return not (local_host(hosts[0]) or _host_name(hosts[0]) in self.app.allowed_hosts)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        self._security = self._security_for(path)
        if self._outsider():
            self._error(403)
            return
        if settingsite.owns(path):
            self._settings(path)
            return
        if self.command not in ("GET", "HEAD"):
            self._error(405, {"Allow": "GET, HEAD"})
            return
        raw = self.path
        if len(raw) > MAX_PATH or not raw.startswith("/") or any(bad in raw.lower() for bad in _BAD_PATH):
            self._error(404)
            return
        if not self._on():
            self._off(path)
            return
        if path.startswith("/api/"):
            if self.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
                self._error(403)
                return
            self.hub.touch()
        self._route(path)

    def _route(self, path: str) -> None:
        if path == "/":
            self._static("index.html")
        elif path == "/performance":
            self._static("performance.html")
        elif path == "/manifest.webmanifest":
            self._static("manifest.webmanifest")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/api/snapshot":
            self._snapshot()
        elif path == "/api/events":
            self._events()
        elif path.startswith("/api/bus/"):
            self._bus(path[len("/api/bus/"):])
        elif path.startswith("/api/history/"):
            self._history(path[len("/api/history/"):])
        elif path == "/api/area":
            self._area()
        elif path == "/api/performance":
            self._performance()
        elif path == "/healthz":
            if self.app.healthy():
                self._send(200, b"ok")
            else:
                self._send(503, b"unavailable")
        else:
            self._error(404)

    def _off(self, path: str) -> None:
        """While the dashboard is off: its page goes to the settings, and the
        container's health check still hears that the server is up."""
        if path == "/":
            self._send(303, b"", extra={"Location": settingsite.PREFIX})
        elif path == "/healthz":
            self._send(200, b"ok")
        else:
            self._error(404)

    def _static(self, relative: str) -> None:
        found = self.app.static.get(relative)
        if found is None:
            if relative == "index.html" and not self.app.static.present:
                self._send(200, NOT_BUILT, CONTENT_TYPES[".html"])
            else:
                self._error(404)
            return
        if _matches(self.headers.get("If-None-Match"), found.etag):
            self._not_modified(found.etag, "no-cache")
            return
        self._send(200, found.body, found.content_type, "no-cache", {"ETag": found.etag})

    def _snapshot(self) -> None:
        version, body = self.hub.current()
        etag = f'"v{version}"'
        if _matches(self.headers.get("If-None-Match"), etag):
            self._not_modified(etag, "no-cache")
            return
        self._send(200, body, _JSON, "no-cache", {"ETag": etag})

    def _lookup(self, row_id: str, kind: str) -> dict | None:
        if not _ID.fullmatch(row_id):
            return None
        entry = self.hub.lookup(row_id)
        return entry.get(kind) if entry else None

    def _bus(self, row_id: str) -> None:
        found = self._lookup(row_id, "bus")
        if found is None:
            self._error(404)
        else:
            self._json(found)

    def _area(self) -> None:
        """The area map: live buses around our stops, from the last bus poll."""
        found = self.hub.get("area")
        if found is None:
            self._error(404)
        else:
            self._json(found)

    def _history(self, row_id: str) -> None:
        found = self._lookup(row_id, "history")
        if found is None:
            self._error(404)
            return
        days, figures = [], None
        try:
            if self.app.recent is not None:
                days = self.app.recent(found["mode"], found["route"], found["scheduled"], self.app.history_days)
            if self.app.performance is not None:
                figures = self.app.performance.route(found["mode"], found["route"])
        except Exception as error:  # noqa: BLE001 - one unreadable history isn't the server's end
            log.warning("Dashboard history unreadable: %s", type(error).__name__)
            self._error(503)
            return
        self._json({
            "id": row_id,
            "mode": found["mode"],
            "label": found["label"],
            "day_type": found["day_type"],
            "phrase": found["phrase"],
            "days": days,
            "route": figures,
        })

    def _performance(self) -> None:
        """The Performance page's figures: 404 without a history to read them from."""
        if self.app.performance is None:
            self._error(404)
            return
        try:
            report = self.app.performance.report()
        except Exception as error:  # noqa: BLE001 - one unreadable history isn't the server's end
            log.warning("Dashboard history unreadable: %s", type(error).__name__)
            self._error(503)
            return
        self._json(report)

    # The settings site

    def _settings(self, path: str) -> None:
        raw = self.path
        if len(raw) > MAX_PATH or any(bad in raw.lower() for bad in _BAD_PATH):
            self._send(404, b"Not Found")
            return
        query = raw.split("#", 1)[0].partition("?")[2]
        reply = self.app.settings.handle(self.command, path, query, self.headers, self.client_address[0], self.rfile)
        self._send(reply.status, reply.body, reply.content_type, extra=reply.headers)

    # Server-sent events

    def _last_event_id(self) -> int | None:
        value = (self.headers.get("Last-Event-ID") or "").strip()
        return int(value) if _EVENT_ID.fullmatch(value) else None

    def _event(self, version: int, body: bytes) -> None:
        self.wfile.write(b"id: %d\nevent: snapshot\ndata: " % version + body + b"\n\n")

    def _events(self) -> None:
        if not self.server.streams.acquire(blocking=False):
            self._error(503, {"Retry-After": "30"})
            return
        try:
            self._head(200, "text/event-stream; charset=utf-8", None, "no-store", {"X-Accel-Buffering": "no"})
            if self.command == "HEAD":
                return
            self.close_connection = True
            self._stream()
        except _GONE:
            pass  # the browser went away, or stalled past the socket timeout
        finally:
            self.server.streams.release()

    def _stream(self) -> None:
        hub, app = self.hub, self.app
        deadline = time.monotonic() + app.stream_seconds
        self.wfile.write(b"retry: %d\n\n" % RETRY_MS)
        sent, body = hub.current()
        if self._last_event_id() != sent:
            self._event(sent, body)
        while not hub.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return  # the browser reconnects
            version = hub.wait(sent, min(app.heartbeat, remaining))
            hub.touch()  # an open page counts as a browser using the dashboard
            if hub.closed:
                return
            if version != sent:
                sent, body = hub.current()
                self._event(sent, body)
            elif time.monotonic() < deadline:
                self.wfile.write(b": ping\n\n")


class DashboardServer(ThreadingHTTPServer):
    """A thread per connection, at most `max_connections` at once and
    `max_per_client` from one address (more are closed at once, unread and
    unanswered), and `max_streams` event streams. Each request must be in
    within `request_seconds`."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: DashboardApp, max_connections: int = MAX_CONNECTIONS,
                 max_per_client: int = MAX_PER_CLIENT, max_streams: int = MAX_STREAMS,
                 request_seconds: float = REQUEST_DEADLINE_S) -> None:
        self.app = app
        self.max_connections = max_connections
        self.max_per_client = max_per_client
        self.request_seconds = request_seconds
        self.streams = threading.BoundedSemaphore(max_streams)
        self._lock = threading.Lock()
        self._open = 0
        self._open_from: dict[str, int] = {}  # each address's open connections; none for one with none
        super().__init__(address, _Handler)

    def _take(self, client: str) -> bool:
        """A connection slot for `client`, if it and the server are under their caps."""
        with self._lock:
            held = self._open_from.get(client, 0)
            if self._open >= self.max_connections or held >= self.max_per_client:
                return False
            self._open += 1
            self._open_from[client] = held + 1
            return True

    def _let_go(self, client: str) -> None:
        with self._lock:
            self._open -= 1
            held = self._open_from.pop(client) - 1
            if held:
                self._open_from[client] = held

    def process_request(self, request, client_address) -> None:
        client = client_address[0]
        if not self._take(client):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._let_go(client)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._let_go(client_address[0])

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, _GONE):
            return
        log.warning("Dashboard request failed: %s", type(error).__name__)


def make_server(app: DashboardApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT, attempts: int = 1,
                **limits) -> DashboardServer:
    """Bind to `port`, or the next free one within `attempts` ports."""
    for candidate in range(port, port + attempts):
        try:
            return DashboardServer((host, candidate), app, **limits)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    span = str(port) if attempts == 1 else f"{port}-{port + attempts - 1}"
    raise OSError(errno.EADDRINUSE, f"Port {span} already in use")
