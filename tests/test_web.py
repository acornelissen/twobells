import http.client
import json
import re
import threading
import time
from urllib.parse import urlencode

import pytest

from departureboard.web import (
    FAILURE_WINDOW_S,
    MAX_FAILURES,
    SESSION_SECONDS,
    WRONG_CODE,
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
        assert limiter.attempt("10.0.0.5")
    assert not limiter.attempt("10.0.0.5")
    assert limiter.attempt("10.0.0.6")  # per address
    clock.now += FAILURE_WINDOW_S + 1
    assert limiter.attempt("10.0.0.5")


def test_a_right_password_clears_the_attempts():
    limiter = SignInLimiter(Clock())
    for _ in range(MAX_FAILURES - 1):
        limiter.attempt("10.0.0.5")
    limiter.succeeded("10.0.0.5")
    assert all(limiter.attempt("10.0.0.5") for _ in range(MAX_FAILURES))


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
    # The page may set its form-token cookie, but never a session.
    assert "isn't right" in text and not (response.getheader("Set-Cookie") or "").startswith("session=")


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
    assert "<title>Saved – Board settings</title>" in text
    assert "Saved. The board shows it within a few minutes; the screen within seconds." in text


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


def test_pages_let_the_browser_send_their_own_origin(server):
    # With no-referrer, browsers send `Origin: null` on form posts and every
    # real sign-in would look cross-site.
    port, _, _ = server
    response, _ = request(port, "GET", "/login")
    assert response.getheader("Referrer-Policy") == "same-origin"


def test_a_null_origin_without_a_referer_is_refused(server):
    port, page, _ = server
    cookie, csrf = sign_in(port)
    response, _ = request(port, "POST", "/", {"csrf": csrf, "title": "x"}, cookie=cookie, origin="null")
    assert response.status == 403 and page.saved == []
    response, _ = request(port, "POST", "/login", {"password": PASSWORD}, origin="null")
    assert response.status == 403


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


class SlowPassword(Password):
    """Counts checks, and holds each one until `go` is set."""

    def __init__(self, path):
        super().__init__(path)
        self.checks = 0
        self.inside = threading.Semaphore(0)
        self.go = threading.Event()
        self._lock = threading.Lock()

    def check(self, password):
        with self._lock:
            self.checks += 1
        self.inside.release()
        self.go.wait(10)
        return False


@pytest.fixture
def slow_server(tmp_path):
    password = SlowPassword(tmp_path / "web.json")
    password.set(PASSWORD)
    handler = make_handler(FakePage(), password, Sessions(), SignInLimiter(), lambda t, b: b, b"")
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], password
    password.go.set()
    httpd.shutdown()
    httpd.server_close()


def test_simultaneous_guesses_cannot_beat_the_limit(slow_server):
    port, password = slow_server
    statuses = []

    def guess():
        for _ in range(5):  # a burst can overflow the listen queue; try again as a browser would
            try:
                response, _ = request(port, "POST", "/login", {"password": "nope nope nope"})
            except OSError:  # reset, or EINVAL on macOS after a reset
                continue
            statuses.append(response.status)
            return

    threads = [threading.Thread(target=guess) for _ in range(MAX_FAILURES + 5)]
    for thread in threads:
        thread.start()
    # Hold every check that gets in until as many as the limit could be in at once.
    for _ in range(MAX_FAILURES + 1):
        if not password.inside.acquire(timeout=1):
            break
    password.go.set()
    for thread in threads:
        thread.join(15)
    assert len(statuses) > MAX_FAILURES  # enough guesses got answers to beat the limit, had they been checked
    assert password.checks <= MAX_FAILURES


def test_simultaneous_attempts_are_counted_exactly():
    limiter = SignInLimiter()
    allowed = []
    threads = [threading.Thread(target=lambda: allowed.append(limiter.attempt("10.0.0.5"))) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == MAX_FAILURES


def test_only_two_passwords_are_checked_at_once(slow_server):
    port, password = slow_server
    results = []

    def guess():
        results.append(request(port, "POST", "/login", {"password": "nope nope nope"}))

    first = [threading.Thread(target=guess) for _ in range(2)]
    for thread in first:
        thread.start()
    assert password.inside.acquire(timeout=5) and password.inside.acquire(timeout=5)
    response, text = request(port, "POST", "/login", {"password": "nope nope nope"})
    assert response.status == 429 and "Busy" in text
    password.go.set()
    for thread in first:
        thread.join(15)
    assert [r.status for r, _ in results] == [401, 401]
    assert password.checks == 2


def test_slow_clients_are_dropped():
    handler = make_handler(FakePage(), Password("none.json"), Sessions(), SignInLimiter(), lambda t, b: b, b"")
    assert handler.timeout and handler.timeout <= 10


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


# --- Forgotten password and first claim, with a code from the board ---

NEW_PASSWORD = "a brand new password"


class Recorder:
    """Every response seen, so a test can show no code was ever in one."""

    def __init__(self, port):
        self.port = port
        self.seen = []

    def __call__(self, method, path, body=None, cookie=None, origin="same"):
        response, text = request(self.port, method, path, body, origin=origin, cookie=cookie)
        self.seen.append(text + "".join(f"{k}: {v}\n" for k, v in response.getheaders()))
        return response, text

    def form(self, path="/login", cookie=None):
        """(Cookie header, hidden form value) from a pre-session page."""
        response, text = self("GET", path, cookie=cookie)
        token = re.search(r'name="form" value="([^"]+)"', text).group(1)
        return response.getheader("Set-Cookie").split(";")[0], token


def board_code(folder):
    return json.loads((folder / "reset-display.json").read_text())["code"]


@pytest.fixture
def reset_server(tmp_path, caplog):
    caplog.set_level("DEBUG")
    clock = Clock()
    clock.now = time.time()
    password = Password(tmp_path / "web.json")
    sessions = Sessions()
    handler = make_handler(FakePage(), password, sessions, SignInLimiter(), lambda t, b: f"<title>{t}</title>{b}", b"",
                           clock=clock)
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Recorder(httpd.server_address[1]), password, tmp_path, clock
    httpd.shutdown()
    httpd.server_close()


def ask_for_code(http, cookie=None):
    form_cookie, token = http.form(cookie=cookie)
    response, _ = http("POST", "/forgot", {"form": token}, cookie=form_cookie)
    assert response.status == 303 and response.getheader("Location") == "/reset?asked"
    return form_cookie


def reset(http, form_cookie, code, new=NEW_PASSWORD, again=None):
    _, token = http.form("/reset?asked", cookie=form_cookie)
    return http("POST", "/reset", {"form": token, "code": code, "password": new, "again": again or new},
                cookie=form_cookie)


def assert_never_shown(code, http, caplog):
    # Random tokens first, which could hold any six digits by chance.
    tokens = re.compile(r'(name="form" value=|form=|session=)[^";\s]+')
    for text in http.seen:
        text = tokens.sub(r"\1(token)", text)
        assert code not in text and f"{code[:3]} {code[3:]}" not in text
    for record in caplog.records:
        assert code not in record.getMessage() and f"{code[:3]} {code[3:]}" not in record.getMessage()


def test_a_forgotten_password_is_reset_with_the_code_from_the_board(reset_server, caplog):
    http, password, folder, _ = reset_server
    password.set(PASSWORD)
    _, sign_in_page = http("GET", "/login")
    assert 'action="/forgot"' in sign_in_page and "Forgot password?" in sign_in_page
    old = http("POST", "/login", {"password": PASSWORD})[0].getheader("Set-Cookie").split(";")[0]

    form_cookie = ask_for_code(http)
    _, page = http("GET", "/reset?asked", cookie=form_cookie)
    assert "Look at the board" in page and 'name="code"' in page and 'autocomplete="new-password"' in page
    code = board_code(folder)
    response, _ = reset(http, form_cookie, f"{code[:3]} {code[3:]}")

    assert response.status == 303 and response.getheader("Location") == "/"
    new = response.getheader("Set-Cookie")
    assert new.startswith("session=") and "HttpOnly" in new and "SameSite=Strict" in new
    assert http("GET", "/", cookie=new.split(";")[0])[0].status == 200
    assert http("GET", "/", cookie=old)[0].getheader("Location") == "/login"  # every old session ended
    assert password.check(NEW_PASSWORD) and not password.check(PASSWORD)
    assert not (folder / "reset-display.json").exists()  # used, so off the board
    assert reset(http, form_cookie, code, "yet another password")[0].status == 401  # and once only
    assert_never_shown(code, http, caplog)


def test_a_wrong_code_changes_nothing_and_says_no_more_than_that(reset_server, caplog):
    http, password, folder, _ = reset_server
    password.set(PASSWORD)
    form_cookie, _ = http.form()
    # No code asked for yet: the same answer as for a wrong one.
    response, without = reset(http, form_cookie, "123456")
    assert response.status == 401 and WRONG_CODE in without
    ask_for_code(http, form_cookie)
    code = board_code(folder)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    error = re.compile(r'<p class="error"[^>]*>(.*?)</p>')
    answers = set()
    for _ in range(5):
        response, text = reset(http, form_cookie, wrong)
        assert response.status == 401 and not response.getheader("Set-Cookie", "").startswith("session=")
        answers.add(error.search(text).group(1))
    response, text = reset(http, form_cookie, code)  # voided after five wrong
    assert response.status == 401
    answers.add(error.search(text).group(1))
    answers.add(error.search(without).group(1))
    assert len(answers) == 1
    assert 'id="code"' in text and 'aria-invalid="true"' in text
    assert password.check(PASSWORD)
    assert_never_shown(code, http, caplog)


def test_a_typo_in_the_new_password_does_not_use_up_the_code(reset_server, caplog):
    http, password, folder, _ = reset_server
    password.set(PASSWORD)
    form_cookie = ask_for_code(http)
    code = board_code(folder)
    response, text = reset(http, form_cookie, code, NEW_PASSWORD, NEW_PASSWORD + "!")
    assert response.status == 422 and "don't match" in text
    response, text = reset(http, form_cookie, code, "short")
    assert response.status == 422 and "at least 10" in text
    assert reset(http, form_cookie, code)[0].status == 303
    assert_never_shown(code, http, caplog)


def test_the_first_claim_needs_a_code_from_the_board(reset_server, caplog):
    http, password, folder, _ = reset_server
    _, page = http("GET", "/login")
    assert "Claim this board" in page and 'action="/forgot"' in page and 'action="/reset"' in page
    assert "--set-password" in page and "--reset-code" in page
    form_cookie, _ = http.form()
    assert reset(http, form_cookie, "000000")[0].status == 401
    assert not password.is_set()
    assert http("POST", "/login", {"password": NEW_PASSWORD})[0].status == 403
    ask_for_code(http, form_cookie)
    code = board_code(folder)
    response, _ = reset(http, form_cookie, code)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("session=")
    assert password.check(NEW_PASSWORD)
    _, page = http("GET", "/login")
    assert "Claim this board" not in page  # claimed: an ordinary sign-in now
    assert_never_shown(code, http, caplog)


def test_asking_for_a_code_needs_this_browsers_form_token(reset_server):
    http, password, folder, clock = reset_server
    password.set(PASSWORD)
    form_cookie, token = http.form()
    other_cookie, other_token = http.form()
    attempts = [
        ({"form": token}, None, "same"),  # no cookie: posted from a page that never set it
        ({}, form_cookie, "same"),  # no token
        ({"form": other_token}, form_cookie, "same"),  # another browser's token
        ({"form": token.replace(token[-1], "0" if token[-1] != "0" else "1")}, form_cookie, "same"),
        ({"form": "9999999999." + token.split(".")[1]}, form_cookie, "same"),  # expiry stretched
        ({"form": token}, form_cookie, "http://evil.example.com"),  # cross-site
        ({"form": token}, form_cookie, None),  # no Origin at all
    ]
    for body, cookie, origin in attempts:
        response, _ = http("POST", "/forgot", body, cookie=cookie, origin=origin)
        assert response.status == 403, (body, cookie, origin)
    assert not (folder / "reset-display.json").exists()
    clock.now += 31 * 60  # the token has run out
    assert http("POST", "/forgot", {"form": token}, cookie=form_cookie)[0].status == 403
    assert not (folder / "reset-display.json").exists()
    form_cookie, token = http.form()
    assert http("POST", "/forgot", {"form": token}, cookie=form_cookie)[0].status == 303
    assert (folder / "reset-display.json").exists()


def test_a_reset_needs_the_form_token_too(reset_server):
    http, password, folder, _ = reset_server
    password.set(PASSWORD)
    form_cookie = ask_for_code(http)
    code = board_code(folder)
    response, _ = http("POST", "/reset", {"form": "1.bad", "code": code, "password": NEW_PASSWORD,
                                          "again": NEW_PASSWORD}, cookie=form_cookie)
    assert response.status == 403 and password.check(PASSWORD)
    assert reset(http, form_cookie, code)[0].status == 303  # the code wasn't spent on it


def test_asking_again_too_soon_looks_the_same_and_keeps_the_code(reset_server):
    http, password, folder, _ = reset_server
    password.set(PASSWORD)
    form_cookie = ask_for_code(http)
    code = board_code(folder)
    ask_for_code(http, form_cookie)  # within the minute: limited, but answered the same
    assert board_code(folder) == code


def test_the_form_cookie_is_strict_http_only_and_short_lived(reset_server):
    http, password, _, _ = reset_server
    response, _ = http("GET", "/login")
    cookie = response.getheader("Set-Cookie")
    assert cookie.startswith("form=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Max-Age=1800" in cookie
    # A second page in the same browser keeps its value, so two tabs both work.
    again = http("GET", "/reset", cookie=cookie.split(";")[0])[0].getheader("Set-Cookie")
    assert again.split(";")[0] == cookie.split(";")[0]


# --- The setup wizard, mounted under /setup/ ---


class FakeSteps:
    """setupapi.SetupApi's handle(), recording what reached it."""

    def __init__(self):
        self.calls = []

    def handle(self, name, payload):
        self.calls.append((name, payload))
        if name != "begin":
            return 404, {"error": "No such setup step"}
        return 200, {"keys": [], "links": [], "job": {"state": "idle"}}


@pytest.fixture
def wizard_server(tmp_path):
    from departureboard.web import Wizard

    password = Password(tmp_path / "web.json")
    password.set(PASSWORD)
    steps = FakeSteps()
    needed = {"now": False}
    sessions = Sessions()
    wizard = Wizard.load(steps, lambda: needed["now"])
    handler = make_handler(FakePage(), password, sessions, SignInLimiter(), lambda t, b: f"<title>{t}</title>{b}",
                           b"body{}", wizard=wizard)
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()  # quick to shut down
    yield httpd.server_address[1], steps, needed, password, tmp_path, sessions
    httpd.shutdown()
    httpd.server_close()


def step(port, name, body=b"{}", cookie=None, csrf=None, origin="same", content_type="application/json",
         length=None):
    """POST a wizard step as the page does: JSON, with the form token in a header."""
    host = f"localhost:{port}"
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", f"/setup/api/{name}", skip_host=True)
    conn.putheader("Host", host)
    if origin == "same":
        conn.putheader("Origin", f"http://{host}")
    elif origin:
        conn.putheader("Origin", origin)
    if cookie:
        conn.putheader("Cookie", cookie)
    if csrf is not None:
        conn.putheader("X-CSRF-Token", csrf)
    if content_type:
        conn.putheader("Content-Type", content_type)
    if length != "none":
        conn.putheader("Content-Length", str(len(body) if length is None else length))
    conn.endheaders()
    conn.send(body)
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response, text


def signed_in(wizard_server):
    """A session cookie, made directly: the sign-in itself is tested above, and is slow on purpose."""
    token, _ = wizard_server[-1].create()
    return f"session={token}"


def session_token(port, cookie):
    response, text = request(port, "GET", "/setup/api/session", cookie=cookie)
    assert response.status == 200 and response.getheader("Content-Type").startswith("application/json")
    return json.loads(text)["csrf"]


def test_the_wizard_needs_signing_in(wizard_server):
    port, steps, *_ = wizard_server
    response, _ = request(port, "GET", "/setup/")
    assert response.status == 303 and response.getheader("Location") == "/login"
    assert request(port, "GET", "/setup/wizard_static/wizard.js")[0].status == 401
    response, text = request(port, "GET", "/setup/api/session")
    assert response.status == 401 and "csrf" not in json.loads(text)
    response, _ = step(port, "begin", csrf="anything")
    assert response.status == 401 and steps.calls == []
    response, _ = step(port, "begin", cookie="session=guessed", csrf="anything")
    assert response.status == 401 and steps.calls == []


def test_setup_without_the_slash_goes_to_the_wizard(wizard_server):
    port, *_ = wizard_server
    response, _ = request(port, "GET", "/setup")
    assert response.status == 303 and response.getheader("Location") == "/setup/"


def test_signed_in_the_wizards_files_come_with_their_own_types(wizard_server):
    from departureboard.setupapi import static_files

    port, *_ = wizard_server
    cookie = signed_in(wizard_server)
    types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8"}
    for relative, path in static_files().items():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/setup/{relative}", headers={"Host": f"localhost:{port}", "Cookie": cookie})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        assert response.status == 200, relative
        assert response.getheader("Content-Type") == types[path.suffix], relative
        assert body == path.read_bytes(), relative
        assert response.getheader("X-Content-Type-Options") == "nosniff"


@pytest.mark.parametrize("path", [
    "/setup/index.html",  # the page is served at the prefix only
    "/setup/wizard_static/index.html",
    "/setup/wizard_static/../web.py",
    "/setup/wizard_static/%2e%2e/web.py",
    "/setup/dashboard_static/app.js",  # the dashboard's own, not the wizard's
    "/setup/dashboard_static/vendor/leaflet-1.9.4/images/marker-icon.png",
    "/setup/setupapi.py",
    "/setup/api/begin",  # steps are POST only
    "/setup/wizard_static/wizard.js/",
])
def test_only_the_wizards_own_files_are_served(wizard_server, path):
    port, *_ = wizard_server
    cookie = signed_in(wizard_server)
    assert request(port, "GET", path, cookie=cookie)[0].status == 404


def test_only_the_wizard_may_run_scripts(wizard_server):
    from departureboard.setupapi import PAGE_HEADERS

    port, *_ = wizard_server
    cookie = signed_in(wizard_server)
    for path in ("/", "/login", "/reset", "/style.css"):
        policy = request(port, "GET", path, cookie=cookie)[0].getheader("Content-Security-Policy")
        assert "default-src 'none'" in policy and "script-src" not in policy, path
    for path in ("/setup/", "/setup/wizard_static/wizard.js", "/setup/api/session"):
        response, _ = request(port, "GET", path, cookie=cookie)
        for name, value in PAGE_HEADERS.items():
            assert response.getheader(name) == value, (path, name)
        assert "script-src 'self'" in response.getheader("Content-Security-Policy")
    response, _ = step(port, "begin", cookie=cookie, csrf=session_token(port, cookie))
    assert response.getheader("Content-Security-Policy") == PAGE_HEADERS["Content-Security-Policy"]


def test_a_step_runs_with_the_sessions_token_in_its_header(wizard_server):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    csrf = session_token(port, cookie)
    response, text = step(port, "begin", b'{"x": [1, 2]}', cookie=cookie, csrf=csrf)
    assert response.status == 200 and response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert json.loads(text)["job"] == {"state": "idle"}
    assert steps.calls == [("begin", {"x": [1, 2]})]


def test_a_step_without_the_right_token_is_refused(wizard_server):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    other = signed_in(wizard_server)
    for csrf in (None, "", "forged", session_token(port, other), session_token(port, cookie) + "x"):
        response, text = step(port, "begin", cookie=cookie, csrf=csrf)
        assert response.status == 403 and "error" in json.loads(text), csrf
    assert steps.calls == []


def test_a_step_from_another_site_is_refused(wizard_server):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    csrf = session_token(port, cookie)
    for origin in ("http://evil.example.com", "null", None):
        assert step(port, "begin", cookie=cookie, csrf=csrf, origin=origin)[0].status == 403, origin
    assert steps.calls == []


def test_a_step_from_outside_the_home_network_is_refused(wizard_server):
    port, steps, *_ = wizard_server
    response, _ = request(port, "GET", "/setup/", host="evil.example.com")
    assert response.status == 403
    response, _ = request(port, "GET", "/setup/api/session", host="evil.example.com")
    assert response.status == 403


@pytest.mark.parametrize("change, status", [
    ({"content_type": "application/x-www-form-urlencoded", "body": b"a=1"}, 415),
    ({"content_type": "text/plain"}, 415),
    ({"content_type": None}, 415),
    ({"body": b"{" + b" " * 20000 + b"}"}, 413),
    ({"length": 10**7}, 413),
    ({"length": -1}, 413),
    ({"length": "none"}, 411),
    ({"body": b"not json"}, 400),
    ({"body": b"[" * 50 + b"]" * 50}, 400),  # nested too deep
    ({"body": b'{"n": NaN}'}, 400),
])
def test_a_step_must_be_json_of_a_sensible_size(wizard_server, change, status):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    csrf = session_token(port, cookie)
    response, text = step(port, "begin", cookie=cookie, csrf=csrf, **change)
    assert response.status == status and "error" in json.loads(text)
    assert steps.calls == []


@pytest.mark.parametrize("name", ["Begin", "be-gin", "begin/x", "a" * 21, ""])
def test_a_step_name_is_plain_lowercase(wizard_server, name):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    response, _ = step(port, name, cookie=cookie, csrf=session_token(port, cookie))
    assert response.status == 404 and steps.calls == []


def test_an_unknown_step_is_the_apis_to_refuse(wizard_server):
    port, steps, *_ = wizard_server
    cookie = signed_in(wizard_server)
    response, _ = step(port, "nosuch", cookie=cookie, csrf=session_token(port, cookie))
    assert response.status == 404 and steps.calls == [("nosuch", {})]


def test_without_a_wizard_nothing_is_under_setup(server):
    port, *_ = server
    cookie, csrf = sign_in(port)
    assert request(port, "GET", "/setup/", cookie=cookie)[0].status == 404
    assert step(port, "begin", cookie=cookie, csrf=csrf)[0].status == 400  # not a form post, and no wizard


def test_a_file_of_an_unknown_kind_is_refused_at_start(tmp_path):
    from departureboard.web import Wizard

    (tmp_path / "picture.svg").write_text("<svg/>")
    with pytest.raises(ValueError, match="content type"):
        Wizard.load(FakeSteps(), paths={"picture.svg": tmp_path / "picture.svg"})


def test_until_the_board_is_set_up_signing_in_opens_the_wizard(wizard_server):
    port, _, needed, *_ = wizard_server
    response, _ = request(port, "POST", "/login", {"password": PASSWORD})
    assert response.getheader("Location") == "/"
    needed["now"] = True
    response, _ = request(port, "POST", "/login", {"password": PASSWORD})
    assert response.status == 303 and response.getheader("Location") == "/setup/"
    # The settings page itself is still there, for the wizard's link back.
    cookie = response.getheader("Set-Cookie").split(";")[0]
    assert request(port, "GET", "/", cookie=cookie)[0].status == 200


def test_claiming_a_board_not_yet_set_up_opens_the_wizard(wizard_server):
    port, _, needed, password, folder, _ = wizard_server
    password.path.unlink()  # no password yet: the first claim
    needed["now"] = True
    http = Recorder(port)
    form_cookie = ask_for_code(http)
    response, _ = reset(http, form_cookie, board_code(folder))
    assert response.status == 303 and response.getheader("Location") == "/setup/"
    assert response.getheader("Set-Cookie").startswith("session=")


def test_a_fault_telling_whether_the_board_is_set_up_opens_the_settings(tmp_path):
    from departureboard.web import Wizard

    def broken():
        raise PermissionError("config.toml")

    password = Password(tmp_path / "web.json")
    password.set(PASSWORD)
    handler = make_handler(FakePage(), password, Sessions(), SignInLimiter(), lambda t, b: b, b"",
                           wizard=Wizard.load(FakeSteps(), broken))
    httpd = make_server(handler, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    try:
        response, _ = request(httpd.server_address[1], "POST", "/login", {"password": PASSWORD})
        assert response.status == 303 and response.getheader("Location") == "/"
    finally:
        httpd.shutdown()
        httpd.server_close()
