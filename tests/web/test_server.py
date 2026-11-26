import contextlib
import hashlib
import http.client
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus

import pytest

from twobells.core.walk import WalkTimes
from twobells.dashboard.performance import Performance, Setup
from twobells.dashboard.snapshot import Hub
from twobells.settings.installs import install_named
from twobells.settings.store import Store
from twobells.web import server as server_module
from twobells.web import settingsite
from twobells.web.formpages import MAX_FORM_BODY
from twobells.web.server import (
    LINGER_BYTES,
    MAX_BODY_BYTES,
    MAX_CONNECTIONS,
    MAX_HEADER_BYTES,
    MAX_PER_CLIENT,
    MAX_REQUEST_BYTES,
    MAX_STREAMS,
    MAX_STREAMS_PER_CLIENT,
    REQUEST_DEADLINE_S,
    DashboardApp,
    Limits,
    StaticFiles,
    make_server,
    make_servers,
)
from twobells.web.settingsite import SettingsSite

# The contract's headers, copied from docs/dashboard-api.md rather than imported.
SECURITY = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: "
        "https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
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
    "performance.html": b"<!doctype html><title>Performance</title>",
    "app.css": b"body{}",
    "main.js": b"import './x.mjs';",
    "x.mjs": b"export {};",
    "icon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    "icon.png": b"\x89PNG\r\n",
    "manifest.webmanifest": b'{"name":"Departures"}',
    "data.json": b"{}",
    "LICENSE.txt": b"MIT",
    "vendor/leaflet-1.9.4/leaflet.js": b"// leaflet",
    "vendor/inter-4.1/InterVariable.woff2": b"wOF2",
}


class Recent:
    def __init__(self):
        self.asked = []
        self.error = None

    def __call__(self, mode, route, scheduled, day_type):
        self.asked.append((mode, route, scheduled, day_type))
        if self.error:
            raise self.error
        return [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": True, "reason": None}]


class Outcomes:
    """History.outcomes, from a list: a train to Manchester late once in three days."""

    def __init__(self):
        self.asked = []
        self.error = None
        self.rows = [
            ("GLO>MAN|Manchester Piccadilly", "2026-10-07", "on_time", 0),
            ("GLO>MAN|Manchester Piccadilly", "2026-10-08", "late", 4),
            ("GLO>MAN|Manchester Piccadilly", "2026-10-09", "on_time", 0),
        ]

    def outcomes(self, mode, since, route=None):
        self.asked.append((mode, since, route))
        if self.error:
            raise self.error
        return [row for row in self.rows if mode == "train" and route in (None, row[0])]

    def arrivals(self, station, since):
        """History.arrivals: one train to Manchester that got there 22 minutes late."""
        return [{"day": "2026-10-08", "scheduled": "07:42", "route": "GLO>MAN|Manchester Piccadilly",
                 "station_name": "Manchester Piccadilly", "operator": "Northern",
                 "destination": "Manchester Piccadilly", "booked": "2026-10-08T08:05:00+00:00",
                 "arrived": "2026-10-08T08:27:00+00:00", "reported": True}] if station == "MAN" else []


def figures(**overrides):
    """The figures of three days' trains: one of three late."""
    found = {"enough": True, "days": 3, "counted": 3, "right_time": 2, "within_3": 2, "within_15": 3,
             "on_time": 2, "cancelled": 0, "unknown": 0}
    return {**found, **overrides}


NOTHING = {"enough": False, "days": 0, "counted": 0, "right_time": 0, "within_3": 0, "within_15": 0, "on_time": 0,
           "cancelled": 0, "unknown": 0}


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
    def __init__(self, app, role="dashboard", **limits):
        self.app = app
        seconds = limits.pop("request_seconds", REQUEST_DEADLINE_S)
        self.server = make_server(app, "127.0.0.1", 0, role=role, limits=Limits(**limits), request_seconds=seconds)
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
        if self.app.hub is not None:
            self.app.hub.close()
        self.server.shutdown()
        self.server.server_close()


class NoSetup:
    def handle(self, name, payload):
        return 404, {"error": "No such setup step"}


def site_beside(folder):
    """A settings site, required by the server, with a store of its own beside `folder`."""
    store = Store.open(folder.parent / "config.toml", state_dir=folder.parent / "store", environ={})
    return SettingsSite(store, install_named("docker"), NoSetup(), stylesheet=b"", files={})


def make_app(static_dir, **overrides):
    hub = Hub("Glossop", WalkTimes(train=10))
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
        "performance": Performance(Outcomes(), lambda: datetime(2026, 10, 9, 18, 0, tzinfo=UTC),
                                   lambda: Setup({"train": 3, "bus": 2}, station="MAN", trains=True, details=True)),
        "allowed_hosts": frozenset({"departures.example.com"}),
        "heartbeat": 0.2,
        "stream_seconds": 30.0,
        "settings": site_beside(static_dir),
    }
    settings.update(overrides)
    return DashboardApp(**settings)


@pytest.fixture
def server(static_dir):
    running = Running(make_app(static_dir))
    yield running
    running.close()


def start(static_dir, limits=None, role="dashboard", **overrides):
    return Running(make_app(static_dir, **overrides), role, **(limits or {}))


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


@pytest.mark.parametrize("data, phrase", [
    (b"GET / HTTP/9.9\r\n\r\n", "HTTP Version Not Supported"),
    (b"GET\r\n\r\n", "Bad Request"),
], ids=["unknown-version", "bad-request-line"])
def test_a_broken_request_line_gets_our_headers_on_every_python(server, data, phrase):
    # http.server would answer these HTTP/0.9 style (a bare phrase) on Python
    # 3.12 but with headers on 3.13; the server answers the same way on both.
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw:
        raw.sendall(data)
        reply = b""
        while chunk := raw.recv(4096):
            reply += chunk
    head, body = reply.decode("latin-1").split("\r\n\r\n", 1)
    assert head.startswith("HTTP/1.0 ") and body == phrase
    assert "Content-Security-Policy: default-src 'none'" in head


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


def test_the_wall_tablet_address_serves_the_same_page(server):
    # Kiosk mode is the page's own doing, from its address: the server sends the same page.
    response, body = server.request("/?kiosk")
    assert response.status == 200 and body == server.request("/")[1]
    assert response.getheader("Content-Security-Policy") == server.request("/")[0].getheader("Content-Security-Policy")


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
    ("vendor/inter-4.1/InterVariable.woff2", "font/woff2"),
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


def test_the_pages_link_to_the_settings_knows_their_port(tmp_path):
    folder = tmp_path / "static"
    folder.mkdir()
    link = b'<a id="settings-link" href="/settings" data-port="<!-- settings port -->">Settings</a>'
    for name in ("index.html", "performance.html", "app.css"):
        (folder / name).write_bytes(link)
    static = StaticFiles.load(folder, settings_port=8093)
    for name in ("index.html", "performance.html"):
        assert static.get(name).body == link.replace(b"<!-- settings port -->", b"8093"), name
    assert static.get("app.css").body == link  # the pages only
    assert StaticFiles.load(folder).get("index.html").body == link  # no port, nothing changed: the link stays /settings


def test_the_page_says_which_version_is_running_in_its_footer(tmp_path):
    folder = tmp_path / "static"
    folder.mkdir()
    page = b"<footer><p>Times from here.</p>\n  <!-- version -->\n</footer>"
    (folder / "index.html").write_bytes(page)
    (folder / "app.css").write_bytes(b"/* <!-- version --> */")
    static = StaticFiles.load(folder, version="1.2.3")
    index = static.get("index.html")
    assert index.body == b'<footer><p>Times from here.</p>\n  <p class="version">twobells 1.2.3</p>\n</footer>'
    assert index.etag == f'"{hashlib.sha256(index.body).hexdigest()}"'  # the page as sent, so a new version is fetched
    assert static.get("app.css").body == b"/* <!-- version --> */"  # the page only
    assert StaticFiles.load(folder).get("index.html").body == page  # nothing to say, nothing changed
    unknown = StaticFiles.load(folder, version="unknown").get("index.html").body
    assert b'<p class="version">twobells, version unknown</p>' in unknown
    odd = StaticFiles.load(folder, version="<script>1</script>").get("index.html").body
    assert b"twobells &lt;script&gt;1&lt;/script&gt;" in odd and b"<script>" not in odd

    running = start(tmp_path / "other", static=StaticFiles.load(folder, version="1.2.3"))
    try:
        response, body = running.request("/")
        assert response.status == 200 and body == index.body
        assert_secure(response)
    finally:
        running.close()


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
    # With its route's figures: every train to Manchester Piccadilly, whatever its time.
    response, body = server.request(f"/api/history/{TRAIN_ID}")
    assert response.status == 200
    assert json.loads(body) == {
        "id": TRAIN_ID, "mode": "train", "label": "07:12 to Manchester Piccadilly", "day_type": "weekday",
        "phrase": "Late 4 of the last 10 weekdays (avg 6 min)",
        "days": [{"day": "2026-10-09", "status": "late", "delay_min": 7, "live": True, "reason": None}],
        "route": {"label": "Manchester Piccadilly", "stop": None, "late_minutes": 3, "periods": [
            {"span": 7, "since": "2026-10-03", **figures()},
            {"span": 28, "since": "2026-09-12", **figures()},
        ]},
    }
    # Its own kind of day only, as the phrase counts them.
    assert server.app.recent.asked == [("train", "GLO>MAN|Manchester Piccadilly", "07:12", "weekday")]
    response, body = server.request(f"/api/history/{BUS_ID}")
    assert response.status == 200
    route = json.loads(body)["route"]
    assert (route["label"], route["stop"], route["late_minutes"]) == ("341 to Hyde", "A", 2)
    assert [period["enough"] for period in route["periods"]] == [False, False]


def test_a_row_s_history_without_figures(static_dir):
    running = start(static_dir, performance=None)
    try:
        response, body = running.request(f"/api/history/{TRAIN_ID}")
        assert response.status == 200 and json.loads(body)["route"] is None
    finally:
        running.close()


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
    server.app.recent.error = None
    server.app.performance._source.error = RuntimeError("database is locked")
    for path in (f"/api/history/{TRAIN_ID}", "/api/performance"):
        response, body = server.request(path)
        assert response.status == 503 and b"locked" not in body, path
        assert_secure(response)


# The Performance page


def test_the_performance_page_is_a_page_of_the_dashboard(server):
    # Served as the dashboard's page is: its headers, its caching, its host checks.
    response, body = server.request("/performance")
    assert response.status == 200 and body == STATIC["performance.html"]
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-cache"
    assert_secure(response)
    etag = response.getheader("ETag")
    assert server.request("/performance", headers={"If-None-Match": etag})[0].status == 304
    assert server.request("/performance", host="evil.example")[0].status == 403
    assert server.request("/performance", method="POST")[0].status == 405
    for path in ("/performance/", "/performance.html", "/static/../performance.html"):
        assert server.request(path)[0].status == 404, path


def test_the_performance_figures(server):
    response, body = server.request("/api/performance")
    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert response.getheader("Cache-Control") == "no-store"
    assert_secure(response)
    periods = [{"span": 7, "since": "2026-10-03", **figures()}, {"span": 28, "since": "2026-09-12", **figures()}]
    assert json.loads(body) == {
        "generated_at": "2026-10-09T18:00:00+00:00",
        "trains": {"late_minutes": 3, "all": {"periods": periods},
                   "routes": [{"label": "Manchester Piccadilly", "stop": None, "periods": periods}]},
        "buses": {"late_minutes": 2, "routes": [], "all": {"periods": [
            {"span": 7, "since": "2026-10-03", **NOTHING}, {"span": 28, "since": "2026-09-12", **NOTHING}]}},
        "delay_repay": {"station": "MAN", "station_name": "Manchester Piccadilly", "since": "2026-09-12",
                        "least_minutes": 15, "off": None, "trains": [{
                            "day": "2026-10-08", "scheduled": "07:42", "destination": "Manchester Piccadilly",
                            "operator": "Northern", "booked": "08:05", "arrived": "08:27", "minutes_late": 22,
                            "band": 15, "reported": True}]},
    }
    assert server.request("/api/performance", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    assert server.request("/api/performance", host="evil.example")[0].status == 403
    assert server.request("/api/performance/x")[0].status == 404


def test_without_a_history_there_are_no_figures(static_dir):
    running = start(static_dir, performance=None)
    try:
        assert running.request("/api/performance")[0].status == 404
        assert running.request("/performance")[0].status == 200  # the page says so
    finally:
        running.close()


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


def failures(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "twobells.web.server"]


@pytest.mark.parametrize("error", [PermissionError("web.json"), FileNotFoundError("x"), OSError(28, "No space")])
def test_a_fault_in_a_handler_is_logged_by_its_type(static_dir, caplog, error):
    def broken():
        raise error

    running = start(static_dir, healthy=broken)
    try:
        with pytest.raises(http.client.RemoteDisconnected):
            running.request("/healthz")
    finally:
        running.close()
    assert failures(caplog) == [f"Dashboard request failed: {type(error).__name__}"]


@pytest.mark.parametrize("error", [ConnectionResetError(), BrokenPipeError(), TimeoutError()])
def test_a_client_going_away_is_not_logged(static_dir, caplog, error):
    def gone():
        raise error

    running = start(static_dir, healthy=gone)
    try:
        with pytest.raises(http.client.RemoteDisconnected):
            running.request("/healthz")
    finally:
        running.close()
    assert failures(caplog) == []


def test_a_fault_in_a_stream_is_logged_too(server, caplog, monkeypatch):
    def broken():
        raise PermissionError("snapshot.json")

    monkeypatch.setattr(server.app.hub, "current", broken)
    conn, response = server.stream()
    try:
        assert response.status == 200
        assert response.read() == b"retry: 5000\n\n"  # then the server lets go
    finally:
        conn.close()
    assert failures(caplog) == ["Dashboard request failed: PermissionError"]  # logged before it let go


# The dashboard off: its view off, not set up, or its settings don't load


SETTINGS_CSP = ("default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; form-action 'self'; "
                "frame-ancestors 'none'; base-uri 'none'")


@pytest.mark.parametrize("off", ["no hub", "view off"])
def test_with_the_dashboard_off_only_the_settings_and_the_health_check_answer(static_dir, off):
    overrides = {"hub": None} if off == "no hub" else {"dashboard_on": lambda: False}
    running = start(static_dir, healthy=lambda: False, **overrides)
    try:
        response, _ = running.request("/")
        assert response.status == 303 and response.getheader("Location") == "/settings"
        for path in ("/api/snapshot", "/api/events", "/api/area", f"/api/bus/{BUS_ID}", f"/api/history/{TRAIN_ID}",
                     "/api/performance", "/performance", "/static/app.css", "/manifest.webmanifest", "/nowhere"):
            response, body = running.request(path)
            assert response.status == 404 and b"Glossop" not in body, path
        response, body = running.request("/healthz")
        assert (response.status, body) == (200, b"ok")  # up, with nothing to fetch
        response, body = running.request("/settings/login")  # which says where the settings are
        assert response.status == 200 and b"The settings are on port" in body
        assert running.request("/api/snapshot", "POST")[0].status == 405
    finally:
        running.close()


def test_with_the_dashboard_off_no_response_lets_a_script_run(static_dir):
    running = start(static_dir, dashboard_on=lambda: False)
    try:
        for path, method, host in [("/", "GET", "localhost"), ("/api/snapshot", "GET", "localhost"),
                                   ("/healthz", "GET", "localhost"), ("/", "GET", "evil.com"),
                                   ("/", "PUT", "localhost")]:
            response, _ = running.request(path, method, host)
            assert response.getheader("Content-Security-Policy") == SETTINGS_CSP, (path, method, host)
            assert response.getheader("X-Frame-Options") == "DENY"
            assert response.getheader("Cache-Control") == "no-store"
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as raw:
            raw.sendall(b"GET\r\n\r\n")
            reply = b""
            while chunk := raw.recv(4096):
                reply += chunk
        assert f"Content-Security-Policy: {SETTINGS_CSP}" in reply.decode("latin-1")
    finally:
        running.close()


def test_the_dashboard_follows_its_view_as_it_changes(static_dir):
    on = {"now": True}
    running = start(static_dir, dashboard_on=lambda: on["now"])
    try:
        assert running.request("/api/snapshot")[0].status == 200
        on["now"] = False
        assert running.request("/api/snapshot")[0].status == 404
        on["now"] = True
        assert running.request("/")[0].status == 200
    finally:
        running.close()


def test_a_fault_telling_whether_the_dashboard_is_on_keeps_it_off(static_dir, caplog):
    def broken():
        raise PermissionError("settings.toml")

    running = start(static_dir, dashboard_on=broken)
    try:
        assert running.request("/api/snapshot")[0].status == 404
        assert running.request("/")[0].getheader("Location") == "/settings"
    finally:
        running.close()
    assert "PermissionError" in caplog.text and "settings.toml" not in caplog.text


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


def drip(raw, start, line, within=5.0):
    """Send `start`, then `line` every 0.1 s until the server closes the
    connection; what it sent back and how long that took."""
    raw.settimeout(0.1)
    raw.sendall(start)
    began = time.monotonic()
    reply = b""
    while time.monotonic() - began < within:
        try:
            chunk = raw.recv(4096)
        except TimeoutError:
            with contextlib.suppress(OSError):  # a closed connection shows at the next recv
                raw.sendall(line)
            continue
        except ConnectionResetError:
            break
        if not chunk:
            break
        reply += chunk
    else:
        raise AssertionError("the server never let go")
    return reply, time.monotonic() - began


def test_a_request_sent_a_line_at_a_time_is_dropped_at_its_deadline(static_dir):
    running = start(static_dir, limits={"request_seconds": 0.5})
    try:
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as raw:
            reply, took = drip(raw, b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n", b"X-A: b\r\n")
        assert reply == b"" and took < 3  # each line well inside the 10 s read timeout, but too slow all told
    finally:
        running.close()


def test_a_body_sent_a_byte_at_a_time_is_dropped_at_its_deadline(static_dir):
    running = start(static_dir, limits={"request_seconds": 0.5}, role="settings")
    head = (b"POST /settings/login HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: 1000\r\n\r\n")
    try:
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as raw:
            reply, took = drip(raw, head, b"a")
        assert reply == b"" and took < 3
    finally:
        running.close()


def test_the_deadline_is_for_each_request_not_the_connection(static_dir):
    running = start(static_dir, limits={"request_seconds": 0.5})
    try:
        conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        for _ in range(3):  # one kept-alive connection, longer than the deadline in all
            conn.request("GET", "/healthz", headers={"Host": "localhost"})
            assert conn.getresponse().read() == b"ok"
            time.sleep(0.3)
        conn.close()
    finally:
        running.close()


class Counted(socket.socket):
    """The server's end of a connection, counting the bytes it takes off the
    wire: in all, and before it began its answer."""

    taken = 0
    answered = None  # the bytes taken when it began its answer

    def recv_into(self, buffer, nbytes=0, flags=0):
        got = super().recv_into(buffer, nbytes, flags)
        self.taken += got
        return got

    def sendall(self, data, flags=0):
        if self.answered is None:
            self.answered = self.taken
        return super().sendall(data, flags)


def assert_took(counted, before_answer):
    """The server took `before_answer` bytes and answered, then threw away
    no more than LINGER_BYTES as the answer went out."""
    assert counted.answered == before_answer
    assert counted.taken - before_answer <= LINGER_BYTES


def counted_from(running, address="192.168.1.66"):
    """A connection to the server, as connect_from, and the server's end of it, counted."""
    ours, theirs = socket.socketpair()
    ours.settimeout(5)
    counted = Counted(fileno=theirs.detach())
    running.server.process_request(counted, (address, 50000))
    return ours, counted


def send_and_read(sock, data, step=None):
    """Send `data` from a thread of its own (the server may stop reading part
    way), all at once or `step` bytes a send, then say it is done, as a
    browser would; and read what comes back until the server closes the
    connection."""
    def send():
        with contextlib.suppress(OSError):
            if step is None:
                sock.sendall(data)
            else:
                for at in range(0, len(data), step):
                    sock.sendall(data[at:at + step])
            sock.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    reply = b""
    with contextlib.suppress(ConnectionResetError):
        while chunk := sock.recv(65536):
            reply += chunk
    sender.join(5)
    return reply


def refusal(reply):
    """(status line, headers by lower-case name, body) of a reply that closes the connection."""
    head, _, body = reply.partition(b"\r\n\r\n")
    status, *lines = head.decode("latin-1").split("\r\n")
    return status, {name.lower(): value for name, _, value in (line.partition(": ") for line in lines)}, body


def assert_refused(reply, status, phrase, security=SECURITY):
    """`reply` is the short plain answer to an over-limit request: our headers
    (`security`, the listener's), nothing of the request's."""
    line, headers, body = refusal(reply)
    assert line == f"HTTP/1.0 {status} {phrase}"
    assert headers["connection"] == "close" and headers["content-type"] == "text/plain; charset=utf-8"
    assert headers["content-length"] == str(len(body)) and body == phrase.encode()
    assert headers["cache-control"] == "no-store"
    for name, value in security.items():
        assert headers[name.lower()] == value, name
    assert b"MARK" not in reply  # nothing of what was sent


GET_HEALTH = b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n"
# What each path answers once it has read a body: no password is set, nor anyone signed in.
READ = {"/settings/login": "HTTP/1.0 403 Forbidden", "/settings/setup/api/begin": "HTTP/1.0 401 Unauthorized"}


def padded(head, size, lines=1):
    """`head`, a request line and headers, padded with `lines` X-Pad headers
    saying MARK to `size` bytes, the blank line ending them included."""
    each, extra = divmod(size - len(head) - len(b"\r\n"), lines)
    pads = [b"X-Pad: MARK" + b"a" * (each - 13) + b"\r\n" for _ in range(lines)]
    pads[0] = b"X-Pad: MARK" + b"a" * (each + extra - 13) + b"\r\n"
    request = head + b"".join(pads) + b"\r\n"
    assert len(request) == size
    return request


def padded_post(path, body, size, lines=1):
    """form_post's request, its line and headers padded to `size` bytes."""
    head = form_post(path, body).partition(b"\r\n\r\n")[0] + b"\r\n"
    return padded(head, size, lines) + body


def taking_any_length(monkeypatch):
    """The settings site caps a body by its Content-Length before reading it;
    lifted, so a test reaches the server's own cap behind that."""
    monkeypatch.setattr(settingsite, "max_body", lambda path: 10**9)


# With Host and the blank line, 98 headers are the 100 lines http.server takes.
@pytest.mark.parametrize("lines", [1, 98], ids=["one-long-line", "many-short-lines"])
@pytest.mark.parametrize("step", [None, 1], ids=["one-write", "byte-by-byte"])
def test_a_line_and_headers_of_32_kib_are_answered(static_dir, lines, step):
    running = start(static_dir)
    request = padded(GET_HEALTH, MAX_HEADER_BYTES, lines)
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request, step)
        line, _, body = refusal(reply)
        assert line == "HTTP/1.0 200 OK" and body == b"ok"
        assert counted.taken == len(request)
    finally:
        running.close()


@pytest.mark.parametrize("lines", [1, 98], ids=["one-long-line", "many-short-lines"])
@pytest.mark.parametrize("step", [None, 1], ids=["one-write", "byte-by-byte"])
def test_a_line_and_headers_past_32_kib_are_refused_with_431_and_no_more_is_used(static_dir, lines, step):
    """Even with no body at all: the line and headers have a budget of
    their own, not a share of the whole request's."""
    running = start(static_dir)
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, padded(GET_HEALTH, MAX_HEADER_BYTES + 1, lines), step)
        assert_refused(reply, 431, "Request Header Fields Too Large")
        assert_took(counted, MAX_HEADER_BYTES)
        assert running.request("/healthz")[1] == b"ok"  # and the server still answers
    finally:
        running.close()


@pytest.mark.parametrize("request_bytes", [
    GET_HEALTH + b"X-A: MARK" + b"a" * (40 * 1024) + b"\r\n\r\n",  # a line http.server would take
    GET_HEALTH + b"X-A: MARK" + b"a" * (70 * 1024) + b"\r\n\r\n",  # one it would refuse, after 64 KiB
    b"GET /MARK" + b"a" * (40 * 1024) + b" HTTP/1.0\r\nHost: localhost\r\n\r\n",
    b"GET /MARK" + b"a" * (70 * 1024) + b" HTTP/1.0\r\nHost: localhost\r\n\r\n",  # 414 from http.server
], ids=["40-kib-header", "70-kib-header", "40-kib-request-line", "70-kib-request-line"])
def test_one_enormous_line_is_refused_with_431_once_32_kib_are_in(static_dir, request_bytes):
    running = start(static_dir)
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request_bytes)
        assert_refused(reply, 431, "Request Header Fields Too Large")
        assert_took(counted, MAX_HEADER_BYTES)
    finally:
        running.close()


def test_the_audits_six_megabytes_of_headers_stop_at_32_kib(static_dir):
    """The audit's case: http.server takes 100 header lines of 64 KiB, about
    6.5 MB a request, all read and parsed before the outsider check, so 16
    such connections from one device took a test server from 51 MB to 440 MB
    (a Pi's unit has MemoryMax=128M). What the server reads, its reader's
    read-ahead included, stays within MAX_HEADER_BYTES; then it says why, and closes."""
    running = start(static_dir)
    line = b"X-A: MARK" + b"a" * (64 * 1024 - 11) + b"\r\n"  # 64 KiB, the longest line http.server takes
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n" + line * 98 + b"\r\n")
        assert_refused(reply, 431, "Request Header Fields Too Large")
        assert_took(counted, MAX_HEADER_BYTES)
        assert running.request("/healthz")[1] == b"ok"
    finally:
        running.close()


def test_too_many_headers_get_http_servers_431_with_our_headers_and_nothing_of_the_request(static_dir):
    # Well under 32 KiB, but past the 100 lines http.server takes: its own answer, as ours looks.
    running = start(static_dir)
    request = GET_HEALTH + b"X-Pad: MARK\r\n" * 100 + b"\r\n"
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request)
        line, headers, body = refusal(reply)
        assert line == "HTTP/1.0 431 Request Header Fields Too Large" and body == b"Request Header Fields Too Large"
        assert headers["content-type"] == "text/plain; charset=utf-8" and headers["cache-control"] == "no-store"
        for name, value in SECURITY.items():
            assert headers[name.lower()] == value, name
        assert b"MARK" not in reply
        assert counted.taken == len(request)
    finally:
        running.close()


@pytest.mark.parametrize("head_size", [1024, MAX_HEADER_BYTES - 100])
@pytest.mark.parametrize("step", [None, 1], ids=["one-write", "byte-by-byte"])
def test_a_body_is_read_whole_whatever_its_headers_read_ahead_of_it(static_dir, head_size, step):
    """The reader in front of the socket reads ahead, so part of the body
    may be in before the headers are parsed: none of it counts against the
    headers' budget, nor is any of it lost to the body's."""
    running = start(static_dir, role="settings")
    request = padded_post("/settings/login", b"a=MARK" + b"b" * (60 * 1024 - 6), head_size)
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request, step)
        assert refusal(reply)[0] == READ["/settings/login"]
        assert counted.taken == len(request)
    finally:
        running.close()


@pytest.mark.parametrize("head_size", [256, 1024, MAX_HEADER_BYTES], ids=["small-headers", "1-kib", "32-kib"])
@pytest.mark.parametrize("path", ["/settings/login", "/settings/setup/api/begin"])
def test_a_body_has_its_own_64_kib_and_past_them_is_refused_with_413(static_dir, monkeypatch, head_size, path):
    """However short the headers, the body has its own allowance, no more:
    so the whole request comes to at most MAX_REQUEST_BYTES."""
    taking_any_length(monkeypatch)
    running = start(static_dir, role="settings")
    try:
        for length in (MAX_BODY_BYTES, MAX_BODY_BYTES + 1):
            request = padded_post(path, b"a=MARK" + b"b" * (length - 6), head_size)
            sock, counted = counted_from(running)
            with sock:
                reply = send_and_read(sock, request)
            if length == MAX_BODY_BYTES:
                assert refusal(reply)[0] == READ[path]
                assert counted.taken == len(request)
            else:
                assert_refused(reply, 413, HTTPStatus(413).phrase, settingsite.SETTINGS_HEADERS)
                assert_took(counted, head_size + MAX_BODY_BYTES)
                assert head_size + MAX_BODY_BYTES <= MAX_REQUEST_BYTES
        assert running.request("/settings/login")[0].status == 200
    finally:
        running.close()


def test_a_post_to_the_dashboards_api_is_refused_with_its_body_unread(static_dir):
    running = start(static_dir)
    request = padded_post("/api/snapshot", b"a=MARK" + b"b" * (MAX_BODY_BYTES - 6), 1024)
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request)
        assert refusal(reply)[0] == "HTTP/1.0 405 Method Not Allowed"
        assert counted.answered < len(request)  # answered with the body unread, then lingered
        assert counted.taken - counted.answered <= LINGER_BYTES
    finally:
        running.close()


@pytest.mark.parametrize("path", ["/settings/login", "/settings/setup/api/begin"])
def test_a_length_past_the_cap_is_refused_with_413_before_any_body_is_sent(static_dir, path):
    running = start(static_dir, role="settings")
    try:
        sock, counted = counted_from(running)
        with sock:
            head = form_post(path, b"").replace(b"Content-Length: 0", b"Content-Length: 1000000000")
            sock.sendall(head)
            reply = b""
            while chunk := sock.recv(65536):  # answered with nothing more sent
                reply += chunk
        line, headers, _ = refusal(reply)
        assert line.startswith("HTTP/1.0 413 ") and headers["connection"] == "close"
        assert counted.taken == len(head)
    finally:
        running.close()


@pytest.mark.parametrize("past, status", [("headers", 431), ("body", 413)])
def test_a_client_still_sending_past_the_budget_hears_why_over_tcp(static_dir, monkeypatch, past, status):
    """A browser posting too much keeps sending as the server stops reading:
    the answer still reaches it, and the server lets go within its deadline."""
    taking_any_length(monkeypatch)
    running = start(static_dir, limits={"request_seconds": 3}, role="settings")
    try:
        body = b"a=" + b"b" * (4 * 1024 * 1024)  # well past the request
        request = padded_post("/settings/login", body, MAX_HEADER_BYTES + 1 if past == "headers" else 1024)
        with socket.create_connection(("127.0.0.1", running.port), timeout=10) as sock:
            began = time.monotonic()
            reply = send_and_read(sock, request)
        assert_refused(reply, status, HTTPStatus(status).phrase, settingsite.SETTINGS_HEADERS)
        assert time.monotonic() - began < 5
        assert running.request("/settings/login")[0].status == 200
    finally:
        running.close()


MORE = b"a=MARK" + b"b" * (4 * 1024 * 1024)  # what a client still sends after the answer


def post_to(path, body, method="POST", origin="http://localhost", length=None):
    """A form POST (or `method`), its Content-Length `length` or the body's."""
    return (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nOrigin: {origin}\r\n"
            f"Content-Type: application/x-www-form-urlencoded\r\n"
            f"Content-Length: {len(body) if length is None else length}\r\n\r\n").encode() + body


# Each answered while part of its request is unread or unsent, with MORE behind it, and its status line.
UNFINISHED = {
    "headers": (padded(GET_HEALTH, MAX_HEADER_BYTES + 1) + MORE, "431"),
    "length": (post_to("/settings/login", MORE), "413"),  # refused by its length, unread
    "too-many-headers": (GET_HEALTH + b"X-Pad: MARK\r\n" * 100 + b"\r\n" + MORE, "431"),  # http.server's own
    "bad-request-line": (b"GET\r\n" + MORE, "400"),
    "http-0.9": (b"GET /healthz\r\n" + MORE, "400"),
    "cross-site": (post_to("/settings/login", MORE, origin="http://evil.example"), "403"),
    "settings-method": (post_to("/settings/login", MORE, method="PUT"), "405"),
    "unknown-step": (post_to("/settings/setup/api/no_where", MORE), "404"),
    "dashboard-post": (post_to("/api/snapshot", MORE), "405"),
    "junk-after-the-body": (post_to("/settings/login", MORE, length=0), "403"),  # the sign-in page
    "moved-post": (post_to("/settings/login", MORE), "405"),  # to the dashboard's port, where the settings aren't
}
# The dashboard's listener answers these; the settings' the rest.
ON_THE_DASHBOARD = {"dashboard-post", "moved-post"}


def listener_for(past):
    return "dashboard" if past in ON_THE_DASHBOARD else "settings"


def refused_mid_send(past):
    """A request answered part way, with 4 MiB more behind it, and the start of its answer."""
    request, status = UNFINISHED[past]
    return request, f"HTTP/1.0 {status} "


@pytest.mark.parametrize("past", list(UNFINISHED))
def test_a_client_still_sending_after_its_answer_has_a_second_to_read_it_then_is_closed_on(static_dir, past):
    """Closing on bytes not yet read resets the connection, which can lose
    the answer on its way; so after any answer that leaves part of the
    request unread, the server waits up to LINGER_S, throwing away no
    more than LINGER_BYTES, then closes."""
    running = start(static_dir, role=listener_for(past))
    request, answer = refused_mid_send(past)
    try:
        sock, counted = counted_from(running)
        with sock:
            began = time.monotonic()
            reply = send_and_read(sock, request)
            took = time.monotonic() - began
        assert reply.startswith(answer.encode()) and b"MARK" not in reply
        assert server_module.LINGER_S * 0.8 < took < server_module.LINGER_S + 2
        assert counted.taken - counted.answered <= LINGER_BYTES
    finally:
        running.close()


@pytest.mark.parametrize("past", list(UNFINISHED))
def test_a_client_done_sending_after_its_answer_is_closed_on_at_once(static_dir, past):
    running = start(static_dir, role=listener_for(past))
    request, answer = refused_mid_send(past)
    request = request[:len(request) - 4 * 1024 * 1024 + 100]  # 100 bytes of the 4 MiB, then done
    try:
        sock, _ = counted_from(running)
        with sock:
            began = time.monotonic()
            reply = send_and_read(sock, request)
            took = time.monotonic() - began
        assert reply.startswith(answer.encode())
        assert took < server_module.LINGER_S / 2
    finally:
        running.close()


def test_the_server_ends_its_side_at_once_and_lingers_after(static_dir):
    """The client sees the end of the answer (a FIN) straight away, though
    it neither stops sending nor closes; the server still takes what it
    sends after that, while it lingers."""
    running = start(static_dir)
    request = padded(GET_HEALTH, MAX_HEADER_BYTES + 1) + b"x" * 100
    try:
        sock, counted = counted_from(running)
        with sock:
            began = time.monotonic()
            sock.sendall(request)
            reply = b""
            while chunk := sock.recv(65536):
                reply += chunk
            took = time.monotonic() - began
            sock.sendall(b"MORE")  # still lingering: taken and thrown away
            until = time.monotonic() + 2
            while counted.taken < len(request) + 4 and time.monotonic() < until:
                time.sleep(0.01)
        assert reply.startswith(b"HTTP/1.0 431 ")
        assert took < server_module.LINGER_S / 2
        assert counted.taken == len(request) + 4
    finally:
        running.close()


def holds_no_slot(running, request):
    """Whether, once `request` is answered and the client has read to the
    end (keeping its connection open), the server lets go of its slot
    well inside LINGER_S: the server here takes one connection an address."""
    probe = "/healthz" if running.server.role == "dashboard" else "/settings/style.css"
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as sock:
        sock.sendall(request)
        reply = b""
        while chunk := sock.recv(65536):
            reply += chunk
        until = time.monotonic() + server_module.LINGER_S / 2
        while time.monotonic() < until:
            try:
                if running.request(probe)[0].status == 200:
                    return reply
            except (ConnectionError, http.client.HTTPException):
                time.sleep(0.02)
    return None


@pytest.mark.parametrize("request_bytes, status", [
    (GET_HEALTH + b"\r\n", "200"),
    (b"HEAD /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n", "200"),
    (b"GET /api/snapshot HTTP/1.1\r\nHost: localhost\r\n\r\n", "200"),
    (post_to("/settings/login", b"password=MARK"), "403"),  # read whole, and the sign-in page: no password set
    (post_to("/settings/login", b""), "403"),
], ids=["get", "head", "api", "form-post", "empty-post"])
def test_an_answer_to_a_request_read_whole_lets_go_at_once(static_dir, request_bytes, status):
    role = "settings" if b"/settings/" in request_bytes.split(b"\r\n", 1)[0] else "dashboard"
    running = start(static_dir, limits={"max_per_client": 1}, role=role)
    try:
        reply = holds_no_slot(running, request_bytes)
        assert reply is not None, "the slot was held"
        assert reply.startswith(f"HTTP/1.0 {status} ".encode())
    finally:
        running.close()


def test_an_http_0_9_request_is_refused_with_a_status_line_and_our_headers(static_dir):
    """No version, so no headers: http.server would answer it with a bare
    body, or nothing; it is refused as a broken request is."""
    running = start(static_dir)
    try:
        sock, _ = counted_from(running)
        with sock:
            sock.sendall(b"GET /healthz?MARK\r\n")  # and nothing more
            reply = b""
            while chunk := sock.recv(65536):
                reply += chunk
        line, headers, body = refusal(reply)
        assert line == "HTTP/1.0 400 Bad Request" and body == b"Bad Request"
        assert headers["content-type"] == "text/plain; charset=utf-8"
        for name, value in SECURITY.items():
            assert headers[name.lower()] == value, name
        assert b"MARK" not in reply
    finally:
        running.close()


def test_waiting_for_a_client_to_read_its_refusal_ends_at_the_requests_deadline(static_dir, monkeypatch):
    monkeypatch.setattr(server_module, "LINGER_S", 60.0)
    running = start(static_dir, limits={"request_seconds": 0.5})
    request, answer = refused_mid_send("headers")
    try:
        sock, _ = counted_from(running)
        with sock:
            began = time.monotonic()
            reply = send_and_read(sock, request)
            took = time.monotonic() - began
        assert reply.startswith(answer.encode())
        assert took < 3
    finally:
        running.close()


def test_a_client_gone_before_its_refusal_is_let_go_quietly(static_dir, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    real = server_module._Handler._head

    def gone(self, status, *args, **kwargs):
        if status in (413, 431):
            raise BrokenPipeError("the browser went away")
        return real(self, status, *args, **kwargs)

    monkeypatch.setattr(server_module._Handler, "_head", gone)
    running = start(static_dir)
    line = b"X-A: " + b"a" * (64 * 1024 - 7) + b"\r\n"
    try:
        sock, _ = counted_from(running)
        with sock:
            reply = send_and_read(sock, b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n" + line * 3 + b"\r\n")
        assert reply == b""
        assert running.request("/healthz")[1] == b"ok"
        assert not [record for record in caplog.records if record.levelname in ("WARNING", "ERROR")]
    finally:
        running.close()


def form_post(path, body, padding=0):
    """A same-origin form POST, its line and headers made `padding` bytes longer."""
    head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nOrigin: http://localhost\r\n"
            f"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n")
    if padding:
        head += "X-Pad: " + "a" * (padding - len("X-Pad: \r\n")) + "\r\n"
    return head.encode() + b"\r\n" + body


def test_the_largest_form_with_the_longest_headers_allowed_is_still_answered(static_dir):
    running = start(static_dir, role="settings")
    body = b"a=" + b"b" * (MAX_FORM_BODY - 2)
    request = form_post("/settings/login", body)
    request = form_post("/settings/login", body, MAX_HEADER_BYTES - (len(request) - len(body)))
    assert len(request) == MAX_HEADER_BYTES + MAX_FORM_BODY
    try:
        sock, counted = counted_from(running)
        with sock:
            reply = send_and_read(sock, request)
        assert refusal(reply)[0] == READ["/settings/login"]
        assert counted.taken == MAX_REQUEST_BYTES  # the documented total, all of it taken
    finally:
        running.close()


def test_the_budget_holds_the_largest_body_the_settings_take():
    assert MAX_REQUEST_BYTES == MAX_HEADER_BYTES + MAX_BODY_BYTES
    for path in ("/settings/login", "/settings/keys", "/settings/setup/api/finish"):
        assert settingsite.max_body(path) <= MAX_BODY_BYTES


def test_a_stream_writes_far_past_the_request_byte_budget(server):
    """The budget is for what a request sends: a stream only writes."""
    conn, response = server.stream()
    try:
        read_block(response)
        read_block(response)
        sent = 0
        for n in range(4):
            server.app.hub.publish("weather", {"n": n, "rows": ["x" * 1000] * 40})
            block = read_block(response)
            while block == [": ping"]:
                block = read_block(response)
            sent += len(block[2])
        assert sent > 1.5 * MAX_REQUEST_BYTES
    finally:
        conn.close()


def test_a_stream_outlasts_the_request_deadline(static_dir):
    running = start(static_dir, limits={"request_seconds": 0.3})
    try:
        conn, response = running.stream()
        started = time.monotonic()
        read_block(response)
        read_block(response)
        while time.monotonic() - started < 1:
            assert read_block(response) == [": ping"]  # still open, past the deadline
        conn.close()
    finally:
        running.close()


def test_slow_clients_holding_every_connection_let_go_at_their_deadline(static_dir):
    """The audit's case: clients sending a header line now and then held every
    connection, so the health check went unanswered for as long as they kept at it."""
    running = start(static_dir, limits={"max_connections": 4, "max_per_client": 4, "request_seconds": 0.5})
    hogs = []
    try:
        for _ in range(4):
            hog = socket.create_connection(("127.0.0.1", running.port), timeout=5)
            hog.sendall(b"GET /healthz HTTP/1.1\r\n")
            hogs.append(hog)
        time.sleep(0.1)
        began = time.monotonic()
        while True:
            for hog in hogs:
                with contextlib.suppress(OSError):
                    hog.sendall(b"X-A: b\r\n")
            try:
                if running.request("/healthz")[1] == b"ok":
                    break
            except (http.client.HTTPException, OSError):
                pass  # every connection still held
            assert time.monotonic() - began < 5, "the slow clients never let go"
            time.sleep(0.1)
    finally:
        for hog in hogs:
            hog.close()
        running.close()


def connect_from(running, address):
    """A connection to the server as if from `address`: the server's end of
    a socket pair, handed over as the listener would hand it a client."""
    ours, theirs = socket.socketpair()
    ours.settimeout(5)
    running.server.process_request(theirs, (address, 50000))
    return ours


def test_one_address_can_hold_only_its_share_of_the_connections(static_dir):
    running = start(static_dir, limits={"max_connections": 4, "max_per_client": 2})
    try:
        hogs = [connect_from(running, "192.168.1.66") for _ in range(2)]
        with connect_from(running, "192.168.1.66") as extra:  # past its cap: closed unanswered, unread
            assert extra.recv(100) == b""
        with connect_from(running, "192.168.1.20") as other:  # another device is still served
            other.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            assert other.recv(4096).startswith(b"HTTP/1.0 200 ")
        assert running.request("/healthz")[1] == b"ok"
        hogs.pop().close()
        deadline = time.monotonic() + 5
        while True:  # the slot comes back once the server lets go of the closed one
            with connect_from(running, "192.168.1.66") as again, contextlib.suppress(OSError):  # still refused
                again.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
                if again.recv(4096).startswith(b"HTTP/1.0 200 "):
                    break
            assert time.monotonic() < deadline, "the closed connection's slot was never freed"
            time.sleep(0.05)
        for hog in hogs:
            hog.close()
    finally:
        running.close()


def test_a_household_behind_one_address_fits_under_its_cap():
    """Docker Desktop and rootless Podman show every device as one address:
    two browsers, each with its six connections (an event stream among
    them), stay under the cap, and one address can't hold every connection."""
    assert 2 * 6 < MAX_PER_CLIENT < MAX_CONNECTIONS


def _stream_from(running, address):
    """An event stream asked for as if from `address`, and the status line it gets."""
    sock = connect_from(running, address)
    sock.sendall(b"GET /api/events HTTP/1.1\r\nHost: localhost\r\n\r\n")
    status = b""
    while not status.endswith(b"\r\n"):
        status += sock.recv(1)
    return sock, status.decode().split(" ")[1]


def test_one_address_can_hold_only_its_share_of_the_event_streams(static_dir):
    running = start(static_dir, limits={"max_streams": 3, "max_streams_per_client": 2})
    held = []
    try:
        for want in ("200", "200", "503"):  # the third from one address is refused: it polls instead
            sock, status = _stream_from(running, "192.168.1.66")
            held.append(sock)
            assert status == want
        sock, status = _stream_from(running, "192.168.1.20")  # another device still gets one
        held.append(sock)
        assert status == "200"
        held.pop(0).close()
        deadline = time.monotonic() + 5
        while True:  # the address's slot comes back once the server notices, at its next ping
            sock, status = _stream_from(running, "192.168.1.66")
            held.append(sock)
            if status == "200":
                break
            assert time.monotonic() < deadline, "the closed stream's slot was never freed"
            time.sleep(0.1)
    finally:
        for sock in held:
            sock.close()
        running.close()


def test_a_household_behind_one_address_keeps_a_couple_of_streams_a_device():
    # Two devices' two tabs each from one address (Docker Desktop, rootless Podman), with
    # connections to spare, and no one address holding every stream.
    assert 2 * 2 <= MAX_STREAMS_PER_CLIENT < MAX_STREAMS and MAX_STREAMS_PER_CLIENT < MAX_PER_CLIENT


def test_connections_beyond_the_cap_are_closed_unanswered(static_dir):
    running = start(static_dir, limits={"max_connections": 1})
    try:
        hog = socket.create_connection(("127.0.0.1", running.port), timeout=5)
        time.sleep(0.1)
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as extra:
            extra.sendall(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
            with contextlib.suppress(ConnectionResetError):  # or reset: either way, no answer
                assert extra.recv(100) == b""  # closed without an answer
        hog.close()
    finally:
        running.close()


def test_a_busy_port_is_an_error_unless_retrying(static_dir):
    app = make_app(static_dir)
    blocker = make_server(app, "127.0.0.1", 0, role="dashboard")
    port = blocker.server_address[1]
    try:
        with pytest.raises(OSError, match=str(port)):
            make_server(app, "127.0.0.1", port, role="dashboard")
        moved = make_server(app, "127.0.0.1", port, attempts=5, role="dashboard")
        assert moved.server_address[1] != port
        moved.server_close()
    finally:
        blocker.server_close()


# The area map


AREA = {"fetched_at": "2026-10-09T10:14:30+01:00", "box": [-1.97, 53.43, -1.94, 53.45],
        "vehicles": [], "stops": [], "routes": []}


def test_the_area_map_is_not_found_until_the_buses_have_one(server):
    response, _ = server.request("/api/area")
    assert response.status == 404
    assert_secure(response)


def test_the_area_map_follows_the_same_rules_as_the_rest_of_the_api(server):
    server.app.hub.put("area", AREA)
    response, body = server.request("/api/area")
    assert response.status == 200 and json.loads(body) == AREA
    assert response.getheader("Content-Type") == "application/json"
    assert response.getheader("Cache-Control") == "no-store"
    assert_secure(response)
    assert server.request("/api/area", host="evil.example")[0].status == 403
    assert server.request("/api/area", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    assert server.request("/api/area", method="POST")[0].status == 405
    assert server.request("/api/area/x")[0].status == 404
    head, body = server.request("/api/area", method="HEAD")
    assert head.status == 200 and body == b""
    server.app.hub.put("area", None)
    assert server.request("/api/area")[0].status == 404


def test_the_area_map_does_not_change_the_snapshot(static_dir):
    running = start(static_dir)
    try:
        before = running.app.hub.current()
        running.app.hub.put("area", AREA)
        assert running.app.hub.current()[0] == before[0]
    finally:
        running.close()


# Two listeners: the dashboard's and the settings', each serving by the socket a request came in on


class Spy:
    """The settings site, counting what it was asked."""

    def __init__(self, site):
        self.site = site
        self.asked = []

    def __getattr__(self, name):
        return getattr(self.site, name)

    def handle(self, method, path, *rest):
        self.asked.append((method, path))
        return self.site.handle(method, path, *rest)


class Pair:
    """Both listeners of one app, on free ports, sharing one set of limits."""

    def __init__(self, app, **limits):
        self.app = app
        seconds = limits.pop("request_seconds", REQUEST_DEADLINE_S)
        self.servers = make_servers(app, "127.0.0.1", 0, 0, limits=Limits(**limits), request_seconds=seconds)
        self.dashboard_port, self.settings_port = self.servers.ports
        self.thread = threading.Thread(target=self.servers.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def request(self, path, method="GET", to="dashboard", host=None, headers=None, body=None):
        port = self.dashboard_port if to == "dashboard" else self.settings_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host or f"localhost:{port}")
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        got = response.read()
        conn.close()
        return response, got

    def close(self):
        if self.app.hub is not None:
            self.app.hub.close()
        self.servers.shutdown()
        self.servers.server_close()
        self.thread.join(5)


@pytest.fixture
def pair(static_dir):
    app = make_app(static_dir, moved_hint="Publish <code>8091</code> too.")
    app.settings = Spy(app.settings)
    running = Pair(app)
    yield running
    running.close()


SETTINGS_PATHS = ["/settings", "/settings/", "/settings/login", "/settings/keys", "/settings/general",
                  "/settings/reset", "/settings/forgot", "/settings/logout", "/settings/style.css",
                  "/settings/settings.js", "/settings/setup", "/settings/setup/", "/settings/setup/wizard.js",
                  "/settings/setup/api/session", "/settings/setup/api/begin", "/settings/nowhere",
                  "/settings?next=keys", "/settings/../api/snapshot", "/settings/%2e%2e/api", "/settings/" + "x" * 600]


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_the_dashboards_listener_never_serves_the_settings_even_to_a_signed_in_browser(pair, method):
    token, session = pair.app.settings.sessions.create()
    sent = {"Cookie": f"settings_session={token}", "X-CSRF-Token": session.csrf,
            "Origin": f"http://localhost:{pair.dashboard_port}", "Content-Type": "application/x-www-form-urlencoded",
            "Sec-Fetch-Site": "same-origin"}
    for path in SETTINGS_PATHS:
        response, body = pair.request(path, method, headers=sent, body=b"")
        assert response.status == (200 if method in ("GET", "HEAD") else 405), path
        assert response.getheader("Set-Cookie") is None, path
        assert session.csrf.encode() not in body and token.encode() not in body, path
        assert b"<form" not in body and b"<script" not in body, path
        if method not in ("GET", "HEAD"):
            assert response.getheader("Allow") == "GET, HEAD" and response.getheader("Connection") == "close"
    assert pair.app.settings.asked == []


def test_a_password_posted_to_the_dashboards_listener_is_never_read(pair):
    pair.app.settings.password.set("correct horse battery")
    response, _ = pair.request("/settings/login", "POST", body=b"password=correct+horse+battery", headers={
        "Origin": f"http://localhost:{pair.dashboard_port}", "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 405 and response.getheader("Set-Cookie") is None
    assert pair.app.settings.asked == []


@pytest.mark.parametrize("host, link", [
    ("glossop.local:8090", "http://glossop.local:{port}/settings"),
    ("Glossop.Local", "http://glossop.local:{port}/settings"),
    ("192.168.1.20:8090", "http://192.168.1.20:{port}/settings"),
    ("[fd00::1]:8090", "http://[fd00::1]:{port}/settings"),
    ("departures.example.com:8090", "http://departures.example.com:{port}/settings"),  # in allowed_hosts
])
def test_the_dashboards_settings_say_where_they_are_now(pair, host, link):
    response, body = pair.request("/settings/keys", host=host)
    assert response.status == 200 and response.getheader("Content-Type") == "text/html; charset=utf-8"
    address = link.format(port=pair.settings_port)
    page = body.decode()
    assert f'<a href="{address}">{address}</a>' in page
    assert f"<h1>The settings are on port {pair.settings_port}</h1>" in page and 'lang="en-GB"' in page
    assert "<p>Publish <code>8091</code> too.</p>" in page  # the install's hint
    assert "script-src" not in response.getheader("Content-Security-Policy")
    assert response.getheader("X-Frame-Options") == "DENY" and response.getheader("Cache-Control") == "no-store"


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
@pytest.mark.parametrize("host", ["evil.com", "evil.com:8090", "8.8.8.8", "glossop.local.evil.com"])
def test_the_dashboards_settings_page_keeps_the_host_check(pair, method, host):
    # Refused before anything else: no page, and no link made from a host the server doesn't answer to.
    for path in ("/settings", "/settings/login", "/settings/setup/api/session"):
        response, body = pair.request(path, method, host=host, body=b"" if method == "POST" else None)
        assert response.status == 403, (path, host)
        assert b"evil" not in body and b"8.8.8.8" not in body and b"href" not in body, (path, host)
        assert response.getheader("Location") is None
    assert pair.app.settings.asked == []


def test_the_moved_page_never_echoes_what_the_browser_sent(pair):
    response, body = pair.request("/settings/<script>?x=<b>", headers={"Cookie": "settings_session=<i>"})
    assert response.status == 200
    assert b"<script" not in body and b"<b>" not in body and b"<i>" not in body


def test_the_moved_page_without_an_address_says_the_port():
    page = server_module.moved_page(None, 8091).decode()
    assert "<code>:8091/settings</code>" in page and "<a " not in page


@pytest.mark.parametrize("path", ["/api/snapshot", "/api/events", "/api/area", f"/api/bus/{BUS_ID}",
                                  f"/api/history/{TRAIN_ID}", "/api/performance", "/static/app.css",
                                  "/static/main.js", "/performance", "/manifest.webmanifest", "/healthz", "/nowhere",
                                  "/index.html"])
def test_the_settings_listener_has_nothing_of_the_dashboards(pair, path):
    for method in ("GET", "HEAD"):
        response, body = pair.request(path, method, to="settings")
        assert response.status == 404, (path, method)
        assert body in (b"", b"Not Found"), path
        assert "script-src" not in response.getheader("Content-Security-Policy"), path


def test_the_settings_listeners_own_address_goes_to_the_dashboard_while_it_is_on(static_dir):
    on = {"now": True}
    running = Pair(make_app(static_dir, dashboard_on=lambda: on["now"]))
    try:
        response, _ = running.request("/", to="settings", host=f"glossop.local:{running.settings_port}")
        assert response.status == 303
        assert response.getheader("Location") == f"http://glossop.local:{running.dashboard_port}/"
        response, _ = running.request("/?kiosk", "HEAD", to="settings", host="[fd00::1]")
        assert response.getheader("Location") == f"http://[fd00::1]:{running.dashboard_port}/"
        on["now"] = False
        response, _ = running.request("/", to="settings")
        assert response.status == 303 and response.getheader("Location") == "/settings"
        assert running.request("/healthz", to="settings")[0].status == 404
        assert running.request("/healthz")[0].status == 200  # the dashboard's still answers, for Docker
    finally:
        running.close()


def test_the_listener_a_request_came_in_on_serves_it_whatever_its_host_says(pair):
    other = f"localhost:{pair.settings_port}"
    response, body = pair.request("/api/snapshot", host=other)  # to the dashboard's, naming the settings' port
    assert response.status == 200 and b"Glossop" in body
    response, body = pair.request("/settings/login", host=other)
    assert b"The settings are on port" in body and pair.app.settings.asked == []
    other = f"localhost:{pair.dashboard_port}"
    assert pair.request("/api/snapshot", to="settings", host=other)[0].status == 404
    response, body = pair.request("/settings/login", to="settings", host=other)
    assert response.status == 200 and b"The settings are on port" not in body
    assert pair.app.settings.asked == [("GET", "/settings/login")]


def test_a_post_from_the_dashboards_origin_is_not_same_origin_to_the_settings(pair):
    pair.app.settings.password.set("correct horse battery")
    body = b"password=correct+horse+battery"
    for origin, status in ((f"http://localhost:{pair.dashboard_port}", 403),
                           (f"http://localhost:{pair.settings_port}", 303)):
        response, _ = pair.request("/settings/login", "POST", to="settings", body=body, headers={
            "Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
        assert response.status == status, origin
        signed_in = (response.getheader("Set-Cookie") or "").startswith("settings_session=")
        assert signed_in is (status == 303), origin


ACROSS = ["/", "/settings", "/settings/login", "/settings/setup/api/session", "/api/snapshot", "/healthz",
          "/static/app.css", "/nowhere"]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "POST"])
def test_no_response_from_either_listener_allows_another_origin(pair, method):
    asking = {"Origin": "http://evil.example.com", "Access-Control-Request-Method": "POST",
              "Access-Control-Request-Headers": "x-csrf-token"}
    for to in ("dashboard", "settings"):
        for path in ACROSS:
            for host in (None, "evil.example.com"):
                response, _ = pair.request(path, method, to=to, host=host, headers=asking, body=b"")
                found = [name for name, _ in response.getheaders() if name.lower().startswith("access-control-")]
                assert found == [], (to, path, method, host)


# Fetch Metadata on the settings' listener: only its own pages, and a browser opening one


def fetched(pair, path, method="GET", **sent):
    names = {"site": "Sec-Fetch-Site", "mode": "Sec-Fetch-Mode", "dest": "Sec-Fetch-Dest"}
    headers = {names[key]: value for key, value in sent.items()}
    return pair.request(path, method, to="settings", headers=headers, body=b"" if method == "POST" else None)[0]


@pytest.mark.parametrize("site, mode, dest", [
    ("same-site", "cors", "empty"),  # the dashboard's script fetching the settings
    ("same-site", "no-cors", "image"),  # an <img> on the dashboard's page
    ("same-site", "navigate", "iframe"),  # a frame
    ("cross-site", "cors", "empty"),
    ("cross-site", "no-cors", "script"),
    ("cross-site", "navigate", "iframe"),
    ("same-site", "navigate", "documents"),
    ("anywhere", "navigate", "document"),  # not a value a browser sends
])
def test_the_settings_refuse_what_another_sites_page_fetches(pair, site, mode, dest):
    for path in ("/settings", "/settings/login", "/settings/setup/api/session", "/settings/style.css", "/"):
        assert fetched(pair, path, site=site, mode=mode, dest=dest).status == 403, path
    assert pair.app.settings.asked == []


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_a_browser_may_open_the_settings_from_another_page(pair, site):
    assert fetched(pair, "/settings/login", site=site, mode="navigate", dest="document").status == 200
    for method in ("POST", "HEAD", "PUT"):  # but only open them
        assert fetched(pair, "/settings/login", method, site=site, mode="navigate", dest="document").status == 403


def test_the_settings_take_their_own_requests_and_those_without_fetch_metadata(pair):
    assert fetched(pair, "/settings/login").status == 200  # an older browser, or curl
    assert fetched(pair, "/settings/login", site="same-origin", mode="cors", dest="empty").status == 200
    assert fetched(pair, "/settings/login", site="none", mode="navigate", dest="document").status == 200
    assert fetched(pair, "/settings/login", site="Same-Origin").status == 200


@pytest.mark.parametrize("twice", [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "navigate"),
                                   ("Sec-Fetch-Dest", "document")])
def test_fetch_metadata_sent_twice_is_refused(pair, twice):
    conn = http.client.HTTPConnection("127.0.0.1", pair.settings_port, timeout=5)
    conn.putrequest("GET", "/settings/login", skip_host=True)
    conn.putheader("Host", f"localhost:{pair.settings_port}")
    for name, value in [("Sec-Fetch-Site", "same-site"), ("Sec-Fetch-Mode", "navigate"),
                        ("Sec-Fetch-Dest", "document"), twice]:
        conn.putheader(name, value)
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()


def test_the_dashboard_keeps_its_own_fetch_metadata_rules(pair):
    sent = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
    assert pair.request("/api/snapshot", headers=sent)[0].status == 200
    assert pair.request("/api/snapshot", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403


# The caps hold for both listeners together


def connect_to(server, address):
    """A connection to one listener, as if from `address` (see connect_from)."""
    ours, theirs = socket.socketpair()
    ours.settimeout(5)
    server.process_request(theirs, (address, 50000))
    return ours


def test_the_connection_caps_are_shared_by_both_listeners(static_dir):
    running = Pair(make_app(static_dir), max_connections=4, max_per_client=2)
    dashboard, settings = running.servers.dashboard, running.servers.settings
    try:
        assert dashboard.limits is settings.limits  # the event streams' caps among them
        hogs = [connect_to(dashboard, "192.168.1.66"), connect_to(settings, "192.168.1.66")]
        for server in (dashboard, settings):
            with connect_to(server, "192.168.1.66") as extra:  # past its address's cap on either: closed unanswered
                assert extra.recv(100) == b""
        hogs += [connect_to(settings, "192.168.1.20"), connect_to(dashboard, "192.168.1.21")]
        for server in (dashboard, settings):
            with connect_to(server, "192.168.1.22") as extra:  # past the total on either
                assert extra.recv(100) == b""
        for hog in hogs:
            hog.close()
    finally:
        running.close()


def test_both_listeners_bind_before_either_serves_and_a_busy_one_names_its_port(static_dir):
    app = make_app(static_dir)
    blocker = make_server(app, "127.0.0.1", 0, role="dashboard")
    busy = blocker.server_address[1]
    try:
        with pytest.raises(OSError, match=f"Port {busy} already in use"):
            make_servers(app, "127.0.0.1", 0, busy)
        with pytest.raises(OSError, match=f"Port {busy} already in use"):
            make_servers(app, "127.0.0.1", busy, 0)
    finally:
        blocker.server_close()


def test_a_desktop_takes_the_next_pair_both_free(static_dir):
    app = make_app(static_dir)
    with socket.socket() as probe:  # a free port, with those after it for the pairs
        probe.bind(("127.0.0.1", 0))
        base = probe.getsockname()[1]
    blocker = make_server(app, "127.0.0.1", base + 1, role="dashboard")  # the first pair's settings port is taken
    try:
        servers = make_servers(app, "127.0.0.1", base, base + 1, attempts=3)
        # The next pair, or the one after if another test running beside this one has a port of it.
        assert servers.ports in ((base + 2, base + 3), (base + 4, base + 5))
        assert (servers.dashboard.role, servers.settings.role) == ("dashboard", "settings")
        assert servers.dashboard.settings_port == servers.settings.settings_port == servers.ports[1]
        assert servers.dashboard.dashboard_port == servers.settings.dashboard_port == servers.ports[0]
        servers.server_close()
        with pytest.raises(OSError, match=f"Port {base + 1} already in use"):
            make_servers(app, "127.0.0.1", base, base + 1)  # one attempt: that port or nothing
    finally:
        blocker.server_close()


def test_a_listener_has_one_of_the_roles():
    with pytest.raises(ValueError, match="role"):
        server_module.DashboardServer(("127.0.0.1", 0), None, "everything")  # type: ignore[arg-type]


# The settings' listener stopping by itself stops the dashboard's, so the service starts again whole.


def serving(servers):
    """servers.serve_forever on a thread of its own, once the dashboard's listener answers."""
    thread = threading.Thread(target=servers.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("how", ["raises", "returns"])
def test_the_settings_listener_stopping_by_itself_stops_the_dashboards(static_dir, caplog, how):
    servers = make_servers(make_app(static_dir), "127.0.0.1", 0, 0)

    def stops(poll_interval=0.5):
        if how == "raises":
            raise OSError("the settings' loop broke")

    servers.settings.serve_forever = stops
    try:
        thread = serving(servers)
        thread.join(5)
        assert not thread.is_alive()  # the dashboard's listener stopped too
        assert servers.failed
        assert "settings" in caplog.text and "stopped" in caplog.text
        assert "the settings' loop broke" not in caplog.text  # its type, never its words
    finally:
        servers.server_close()


def test_stopping_both_as_asked_is_no_failure(static_dir):
    servers = make_servers(make_app(static_dir), "127.0.0.1", 0, 0)
    try:
        thread = serving(servers)
        conn = http.client.HTTPConnection("127.0.0.1", servers.ports[1], timeout=5)
        conn.request("GET", "/settings/login", headers={"Host": "localhost"})
        assert conn.getresponse().status == 200
        conn.close()
        servers.shutdown()
        thread.join(5)
        assert not thread.is_alive() and not servers.failed
    finally:
        servers.server_close()


def test_an_interrupt_on_the_dashboards_listener_is_no_failure_either(static_dir):
    servers = make_servers(make_app(static_dir), "127.0.0.1", 0, 0)

    def interrupted(poll_interval=0.5):
        time.sleep(0.2)  # the settings' listener under way
        raise KeyboardInterrupt

    servers.dashboard.serve_forever = interrupted
    try:
        with pytest.raises(KeyboardInterrupt):
            servers.serve_forever(0.05)
        assert not servers.failed
    finally:
        servers.server_close()
