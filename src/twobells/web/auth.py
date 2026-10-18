"""Signing in to a page on the home network: the password, sessions, form
tokens, the lockout after wrong guesses, and the checks every request passes.

Used by the settings site (web/settingsite.py) under the one server. The password
lives in a file only the service user can read. It is set on the Pi itself,
or on the page with a one-time code shown on the board (see
settings/resetcodes.py), so being on the network is not enough to claim it. A session is a random token in an
HttpOnly, SameSite=Strict cookie; the server keeps the rest, in the store as a
hash of the token, so a restart doesn't sign anyone out. Every change
needs that session's form token, compared in constant time, and a request
whose Origin (or Referer) is this same host.
"""

from __future__ import annotations

import base64
import enum
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
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass, field
from functools import lru_cache
from http.cookies import SimpleCookie
from importlib.resources import files as package_files
from pathlib import Path
from urllib.parse import urlparse

from ..core import files
from ..settings.resetcodes import client_key

log = logging.getLogger(__name__)

MIN_PASSWORD = 10
# What every way of setting the password says of one on the bundled list (see common_password).
TOO_COMMON = "That's one of the most common passwords. Choose another."
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


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    """The bundled list of common passwords, lower-cased, read once."""
    text = package_files("twobells").joinpath("data/common_passwords.txt").read_text("utf-8")
    return frozenset(line for line in text.splitlines() if line and not line.startswith("#"))


def common_password(password: str) -> bool:
    """Whether `password` is on the bundled list of the most common passwords
    of MIN_PASSWORD characters or more, whatever its case. Guessers try those first."""
    return password.lower() in _common_passwords()


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
        if common_password(password):
            raise ValueError(TOO_COMMON)
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
    mac = hmac.new(token.encode(), b"twobells form token", hashlib.sha256).digest()
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
        if not _COOKIE_NAME.fullmatch(self.cookie_name):
            raise ValueError("A cookie name is 1 to 64 letters, digits, _ or -")
        if not _COOKIE_PATH.fullmatch(self.cookie_path):
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
        if not _COOKIE_NAME.fullmatch(self.cookie_name):
            raise ValueError("A cookie name is 1 to 64 letters, digits, _ or -")
        if not _COOKIE_PATH.fullmatch(self.cookie_path):
            raise ValueError("A cookie path starts with / and has only letters, digits, _, - and /")

    def nonce(self, cookie_header: str | None) -> str:
        """The browser's value from its cookie, or a new one if it has none, so
        two open tabs don't spoil each other's forms."""
        value = _cookie_value(cookie_header, self.cookie_name)
        return value if value and _NONCE.fullmatch(value) else secrets.token_urlsafe(32)

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
        if not nonce or not _NONCE.fullmatch(nonce) or not _EXPIRES.fullmatch(expires):
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
    # When the last try was let in, right or wrong: a wait counts from it, so
    # the owner signing in doesn't open the next turn at once.
    _last_let_in: float | None = None
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
        if self._last_let_in is not None and self._last_let_in > now:
            self._last_let_in = None

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
        if self._last_let_in is not None:
            last = max(last, self._last_let_in)
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
            self._last_let_in = now
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


def outsider(address: str, hosts: Sequence[str], allowed_hosts: Set[str] = frozenset(),
             client_allowed: Callable[[str], bool] = private_address) -> bool:
    """Whether the web server refuses a request: it comes from an address
    `client_allowed` refuses (by default, one outside the home network), it
    has other than one Host header (`hosts`, all it sent), or that names
    somewhere other than this machine or a name in `allowed_hosts`."""
    if len(hosts) != 1 or not client_allowed(address):
        return True
    return not (local_host(hosts[0]) or _host_name(hosts[0]) in allowed_hosts)


def _host_name(host: str) -> str:
    """The name in a Host header, without its port; "" for an IPv6 literal,
    which allowed_hosts never holds."""
    name = host.strip().lower()
    if name.startswith("["):
        return ""
    return name.rsplit(":", 1)[0] if name.count(":") == 1 else name
