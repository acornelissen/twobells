"""Signing in to a page on the home network: the password, sessions, form
tokens, the lockout after wrong guesses, and the checks every request passes.

Shared by the settings page (web.py) and the dashboard server. The password
lives in a file only the service user can read, set on the Pi itself, so
nobody on the network can claim it first. A session is a random token in an
HttpOnly, SameSite=Strict cookie; the server keeps the rest. Every change
needs that session's form token, compared in constant time, and a request
whose Origin (or Referer) is this same host.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

MIN_PASSWORD = 10
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 20
# Sign-in: this many failures from one address in the window locks it out for the window.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
# Each check is a scrypt run of 16 MiB and up to a second; more at once could exhaust a Pi.
MAX_CHECKS = 2
SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# Headers for a page drawn on the server, with a form and no JavaScript.
FORM_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",  # no-referrer makes browsers post `Origin: null`
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_COOKIE_PATH = re.compile(r"^/[A-Za-z0-9_/-]{0,200}$")


class PasswordStore:
    """The password, as a scrypt hash in a file only the service user can read.

    The file is JSON: the salt and hash in hex, and the scrypt cost settings
    used, so a check still works if the defaults change later.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def is_set(self) -> bool:
        return self._path.exists()

    def set(self, password: str) -> None:
        """Hash and save a new password, replacing the old one in one step."""
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
        record = {"salt": salt.hex(), "hash": digest.hex(), **SCRYPT}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
        os.replace(tmp, self._path)

    def check(self, password: str) -> bool:
        """Whether `password` is the saved one. False when none is saved or the
        file is unreadable."""
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
    csrf: str  # the form token: every change made in this session must carry it


@dataclass
class Sessions:
    """Server-side sessions: the cookie holds only a random token.

    `cookie_name` and `cookie_path` set the cookie this store hands out. Two
    servers on one host share cookies whatever their ports, so each needs its
    own name.
    """

    clock: Callable[[], float] = time.time
    cookie_name: str = "session"
    cookie_path: str = "/"
    _live: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Both go into a Set-Cookie header as they are, so only plain characters.
        if not _COOKIE_NAME.match(self.cookie_name):
            raise ValueError("A cookie name is 1 to 64 letters, digits, _ or -")
        if not _COOKIE_PATH.match(self.cookie_path):
            raise ValueError("A cookie path starts with / and has only letters, digits, _, - and /")

    def create(self) -> tuple[str, Session]:
        """A new session and its token, making room by dropping expired
        sessions, then the oldest, so there are never more than MAX_SESSIONS."""
        with self._lock:
            now = self.clock()
            self._live = {t: s for t, s in self._live.items() if s.expires > now}
            while len(self._live) >= MAX_SESSIONS:
                self._live.pop(min(self._live, key=lambda t: self._live[t].expires))
            token = secrets.token_urlsafe(32)
            session = Session(now + SESSION_SECONDS, secrets.token_urlsafe(32))
            self._live[token] = session
            return token, session

    def get(self, token: str | None) -> Session | None:
        """The live session for `token`, or None if there isn't one or it has expired."""
        with self._lock:
            session = self._live.get(token or "")
            if session is None or session.expires <= self.clock():
                self._live.pop(token or "", None)
                return None
            return session

    def end(self, token: str | None) -> None:
        with self._lock:
            self._live.pop(token or "", None)

    def token_from(self, cookie_header: str | None) -> str | None:
        """This store's session token from a request's Cookie header, if it has one."""
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
        except Exception:  # noqa: BLE001 - a malformed cookie is no cookie
            return None
        morsel = cookie.get(self.cookie_name)
        return morsel.value if morsel else None

    def cookie(self, token: str) -> str:
        """The Set-Cookie value that signs a browser in for SESSION_SECONDS."""
        return (f"{self.cookie_name}={token}; HttpOnly; SameSite=Strict; "
                f"Path={self.cookie_path}; Max-Age={SESSION_SECONDS}")

    def expired_cookie(self) -> str:
        """The Set-Cookie value that makes a browser forget its session cookie."""
        return f"{self.cookie_name}=; HttpOnly; SameSite=Strict; Path={self.cookie_path}; Max-Age=0"


def csrf_matches(session: Session | None, submitted: str) -> bool:
    """Whether a form's token is the session's own, compared in constant time.
    False without a session."""
    return session is not None and hmac.compare_digest(submitted, session.csrf)


@dataclass
class Lockout:
    """Counts wrong passwords per client address. MAX_FAILURES within
    FAILURE_WINDOW_S locks that address out until the oldest falls out of
    the window."""

    clock: Callable[[], float] = time.time
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, address: str) -> list[float]:
        cutoff = self.clock() - FAILURE_WINDOW_S
        recent = [t for t in self._failures.get(address, []) if t > cutoff]
        self._failures[address] = recent
        return recent

    def attempt(self, address: str) -> bool:
        """Whether this address may try a password now. The try counts as a
        failure until `succeeded`, so simultaneous guesses can't slip past the limit."""
        with self._lock:
            recent = self._recent(address)
            if len(recent) >= MAX_FAILURES:
                return False
            recent.append(self.clock())
            return True

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


class SignInResult(enum.Enum):
    SIGNED_IN = "signed in"
    WRONG = "wrong password"
    LOCKED = "locked out"  # too many wrong passwords from this address
    BUSY = "busy"  # MAX_CHECKS checks already running


class PasswordCheck:
    """Checks a typed password under the lockout, with at most MAX_CHECKS
    scrypt runs at once across every client."""

    def __init__(self, password: PasswordStore, lockout: Lockout, max_checks: int = MAX_CHECKS) -> None:
        self._password = password
        self._lockout = lockout
        self._checking = threading.BoundedSemaphore(max_checks)

    def sign_in(self, address: str, typed: str) -> SignInResult:
        if not self._checking.acquire(blocking=False):
            return SignInResult.BUSY
        try:
            allowed = self._lockout.attempt(address)
            right = allowed and self._password.check(typed)
        finally:
            self._checking.release()
        if not allowed:
            return SignInResult.LOCKED
        if not right:
            return SignInResult.WRONG
        self._lockout.succeeded(address)
        return SignInResult.SIGNED_IN


def same_origin(headers) -> bool:
    """Whether a request's Origin, or its Referer when there is no Origin,
    names the host it was sent to. A form posted from another site, or with
    `Origin: null`, fails. `headers` is anything with .get, like http.server's."""
    origin = headers.get("Origin") or headers.get("Referer")
    if not origin:
        return False
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == headers.get("Host")


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


def outsider(address: str, host: str | None) -> bool:
    """Whether to refuse a request: it comes from outside the home network, or
    its Host header names somewhere other than this machine."""
    return not private_address(address) or not local_host(host)
