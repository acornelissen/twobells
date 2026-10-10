from urllib.parse import urlsplit

import pytest

from departureboard import http

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches the internet: the board's way out (http.urlopen) fails
    for anything but this machine, unless a test puts its own fake there.
    Building a timetable asks the road router by default, so this keeps those
    tests offline and quick."""
    real = http.urlopen

    def offline(request, timeout):
        if urlsplit(request.full_url).hostname in _LOOPBACK:
            return real(request, timeout)
        raise OSError("No network in tests")

    monkeypatch.setattr(http, "urlopen", offline)
