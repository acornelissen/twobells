import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from departureboard.core import net


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def capture(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(net, "urlopen", fake_urlopen)
    return seen


def test_sends_a_named_user_agent(monkeypatch):
    # The Rail Data Marketplace gateway returns 403 to urllib's default agent.
    seen = capture(monkeypatch)
    net.get_json("https://example.test/x")
    agent = seen["request"].get_header("User-agent")
    assert agent and agent.startswith("inky-departures/")
    assert "Python-urllib" not in agent


def test_keeps_caller_headers_and_asks_for_json(monkeypatch):
    seen = capture(monkeypatch)
    assert net.get_json("https://example.test/x", {"x-apikey": "k"}) == {"ok": True}
    request = seen["request"]
    assert request.get_header("X-apikey") == "k"
    assert request.get_header("Accept") == "application/json"


@pytest.fixture
def redirecting():
    """A local server: /elsewhere redirects to another host name for the same
    server, /same to the same host; /end records the headers it was sent."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            if self.path in ("/elsewhere", "/same"):
                host = "localhost" if self.path == "/elsewhere" else "127.0.0.1"
                self.send_response(302)
                self.send_header("Location", f"http://{host}:{port}/end")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            seen.append(dict(self.headers))
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()
    server.server_close()


def test_redirect_to_another_host_drops_the_api_key(redirecting):
    base, seen = redirecting
    assert net.get_json(base + "/elsewhere", {"x-apikey": "secret"}) == {"ok": True}
    sent = {name.lower(): value for name, value in seen[0].items()}
    assert "x-apikey" not in sent
    assert sent["user-agent"].startswith("inky-departures/")


def test_redirect_on_the_same_host_keeps_headers(redirecting):
    base, seen = redirecting
    assert net.get_json(base + "/same", {"x-apikey": "secret"}) == {"ok": True}
    assert {name.lower(): value for name, value in seen[0].items()}["x-apikey"] == "secret"


def redirect(old_url, new_url, headers=None):
    request = Request(old_url, headers={"User-Agent": net.USER_AGENT, **(headers or {})})
    return net.SafeRedirects().redirect_request(request, io.BytesIO(), 302, "Found", {}, new_url)


@pytest.mark.parametrize("new_url", ["http://api.example/next", "http://other.example/next"])
def test_refuses_a_redirect_from_https_to_http(new_url):
    with pytest.raises(HTTPError, match="https to http"):
        redirect("https://api.example/x", new_url, {"x-apikey": "k"})


def test_https_redirect_to_another_port_counts_as_another_host():
    followed = redirect("https://api.example/x", "https://api.example:8443/x", {"x-apikey": "k"})
    assert followed.get_header("X-apikey") is None
    assert followed.get_header("User-agent") == net.USER_AGENT


def test_https_redirect_to_the_same_host_keeps_the_key():
    followed = redirect("https://api.example/x", "https://API.example:443/y", {"x-apikey": "k"})
    assert followed.get_header("X-apikey") == "k"


def serve(monkeypatch, body: bytes):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        return FakeResponse(body)

    monkeypatch.setattr(net, "urlopen", fake_urlopen)
    return seen


SECRET_URL = "https://example.test/board/GLO?key=hunter2&numRows=10"


def test_capped_json_reads_a_small_body_with_the_callers_headers(monkeypatch):
    seen = serve(monkeypatch, b'{"ok": true}')
    assert net.get_json_capped(SECRET_URL, {"x-apikey": "k"}, max_bytes=100) == {"ok": True}
    request = seen["request"]
    assert request.get_header("X-apikey") == "k"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent").startswith("inky-departures/")


def test_capped_json_refuses_a_body_over_the_cap(monkeypatch):
    serve(monkeypatch, b'{"pad": "' + b"x" * 200 + b'"}')
    with pytest.raises(ValueError, match="larger than 100 bytes") as caught:
        net.get_json_capped(SECRET_URL, {"x-apikey": "k"}, max_bytes=100)
    assert "hunter2" not in str(caught.value) and "example.test" not in str(caught.value)


def test_capped_json_takes_a_body_exactly_at_the_cap(monkeypatch):
    body = b'{"pad": "' + b"x" * 50 + b'"}'
    serve(monkeypatch, body)
    assert net.get_json_capped(SECRET_URL, None, max_bytes=len(body)) == {"pad": "x" * 50}


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 10_000 + b"]" * 10_000,  # deep enough to blow the decoder's stack
        b"<html>Service Unavailable</html>",
        b"",
        b'{"name": "Gl\xffssop"}',  # not UTF-8
        '{"a": 1}'.encode("utf-16"),
        b'{"a": 1',
    ],
)
def test_capped_json_refuses_anything_but_json_with_one_clear_error(monkeypatch, body):
    serve(monkeypatch, body)
    with pytest.raises(ValueError) as caught:
        net.get_json_capped(SECRET_URL, {"x-apikey": "k"}, max_bytes=1_000_000)
    assert str(caught.value) == "Response is not valid JSON"
    # Nothing chained either: a decoder's message can quote the body.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_get_bytes_adds_the_callers_headers_and_may_replace_the_agent(monkeypatch):
    seen = serve(monkeypatch, b"body")
    assert net.get_bytes("https://example.test/x", 100) == b"body"
    assert seen["request"].get_header("User-agent") == net.USER_AGENT
    net.get_bytes("https://example.test/x", 100, headers={"User-Agent": "inky-departures/0.1 (+contact)"})
    assert seen["request"].get_header("User-agent") == "inky-departures/0.1 (+contact)"


# A deadline for the whole request.


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


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_a_deadline_stops_a_server_that_sends_slowly(dripping, path):
    # Each read gets a byte well within its 1 s timeout, so only the deadline stops it.
    start = time.monotonic()
    with pytest.raises(OSError):
        net.get_bytes(dripping + path, 1_000, timeout=1.0, deadline=0.3)
    assert time.monotonic() - start < 1.0


def test_a_request_within_its_deadline_is_answered(dripping):
    assert net.get_bytes(dripping + "/quick", 100, timeout=1.0, deadline=5.0) == b"ok"


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_capped_json_gives_up_on_a_server_that_sends_slowly(dripping, path):
    start = time.monotonic()
    with pytest.raises(OSError):
        net.get_json_capped(dripping + path, None, 1_000, timeout=1.0, deadline=0.3)
    assert time.monotonic() - start < 1.0


def test_capped_json_has_a_deadline_unless_told_otherwise(monkeypatch):
    # The board's fetches rely on it: a trickled answer mustn't hang `run --loop`.
    seen = serve(monkeypatch, b'{"ok": true}')
    before = time.monotonic()
    net.get_json_capped(SECRET_URL, None, max_bytes=100)
    assert before < seen["request"].deadline <= time.monotonic() + net.JSON_DEADLINE



@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://example.test/x", "data:,hello"])
def test_only_http_and_https_are_opened(url):
    # Through the opener itself: conftest stands in for urlopen to keep tests offline.
    with pytest.raises(URLError, match="unknown url type"):
        net._opener.open(Request(url), timeout=1)
