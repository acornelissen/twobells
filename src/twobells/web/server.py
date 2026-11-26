"""The one web server on every install, in two listeners on two ports, so
each is an origin of its own: the dashboard's page, its static files and
the read-only API in docs/dashboard-api.md on one (DEFAULT_PORT), and the
settings site on the other (SETTINGS_PORT). What a request gets is the
role of the socket that accepted it (Servers, make_servers), never what
its Host says, and the links between the two are made from the name it
used and the other's port (auth.origin_for). So a script on the
dashboard's page, were one ever to get past its CSP, can't read a
settings page or its form token.

Home network only. Every request must come from a private address and name
a local host (or one in allowed_hosts), in one Host header, which blocks
DNS rebinding from outside websites; API calls from another site's page
are refused too. Connections are capped, in all and from any one
address, and so are event streams; a request's line, headers and body
must all arrive within REQUEST_DEADLINE_S, its line and headers come to
no more than MAX_HEADER_BYTES and its body to no more than
MAX_BODY_BYTES. So a misbehaving client, or one sending its request a
byte at a time, can't use up the threads, nor one sending huge headers
the memory. One past its deadline is dropped unanswered; one past its
bytes is answered 431 (its line and headers) or 413 (its body), with
nothing more of it used, and let go after at most LINGER_S. The caps are
for both listeners together (Limits).

On the settings' listener, everything under /settings goes to the
settings site (web/settingsite.py): it signs the owner in, takes POSTs
(same-origin, with a CSRF token, bodies capped) and has its own security
headers, with scripts only on its own pages that need them. A request
another site's page or the dashboard's sent is refused there, but a
browser opening a page. Its / goes to the dashboard while that is on;
nothing else is found.

On the dashboard's listener, everything under /settings is a fixed page
saying where the settings are now, which asks the settings site nothing.
The dashboard is the rest, while it is on (DashboardApp.dashboard_on): read
only, with no password: only GET and HEAD, only files from an allowlist read
into memory at start. Its two pages, / and /performance, are files from
it. While it is off (its view is off, the settings aren't set up or don't
load), / goes to /settings, /healthz is still answered, and its pages,
files and API are not found. Every response, errors included, carries the
security headers: the dashboard's while it is on, else the settings site's
with no script.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import html
import io
import json
import logging
import os
import re
import socket
import socketserver
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
from ..setup import api as setupapi
from ..version import about
from . import settingsite
from .auth import origin_for, outsider, private_address
from .formpages import MAX_FORM_BODY

if TYPE_CHECKING:
    from ..dashboard.performance import Performance
    from .settingsite import SettingsSite

log = logging.getLogger(__name__)

DEFAULT_PORT = 8090  # the dashboard's
SETTINGS_PORT = 8091  # the settings', by default the dashboard's plus one
# What a listener serves: the dashboard or the settings, never both (see Servers).
ROLES = ("dashboard", "settings")
MAX_CONNECTIONS = 48
# Open connections from one address. Above two browsers' six each (an event stream
# among them), as Docker Desktop and rootless Podman show a household as one address.
MAX_PER_CLIENT = 16
MAX_STREAMS = 12
# Event streams from one address: two devices' two tabs each, behind one address
# as above, so no one address holds them all. A tab refused one polls instead.
MAX_STREAMS_PER_CLIENT = 4
REQUEST_TIMEOUT_S = 10  # the longest one read waits
REQUEST_DEADLINE_S = 15  # the longest a request's line, headers and body take all told
# A request's line and headers, to the blank line ending them: a browser's are a few kB,
# the wizard's no more; nginx allows 32 kB.
MAX_HEADER_BYTES = 32 * 1024
# A request's body, after its headers: the largest a POST may have, whatever the headers took.
MAX_BODY_BYTES = max(MAX_FORM_BODY, setupapi.MAX_BODY)
MAX_REQUEST_BYTES = MAX_HEADER_BYTES + MAX_BODY_BYTES  # the most a request may send all told
# After refusing a request part unread, how long the server lets the answer go out before it
# closes, and the most of what the client still sends it reads and throws away meanwhile.
LINGER_S = 1.0
LINGER_BYTES = 64 * 1024
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
# Where each page's link to the settings says their port, for settingslink.js, filled in at start.
SETTINGS_PORT_SLOT = b"<!-- settings port -->"
PAGES = ("index.html", "performance.html")

NOT_BUILT = (
    b'<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><title>Departures</title></head>'
    b"<body><main><h1>Front end not built</h1><p>The dashboard's server is running, but its page "
    b"isn't installed (src/twobells/dashboard_static is missing).</p></main></body></html>"
)


def moved_page(address: str | None, port: int, hint: str = "") -> bytes:
    """The page at /settings on the dashboard's port: the settings are on
    `port` now, at `address` (made from the name the browser used, or None
    if it can't be), with the install's `hint` (trusted HTML, or nothing)
    on what to do if that doesn't open. No script, no form."""
    e = html.escape
    if address:
        where = f'<p>Open them at <a href="{e(address)}">{e(address)}</a>, and change any bookmark to it.</p>'
    else:
        where = (f"<p>Open this device's address with <code>:{port}/settings</code> on the end, "
                 "and change any bookmark.</p>")
    title = f"The settings are on port {port}"
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title></head><body><main><h1>{e(title)}</h1>"
        f"<p>They have moved from this port, which has the dashboard alone.</p>{where}"
        f"{f'<p>{hint}</p>' if hint else ''}</main></body></html>"
    ).encode()


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
    def load(cls, folder: Path | str | None, version: str | None = None,
             settings_port: int | None = None) -> StaticFiles:
        """Every file under `folder` with a known extension, skipping hidden
        files, symlinks, odd names and anything too big. With `version`,
        each page's VERSION_SLOT says it, escaped, in a line of its own; with
        `settings_port`, its SETTINGS_PORT_SLOT is that number."""
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
                if relative in PAGES and settings_port is not None:
                    body = body.replace(SETTINGS_PORT_SLOT, b"%d" % settings_port)
                found[relative] = StaticFile(body, content_type, f'"{hashlib.sha256(body).hexdigest()}"')
        return cls(found, True)

    def get(self, relative: str) -> StaticFile | None:
        return self.files.get(relative)


def default_static_dir() -> Path:
    return Path(str(files("twobells") / "dashboard_static"))


# (mode, route, scheduled, day_type) -> History.recent of that day type, over the
# days [dashboard] history_days keeps, bank holidays counted as the dashboard counts them.
Recent = Callable[[str, str, str, str], list]


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
    allowed_hosts: frozenset[str] = frozenset()
    # What the dashboard's /settings page says to do if the settings' port doesn't open (Install.settings_moved).
    moved_hint: str = ""
    client_allowed: Callable[[str], bool] = private_address
    heartbeat: float = HEARTBEAT_S
    stream_seconds: float = STREAM_SECONDS


def _matches(header: str | None, etag: str) -> bool:
    """Whether an If-None-Match header names `etag` (weakly, as the spec says)."""
    if not header:
        return False
    tags = [tag.strip() for tag in header.split(",")]
    return "*" in tags or etag in tags or f"W/{etag}" in tags


class _TooBig(TimeoutError):
    """A request past its byte budget. A TimeoutError, as one past its
    deadline is, so http.server stops handling it; the handler then answers
    it (431 or 413) and closes the connection."""


class _Reads(io.RawIOBase):
    """A connection's reads, none waiting past its request's deadline, so a
    client sending its request a byte at a time lets go of its thread then,
    and none taking more than the request's byte budget off the wire, so
    neither http.server nor the buffer in front of this holds more of it.
    Only reads: an event stream, which only writes, runs on. tell() is the
    bytes taken so far, so the buffer's own tell() says how many of them it
    has handed on."""

    def __init__(self, connection: socket.socket, handler: _Handler, timeout: float) -> None:
        super().__init__()
        self._connection = connection
        self._handler = handler
        self._timeout = timeout
        self._taken = 0

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._taken

    def readinto(self, buffer) -> int:
        left = self._handler.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request not in by its deadline")
        room = self._handler.unread
        if room <= 0:
            self._handler.too_big = True
            raise _TooBig("request past its byte budget")
        self._connection.settimeout(min(self._timeout, left))
        try:
            with memoryview(buffer) as view:
                got = self._connection.recv_into(view[:room])
        finally:
            self._connection.settimeout(self._timeout)  # writes keep the usual timeout
        self._handler.unread -= got
        self._taken += got
        return got


class _Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S  # a client that stalls lets go of its thread
    server: DashboardServer
    deadline = 0.0  # when this request must be in by, on time.monotonic()
    unread = 0  # the bytes this request may still send: of its line and headers, then of its body
    too_big = False  # whether it tried to send more
    headers_in = False  # whether its line and headers were all read
    answered = False  # whether an answer to it was begun
    _body_start: int | None = None  # where its body starts in rfile, once its headers are parsed
    raw_requestline: bytes  # http.server's, which its stubs leave out
    _security: dict[str, str] | None = None  # this request's security headers, once its path is known
    _reads: _Reads  # the connection's reads, behind rfile's buffer

    def setup(self) -> None:
        super().setup()
        self.rfile.close()  # http.server's own reader, in place of ours
        self._reads = _Reads(self.connection, self, REQUEST_TIMEOUT_S)
        self.rfile = io.BufferedReader(self._reads)

    def handle_one_request(self) -> None:
        # http.server reads the request line and headers here, and the
        # handler any body; past the deadline or the byte budget a read
        # raises TimeoutError, on which http.server stops, with nothing
        # sent. Past the deadline it closes the connection unanswered; past
        # the budget it is told why first. HTTP/1.0: one request a
        # connection, so what the buffer read ahead is this request's.
        # The line and headers have MAX_HEADER_BYTES; parse_request gives
        # the body its own. http.server's own limits answer through
        # send_error, with our headers; of them only its 100 headers can
        # come first, as its 64 KiB line is past our 32 KiB.
        self.deadline = time.monotonic() + self.server.request_seconds
        self.unread = MAX_HEADER_BYTES
        self.too_big = self.headers_in = self.answered = False
        self._body_start = None
        self.command = self.requestline = ""  # until the request line is read, as the answer looks at them
        super().handle_one_request()
        if self.too_big:
            self._too_big(413 if self.headers_in else 431)
        elif self.answered and self._unfinished():
            self._linger()

    def parse_request(self) -> bool:
        if len(str(self.raw_requestline, "iso-8859-1").split()) == 2:
            # HTTP/0.9, as http.server splits it: no headers, and an answer
            # with no status line or headers. Refused, as a broken line is.
            self.send_error(HTTPStatus.BAD_REQUEST)
            parsed = False
        else:
            parsed = super().parse_request()  # raises _TooBig, as any read, past the budget
        self.headers_in = True
        if parsed:
            self._body_start = self.rfile.tell()
            # The body's budget: MAX_BODY_BYTES, less what the buffer read
            # ahead of the headers' end, which is the body's start. So that
            # read-ahead counts once, against the body, and the whole
            # request stays within MAX_REQUEST_BYTES.
            ahead = self._reads.tell() - self.rfile.tell()
            self.unread = MAX_BODY_BYTES - ahead
        return parsed

    def _too_big(self, status: int) -> None:
        """The answer to a request past its byte budget, its headers (431)
        or its body (413): a fixed phrase, nothing of the request, and the
        connection closed after _linger. Nothing more of it is used; a
        browser gone by now is no fault."""
        self.close_connection = True
        self.request_version = "HTTP/1.0"  # as send_error: the request's own may not have been read
        try:
            self._send(status, HTTPStatus(status).phrase.encode(), extra={"Connection": "close"},
                       security=self._security_for(None))
        except OSError:  # the browser went away, or stopped reading
            return
        self._linger()

    def _unfinished(self) -> bool:
        """Whether part of the request may still be unread or unsent: its
        line or headers not all parsed, a body it declared not all read
        (or one it gave no plain length for), bytes past it already in."""
        if self._body_start is None:
            return True
        lengths = self.headers.get_all("Content-Length") or ["0"]
        length = lengths[0].strip()
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1 or not (
                length.isascii() and length.isdigit() and len(length) <= 9):
            return True
        if self.rfile.tell() - self._body_start < int(length) or self._reads.tell() > self.rfile.tell():
            return True
        self.connection.settimeout(0)
        try:
            return bool(self.connection.recv(1, socket.MSG_PEEK))  # more already on the wire
        except OSError:  # nothing more yet, or the client is gone
            return False
        finally:
            self.connection.settimeout(self.timeout)

    def _linger(self) -> None:
        """After an answer that leaves part of the request unsent or unread:
        end our side (the answer, then a FIN), and throw away what the
        client still sends, up to LINGER_BYTES, until it stops or LINGER_S
        have passed, never past the request's deadline. Closing with its
        bytes unread would reset the connection, which can lose the answer
        on the way (a proxy in front, say); this gives it that long to
        arrive. A client still sending then is reset, as before."""
        until = min(time.monotonic() + LINGER_S, self.deadline)
        left = LINGER_BYTES
        scrap = bytearray(16 * 1024)
        with contextlib.suppress(OSError):  # gone, reset, or still sending at `until`
            self.connection.shutdown(socket.SHUT_WR)
            while (wait := until - time.monotonic()) > 0:
                if left <= 0:  # read no more of it, but give the answer its time
                    time.sleep(wait)
                    return
                self.connection.settimeout(wait)
                got = self.connection.recv_into(scrap, min(left, len(scrap)))
                if not got:  # the client has stopped: nothing to reset
                    return
                left -= got

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
        """The security headers for `path` (None: not known). On the
        settings' listener, the settings site's, so scripts run only on its
        pages that have them. On the dashboard's, the dashboard's while it
        is on; else, and on the page saying where the settings are, the
        settings site's with no script."""
        owned = path is not None and settingsite.owns(path)
        if self.server.role == "settings":
            return settingsite.headers_for(path) if path is not None and owned else settingsite.SETTINGS_HEADERS
        return SECURITY_HEADERS if not owned and self._on() else settingsite.SETTINGS_HEADERS

    def _head(self, status: int, content_type: str | None, length: int | None, cache: str = "no-store",
              extra: dict[str, str] | None = None, security: dict[str, str] | None = None) -> None:
        if security is None:
            security = self._security if self._security is not None else self._security_for(None)
        self.answered = True
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
        return outsider(self.client_address[0], self.headers.get_all("Host") or [], self.app.allowed_hosts,
                        self.app.client_allowed)

    def _fetch_header(self, name: str) -> str | None:
        """A Fetch Metadata header's value, lower-cased; None unless it was sent once."""
        values = self.headers.get_all(name) or []
        return values[0].strip().lower() if len(values) == 1 else None

    def _from_elsewhere(self) -> bool:
        """Whether a request to the settings' listener came from another
        site's page or the dashboard's (Sec-Fetch-Site other than
        same-origin or none, or sent twice), unless it is a GET opening a
        page in the browser's window, as following a link does. One without
        Sec-Fetch-Site, from an older browser or curl, is let in: the
        settings site's own checks still apply to it."""
        if self.headers.get_all("Sec-Fetch-Site") is None:
            return False
        site = self._fetch_header("Sec-Fetch-Site")
        if site in ("same-origin", "none"):
            return False
        opening = (site in ("same-site", "cross-site") and self.command == "GET"
                   and self._fetch_header("Sec-Fetch-Mode") == "navigate"
                   and self._fetch_header("Sec-Fetch-Dest") == "document")
        return not opening

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        role = self.server.role
        self._security = self._security_for(path)
        if self._outsider():
            self._error(403)
            return
        if role == "settings" and self._from_elsewhere():
            self._error(403)
            return
        if settingsite.owns(path):
            if role == "dashboard":
                self._moved()
            else:
                self._settings(path)
            return
        if self.command not in ("GET", "HEAD"):
            self._error(405, {"Allow": "GET, HEAD"})
            return
        raw = self.path
        if len(raw) > MAX_PATH or not raw.startswith("/") or any(bad in raw.lower() for bad in _BAD_PATH):
            self._error(404)
            return
        if role == "settings":
            self._settings_home(path)
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
                days = self.app.recent(found["mode"], found["route"], found["scheduled"], found["day_type"])
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

    def _settings_home(self, path: str) -> None:
        """A GET or HEAD outside /settings on the settings' listener, which
        has none of the dashboard's pages, files, API or health check: its
        / goes to the dashboard while that is on, at the name the browser
        used on the dashboard's port, else to the settings; nothing else is found."""
        if path != "/":
            self._error(404)
            return
        port = self.server.dashboard_port
        dashboard = origin_for(self.headers.get("Host"), port) if port is not None and self._on() else None
        self._send(303, b"", extra={"Location": f"{dashboard}/" if dashboard else settingsite.PREFIX})

    def _moved(self) -> None:
        """Every path under /settings on the dashboard's listener: for a GET
        or HEAD, a fixed page saying where the settings are now; any other
        method is refused with its body unread (handle_one_request then
        lingers, as after any answer that leaves part of a request unread).
        The settings site is never asked, so nothing here reads a cookie,
        sets one or shows a form token."""
        if self.command not in ("GET", "HEAD"):
            self._error(405, {"Allow": "GET, HEAD", "Connection": "close"})
            return
        port = self.server.settings_port or SETTINGS_PORT
        address = origin_for(self.headers.get("Host"), port)
        page = moved_page(f"{address}{settingsite.PREFIX}" if address else None, port, self.app.moved_hint)
        self._send(200, page, CONTENT_TYPES[".html"])

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
        client = self.client_address[0]
        if not self.server.limits.take_stream(client):
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
            self.server.limits.let_go_stream(client)

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


class Limits:
    """The caps on connections and event streams, for every listener of one
    process together, so two sockets hold no more than one did: at most
    `max_connections` connections at once and `max_per_client` from one
    address, and `max_streams` event streams, `max_streams_per_client` from
    one address."""

    def __init__(self, max_connections: int = MAX_CONNECTIONS, max_per_client: int = MAX_PER_CLIENT,
                 max_streams: int = MAX_STREAMS, max_streams_per_client: int = MAX_STREAMS_PER_CLIENT) -> None:
        self.max_connections = max_connections
        self.max_per_client = max_per_client
        self.max_streams = max_streams
        self.max_streams_per_client = max_streams_per_client
        self._lock = threading.Lock()
        self._open = 0
        self._open_from: dict[str, int] = {}  # each address's open connections; none for one with none
        self._streams = 0
        self._streams_from: dict[str, int] = {}  # each address's event streams; none for one with none

    def take_stream(self, client: str) -> bool:
        """An event stream's slot for `client`, if it and the server are under their caps."""
        with self._lock:
            held = self._streams_from.get(client, 0)
            if self._streams >= self.max_streams or held >= self.max_streams_per_client:
                return False
            self._streams += 1
            self._streams_from[client] = held + 1
            return True

    def let_go_stream(self, client: str) -> None:
        with self._lock:
            self._streams -= 1
            held = self._streams_from.pop(client) - 1
            if held:
                self._streams_from[client] = held

    def take(self, client: str) -> bool:
        """A connection slot for `client`, if it and the server are under their caps."""
        with self._lock:
            held = self._open_from.get(client, 0)
            if self._open >= self.max_connections or held >= self.max_per_client:
                return False
            self._open += 1
            self._open_from[client] = held + 1
            return True

    def let_go(self, client: str) -> None:
        with self._lock:
            self._open -= 1
            held = self._open_from.pop(client) - 1
            if held:
                self._open_from[client] = held


class DashboardServer(ThreadingHTTPServer):
    """One listener: a thread per connection, under `limits` (more are
    closed at once, unread and unanswered), each request in within
    `request_seconds`. Its `role` is what it serves, fixed by the socket
    that accepts a connection and never by what a request says:
    "dashboard" or "settings".
    `dashboard_port` and `settings_port` are where each is, for the links
    between them."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: DashboardApp, role: str, limits: Limits | None = None,
                 request_seconds: float = REQUEST_DEADLINE_S) -> None:
        if role not in ROLES:
            raise ValueError(f"Unknown role {role!r}")
        self.app = app
        self.role = role
        self.limits = Limits() if limits is None else limits
        self.request_seconds = request_seconds
        super().__init__(address, _Handler)
        # Set by make_servers; None for a listener alone, with no other to link to.
        self.dashboard_port: int | None = None
        self.settings_port: int | None = None

    def server_bind(self) -> None:
        """Binds as http.server's does, but without looking up this
        computer's name for server_name (socket.getfqdn), which nothing here
        uses: on a Pi that lookup could go to the router's DNS."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port

    def process_request(self, request, client_address) -> None:
        client = client_address[0]
        if not self.limits.take(client):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.limits.let_go(client)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.limits.let_go(client_address[0])

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, _GONE):
            return
        log.warning("Dashboard request failed: %s", type(error).__name__)


def make_server(app: DashboardApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT, attempts: int = 1,
                *, role: str, limits: Limits | None = None,
                request_seconds: float = REQUEST_DEADLINE_S) -> DashboardServer:
    """One listener with `role`, bound to `port`, or the next free one within `attempts` ports."""
    for candidate in range(port, port + attempts):
        try:
            return DashboardServer((host, candidate), app, role, limits, request_seconds)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
    span = str(port) if attempts == 1 else f"{port}-{port + attempts - 1}"
    raise OSError(errno.EADDRINUSE, f"Port {span} already in use")


class Servers:
    """The dashboard's listener and the settings', in one process, sharing
    one app and one set of limits. If the settings' listener stops by
    itself, it stops the dashboard's too and `failed` says so, so serve
    ends in failure and the service is started again whole, rather than
    run on with its settings' port bound but unanswered."""

    def __init__(self, dashboard: DashboardServer, settings: DashboardServer) -> None:
        self.dashboard = dashboard
        self.settings = settings
        self.failed = False
        self._stopping = threading.Event()  # set once stopping was asked for, or the caller's thread interrupted
        self._settings_done = threading.Event()  # set once the settings' loop has ended, however

    @property
    def ports(self) -> tuple[int, int]:
        """The dashboard's port and the settings'."""
        return self.dashboard.server_address[1], self.settings.server_address[1]

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Serve both until shutdown(), or until the caller's thread is
        interrupted: the settings on a thread of their own, the dashboard
        on the caller's. Either way the settings have stopped when this returns."""
        thread = threading.Thread(target=self._serve_settings, args=(poll_interval,),
                                  name="settings-server", daemon=True)
        thread.start()
        try:
            self.dashboard.serve_forever(poll_interval)
        finally:
            self._stopping.set()
            if not self._settings_done.is_set():  # else shutdown could wait for it forever
                self.settings.shutdown()
            thread.join()

    def _serve_settings(self, poll_interval: float) -> None:
        """The settings' listener's loop; if it ends without being asked to,
        the dashboard's is stopped too."""
        try:
            self.settings.serve_forever(poll_interval)
        except Exception as error:  # noqa: BLE001 - said, by its type, and made the end of both
            log.error("The settings' listener stopped: %s", type(error).__name__)
        finally:
            self._settings_done.set()  # before the dashboard's stops, which serve_forever then sees
        if not self._stopping.is_set():
            self.failed = True
            log.error("The settings' listener stopped by itself; stopping the dashboard's too")
            self.dashboard.shutdown()

    def shutdown(self) -> None:
        """Stop serve_forever from another thread, as one server's shutdown does."""
        self._stopping.set()
        self.dashboard.shutdown()
        self.settings.shutdown()

    def server_close(self) -> None:
        self.dashboard.server_close()
        self.settings.server_close()


def _listener(app: DashboardApp, host: str, port: int, role: str, limits: Limits,
              request_seconds: float) -> DashboardServer | None:
    """A listener on `port`, or None if the port is taken; OSError naming
    the port if it can't be had for another reason."""
    try:
        return DashboardServer((host, port), app, role, limits, request_seconds)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            return None
        raise OSError(error.errno, f"Port {port}: {error.strerror or error}") from error


def make_servers(app: DashboardApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 settings_port: int = SETTINGS_PORT, attempts: int = 1, *, limits: Limits | None = None,
                 request_seconds: float = REQUEST_DEADLINE_S) -> Servers:
    """The dashboard's listener on `port` and the settings' on
    `settings_port`, both bound before either serves, sharing `limits`; or,
    within `attempts`, the next pair both free, each port two on. OSError
    naming the port if one can't be had."""
    limits = Limits() if limits is None else limits
    busy = []
    for step in range(attempts):
        wanted = port + 2 * step, settings_port + 2 * step
        dashboard = _listener(app, host, wanted[0], "dashboard", limits, request_seconds)
        if dashboard is None:
            busy.append(wanted[0])
            continue
        try:
            settings = _listener(app, host, wanted[1], "settings", limits, request_seconds)
        except OSError:
            dashboard.server_close()
            raise
        if settings is None:
            dashboard.server_close()
            busy.append(wanted[1])
            continue
        servers = Servers(dashboard, settings)
        for server in (dashboard, settings):
            server.dashboard_port, server.settings_port = servers.ports
        return servers
    raise OSError(errno.EADDRINUSE, f"Port {', '.join(map(str, busy))} already in use")
