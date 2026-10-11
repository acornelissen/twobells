"""Thin HTTPS adapter. Kept dependency-free for the Pi Zero."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator, Mapping
from functools import partial
from http.client import HTTPConnection, HTTPSConnection
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (
    AbstractHTTPHandler,
    HTTPDefaultErrorHandler,
    HTTPErrorProcessor,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    Request,
    UnknownHandler,
)

# Some gateways (the Rail Data Marketplace among them) refuse urllib's default
# "Python-urllib/x.y" agent with a 403, so always identify ourselves.
USER_AGENT = "inky-departures/0.1 (e-paper departure board)"

# Headers that may follow a redirect to another host; anything else (an API
# key header such as Darwin's x-apikey) stays with the host it was meant for.
_PORTABLE_HEADERS = {"user-agent", "accept"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or ""), parts.port or _DEFAULT_PORTS.get(parts.scheme)


class SafeRedirects(HTTPRedirectHandler):
    """urllib copies every request header to wherever a redirect points. This
    refuses https to http, and drops all but harmless headers on a change of
    host or port, so a key header never reaches a host it wasn't sent to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = _origin(req.full_url), _origin(newurl)
        if old[0] == "https" and new[0] != "https":
            raise HTTPError(req.full_url, code, f"Refusing a redirect from https to {new[0]}", headers, fp)
        followed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if followed is not None and new != old:
            for name in list(followed.headers):
                if name.lower() not in _PORTABLE_HEADERS:
                    del followed.headers[name]
        deadline = getattr(req, "deadline", None)
        if followed is not None and deadline is not None:
            # The redirect shares the request's deadline. urllib makes the new
            # Request, so it can't be a _TimedRequest that declares one.
            followed.deadline = deadline  # pyright: ignore[reportAttributeAccessIssue]
        return followed


# A deadline for a whole request. urllib's timeout is for each step (each
# read, each write), so a server sending a byte at a time can keep a request
# open for ever. A request given a `deadline` (a time.monotonic time, see
# get_bytes) is connected through the classes below, which give each step
# only what is left of it. Looking up the server's name isn't covered: the
# standard library can't time that.


class _TimedRequest(Request):
    """A Request with the time.monotonic time its whole request must be done by, if any."""

    deadline: float | None = None


def _time_left(deadline: float, timeout: float | None) -> float:
    """`timeout` (none: no limit of its own), or what is left before
    `deadline` if that is less; TimeoutError once it has passed."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("Gave up: the server took too long")
    return left if timeout is None else min(timeout, left)


class _DeadlineSocket:
    """A connected socket whose every read and write waits at most what is
    left before the deadline. Closed once both the connection and the
    response reading from it have let go of it, as a real socket is."""

    def __init__(self, sock, deadline: float, timeout: float):
        self._sock = sock
        self._deadline = deadline
        self._timeout = timeout
        self._users = 1  # the connection; makefile adds the response

    def sendall(self, data) -> None:
        self._sock.settimeout(_time_left(self._deadline, self._timeout))
        self._sock.sendall(data)

    def recv_into(self, buffer) -> int:
        self._sock.settimeout(_time_left(self._deadline, self._timeout))
        return self._sock.recv_into(buffer)

    def makefile(self, mode: str = "rb"):
        self._users += 1
        return io.BufferedReader(_DeadlineReader(self))

    def close(self) -> None:
        self._users -= 1
        if self._users == 0:
            self._sock.close()


class _DeadlineReader(io.RawIOBase):
    """What the response reads its headers and body from."""

    def __init__(self, sock: _DeadlineSocket):
        self._sock = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        return self._sock.recv_into(buffer)

    def close(self) -> None:
        if not self.closed:
            self._sock.close()
        super().close()


class _DeadlineConnection(HTTPConnection):
    def __init__(self, *args, deadline: float, **kwargs):
        super().__init__(*args, **kwargs)
        self._deadline = deadline

    def connect(self) -> None:
        # Connecting, and for https the TLS handshake, get what is left at most.
        self.timeout = _time_left(self._deadline, self.timeout)
        super().connect()
        self.sock = _DeadlineSocket(self.sock, self._deadline, self.timeout)


class _DeadlineHTTPSConnection(_DeadlineConnection, HTTPSConnection):
    pass


class _Deadlines(AbstractHTTPHandler):
    """Handler mixin: a request with a `deadline` gets a connection keeping to it."""

    def do_open(self, http_class, req, **kwargs):
        deadline = getattr(req, "deadline", None)
        if deadline is not None:
            kind = _DeadlineHTTPSConnection if issubclass(http_class, HTTPSConnection) else _DeadlineConnection
            http_class = partial(kind, deadline=deadline)
        return super().do_open(http_class, req, **kwargs)


class _HTTPHandler(_Deadlines, HTTPHandler):
    pass


class _HTTPSHandler(_Deadlines, HTTPSHandler):
    pass


def _build_opener() -> OpenerDirector:
    """urllib's build_opener always adds file:, ftp: and data: handlers; this
    opener has only http and https, so any other address fails as an
    unknown url type, wherever it came from (a redirect included)."""
    opener = OpenerDirector()
    for handler in (UnknownHandler, HTTPDefaultErrorHandler, SafeRedirects, _HTTPHandler, _HTTPSHandler,
                    HTTPErrorProcessor):
        opener.add_handler(handler())
    return opener


_opener = _build_opener()


def urlopen(request: Request, timeout: float):
    return _opener.open(request, timeout=timeout)


def get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    request = Request(  # noqa: S310 - _opener opens only http and https
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


class TooLarge(ValueError):
    """The response was bigger than the caller allowed."""


class NotJSON(ValueError):
    """The response was empty, or not UTF-8 JSON."""


# Seconds for a whole request, redirects and all, unless the caller says
# otherwise. The board's and dashboard's fetches rely on it: a trickled answer
# mustn't hang `run --loop`.
DEADLINE = 30.0
# For a big file on a slow line (a timetable zip, a NaPTAN area): long, but not for ever.
DOWNLOAD_DEADLINE = 30 * 60.0


def get_json_capped(url: str, headers: dict | None, max_bytes: int, timeout: float = 15.0,
                    deadline: float | None = DEADLINE):
    """Like get_json, but the body is refused if it is larger than `max_bytes`,
    and a body that isn't UTF-8 JSON (or nests too deeply to read) gives one
    plain ValueError (NotJSON; TooLarge for the size). The error text never
    repeats the URL or the headers. `timeout` is for each step; the whole
    request is given up after `deadline` seconds (None: no limit), however
    slowly the server sends."""
    # _opener opens only http and https, whatever the URL says.
    request = _TimedRequest(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    if deadline is not None:
        request.deadline = time.monotonic() + deadline
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise TooLarge(f"Response larger than {max_bytes} bytes")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError too
        # `from None`: the decoder's own message can quote part of the body.
        raise NotJSON("Response is not valid JSON") from None


def get_bytes(url: str, max_bytes: int, timeout: float = 15.0, headers: dict | None = None,
              deadline: float | None = DEADLINE) -> bytes:
    """A response body, refused if it is larger than `max_bytes`. `headers`
    are added to (or replace) the default User-Agent. `timeout` is for each
    step; the whole request, redirects and all, is given up after `deadline`
    seconds (None: no limit), however slowly the server sends. A big download
    passes DOWNLOAD_DEADLINE."""
    # _opener opens only http and https, whatever the URL says.
    request = _TimedRequest(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if deadline is not None:
        request.deadline = time.monotonic() + deadline
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"Response larger than {max_bytes} bytes")
    return body


def stream(url: str, headers: Mapping[str, str], max_bytes: int, timeout: float = 15.0) -> Iterator[bytes]:
    """A response body in chunks as it arrives, refusing anything but https
    (ValueError) and more than `max_bytes` (TooLarge)."""
    if urlsplit(url).scheme != "https":
        raise ValueError("Refusing an address that isn't https")
    request = Request(url, headers={"User-Agent": USER_AGENT, **headers})  # noqa: S310 - _opener opens only http and https
    total = 0
    with urlopen(request, timeout=timeout) as response:
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > max_bytes:
                raise TooLarge(f"Response larger than {max_bytes} bytes")
            yield chunk
