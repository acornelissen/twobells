import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from twobells.core import net
from twobells.settings import store

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No test reaches the internet: the board's way out (http.urlopen) fails
    for anything but this machine, unless a test puts its own fake there.
    Building a timetable asks the road router by default, so this keeps those
    tests offline and quick."""
    real = net.urlopen

    def offline(request, timeout):
        if urlsplit(request.full_url).hostname in _LOOPBACK:
            return real(request, timeout)
        raise OSError("No network in tests")

    monkeypatch.setattr(net, "urlopen", offline)


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


# A server that sends very slowly, for the deadlines.


@pytest.fixture
def dripping():
    """A local server that sends very slowly: /body sends its headers, then a
    byte every 50 ms; /headers sends a header line every 50 ms; /quick
    answers at once; /moved redirects to /body. Each gives up after 5 s."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/quick":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                elif self.path == "/moved":
                    self.send_response(302)
                    self.send_header("Location", "/body")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/body":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.flush()
                    self.drip(b"x")
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    self.drip(b"X-Pad: x\r\n")
            except OSError:
                pass  # the client gave up

        def drip(self, piece):
            for _ in range(100):
                self.wfile.write(piece)
                self.wfile.flush()
                threading.Event().wait(0.05)

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def drip_to(dripping, monkeypatch):
    """`drip_to(path)` sends every request from then on to the dripping
    server's `path`, so code that opens only https can meet a slow server.
    The request keeps its own deadline."""

    def to(path):
        def opener(request, timeout):
            request.full_url = dripping + path
            return net._opener.open(request, timeout=timeout)

        monkeypatch.setattr(net, "urlopen", opener)

    return to


# A server whose answers are compressed, for Content-Encoding.


@pytest.fixture
def encoded():
    """A local server sending compressed answers: `encoded(body, encoding)`
    gives the address of an answer with `body` as sent (already compressed)
    and a Content-Encoding of `encoding` (None: no header). `status` sets the
    answer's status; `drip=True` sends the body a byte every 50 ms.
    `encoded.seen` has the headers of each request it was sent."""
    answers = {}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({name.lower(): value for name, value in self.headers.items()})
            status, body, encoding, drip = answers[self.path]
            try:
                self.send_response(status)
                self.send_header("Content-Type", "text/xml")
                if encoding is not None:
                    self.send_header("Content-Encoding", encoding)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not drip:
                    self.wfile.write(body)
                    return
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    threading.Event().wait(0.05)
            except OSError:
                pass  # the client gave up

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def answer(body: bytes, encoding: str | None, status: int = 200, drip: bool = False) -> str:
        path = f"/{len(answers)}"
        answers[path] = (status, body, encoding, drip)
        return base + path

    answer.seen = seen  # pyright: ignore[reportFunctionMemberAccess]
    yield answer
    server.shutdown()
    server.server_close()


@pytest.fixture
def encoded_to(encoded, monkeypatch):
    """`encoded_to(body, encoding)` sends every request from then on to that
    answer of the `encoded` server, so code that opens only https can meet it."""

    def to(body: bytes, encoding: str | None, status: int = 200, drip: bool = False) -> None:
        url = encoded(body, encoding, status, drip)

        def opener(request, timeout):
            request.full_url = url
            return net._opener.open(request, timeout=timeout)

        monkeypatch.setattr(net, "urlopen", opener)

    return to
