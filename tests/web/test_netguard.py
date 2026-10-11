"""Whether the web server can connect out, from what one datagram's send says."""

import errno
import ipaddress
import logging
import socket

import pytest

from departureboard.web import netguard


def test_it_sends_to_a_documentation_address_that_is_never_routed():
    address, port = netguard.PROBE_ADDRESS
    assert ipaddress.ip_address(address) in ipaddress.ip_network("192.0.2.0/24")  # TEST-NET-1, RFC 5737
    assert port == 9  # discard
    sent = []
    netguard.probe(lambda to: sent.append(to) or 0)
    assert sent == [netguard.PROBE_ADDRESS]


@pytest.mark.parametrize("code, said", [
    (errno.EPERM, netguard.ON),  # refused at once: nftables, or systemd's firewall
    (errno.EACCES, netguard.ON),
    (0, netguard.OFF),  # it went out
    (errno.ENETUNREACH, netguard.UNKNOWN),  # no route at all: nothing to see
    (errno.EHOSTUNREACH, netguard.UNKNOWN),
    (errno.EADDRNOTAVAIL, netguard.UNKNOWN),
    (errno.ENOBUFS, netguard.UNKNOWN),
])
def test_what_the_send_says_is_whether_the_guard_is_on(code, said):
    assert netguard.probe(lambda to: code) == said


def test_a_socket_that_cant_be_made_cant_tell():
    def broken(to):
        raise OSError(errno.EAFNOSUPPORT, "no IPv4")

    assert netguard.probe(broken) == netguard.UNKNOWN


class FakeSocket:
    def __init__(self, family, kind, refuse=None):
        self.family, self.kind, self.refuse = family, kind, refuse
        self.blocking, self.closed, self.sent = True, False, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def setblocking(self, flag):
        self.blocking = flag

    def sendto(self, data, address):
        if self.refuse is not None:
            raise PermissionError(self.refuse, "refused")
        self.sent.append((data, address))
        return len(data)


@pytest.mark.parametrize("refuse, code", [(None, 0), (errno.EPERM, errno.EPERM)])
def test_one_empty_datagram_without_waiting_then_the_socket_is_closed(monkeypatch, refuse, code):
    made = []
    monkeypatch.setattr(netguard.socket, "socket",
                        lambda family, kind: made.append(FakeSocket(family, kind, refuse)) or made[-1])
    assert netguard._send(netguard.PROBE_ADDRESS) == code
    [sock] = made
    assert (sock.family, sock.kind) == (socket.AF_INET, socket.SOCK_DGRAM)  # UDP: nothing to connect
    assert sock.blocking is False and sock.closed
    assert sock.sent == ([] if refuse else [(b"", netguard.PROBE_ADDRESS)])


def test_a_real_send_on_this_computer_goes():
    # To this computer's own discard port: never leaves it, and nothing guards it.
    assert netguard._send(("127.0.0.1", 9)) == 0


@pytest.mark.parametrize("code, level, words", [
    (errno.EPERM, logging.INFO, "Network guard on"),
    (0, logging.WARNING, "Network guard off"),
    (errno.ENETUNREACH, logging.WARNING, "can't tell"),
])
def test_the_check_says_what_it_found_in_the_log(caplog, code, level, words):
    caplog.set_level(logging.INFO)
    found = netguard.check(lambda to: code)
    assert found == netguard.probe(lambda to: code)
    [record] = caplog.records
    assert record.levelno == level and words in record.getMessage()
    if found == netguard.OFF:
        assert "still turns away visitors outside the home network" in record.getMessage()
