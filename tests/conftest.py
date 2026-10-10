from urllib.parse import urlsplit

import pytest

from departureboard import http, store

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


@pytest.fixture(autouse=True)
def own_home(tmp_path_factory, monkeypatch):
    """No test finds, reads or writes the real user's store: a config.toml
    that names no folder leads to the platform's folder in a home of the
    test's own, and nothing says systemd chose one. HOME itself is left
    alone, for the tools tests run (node, through mise)."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(store, "_home", lambda: home)
    for name in ("XDG_STATE_HOME", "STATE_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)
