"""Whether the web server can connect out. A Pi's is kept off the internet
twice: by its unit's systemd IP firewall and by nftables rules for it alone
(deploy/pi/twobells-web.nft, which twobells-guard.service loads,
matching this server by the cgroup systemd puts in its set). Either can fail
to apply (systemd's firewall does on a Pi Zero, and the set stays empty
before systemd 254), so serve checks at start that one holds, says so in the
log, and the settings' Overview says so too. The Overview looks again as it is
drawn, at most once a minute: Wi-Fi may have had no route yet at start, and
a ruleset can be flushed while the server runs.

The check sends one empty UDP datagram to 192.0.2.1, a documentation
address (RFC 5737) that is never routed, at the discard port. A guard
refuses it there and then (EPERM, or EACCES); with none it goes, to
nowhere. A TCP connect couldn't tell: a guard refuses its first packet
after connect has already said EINPROGRESS. With no route at all the send
fails before any guard sees it, so that can't tell either; then it tries
IPv6 the same way, to 2001:db8::1 (RFC 3849), for a network with no IPv4.
Both guards hold for both kinds of address.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

ON, OFF, UNKNOWN = "on", "off", "unknown"
PROBE_ADDRESS = ("192.0.2.1", 9)
PROBE_ADDRESS_V6 = ("2001:db8::1", 9)
# The shortest time between two checks, in seconds: a page drawn more often
# sends nothing more.
EVERY = 60.0

Send = Callable[[tuple[str, int]], int]  # (address) -> 0 if sent, else the errno


def _send(address: tuple[str, int]) -> int:
    """Send one empty datagram to `address` without waiting; 0 if it went,
    else why not (an errno). The socket is closed at once."""
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            sock.sendto(b"", address)
        except OSError as error:
            return error.errno or -1
    return 0


def _probe_one(send: Send, address: tuple[str, int]) -> str:
    try:
        code = send(address)
    except OSError:  # no socket to send with
        return UNKNOWN
    if code == 0:
        return OFF
    if code in (errno.EPERM, errno.EACCES):
        return ON
    return UNKNOWN


def probe(send: Send | None = None) -> str:
    """ON if something stops this process sending out, OFF if nothing does,
    UNKNOWN if that can't be told over IPv4 or IPv6. `send` is _send unless a
    test says."""
    send = send or _send
    found = _probe_one(send, PROBE_ADDRESS)
    if found == UNKNOWN:
        found = _probe_one(send, PROBE_ADDRESS_V6)
    return found


def _say(found: str) -> None:
    """What the check found, in the log: a warning unless the guard is on."""
    if found == ON:
        log.info("Network guard on: this server can't connect out to the internet")
    elif found == OFF:
        log.warning("Network guard off: nothing stops this server connecting out to the internet. It still turns "
                    "away visitors outside the home network itself. On a Pi, see why with "
                    "`journalctl -u twobells-guard -u twobells-web`, and check that "
                    "`sudo nft list set inet twobells_web web` lists this server")
    else:
        log.warning("Network guard: can't tell whether this server can connect out (no route out to check "
                    "with yet?)")


class Guard:
    """The guard as last found, and when (a time.time()), shared by the
    server's threads: `last`, None until the first look. check() looks now;
    latest() looks again first if the last look is `every` seconds old, or
    the clock has gone back since. What is found is said in the log the
    first time, and whenever it changes."""

    def __init__(self, send: Send | None = None, clock: Callable[[], float] = time.time,
                 every: float = EVERY) -> None:
        self._send = send
        self._clock = clock
        self._every = every
        self._lock = threading.Lock()  # held only for one send, which never waits
        self.last: tuple[str, float] | None = None

    def check(self) -> str:
        with self._lock:
            return self._check()[0]

    def latest(self) -> tuple[str, float]:
        with self._lock:
            if self.last is not None and 0 <= self._clock() - self.last[1] < self._every:
                return self.last
            return self._check()

    def _check(self) -> tuple[str, float]:
        found = probe(self._send)
        if self.last is None or found != self.last[0]:
            _say(found)
        self.last = (found, self._clock())
        return self.last
