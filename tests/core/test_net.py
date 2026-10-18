import gzip
import io
import json
import threading
import time
import tracemalloc
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from tests.paths import FIXTURES
from twobells.core import net


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
    net.get_json_capped("https://example.test/x", None, 100)
    agent = seen["request"].get_header("User-agent")
    assert agent and agent.startswith("twobells/")
    assert "Python-urllib" not in agent


def test_keeps_caller_headers_and_asks_for_json(monkeypatch):
    seen = capture(monkeypatch)
    assert net.get_json_capped("https://example.test/x", {"x-apikey": "k"}, 100) == {"ok": True}
    request = seen["request"]
    assert request.get_header("X-apikey") == "k"
    assert request.get_header("Accept") == "application/json"


@pytest.fixture
def redirecting():
    """A local server: /elsewhere redirects to another host name for the same
    server, /same to the same host; /end records the headers it was sent.
    /heavy redirects to /end with a body of 50 MB, of which it sends 1 KB
    and then waits."""
    seen = []
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_address[1]
            if self.path == "/heavy":
                self.send_response(302)
                self.send_header("Location", "/end")
                self.send_header("Content-Length", "50000000")
                self.end_headers()
                self.wfile.write(b"x" * 1024)
                self.wfile.flush()
                done.wait(5)
                return
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
    done.set()
    server.shutdown()
    server.server_close()


def test_a_redirects_body_is_never_read(redirecting):
    # urllib read a redirect's body whole, whatever the cap: 200 MB of it took 265 MB.
    base, seen = redirecting
    start = time.monotonic()
    assert net.get_json_capped(base + "/heavy", None, 100, timeout=2.0, deadline=4.0) == {"ok": True}
    assert time.monotonic() - start < 2.0  # the rest of the body never came


def test_redirect_to_another_host_drops_the_api_key(redirecting):
    base, seen = redirecting
    assert net.get_json_capped(base + "/elsewhere", {"x-apikey": "secret"}, 100) == {"ok": True}
    sent = {name.lower(): value for name, value in seen[0].items()}
    assert "x-apikey" not in sent
    assert sent["user-agent"].startswith("twobells/")


def test_redirect_on_the_same_host_keeps_headers(redirecting):
    base, seen = redirecting
    assert net.get_json_capped(base + "/same", {"x-apikey": "secret"}, 100) == {"ok": True}
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
    assert request.get_header("User-agent").startswith("twobells/")


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
    net.get_bytes("https://example.test/x", 100, headers={"User-Agent": "twobells/0.1 (+contact)"})
    assert seen["request"].get_header("User-agent") == "twobells/0.1 (+contact)"


# A deadline for the whole request.


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_a_deadline_stops_a_server_that_sends_slowly(dripping, path):
    # Each read gets a byte well within its 1 s timeout, so only the deadline stops it.
    start = time.monotonic()
    with pytest.raises(OSError):
        net.get_bytes(dripping + path, 1_000, timeout=1.0, deadline=0.3)
    assert time.monotonic() - start < 1.0


class FastClock:
    """time.monotonic, but a second passes each time it is read: a long wait, quickly."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        self.offset += 1.0
        return time.monotonic() + self.offset


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_get_bytes_gives_up_on_a_slow_server_unless_told_otherwise(dripping, monkeypatch, path):
    # Bus positions, weather and bank holidays come through get_bytes: a
    # trickled answer mustn't hang `run --loop`.
    monkeypatch.setattr(net, "time", SimpleNamespace(monotonic=FastClock().monotonic))
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        net.get_bytes(dripping + path, 1_000, timeout=1.0)
    assert time.monotonic() - start < 4.0


def test_get_bytes_has_the_usual_deadline_unless_told_otherwise(monkeypatch):
    seen = serve(monkeypatch, b"body")
    before = time.monotonic()
    net.get_bytes("https://example.test/x", 100)
    assert before < seen["request"].deadline <= time.monotonic() + net.DEADLINE
    net.get_bytes("https://example.test/x", 100, deadline=None)
    assert seen["request"].deadline is None


def test_a_request_within_its_deadline_is_answered(dripping):
    assert net.get_bytes(dripping + "/quick", 100, timeout=1.0, deadline=5.0) == b"ok"


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_stream_gives_up_on_a_server_that_sends_slowly(drip_to, path):
    drip_to(path)
    start = time.monotonic()
    with pytest.raises(OSError):
        list(net.stream("https://example.test/feed", {}, 1_000, timeout=1.0, deadline=0.3))
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize("path", ["/body", "/headers", "/moved"])
def test_stream_gives_up_on_a_slow_server_unless_told_otherwise(drip_to, monkeypatch, path):
    # The dashboard's disruption feeds come through stream: a trickled answer
    # mustn't tie up their poller for ever.
    drip_to(path)
    monkeypatch.setattr(net, "time", SimpleNamespace(monotonic=FastClock().monotonic))
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        list(net.stream("https://example.test/feed", {}, 1_000, timeout=1.0))
    assert time.monotonic() - start < 4.0


def test_stream_has_the_usual_deadline_unless_told_otherwise(monkeypatch):
    seen = serve(monkeypatch, b"body")
    before = time.monotonic()
    assert b"".join(net.stream("https://example.test/x", {}, 100)) == b"body"
    assert before < seen["request"].deadline <= time.monotonic() + net.DEADLINE
    list(net.stream("https://example.test/x", {}, 100, deadline=None))
    assert seen["request"].deadline is None


def test_a_timed_request_carries_the_agent_and_its_deadline():
    before = time.monotonic()
    request = net.timed_request("https://example.test/x", {"Accept": "text/xml"}, 5.0)
    assert request.get_header("User-agent") == net.USER_AGENT
    assert request.get_header("Accept") == "text/xml"
    assert before + 5.0 <= request.deadline <= time.monotonic() + 5.0
    assert net.timed_request("https://example.test/x", {}, None).deadline is None


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
    assert before < seen["request"].deadline <= time.monotonic() + net.DEADLINE



@pytest.mark.parametrize("url", ["file:///etc/hosts", "ftp://example.test/x", "data:,hello"])
def test_only_http_and_https_are_opened(url):
    # Through the opener itself: conftest stands in for urlopen to keep tests offline.
    with pytest.raises(URLError, match="unknown url type"):
        net._opener.open(Request(url), timeout=1)


# Compressed answers. The Rail Data Marketplace sends its Knowledgebase
# incidents gzipped (Content-Encoding: gzip) though nothing asks it to.

INCIDENTS = (FIXTURES / "kb_incidents_synthetic.xml").read_bytes()
# 64 MB of nothing in about 64 kB: inflated in full, it would show.
BOMB = gzip.compress(bytes(64 * 1024 * 1024), compresslevel=9)


@pytest.mark.parametrize("encoding, compress", [
    ("gzip", gzip.compress), ("x-gzip", gzip.compress), ("GZIP", gzip.compress), ("deflate", zlib.compress),
])
def test_get_bytes_reads_a_compressed_answer(encoded, encoding, compress):
    url = encoded(compress(INCIDENTS), encoding)
    assert net.get_bytes(url, len(INCIDENTS)) == INCIDENTS


@pytest.mark.parametrize("encoding", [None, "identity", " Identity "])
def test_an_answer_sent_as_it_is_is_read_as_it_is(encoded, encoding):
    assert net.get_bytes(encoded(INCIDENTS, encoding), len(INCIDENTS)) == INCIDENTS


def test_a_gzipped_answer_in_several_members_is_read_whole(encoded):
    body = gzip.compress(INCIDENTS[:1000]) + gzip.compress(INCIDENTS[1000:])
    assert net.get_bytes(encoded(body, "gzip"), len(INCIDENTS)) == INCIDENTS


def test_capped_json_reads_a_gzipped_answer(encoded):
    answer = {"trainServices": [{"std": "10:15", "destination": "Manchester Piccadilly"}]}
    assert net.get_json_capped(encoded(gzip.compress(json.dumps(answer).encode()), "gzip"), None, 1000) == answer


def test_stream_reads_a_gzipped_answer(encoded_to):
    big = INCIDENTS * 40  # more than one chunk, decoded
    encoded_to(gzip.compress(big), "gzip")
    assert b"".join(net.stream("https://example.test/incidents.xml", {}, len(big))) == big


def test_the_cap_is_on_the_decoded_size(encoded, encoded_to):
    small = gzip.compress(INCIDENTS)
    assert len(small) < len(INCIDENTS) // 2
    with pytest.raises(net.TooLarge):
        net.get_bytes(encoded(small, "gzip"), len(INCIDENTS) - 1)
    with pytest.raises(net.TooLarge):
        net.get_json_capped(encoded(gzip.compress(b'{"a": "' + b"x" * 500 + b'"}'), "gzip"), None, 100)
    encoded_to(small, "gzip")
    with pytest.raises(net.TooLarge):
        list(net.stream("https://example.test/incidents.xml", {}, len(INCIDENTS) - 1))


def peak_while(call) -> int:
    """The most memory allocated at once while `call` runs, in bytes."""
    tracemalloc.start()
    try:
        call()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("reader", ["get_bytes", "get_json_capped", "stream"])
def test_a_gzip_bomb_is_refused_without_being_inflated(encoded, encoded_to, reader):
    cap = 1_000_000
    url = encoded(BOMB, "gzip")
    encoded_to(BOMB, "gzip")  # stream opens only https, so it is sent there
    calls = {
        "get_bytes": lambda: net.get_bytes(url, cap),
        "get_json_capped": lambda: net.get_json_capped(url, None, cap),
        "stream": lambda: list(net.stream("https://example.test/bomb", {}, cap)),
    }

    def read():
        with pytest.raises(net.TooLarge):
            calls[reader]()

    assert peak_while(read) < 8 * cap  # a small part of the 64 MB it would make


@pytest.mark.parametrize("encoding", ["br", "compress", "gzip, br", "zstd"])
def test_an_unknown_encoding_is_refused_plainly(encoded, encoded_to, encoding):
    url = encoded(b"\x00\x01", encoding)
    with pytest.raises(net.BadEncoding, match="sent its answer in a form this app can't read"):
        net.get_bytes(url, 100)
    with pytest.raises(net.BadEncoding):
        net.get_json_capped(url, None, 100)
    encoded_to(b"\x00\x01", encoding)
    with pytest.raises(net.BadEncoding):
        list(net.stream("https://example.test/incidents.xml", {}, 100))


@pytest.mark.parametrize("body", [
    b"not gzip at all",
    gzip.compress(INCIDENTS)[:200],  # cut short
    gzip.compress(INCIDENTS) + b"trailing rubbish",
], ids=["not gzip", "cut short", "trailing rubbish"])
def test_a_broken_compressed_answer_is_refused_plainly(encoded, body):
    with pytest.raises(net.BadEncoding, match="broken or cut short") as caught:
        net.get_bytes(encoded(body, "gzip"), 1_000_000)
    # No zlib error chained either: one plain message.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_an_errors_body_is_decoded_and_it_keeps_its_status(encoded):
    # The key test reads a refusal's body for clues.
    url = encoded(gzip.compress(b"Invalid ApiKey for given resource"), "gzip", status=401)
    with pytest.raises(HTTPError) as caught:
        net.get_bytes(url, 100)
    assert caught.value.code == 401
    assert caught.value.read(4096) == b"Invalid ApiKey for given resource"


def test_an_error_in_an_unknown_encoding_keeps_its_status(encoded):
    with pytest.raises(HTTPError) as caught:
        net.get_bytes(encoded(b"\x00\x01", "br", status=503), 100)
    assert caught.value.code == 503


def test_compression_is_not_asked_for(encoded):
    # What the app asks for is unchanged; it reads what it is sent anyway.
    net.get_bytes(encoded(gzip.compress(INCIDENTS), "gzip"), len(INCIDENTS))
    assert encoded.seen[0]["accept-encoding"] == "identity"


def test_a_deadline_stops_a_gzipped_answer_sent_slowly(encoded):
    url = encoded(gzip.compress(INCIDENTS), "gzip", drip=True)
    start = time.monotonic()
    with pytest.raises(OSError):
        net.get_bytes(url, 1_000_000, timeout=1.0, deadline=0.3)
    assert time.monotonic() - start < 1.0


def test_a_zip_download_arrives_as_it_was_sent(encoded_to):
    # A timetable zip is a file, not a Content-Encoding: it is never unpacked on the way.
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as folder:
        folder.writestr("timetable.xml", INCIDENTS)
    encoded_to(archive.getvalue(), None)
    assert b"".join(net.stream("https://example.test/timetable.zip", {}, 1_000_000)) == archive.getvalue()
