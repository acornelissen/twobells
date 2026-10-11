"""Signing in to a page on the home network: the password, sessions, form
tokens, the lockout after wrong guesses, and the checks every request passes.

Used by the settings site (web/settingsite.py) under the one server. The password
lives in a file only the service user can read. It is set on the Pi itself,
or on the page with a one-time code shown on the board (see ResetCodes), so
being on the network is not enough to claim it. A session is a random token in an
HttpOnly, SameSite=Strict cookie; the server keeps the rest, in the store as a
hash of the token, so a restart doesn't sign anyone out. Every change
needs that session's form token, compared in constant time, and a request
whose Origin (or Referer) is this same host.
"""

from __future__ import annotations

import base64
import enum
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

from ..core import files

log = logging.getLogger(__name__)

MIN_PASSWORD = 10
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 20
# Sign-in: this many failures from one client (see client_key) in the window locks it out for the window.
MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
# Past this many failures in the window from every client together, everyone is
# slowed: a try waits 1 s after the last, doubling with each failure, at most MAX_SLOW_S.
TOTAL_FAILURES = 20
MAX_SLOW_S = 60
# While slowed, clients take turns: a client keeps its place this long after its
# last try (the sign-in page says to try again in a minute), and at most this many
# wait, about what one guesser on a /24 of home addresses can hold.
WAITING_KEEP_S = 2 * MAX_SLOW_S
MAX_WAITING = 256
# Each check is a scrypt run of 16 MiB and up to a second; more at once could exhaust a Pi.
MAX_CHECKS = 2
SCRYPT = {"n": 2**14, "r": 8, "p": 1}

# Headers for a page drawn on the server, with a form and no JavaScript.
FORM_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; form-action 'self'; "
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

    @property
    def path(self) -> Path:
        return self._path

    def is_set(self) -> bool:
        return self._path.exists()

    def set(self, password: str) -> None:
        """Hash and save a new password, replacing the old one in one step."""
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Use at least {MIN_PASSWORD} characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
        record = {"salt": salt.hex(), "hash": digest.hex(), **SCRYPT}
        files.write_atomic(self._path, json.dumps(record), 0o600)

    def stamp(self) -> str | None:
        """What identifies the saved password: its salt, new with every change.
        None when there is none or it can't be read."""
        try:
            salt = json.loads(self._path.read_text()).get("salt")
        except (OSError, ValueError, AttributeError):
            return None
        return salt if isinstance(salt, str) else None

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
            return hmac.compare_digest(digest.hex().encode(), str(record["hash"]).encode())
        except (OSError, ValueError, KeyError):
            return False


@dataclass
class Session:
    expires: float
    csrf: str  # the form token: every change made in this session must carry it
    password: str | None = None  # PasswordStore.stamp() when it was created


# What secrets.token_urlsafe(32) makes: a session token, or a form cookie's value.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_SESSIONS_FILE = 64 * 1024  # bytes; MAX_SESSIONS entries take under 3 kB


def _token_hash(token: str) -> str:
    """What the sessions file keeps of a token. A token is 256 random bits,
    so a fast hash is enough: nothing to guess, only to look up."""
    return hashlib.sha256(token.encode()).hexdigest()


def _form_token(token: str) -> str:
    """A session's form token, made from its token so the file needn't hold
    it: the page shows it, and it says nothing of the token behind it."""
    mac = hmac.new(token.encode(), b"departureboard form token", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _read_private(path: Path, limit: int) -> bytes:
    """Up to `limit` + 1 bytes of a file the app wrote for itself, never
    through a link put in its place. OSError if it can't be read."""
    fd = os.open(path, os.O_RDONLY | files.NOFOLLOW | files.CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(limit + 1)


@dataclass
class Sessions:
    """Server-side sessions: the cookie holds only a random token.

    `cookie_name` and `cookie_path` set the cookie this store hands out. Two
    servers on one host share cookies whatever their ports, so each needs its
    own name.

    With `file` (the store's sessions.json), sessions outlive the process, so
    the restart after a save on a desktop or in Docker, or a Pi's update,
    doesn't sign anyone out. The file is 0600, like web.json beside it, and
    replaced whole on every change. It holds, for each session, a SHA-256 of
    its token, its expiry and the password stamp it was made under: never the
    token, nor its form token, which is made again from the token the browser
    sends. Expired sessions, and those of an older password, are dropped on
    every write. A file that can't be read, or doesn't look as written, means
    no sessions: everyone signs in again. One server per store writes it; a
    password set in another process ends its sessions through the stamp.
    """

    clock: Callable[[], float] = time.time
    cookie_name: str = "session"
    cookie_path: str = "/"
    # With the password store, a password set elsewhere (the command line runs
    # in its own process) ends every session made under the old one.
    password: PasswordStore | None = None
    file: Path | None = None
    _live: dict[str, Session] = field(default_factory=dict)  # by _token_hash
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Both go into a Set-Cookie header as they are, so only plain characters.
        if not _COOKIE_NAME.match(self.cookie_name):
            raise ValueError("A cookie name is 1 to 64 letters, digits, _ or -")
        if not _COOKIE_PATH.match(self.cookie_path):
            raise ValueError("A cookie path starts with / and has only letters, digits, _, - and /")
        if self.file is not None:
            self._live = self._load()

    def create(self) -> tuple[str, Session]:
        """A new session and its token, making room by dropping expired
        sessions, then the oldest, so there are never more than MAX_SESSIONS."""
        with self._lock:
            now = self.clock()
            stamp = self._stamp()
            self._prune(now, stamp)
            while len(self._live) >= MAX_SESSIONS:
                self._live.pop(min(self._live, key=lambda t: self._live[t].expires))
            token = secrets.token_urlsafe(32)
            session = Session(now + SESSION_SECONDS, _form_token(token), stamp)
            self._live[_token_hash(token)] = session
            self._save()
            return token, session

    def get(self, token: str | None) -> Session | None:
        """The live session for `token`, or None if there isn't one or it has expired."""
        if not token:
            return None
        with self._lock:
            found = self._find(token)
            if found is None:
                return None
            session = self._live[found]
            if session.password != self._stamp() or session.expires <= self.clock():
                del self._live[found]
                self._save()
                return None
            session.csrf = _form_token(token)  # not in the file: made again from the token
            return session

    def end(self, token: str | None) -> None:
        with self._lock:
            found = self._find(token)
            if found is not None:
                del self._live[found]
                self._save()

    def end_all(self) -> None:
        """Signs every browser out, as after the password is reset."""
        with self._lock:
            self._live.clear()
            self._save()

    def _stamp(self) -> str | None:
        return self.password.stamp() if self.password is not None else None

    def _find(self, token: str | None) -> str | None:
        """The key of `token`'s session, if there is one, compared in constant
        time. Called with the lock held."""
        if not token or not _TOKEN.fullmatch(token):
            return None
        digest = _token_hash(token)
        return next((key for key in self._live if hmac.compare_digest(key, digest)), None)

    def _prune(self, now: float, stamp: str | None) -> None:
        """Drop the expired sessions, and those made under another password."""
        self._live = {t: s for t, s in self._live.items() if s.expires > now and s.password == stamp}

    def _save(self) -> None:
        """Write the live sessions to the file, if there is one. Called with the lock held."""
        if self.file is None:
            return
        self._prune(self.clock(), self._stamp())
        entries = [{"hash": key, "expires": s.expires, "password": s.password} for key, s in self._live.items()]
        try:
            files.write_atomic(self.file, json.dumps({"sessions": entries}), 0o600)
        except OSError as error:
            log.warning("Can't save the settings sessions (%s); they end when the server stops",
                        type(error).__name__)
            # Whatever it held, nothing in it may come back: a session ended here among them.
            try:
                self.file.unlink(missing_ok=True)
            except OSError as gone:
                log.warning("Can't remove the old settings sessions either (%s)", type(gone).__name__)

    def _load(self) -> dict[str, Session]:
        """The sessions in the file still live under the current password; none
        if it is missing, unreadable or not as written, so nothing odd is accepted."""
        if self.file is None:
            return {}
        try:
            raw = _read_private(self.file, MAX_SESSIONS_FILE)
        except FileNotFoundError:
            return {}
        except OSError as error:
            log.warning("Can't read the settings sessions (%s); everyone signs in again", type(error).__name__)
            return {}
        try:
            data = json.loads(raw) if len(raw) <= MAX_SESSIONS_FILE else None
        except (ValueError, RecursionError):
            data = None
        entries = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            log.warning("The settings sessions file isn't as written; everyone signs in again")
            return {}
        now, stamp = self.clock(), self._stamp()
        live: dict[str, Session] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key, expires, password = entry.get("hash"), entry.get("expires"), entry.get("password")
            if (isinstance(key, str) and _SHA256.fullmatch(key)
                    and isinstance(expires, (int, float)) and not isinstance(expires, bool)
                    and now < expires <= now + SESSION_SECONDS  # never longer than a session lasts
                    and (password is None or isinstance(password, str)) and password == stamp):
                live[key] = Session(float(expires), "", password)  # its form token comes with its token
        newest = sorted(live.items(), key=lambda item: item[1].expires)[-MAX_SESSIONS:]
        return dict(newest)

    def token_from(self, cookie_header: str | None) -> str | None:
        """This store's session token from a request's Cookie header, if it has one."""
        return _cookie_value(cookie_header, self.cookie_name)

    def cookie(self, token: str) -> str:
        """The Set-Cookie value that signs a browser in for SESSION_SECONDS."""
        return (f"{self.cookie_name}={token}; HttpOnly; SameSite=Strict; "
                f"Path={self.cookie_path}; Max-Age={SESSION_SECONDS}")

    def expired_cookie(self) -> str:
        """The Set-Cookie value that makes a browser forget its session cookie."""
        return f"{self.cookie_name}=; HttpOnly; SameSite=Strict; Path={self.cookie_path}; Max-Age=0"


def _cookie_value(cookie_header: str | None, name: str) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header or "")
    except Exception:  # noqa: BLE001 - a malformed cookie is no cookie
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def csrf_matches(session: Session | None, submitted: str) -> bool:
    """Whether a form's token is the session's own, compared in constant time.
    False without a session."""
    # As bytes: compare_digest raises on non-ASCII str, and a form can send anything.
    return (session is not None and bool(session.csrf)
            and hmac.compare_digest(submitted.encode(), session.csrf.encode()))


FORM_TOKEN_SECONDS = 30 * 60
FORM_KEY_BYTES = 32
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_EXPIRES = re.compile(r"^[0-9]{1,12}$")


@dataclass
class FormTokens:
    """Form tokens for the forms posted before there is a session to hold
    one: the forgotten password and the first claim.

    A signed double-submit cookie: the browser keeps a random value in an
    HttpOnly, SameSite=Strict cookie, and the form carries an expiry and an
    HMAC of that value and expiry under a key only the server knows. A page
    on another site can neither read the cookie nor make the matching form
    value, and a cookie planted by a neighbouring host is no use without the
    key. A token lasts FORM_TOKEN_SECONDS. This is on top of the Origin
    check every POST passes.

    The key is random, 32 bytes. With `key_file` (the store's form.key, 0600
    like web.json beside it) it is kept there, so a form opened before a
    restart still posts after it; without, every token dies with the process.
    The kept key is read, or made, when a form first needs it, so starting
    the server writes nothing.
    """

    clock: Callable[[], float] = time.time
    cookie_name: str = "form"
    cookie_path: str = "/"
    key_file: Path | None = None
    _key: bytes = field(default_factory=lambda: secrets.token_bytes(FORM_KEY_BYTES))
    _key_read: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not _COOKIE_NAME.match(self.cookie_name):
            raise ValueError("A cookie name is 1 to 64 letters, digits, _ or -")
        if not _COOKIE_PATH.match(self.cookie_path):
            raise ValueError("A cookie path starts with / and has only letters, digits, _, - and /")

    def nonce(self, cookie_header: str | None) -> str:
        """The browser's value from its cookie, or a new one if it has none, so
        two open tabs don't spoil each other's forms."""
        value = _cookie_value(cookie_header, self.cookie_name)
        return value if value and _NONCE.match(value) else secrets.token_urlsafe(32)

    def cookie(self, nonce: str) -> str:
        return (f"{self.cookie_name}={nonce}; HttpOnly; SameSite=Strict; "
                f"Path={self.cookie_path}; Max-Age={FORM_TOKEN_SECONDS}")

    def form_value(self, nonce: str) -> str:
        """The value for the form's hidden field, for the browser holding `nonce`."""
        expires = int(self.clock()) + FORM_TOKEN_SECONDS
        return f"{expires}.{self._mac(nonce, expires)}"

    def check(self, cookie_header: str | None, submitted: str) -> bool:
        """Whether a posted form's value was made for this browser's cookie, by
        this process, and hasn't run out."""
        nonce = _cookie_value(cookie_header, self.cookie_name)
        expires, _, mac = submitted.partition(".")
        if not nonce or not _NONCE.match(nonce) or not _EXPIRES.match(expires):
            return False
        right = hmac.compare_digest(mac.encode(), self._mac(nonce, int(expires)).encode())
        return right and self.clock() < int(expires)

    def _mac(self, nonce: str, expires: int) -> str:
        return hmac.new(self._secret(), f"{nonce}.{expires}".encode(), hashlib.sha256).hexdigest()

    def _secret(self) -> bytes:
        if self.key_file is not None:
            with self._lock:
                if not self._key_read:
                    self._key = _kept_key(self.key_file)
                    self._key_read = True
        return self._key


def _kept_key(path: Path) -> bytes:
    """The form key saved at `path`, or a new random one saved there in its
    place when it is missing or not FORM_KEY_BYTES. One that can't be saved is
    still used, for this process only."""
    try:
        key = _read_private(path, FORM_KEY_BYTES)
        if len(key) == FORM_KEY_BYTES:
            return key
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("Can't read the settings form key (%s); making a new one", type(error).__name__)
    key = secrets.token_bytes(FORM_KEY_BYTES)
    try:
        files.write_atomic(path, key, 0o600)
    except OSError as error:
        log.warning("Can't save the settings form key (%s); open forms stop working when the server stops",
                    type(error).__name__)
    return key


def client_key(address: str) -> str:
    """Who a client counts as, for the sign-in lockout and a reset code's wrong
    tries: an IPv4 address as it is (also when it comes as an IPv4-mapped IPv6
    one), an IPv6 address by its /64. A host can take any address in its /64
    at will, so counting each address apart would give it endless tries; the
    cost is that the devices on one /64, on a home network usually all of them
    over IPv6, share a count. Anything that isn't an address, as given."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


class SignInResult(enum.Enum):
    SIGNED_IN = "signed in"
    WRONG = "wrong password"
    LOCKED = "locked out"  # too many wrong passwords from this client
    SLOWED = "slowed"  # too many from every client together: everyone waits a moment
    BUSY = "busy"  # MAX_CHECKS checks already running


@dataclass
class Lockout:
    """Counts wrong passwords by client (client_key: an IPv6 one by its /64).
    MAX_FAILURES within FAILURE_WINDOW_S locks that client out until the
    oldest falls out of the window.

    Past TOTAL_FAILURES within the window from every client together, so a
    guesser changing address gets no more than that, everyone is slowed
    rather than locked out: one try is let in at a time, each after a wait
    from the last that grows with each failure past the total, at most
    MAX_SLOW_S. Refused tries cost nothing, so clients take turns, or a
    script polling from a few addresses would take every turn: the next turn
    goes to the client that has waited longest, by its first refused try
    since it was last let in. A client that stops trying for WAITING_KEEP_S
    loses its place, so a gone one can't hold the turn for long. With N
    clients guessing, the owner, trying again every minute or so, is let in
    within about N + 1 turns of first trying: each at most MAX_SLOW_S, or
    WAITING_KEEP_S where a guesser lets its turn lapse. The reset code (which
    this doesn't count) and the command line still work.

    Clients with no failures left in the window are dropped, so the table
    holds only what the limits let through in one window. At most
    MAX_WAITING clients wait; past that, newcomers are refused without a
    place rather than pushing out those waiting longest."""

    clock: Callable[[], float] = time.time
    _failures: dict[str, list[float]] = field(default_factory=dict)
    # Waiting clients, first come first: each one's last try.
    _waiting: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _prune(self, now: float) -> None:
        """Keep only the failures in the window, and the clients that have
        some, and the waiting clients that tried lately. Times ahead of now
        are from before the clock went back: not counted, so a clock set back
        can't stretch a lockout or hold a turn. Called with the lock held."""
        cutoff = now - FAILURE_WINDOW_S
        kept: dict[str, list[float]] = {}
        for key, times in self._failures.items():
            recent = [t for t in times if cutoff < t <= now]
            if recent:
                kept[key] = recent
        self._failures = kept
        quiet = now - WAITING_KEEP_S
        self._waiting = {key: t for key, t in self._waiting.items() if quiet < t <= now}

    def _slowed(self, now: float, key: str) -> bool:
        """Whether this client must wait for its turn: everyone waits after the
        last try let in, and then the turn is the longest waiter's. Called
        with the lock held, after _prune."""
        total = sum(len(times) for times in self._failures.values())
        if total < TOTAL_FAILURES:
            return False
        if key in self._waiting or len(self._waiting) < MAX_WAITING:
            self._waiting[key] = now  # a new client goes last; one waiting keeps its place
        last = max(max(times) for times in self._failures.values())
        if now < last + min(2.0 ** (total - TOTAL_FAILURES), MAX_SLOW_S):
            return True
        return next(iter(self._waiting), None) != key

    def admit(self, address: str) -> SignInResult | None:
        """None if this client may try a password now; else LOCKED, or SLOWED
        while everyone waits or until its turn. An admitted try counts as a
        failure until `succeeded`, so simultaneous guesses can't slip past
        the limits; a refused one counts for nothing."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            key = client_key(address)
            mine = self._failures.get(key, [])
            if len(mine) >= MAX_FAILURES:
                return SignInResult.LOCKED
            if self._slowed(now, key):
                return SignInResult.SLOWED
            self._waiting.pop(key, None)
            self._failures[key] = [*mine, now]
            return None

    def attempt(self, address: str) -> bool:
        """Whether this client may try a password now (see `admit`)."""
        return self.admit(address) is None

    def succeeded(self, address: str) -> None:
        """Clears this client's failures, from its own count and the total."""
        with self._lock:
            self._failures.pop(client_key(address), None)


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
            refused = self._lockout.admit(address)
            right = refused is None and self._password.check(typed)
        finally:
            self._checking.release()
        if refused is not None:
            return refused
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


# --- Reset codes ---
#
# A forgotten password, or the first claim on a board with none, is proven by
# a one-time code shown on the board's own display: whoever can read the
# board is in the home, which being on the network doesn't prove.
#
# Two processes share the state folder as the same user: the settings site
# (`serve`) issues and checks codes; the board (`run`, each minute or in a
# loop) only shows them. So there are three files:
#
#   reset.json          what `verify` needs: a salted sha256 of the code, its
#                       expiry and wrong tries (in all, and by client_key),
#                       and when codes were issued (for the rate limits). Never
#                       the code itself.
#   reset-display.json  the plain code and its expiry, for the board to draw.
#                       Written only when the code is meant for the board, and
#                       read only by it. Removed when the code is used or
#                       voided (by `verify`), replaced (by `issue`, once it
#                       is used or has run out, or by the command line), or
#                       found expired (by the board).
#   reset.lock          an flock held around every change to the other two, so
#                       the board can't delete a code `issue` has just written,
#                       and two checks at once can't both use one code.
#
# All are 0600 and replaced whole: written aside, then renamed.
#
# The trade-off: the plain code sits on disk for up to 10 minutes, and a
# 6-digit hash is no secret from anyone who can read reset.json. Both are
# readable only by the service user, who can already write web.json and so
# set any password it likes; the hash keeps the code out of a stray glance or
# a copied folder, not from that user. In return the board, a separate
# process that may not even be running when the code is issued, needs no
# channel to the page beyond the folder they already share.
#
# Asking again: while a code lasts, the page never replaces it. Asking again
# leaves it where it is shown (the board keeps drawing it; the log says the
# last one still stands) and spends none of the limits below, so no one else
# on the network can swap the owner's code for one the owner hasn't seen, or
# use up the day's codes while one is showing.
#
# Guessing: 5 wrong tries from one client (by client_key: an IPv6 one by its
# /64) shut that client out of the code, so a guesser can't void the owner's
# code (asking again from there says so, and when the code runs out); 10 from
# all clients together void it, so changing address buys no more than that. At most 10
# codes are issued a day, so a guesser on the network gets 100 tries in a
# million a day, and every code it asks for shows on the board for the owner
# to see.

RESET_CODE_SECONDS = 10 * 60
RESET_MAX_TRIES = 5
RESET_MAX_TRIES_ALL = 10
RESET_ISSUE_GAP_S = 60
RESET_ISSUES_PER_DAY = 10
_DAY_S = 24 * 3600
_CODE = re.compile(r"^[0-9]{6}$")


class ResetLimited(Exception):
    """A code was issued too recently, or too many today."""


class ResetShutOut(Exception):
    """The address asking has had RESET_MAX_TRIES wrong tries at the code
    that still stands; `until` is when it runs out, as HH:MM in the zone
    given, after which a new code can be asked for."""

    def __init__(self, until: str) -> None:
        super().__init__(until)
        self.until = until


@dataclass(frozen=True)
class PendingCode:
    """A code for the board to show, and when it runs out."""

    code: str
    expires: float  # a Unix time


def spaced(code: str) -> str:
    """`123456` as `123 456`, easier to read off a board and type."""
    return f"{code[:3]} {code[3:]}"


def clock_time(when: float, zone: tzinfo | None) -> str:
    """The Unix time `when` as HH:MM in `zone`, or the process's own zone for
    None (which in Docker is UTC, not the board's)."""
    return datetime.fromtimestamp(when, zone).strftime("%H:%M")


def show_in_log(code: str, expires: float, zone: tzinfo | None = None) -> None:
    """Where a code goes when there is no board to show it (or its view is off): the
    log, which only the owner can read, with when it runs out in `zone`. The
    one place a code is logged on purpose."""
    log.warning("Settings reset code: %s, valid until %s", spaced(code), clock_time(expires, zone))


def _code_hash(salt: bytes, code: str) -> str:
    return hashlib.sha256(salt + code.encode()).hexdigest()


def _tries_by(current: dict, client: str) -> int:
    """The wrong tries the address `client` (counted by client_key) has had
    at the saved code `current`; 0 if they can't be read."""
    try:
        return int(current.get("by_client", {}).get(client_key(client), 0))
    except (TypeError, ValueError, AttributeError):
        return 0


class ResetCodes:
    """One-time codes that prove someone can see the board (or the log, or the
    command line). See the notes above for the files and why.

    `show(code, expires)` is where a new code goes. Without one, each code
    asks `to_board()`: true hands it to the board through
    reset-display.json, false puts it in the log (show_in_log), so turning
    the board view on or off changes where the next code goes. `zone()` is
    the time zone the times a code runs out are said in, as the board would
    say them; by default the process's own.
    """

    def __init__(self, folder: Path | str, show: Callable[[str, float], None] | None = None,
                 to_board: Callable[[], bool] = lambda: True,
                 zone: Callable[[], tzinfo | None] = lambda: None) -> None:
        self._folder = Path(folder)
        self._record = self._folder / "reset.json"
        self._display = self._folder / "reset-display.json"
        self._lock_path = self._folder / "reset.lock"
        self._show = show
        self._to_board = to_board
        self._zone = zone

    def issue(self, now: float, limit: bool = True, client: str = "") -> str | None:
        """A new code, valid for RESET_CODE_SECONDS, passed to `show`. With
        `limit` (the page; not the command line, which proves access by
        running at all) a code that is still live where codes go now stands:
        None, and nothing spent; or ResetShutOut if the address `client` (by
        client_key) has had its RESET_MAX_TRIES wrong tries at it. Otherwise it raises
        ResetLimited within RESET_ISSUE_GAP_S of the last code, or after
        RESET_ISSUES_PER_DAY in a day. Without `limit` the new code replaces
        any before it."""
        with self._locked():
            record = self._load()
            expires = self._still_shown(record["code"], now)
            if limit and expires is not None:
                until = clock_time(expires, self._zone())
                log.warning("Settings reset code asked for again: the last one still stands, valid until %s", until)
                if _tries_by(record["code"], client) >= RESET_MAX_TRIES:
                    raise ResetShutOut(until)
                return None
            # Times ahead of now are from before the clock went back: not counted.
            recent = [t for t in record["issued"] if 0 <= now - t < _DAY_S]
            too_soon = any(now - t < RESET_ISSUE_GAP_S for t in recent)
            if limit and (too_soon or len(recent) >= RESET_ISSUES_PER_DAY):
                raise ResetLimited
            code = f"{secrets.randbelow(10**6):06d}"
            salt = secrets.token_bytes(16)
            expires = now + RESET_CODE_SECONDS
            self._write(self._record, {
                "issued": [*recent, now],
                "code": {"salt": salt.hex(), "hash": _code_hash(salt, code), "expires": expires, "tries": 0},
            })
            self._display.unlink(missing_ok=True)  # it showed the old code, void now
            if self._show is not None:
                self._show(code, expires)
            elif self._to_board():
                self._hand_to_board(code, expires)
            else:
                show_in_log(code, expires, self._zone())
        log.info("Settings reset code issued, valid for %d minutes", RESET_CODE_SECONDS // 60)
        return code

    def verify(self, typed: str, now: float, client: str = "") -> bool:
        """Whether `typed`, from the address `client`, is the current code,
        compared in constant time. A right code is used up. RESET_MAX_TRIES
        wrong ones shut that client (by client_key) out of this code, and
        RESET_MAX_TRIES_ALL from every client together void it."""
        typed = "".join(typed.split())  # "123 456" as shown
        client = client_key(client)
        with self._locked():
            record = self._load()
            current, found = record["code"], True
            try:
                salt = bytes.fromhex(current["salt"])
                stored, expires, tries = str(current["hash"]), float(current["expires"]), int(current["tries"])
                by_client = current.get("by_client", {})  # none in a code saved before they were counted
                if not isinstance(by_client, dict):
                    raise TypeError
                mine = int(by_client.get(client, 0))
            except (TypeError, KeyError, ValueError, AttributeError):
                found, salt, stored, expires, tries, by_client, mine = False, b"", "", 0.0, 0, {}, 0
            # Hashed, compared and saved with or without a code, so the time
            # taken doesn't say whether there is one.
            right = hmac.compare_digest(_code_hash(salt, typed).encode(), stored.encode())
            live = found and now < expires and tries < RESET_MAX_TRIES_ALL
            allowed = mine < RESET_MAX_TRIES  # this address has tries left
            if live and allowed and not right:
                current["tries"] = tries + 1
                # At most RESET_MAX_TRIES_ALL clients before the code is void, so this stays small.
                current["by_client"] = {**by_client, client: mine + 1}
                if current["tries"] >= RESET_MAX_TRIES_ALL:
                    log.warning("Settings reset code voided after %d wrong tries", RESET_MAX_TRIES_ALL)
            used = right and live and allowed
            if used or not live or current["tries"] >= RESET_MAX_TRIES_ALL:
                record["code"] = None  # used, expired or voided
                self._display.unlink(missing_ok=True)
            self._write(self._record, record)
            return used

    def pending(self, now: float) -> PendingCode | None:
        """For the board: the code it should be showing, if any. An expired or
        unreadable handover is removed, so the plain code doesn't linger."""
        if not self._display.exists():
            return None  # the usual case, every tick: no lock taken, nothing written
        try:
            with self._locked():
                data = self._read(self._display)
                code, expires = data.get("code"), data.get("expires")
                if (isinstance(code, str) and _CODE.match(code) and isinstance(expires, (int, float))
                        and now < expires):
                    return PendingCode(code, float(expires))
                self._display.unlink(missing_ok=True)
        except OSError as error:
            log.warning("Can't read the settings reset code handover: %s", type(error).__name__)
        return None

    def _still_shown(self, current: dict | None, now: float) -> float | None:
        """When the code saved in `current` runs out, if it is live and still
        where a new code would go: on the board while codes go there, or in
        the log while they don't (or with `show`, wherever that put it).
        None if there is no such code, so a new one may be issued."""
        if current is None:
            return None
        try:
            expires, tries = float(current["expires"]), int(current["tries"])
        except (TypeError, KeyError, ValueError):
            return None
        if not (now < expires and tries < RESET_MAX_TRIES_ALL):
            return None
        if self._show is None and self._display.exists() != self._to_board():
            return None  # the board view went on or off since: the code isn't where the page says to look
        return expires

    def _locked(self):
        """The lock, held until the returned file is closed."""
        self._folder.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(files.open_lock(self._lock_path), "r+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            handle.close()
            raise
        return handle

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        files.write_atomic(path, json.dumps(data), 0o600)

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load(self) -> dict:
        record = self._read(self._record)
        issued, code = record.get("issued"), record.get("code")
        return {
            "issued": [t for t in issued if isinstance(t, (int, float))] if isinstance(issued, list) else [],
            "code": code if isinstance(code, dict) else None,
        }

    def _hand_to_board(self, code: str, expires: float) -> None:
        self._write(self._display, {"code": code, "expires": expires})
