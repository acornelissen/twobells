"""Whether the web server can connect out. A Pi's is kept off the internet
twice: by its unit's systemd IP firewall and by nftables rules for it alone
(deploy/pi/departureboard-web.nft). Either can fail to apply (systemd's does
on a Pi Zero), so serve checks at start that one holds, says so in the log,
and the settings' Overview says so too.

The check sends one empty UDP datagram to 192.0.2.1, a documentation
address (RFC 5737) that is never routed, at the discard port. A guard
refuses it there and then (EPERM, or EACCES); with none it goes, to
nowhere. A TCP connect couldn't tell: a guard refuses its first packet
after connect has already said EINPROGRESS. With no route at all the send
fails before any guard sees it, so that can't tell either.
"""

from __future__ import annotations

import errno
import logging
import socket
from collections.abc import Callable

log = logging.getLogger(__name__)

ON, OFF, UNKNOWN = "on", "off", "unknown"
PROBE_ADDRESS = ("192.0.2.1", 9)

Send = Callable[[tuple[str, int]], int]  # (address) -> 0 if sent, else the errno


def _send(address: tuple[str, int]) -> int:
    """Send one empty datagram to `address` without waiting; 0 if it went,
    else why not (an errno). The socket is closed at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            sock.sendto(b"", address)
        except OSError as error:
            return error.errno or -1
    return 0


def probe(send: Send | None = None) -> str:
    """ON if something stops this process sending out, OFF if nothing does,
    UNKNOWN if that can't be told. `send` is _send unless a test says."""
    try:
        code = (send or _send)(PROBE_ADDRESS)
    except OSError:  # no socket to send with
        return UNKNOWN
    if code == 0:
        return OFF
    if code in (errno.EPERM, errno.EACCES):
        return ON
    return UNKNOWN


def check(send: Send | None = None) -> str:
    """probe(), said in the log: a warning unless the guard is on."""
    found = probe(send)
    if found == ON:
        log.info("Network guard on: this server can't connect out to the internet")
    elif found == OFF:
        log.warning("Network guard off: nothing stops this server connecting out to the internet. It still turns "
                    "away visitors outside the home network itself. On a Pi, see why in this log, and check "
                    "`sudo nft list table inet departureboard_web`")
    else:
        log.warning("Network guard: can't tell whether this server can connect out (no route to the internet?)")
    return found
