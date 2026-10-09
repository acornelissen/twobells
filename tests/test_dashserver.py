import hashlib
import http.client
import json
import os
import socket
import threading
import time

import pytest

from departureboard.dashserver import DashboardApp, StaticFiles, make_server
from departureboard.snapshot import Hub
from departureboard.walk import WalkTimes

# The contract's headers, copied from docs/dashboard-api.md rather than imported.
SECURITY = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: "
        "https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

BUS_ID = "4b7e000000000001"
TRAIN_ID = "9f1c000000000002"
STATIC = {
    "index.html": b"<!doctype html><title>Departures</title>",
    "app.css": b"body{}",
    "main.js": b"import './x.mjs';",
    "x.mjs": b"export {};",
    "icon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    "icon.png": b"\x89PNG\r\n",
    "manifest.webmanifest": b'{"name":"Departures"}',
    "data.json": b"{}",
    "LICENSE.txt": b"MIT",
    "vendor/leaflet-1.9.4/leaflet.js": b"// leaflet",
}


class Recent:
    def __init__(self):
        self.asked = []
        self.error = None

    def __call__(self, mode, route, scheduled, days):
        self.asked.append((mode, route, scheduled, days))
        if self.error:
            raise self.error
        return [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": True, "reason": None}]


@pytest.fixture
def static_dir(tmp_path):
    folder = tmp_path / "static"
    for name, body in STATIC.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (folder / ".secret.js").write_bytes(b"hidden")
    (folder / "tool.py").write_bytes(b"print()")
    (folder / ".git").mkdir()
    (folder / ".git" / "config.txt").write_bytes(b"hidden")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not for you")
    os.symlink(outside, folder / "link.txt")
    return folder


class Running:
    def __init__(self, app, **limits):
        self.app = app
        self.server = make_server(app, "127.0.0.1", 0, **limits)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def request(self, path, method="GET", host="localhost", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        response = conn.getresponse()
        body = response.read() if not (path == "/api/events" and response.status == 200 and method == "GET") else b""
        conn.close()
        return response, body

    def stream(self, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/events", headers={"Host": "localhost", **(headers or {})})
        return conn, conn.getresponse()

    def close(self):
        self.app.hub.close()
        self.server.shutdown()
        self.server.server_close()


def make_app(static_dir, **overrides):
    hub = Hub("Glossop", WalkTimes(train=12))
    hub.publish("buses", {"rows": [{"id": BUS_ID}]}, {
        BUS_ID: {
            "bus": {"id": BUS_ID, "line": "341", "destination": "Hyde", "stop": "A", "stops": [], "vehicle": None,
                    "where": "No live position"},
            "history": {"mode": "bus", "route": "A|341|Hyde", "scheduled": "07:20", "label": "07:20 341 to Hyde",
                        "day_type": "weekday", "phrase": None},
        },
    })
    hub.publish("trains", {"rows": [{"id": TRAIN_ID}]}, {
        TRAIN_ID: {"history": {"mode": "train", "route": "GLO>MAN|Manchester Piccadilly", "scheduled": "07:12",
                               "label": "07:12 to Manchester Piccadilly", "day_type": "weekday",
                               "phrase": "Late 4 of the last 10 weekdays (avg 6 min)"}},
    })
    settings = {
        "hub": hub,
        "static": StaticFiles.load(static_dir),
        "healthy": lambda: True,
        "recent": Recent(),
        "history_days": 90,
        "allowed_hosts": frozenset({"departures.example.com"}),
        "heartbeat": 0.2,
        "stream_seconds": 30.0,
    }
    settings.update(overrides)
    return DashboardApp(**settings)


@pytest.fixture
def server(static_dir):
    running = Running(make_app(static_dir))
    yield running
    running.close()


def start(static_dir, limits=None, **overrides):
    return Running(make_app(static_dir, **overrides), **(limits or {}))


def assert_secure(response):
    for name, value in SECURITY.items():
        assert response.getheader(name) == value, name
    assert "Python" not in (response.getheader("Server") or "")


# Headers on everything


@pytest.mark.parametrize("path, method, host, status", [
    ("/api/snapshot", "GET", "localhost", 200),
    ("/nowhere", "GET", "localhost", 404),
    ("/api/snapshot", "GET", "evil.com", 403),
    ("/api/snapshot", "POST", "localhost", 405),
    ("/static/app.css", "HEAD", "localhost", 200),
])
def test_every_response_carries_the_security_headers(server, path, method, host, status):
    response, _ = server.request(path, method, host)
    assert response.status == status
    assert_secure(response)


def test_http_server_errors_carry_them_too(server):
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw:
        raw.sendall(b"GET / HTTP/1.0\r\n" + b"X: y\r\n" * 101 + b"\r\n")
        reply = b""
        while chunk := raw.recv(4096):
            reply += chunk
    head = reply.decode("latin-1")
    assert head.startswith("HTTP/1.0 431")
    assert "Content-Security-Policy: default-src 'none'" in head and "<html" not in head.lower()


def test_other_methods_are_not_allowed(server):
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "BREW"):
        response, _ = server.request("/api/snapshot", method)
        assert response.status == 405, method
        assert response.getheader("Allow") == "GET, HEAD"


# Who may ask


@pytest.mark.parametrize("host", ["localhost", "localhost:8090", "nas", "nas.local", "192.168.1.5:8090",
                                  "127.0.0.1:8090", "departures.example.com", "Departures.Example.com:8090"])
def test_local_and_allowed_hosts_are_served(server, host):
    response, _ = server.request("/api/snapshot", host=host)
    assert response.status == 200


@pytest.mark.parametrize("host", ["evil.com", "x.synology.me", "", "8.8.8.8", "localhost.evil.com",
                                  "evil.departures.example.com"])
def test_other_hosts_are_refused(server, host):
    response, _ = server.request("/api/snapshot", host=host)
    assert response.status == 403


def test_a_request_without_a_host_or_with_two_is_refused(server):
    assert server.request("/api/snapshot", host=None)[0].status == 403
    response, _ = server.request("/api/snapshot", headers={"Host": "evil.com"})
    assert response.status == 403


def test_a_client_outside_the_home_network_is_refused(static_dir):
    running = start(static_dir, client_allowed=lambda address: False)
    try:
        assert running.request("/")[0].status == 403
    finally:
        running.close()


def test_api_calls_from_another_site_are_refused(server):
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert server.request("/api/snapshot", headers=cross)[0].status == 403
    assert server.request("/api/snapshot", headers={"Sec-Fetch-Site": "same-origin"})[0].status == 200
    assert server.request("/static/app.css", headers=cross)[0].status == 200  # an image or link elsewhere is fine


# Paths


@pytest.mark.parametrize("path", [
    "/static/../dashserver.py", "/static/%2e%2e/x", "/static/%2E%2E/x", "/static/a%2fb", "/static/a%2Fb",
    "/static\\app.css", "/static/..%5capp.css", "/static/app.css%00", "/" + "a" * 512,
    "http://localhost/api/snapshot", "/static/.secret.js", "/static/.git/config.txt", "/static/tool.py",
    "/static/link.txt", "/static/", "/static//app.css", "/static/app.css/", "/index.html",
])
def test_paths_outside_the_allowlist_are_not_found(server, path):
    response, body = server.request(path)
    assert response.status == 404
    assert b"not for you" not in body and b"hidden" not in body


def test_a_query_string_is_ignored(server):
    assert server.request("/api/snapshot?x=1")[0].status == 200


# Static files


@pytest.mark.parametrize("name, content_type", [
    ("index.html", "text/html; charset=utf-8"),
    ("app.css", "text/css; charset=utf-8"),
    ("main.js", "text/javascript; charset=utf-8"),
    ("x.mjs", "text/javascript; charset=utf-8"),
    ("icon.svg", "image/svg+xml"),
    ("icon.png", "image/png"),
    ("manifest.webmanifest", "application/manifest+json"),
    ("data.json", "application/json"),
    ("LICENSE.txt", "text/plain; charset=utf-8"),
    ("vendor/leaflet-1.9.4/leaflet.js", "text/javascript; charset=utf-8"),
])
def test_static_files_have_their_own_types(server, name, content_type):
    response, body = server.request(f"/static/{name}")
    assert response.status == 200 and body == STATIC[name]
    assert response.getheader("Content-Type") == content_type
    assert response.getheader("ETag") == f'"{hashlib.sha256(STATIC[name]).hexdigest()}"'
    assert response.getheader("Cache-Control") == "no-cache"


def test_the_page_and_the_manifest_have_their_own_paths(server):
    response, body = server.request("/")
    assert response.status == 200 and body == STATIC["index.html"]
    response, body = server.request("/manifest.webmanifest")
    assert response.getheader("Content-Type") == "application/manifest+json" and body == STATIC["manifest.webmanifest"]


def test_an_unchanged_static_file_is_not_sent_again(server):
    etag = server.request("/static/app.css")[0].getheader("ETag")
    response, body = server.request("/static/app.css", headers={"If-None-Match": etag})
    assert response.status == 304 and body == b""
    assert response.getheader("ETag") == etag
    assert_secure(response)
    assert server.request("/static/app.css", headers={"If-None-Match": '"other"'})[0].status == 200


def test_head_sends_the_length_without_the_body(server):
    response, body = server.request("/static/app.css", "HEAD")
    assert response.status == 200 and body == b""
    assert response.getheader("Content-Length") == str(len(STATIC["app.css"]))


def test_files_are_read_once_at_start(server, static_dir):
    (static_dir / "app.css").write_bytes(b"changed")
    (static_dir / "new.css").write_bytes(b"new")
    assert server.request("/static/app.css")[1] == STATIC["app.css"]
    assert server.request("/static/new.css")[0].status == 404


def test_without_a_front_end_the_page_says_so(tmp_path):
    running = start(tmp_path / "missing")
    try:
        response, body = running.request("/")
        assert response.status == 200 and b"Front end not built" in body
        assert_secure(response)
        assert running.request("/static/app.css")[0].status == 404
        assert running.request("/api/snapshot")[0].status == 200
    finally:
        running.close()


# The snapshot


def test_the_snapshot_has_an_etag_of_its_version(server):
    response, body = server.request("/api/snapshot")
    snapshot = json.loads(body)
    assert response.getheader("ETag") == f'"v{snapshot["version"]}"'
    assert response.getheader("Content-Type") == "application/json"
    assert snapshot["title"] == "Glossop"


def test_an_unchanged_snapshot_is_not_sent_again(server):
    etag = server.request("/api/snapshot")[0].getheader("ETag")
    response, body = server.request("/api/snapshot", headers={"If-None-Match": etag})
    assert response.status == 304 and body == b""
    server.app.hub.publish("weather", {"rain": "Rain from 08:00 (70%)"})
    response, body = server.request("/api/snapshot", headers={"If-None-Match": etag})
    assert response.status == 200 and json.loads(body)["weather"]["rain"] == "Rain from 08:00 (70%)"


def test_api_calls_count_as_a_browser_about(server):
    assert not server.app.hub.active(600)
    server.request("/static/app.css")
    assert not server.app.hub.active(600)
    server.request("/api/snapshot")
    assert server.app.hub.active(600)


# Rows


def test_a_bus_s_map(server):
    response, body = server.request(f"/api/bus/{BUS_ID}")
    assert response.status == 200 and json.loads(body)["line"] == "341"
    assert response.getheader("Cache-Control") == "no-store"


def test_a_row_s_history(server):
    response, body = server.request(f"/api/history/{TRAIN_ID}")
    assert response.status == 200
    assert json.loads(body) == {
        "id": TRAIN_ID, "mode": "train", "label": "07:12 to Manchester Piccadilly", "day_type": "weekday",
        "phrase": "Late 4 of the last 10 weekdays (avg 6 min)",
        "days": [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": True, "reason": None}],
    }
    assert server.app.recent.asked == [("train", "GLO>MAN|Manchester Piccadilly", "07:12", 90)]
    assert server.request(f"/api/history/{BUS_ID}")[0].status == 200


@pytest.mark.parametrize("path", [
    "/api/bus/0000000000000000", f"/api/bus/{TRAIN_ID}", f"/api/bus/{BUS_ID.upper()}", f"/api/bus/{BUS_ID}0",
    "/api/bus/", "/api/history/0000000000000000", "/api/history/xyz", "/api/train/" + TRAIN_ID, "/api/",
])
def test_unknown_ids_are_not_found(server, path):
    assert server.request(path)[0].status == 404


def test_a_broken_history_is_unavailable_not_a_crash(server):
    server.app.recent.error = RuntimeError("database is locked")
    response, body = server.request(f"/api/history/{TRAIN_ID}")
    assert response.status == 503 and b"locked" not in body


# Health


def test_healthz(static_dir):
    healthy = {"now": True}
    running = start(static_dir, healthy=lambda: healthy["now"])
    try:
        response, body = running.request("/healthz", host="127.0.0.1:8090")
        assert (response.status, body) == (200, b"ok")
        healthy["now"] = False
        assert running.request("/healthz")[0].status == 503
    finally:
        running.close()


# Server-sent events


def read_block(response):
    """The lines of one event (up to a blank line)."""
    lines = []
    while True:
        line = response.readline()
        assert line, "stream ended"
        line = line.decode().rstrip("\n")
        if line == "":
            return lines
        lines.append(line)


def test_a_stream_starts_with_the_snapshot_then_pings_then_pushes(server):
    conn, response = server.stream()
    try:
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.getheader("Cache-Control") == "no-store"
        assert_secure(response)
        assert read_block(response) == ["retry: 5000"]
        version, body = server.app.hub.current()
        assert read_block(response) == [f"id: {version}", "event: snapshot", f"data: {body.decode()}"]
        assert read_block(response) == [": ping"]
        server.app.hub.publish("weather", {"rain": None})
        block = read_block(response)
        while block == [": ping"]:
            block = read_block(response)
        assert block[:2] == [f"id: {version + 1}", "event: snapshot"]
        assert json.loads(block[2][len("data: "):])["version"] == version + 1
    finally:
        conn.close()


def test_a_stream_resuming_at_the_current_version_waits_for_the_next(server):
    version = server.app.hub.version
    conn, response = server.stream({"Last-Event-ID": str(version)})
    try:
        assert read_block(response) == ["retry: 5000"]
        assert read_block(response) == [": ping"]
    finally:
        conn.close()


def test_a_stream_resuming_from_an_old_version_gets_the_current_one(server):
    version = server.app.hub.version
    conn, response = server.stream({"Last-Event-ID": str(version - 1)})
    try:
        read_block(response)
        assert read_block(response)[0] == f"id: {version}"
    finally:
        conn.close()


def test_a_stream_ends_after_its_time(static_dir):
    running = start(static_dir, stream_seconds=0.3)
    try:
        conn, response = running.stream()
        started = time.monotonic()
        assert response.read()  # until the server closes it
        assert time.monotonic() - started < 3
        conn.close()
    finally:
        running.close()


def test_streams_are_capped_and_a_closed_one_frees_its_slot(static_dir):
    running = start(static_dir, limits={"max_streams": 2})
    try:
        first, second = running.stream(), running.stream()
        for _, response in (first, second):
            assert response.status == 200
            read_block(response)
        response, _ = running.request("/api/events")
        assert response.status == 503 and response.getheader("Retry-After") == "30"
        assert_secure(response)
        first[0].close()
        first[1].close()
        deadline = time.monotonic() + 5
        while True:  # the server notices at its next ping
            conn, response = running.stream()
            if response.status == 200:
                conn.close()
                break
            conn.close()
            assert time.monotonic() < deadline, "the slot was never freed"
            time.sleep(0.1)
        second[0].close()
    finally:
        running.close()


def test_a_stream_keeps_the_dashboard_awake(server):
    conn, response = server.stream()
    try:
        read_block(response)
        read_block(response)
        read_block(response)  # a ping, after the hub's wait
        assert server.app.hub.active(600)
    finally:
        conn.close()


# Connections


def test_connections_beyond_the_cap_are_closed_unanswered(static_dir):
    running = start(static_dir, limits={"max_connections": 1})
    try:
        hog = socket.create_connection(("127.0.0.1", running.port), timeout=5)
        time.sleep(0.1)
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as extra:
            extra.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            try:
                assert extra.recv(100) == b""  # closed without an answer
            except ConnectionResetError:
                pass  # or reset: either way, no answer
        hog.close()
    finally:
        running.close()


def test_a_busy_port_is_an_error_unless_retrying(static_dir):
    app = make_app(static_dir)
    blocker = make_server(app, "127.0.0.1", 0)
    port = blocker.server_address[1]
    try:
        with pytest.raises(OSError, match=str(port)):
            make_server(app, "127.0.0.1", port)
        moved = make_server(app, "127.0.0.1", port, attempts=5)
        assert moved.server_address[1] != port
        moved.server_close()
    finally:
        blocker.server_close()
