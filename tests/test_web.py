import http.client
import re
import threading
from urllib.parse import urlencode

import pytest

from departureboard.web import (
    FAILURE_WINDOW_S,
    MAX_FAILURES,
    SESSION_SECONDS,
    Password,
    Sessions,
    SignInLimiter,
    local_host,
    make_handler,
    make_server,
    private_address,
)

PASSWORD = "correct horse battery"


# --- Password ---


def test_password_is_stored_hashed_and_private(tmp_path):
    password = Password(tmp_path / "web.json")
    assert not password.is_set()
    password.set(PASSWORD)
    stored = (tmp_path / "web.json").read_text()
    assert PASSWORD not in stored
    assert (tmp_path / "web.json").stat().st_mode & 0o777 == 0o600
    assert password.check(PASSWORD)
    assert not password.check("wrong password!")


def test_short_passwords_are_refused(tmp_path):
    with pytest.raises(ValueError, match="10"):
        Password(tmp_path / "web.json").set("short")


def test_missing_or_corrupt_password_file_never_matches(tmp_path):
    assert not Password(tmp_path / "none.json").check("")
    (tmp_path / "bad.json").write_text("{nope")
    assert not Password(tmp_path / "bad.json").check(PASSWORD)


# --- Sessions and limiter ---


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_sessions_expire_and_end():
    clock = Clock()
    sessions = Sessions(clock)
    token, session = sessions.create()
    assert sessions.get(token) is session
    assert sessions.get("guess") is None
    clock.now += SESSION_SECONDS + 1
    assert sessions.get(token) is None
    token, _ = sessions.create()
    sessions.end(token)
    assert sessions.get(token) is None


def test_tokens_and_form_tokens_are_long_and_unique():
    sessions = Sessions()
    (a, sa), (b, sb) = sessions.create(), sessions.create()
    assert a != b and sa.csrf != sb.csrf and len(a) >= 40


def test_sign_in_is_locked_after_repeated_failures_then_released():
    clock = Clock()
    limiter = SignInLimiter(clock)
    for _ in range(MAX_FAILURES):
        assert limiter.allowed("10.0.0.5")
        limiter.failed("10.0.0.5")
    assert not limiter.allowed("10.0.0.5")
    assert limiter.allowed("10.0.0.6")  # per address
    clock.now += FAILURE_WINDOW_S + 1
    assert limiter.allowed("10.0.0.5")


# --- Address and host checks ---


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.20", "10.1.2.3", "172.16.0.9", "::1", "fe80::1%eth0", "fd00::5", "::ffff:192.168.1.2"])
def test_home_network_addresses_are_allowed(address):
    assert private_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2a00:1450::1", "::ffff:8.8.8.8", "nonsense"])
def test_internet_addresses_are_refused(address):
    assert not private_address(address)


@pytest.mark.parametrize("host", ["192.168.1.20:8080", "departureboard:8080", "raspberrypi.local", "pi.lan:8080", "board.home.arpa", "[fe80::1]:8080", "localhost:8080"])
def test_local_host_names_are_allowed(host):
    assert local_host(host)


@pytest.mark.parametrize("host", ["evil.example.com", "8.8.8.8", "board.local.evil.com", "", None, "a..local", "x_y.local"])
def test_other_host_names_are_refused(host):
    assert not local_host(host)


# --- The server, end to end ---


class FakePage:
    def __init__(self):
        self.saved = []

    def render(self, csrf, message="", errors=None, values=None):
        return f'<form><input name="csrf" value="{csrf}"></form><p>{message}</p><p>{errors or ""}</p>'

    def save(self, fields):
        if fields.get("title") == "bad":
            return {"title": "Too bad"}
        self.saved.append(fields)
        return {}


@pytest.fixture
def server(tmp_path):
    password = Password(tmp_path / "web.json")
    password.set(PASSWORD)
    page = FakePage()
    handler = make_handler(page, password, Sessions(), SignInLimiter(), lambda title, body: f"<title>{title}</title>{body}", b"body{}")
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], page, password
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, host=None, origin="same", cookie=None):
    host = host or f"localhost:{port}"
    headers = {"Host": host}
    if origin == "same":
        headers["Origin"] = f"http://{host}"
    elif origin:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = urlencode(body)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, data, headers)
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response, text


def sign_in(port):
    response, _ = request(port, "POST", "/login", {"password": PASSWORD})
    assert response.status == 303
    cookie = response.getheader("Set-Cookie").split(";")[0]
    _, page = request(port, "GET", "/", cookie=cookie)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return cookie, csrf


def test_settings_need_signing_in(server):
    port, _, _ = server
    response, _ = request(port, "GET", "/")
    assert response.status == 303 and response.getheader("Location") == "/login"


def test_every_response_carries_the_security_headers(server):
    port, _, _ = server
    response, _ = request(port, "GET", "/login")
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")
    assert "frame-ancestors 'none'" in response.getheader("Content-Security-Policy")
    assert response.getheader("X-Frame-Options") == "DENY"
    assert response.getheader("Cache-Control") == "no-store"
    assert response.getheader("Server") == "departureboard"


def test_sign_in_sets_a_strict_http_only_cookie(server):
    port, _, _ = server
    response, _ = request(port, "POST", "/login", {"password": PASSWORD})
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie


def test_wrong_password_is_refused_without_saying_why(server):
    port, _, _ = server
    response, text = request(port, "POST", "/login", {"password": "nope nope nope"})
    assert response.status == 401
    assert "isn't right" in text and response.getheader("Set-Cookie") is None


def test_repeated_wrong_passwords_lock_sign_in(server):
    port, _, _ = server
    for _ in range(MAX_FAILURES):
        request(port, "POST", "/login", {"password": "nope nope nope"})
    response, _ = request(port, "POST", "/login", {"password": PASSWORD})
    assert response.status == 429


def test_signed_in_save_with_the_form_token_works(server):
    port, page, _ = server
    cookie, csrf = sign_in(port)
    response, text = request(port, "POST", "/", {"csrf": csrf, "title": "Glossop"}, cookie=cookie)
    assert response.status == 200 and "Saved" in text
    assert page.saved == [{"title": "Glossop"}]


def test_save_without_the_form_token_is_refused(server):
    port, page, _ = server
    cookie, _ = sign_in(port)
    response, _ = request(port, "POST", "/", {"csrf": "forged", "title": "x"}, cookie=cookie)
    assert response.status == 403 and page.saved == []


def test_save_from_another_site_is_refused(server):
    port, page, _ = server
    cookie, csrf = sign_in(port)
    response, _ = request(port, "POST", "/", {"csrf": csrf, "title": "x"}, cookie=cookie, origin="http://evil.example.com")
    assert response.status == 403 and page.saved == []
    response, _ = request(port, "POST", "/", {"csrf": csrf, "title": "x"}, cookie=cookie, origin=None)
    assert response.status == 403 and page.saved == []


def test_requests_naming_an_outside_host_are_refused(server):
    port, _, _ = server
    response, _ = request(port, "GET", "/login", host="evil.example.com")
    assert response.status == 403


def test_save_errors_come_back_on_the_form(server):
    port, page, _ = server
    cookie, csrf = sign_in(port)
    response, text = request(port, "POST", "/", {"csrf": csrf, "title": "bad"}, cookie=cookie)
    assert response.status == 422 and "Too bad" in text and page.saved == []


def test_sign_out_ends_the_session(server):
    port, _, _ = server
    cookie, csrf = sign_in(port)
    response, _ = request(port, "POST", "/logout", {"csrf": csrf}, cookie=cookie)
    assert response.status == 303
    response, _ = request(port, "GET", "/", cookie=cookie)
    assert response.status == 303 and response.getheader("Location") == "/login"


def test_oversized_or_wrong_type_bodies_are_refused(server):
    port, _, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/login", "{}", {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}", "Content-Type": "application/json"})
    assert conn.getresponse().status == 400
    conn.close()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/login", "a=1", {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}", "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(10**7)})
    assert conn.getresponse().status == 400
    conn.close()


def test_without_a_password_the_page_says_how_to_set_one(tmp_path):
    handler = make_handler(FakePage(), Password(tmp_path / "none.json"), Sessions(), SignInLimiter(), lambda t, b: b, b"")
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        _, text = request(port, "GET", "/login")
        assert "--set-password" in text
        response, _ = request(port, "POST", "/login", {"password": "anything at all"})
        assert response.status == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_client_hanging_up_early_is_not_reported(capsys):
    from departureboard.web import _Server

    server = _Server.__new__(_Server)
    try:
        raise BrokenPipeError(32, "Broken pipe")
    except BrokenPipeError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""
