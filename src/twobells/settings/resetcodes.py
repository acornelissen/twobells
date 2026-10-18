"""One-time codes that prove someone can see the board: for a forgotten
password, or the first claim on a board with none.

The settings site (web/formpages.py) issues and checks them; the board
(board/app.py) only shows them, and the command line can issue one too. They
are kept in the store, beside the board's state, so all three share them;
the module sits in settings, below the board and the web server, as both use it.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from ..core import files

log = logging.getLogger(__name__)


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
                if (isinstance(code, str) and _CODE.fullmatch(code) and isinstance(expires, (int, float))
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
