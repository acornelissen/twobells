"""Whether the web server can connect out, from what one datagram's send says."""

import errno
import ipaddress
import logging
import socket

import pytest

from twobells.web import netguard


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
    found = netguard.Guard(lambda to: code).check()
    assert found == netguard.probe(lambda to: code)
    [record] = caplog.records
    assert record.levelno == level and words in record.getMessage()
    if found == netguard.OFF:
        assert "still turns away visitors outside the home network" in record.getMessage()
        # Where to look on a Pi: the guard's unit, and the set systemd puts the server in.
        assert "journalctl -u twobells-guard -u twobells-web" in record.getMessage()
        assert "`sudo nft list set inet twobells_web web`" in record.getMessage()


# IPv6, when IPv4 can't tell


def test_ipv6_goes_to_a_documentation_address_too():
    address, port = netguard.PROBE_ADDRESS_V6
    assert ipaddress.ip_address(address) in ipaddress.ip_network("2001:db8::/32")  # documentation, RFC 3849
    assert port == 9


@pytest.mark.parametrize("v4, v6, said", [
    (errno.ENETUNREACH, errno.EPERM, netguard.ON),  # an IPv6-only network, guarded
    (errno.ENETUNREACH, 0, netguard.OFF),  # an IPv6-only network, nothing stopping it
    (errno.ENETUNREACH, errno.ENETUNREACH, netguard.UNKNOWN),  # no route either way
    (errno.EADDRNOTAVAIL, errno.EACCES, netguard.ON),
])
def test_when_ipv4_cant_tell_ipv6_is_asked(v4, v6, said):
    sent = []

    def send(to):
        sent.append(to)
        return v4 if to == netguard.PROBE_ADDRESS else v6

    assert netguard.probe(send) == said
    assert sent == [netguard.PROBE_ADDRESS, netguard.PROBE_ADDRESS_V6]


@pytest.mark.parametrize("v4", [errno.EPERM, 0])
def test_ipv6_isnt_asked_when_ipv4_can_tell(v4):
    sent = []
    netguard.probe(lambda to: sent.append(to) or v4)
    assert sent == [netguard.PROBE_ADDRESS]


def test_no_ipv4_socket_still_asks_ipv6():
    def send(to):
        if to == netguard.PROBE_ADDRESS:
            raise OSError(errno.EAFNOSUPPORT, "no IPv4")
        return errno.EPERM

    assert netguard.probe(send) == netguard.ON


def test_an_ipv6_address_is_sent_from_an_ipv6_socket(monkeypatch):
    made = []
    monkeypatch.setattr(netguard.socket, "socket",
                        lambda family, kind: made.append(FakeSocket(family, kind)) or made[-1])
    assert netguard._send(netguard.PROBE_ADDRESS_V6) == 0
    [sock] = made
    assert (sock.family, sock.kind) == (socket.AF_INET6, socket.SOCK_DGRAM)
    assert sock.blocking is False and sock.closed
    assert sock.sent == [(b"", netguard.PROBE_ADDRESS_V6)]


# Checked again, at most once a minute


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_the_guard_is_checked_again_once_its_last_check_is_a_minute_old():
    codes, sent, clock = [errno.ENETUNREACH], [], Clock()

    def send(to):
        sent.append(to)
        return codes[0]

    guard = netguard.Guard(send, clock)
    assert guard.check() == netguard.UNKNOWN  # Wi-Fi still connecting at start
    started = len(sent)
    codes[0] = errno.EPERM
    clock.now += 59
    assert guard.latest() == (netguard.UNKNOWN, 1_000_000.0)  # too soon: nothing sent
    assert len(sent) == started
    clock.now += 1
    assert guard.latest() == (netguard.ON, 1_000_060.0)
    assert sent[started:] == [netguard.PROBE_ADDRESS]  # one send
    assert guard.latest() == (netguard.ON, 1_000_060.0)
    assert len(sent) == started + 1


def test_a_flushed_ruleset_shows_on_the_next_look():
    codes, clock = [errno.EPERM], Clock()
    guard = netguard.Guard(lambda to: codes[0], clock)
    assert guard.check() == netguard.ON
    codes[0] = 0  # sudo nft flush ruleset
    clock.now += 60
    assert guard.latest()[0] == netguard.OFF


def test_a_clock_put_back_checks_again():
    # A Pi has no clock of its own: it can jump when the time is first set.
    sent, clock = [], Clock()
    guard = netguard.Guard(lambda to: sent.append(to) or errno.EPERM, clock)
    guard.check()
    clock.now -= 3600
    guard.latest()
    assert len(sent) == 2


def test_the_first_look_checks_when_nothing_has():
    clock = Clock()
    guard = netguard.Guard(lambda to: errno.EPERM, clock)
    assert guard.latest() == (netguard.ON, clock.now)


def test_only_a_change_is_said_in_the_log(caplog):
    caplog.set_level(logging.INFO)
    codes, clock = [errno.EPERM], Clock()
    guard = netguard.Guard(lambda to: codes[0], clock)
    guard.check()
    clock.now += 60
    guard.latest()
    assert len(caplog.records) == 1  # the same again: not said twice
    codes[0] = 0
    clock.now += 60
    guard.latest()
    assert len(caplog.records) == 2 and "Network guard off" in caplog.records[-1].getMessage()
