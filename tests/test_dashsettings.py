"""The dashboard's settings area, end to end through the dashboard's server."""

import http.client
import json
import re
import threading
import tomllib
from urllib.parse import urlencode

import pytest

from departureboard.auth import MAX_FAILURES, Sessions
from departureboard.dashform import DashboardForm
from departureboard.store import Store
from departureboard.dashserver import DashboardApp, StaticFiles, make_server
from departureboard.dashsettings import SettingsArea
from departureboard.snapshot import Hub
from departureboard.walk import WalkTimes

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new password"
WRONG_CODE = "That code isn't right, or it has run out."

# The read-only dashboard's headers, as docs/dashboard-api.md has them.
DASHBOARD_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: "
    "https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


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


class FakeSetup:
    def __init__(self):
        self.calls = []

    def handle(self, name, payload):
        self.calls.append((name, payload))
        return (200, {"step": name}) if name == "begin" else (404, {"error": "No such setup step"})


class Running:
    """A dashboard server with a settings area, and every response it gave."""

    def __init__(self, folder, page=None, setup=None, hosts=frozenset()):
        self.folder = folder
        self.page = page or FakePage()
        self.setup = setup or FakeSetup()
        self.area = SettingsArea(folder, self.page, self.setup, stylesheet=b"body{}")
        self.app = DashboardApp(Hub("Glossop", WalkTimes()), StaticFiles({}, True),
                                lambda: True, allowed_hosts=hosts, settings=self.area)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.seen = []
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def __call__(self, method, path, body=None, cookie=None, origin="same", host=None, headers=None,
                 json_body=None):
        host = host or f"localhost:{self.port}"
        sent = {"Host": host}
        if origin == "same":
            sent["Origin"] = f"http://{host}"
        elif origin:
            sent["Origin"] = origin
        if cookie:
            sent["Cookie"] = cookie
        data = None
        if body is not None:
            data = urlencode(body)
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        if json_body is not None:
            data = json.dumps(json_body)
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, data, sent)
        response = conn.getresponse()
        text = response.read().decode()
        conn.close()
        self.seen.append(text + "".join(f"{k}: {v}\n" for k, v in response.getheaders()))
        return response, text

    def form(self, path="/settings/login", cookie=None):
        """(Cookie header, hidden form value) from a pre-session page."""
        response, text = self("GET", path, cookie=cookie)
        token = re.search(r'name="form" value="([^"]+)"', text).group(1)
        return response.getheader("Set-Cookie").split(";")[0], token

    def sign_in(self):
        response, _ = self("POST", "/settings/login", {"password": PASSWORD})
        assert response.status == 303 and response.getheader("Location") == "/settings"
        cookie = response.getheader("Set-Cookie").split(";")[0]
        _, page = self("GET", "/settings", cookie=cookie)
        return cookie, re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def close(self):
        self.app.hub.close()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def dash(tmp_path, caplog):
    caplog.set_level("DEBUG")
    running = Running(tmp_path)
    yield running
    running.close()


@pytest.fixture
def signed(dash):
    dash.area.password.set(PASSWORD)
    return dash


def code_from_the_log(caplog):
    codes = [re.search(r"Settings reset code: (\d{3}) (\d{3})", r.getMessage()) for r in caplog.records]
    found = [m.group(1) + m.group(2) for m in codes if m]
    assert found, "no code in the log"
    return found[-1]


def ask_for_code(dash, cookie=None):
    form_cookie, token = dash.form(cookie=cookie)
    response, _ = dash("POST", "/settings/forgot", {"form": token}, cookie=form_cookie)
    assert response.status == 303 and response.getheader("Location") == "/settings/reset?asked"
    return form_cookie


def reset(dash, form_cookie, code, new=NEW_PASSWORD, again=None):
    _, token = dash.form("/settings/reset?asked", cookie=form_cookie)
    return dash("POST", "/settings/reset", {"form": token, "code": code, "password": new, "again": again or new},
                cookie=form_cookie)


def assert_only_in_the_log(code, dash, caplog):
    tokens = re.compile(r'(name="form" value=|name="csrf" value=|dash_form=|dash_session=|"csrf":)[^";\s]+')
    for text in dash.seen:
        text = tokens.sub(r"\1(token)", text)
        assert code not in text and f"{code[:3]} {code[3:]}" not in text
    logged = [r for r in caplog.records if code[:3] in r.getMessage() and code[3:] in r.getMessage()]
    assert all(r.getMessage().startswith("Settings reset code: ") for r in logged)


# Signing in


def test_settings_need_signing_in(signed):
    response, _ = signed("GET", "/settings")
    assert response.status == 303 and response.getheader("Location") == "/settings/login"
    response, text = signed("GET", "/settings/login")
    assert response.status == 200 and 'action="/settings/login"' in text and 'type="password"' in text


def test_sign_in_sets_its_own_strict_cookie_for_the_settings_only(signed):
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD})
    cookie = response.getheader("Set-Cookie")
    assert cookie.startswith("dash_session=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/settings;" in cookie
    form_cookie = signed("GET", "/settings/login")[0].getheader("Set-Cookie")
    assert form_cookie.startswith("dash_form=") and "Path=/settings;" in form_cookie


def test_the_cookie_never_collides_with_the_pi_s(signed):
    cookie, _ = signed.sign_in()
    token = cookie.split("=", 1)[1]
    # The Pi's settings page on the same host sets `session` for Path=/.
    assert signed("GET", "/settings", cookie=f"session={token}")[0].status == 303
    both = f"session=pi-token-xyz; {cookie}"
    assert signed("GET", "/settings", cookie=both)[0].status == 200
    assert Sessions().token_from(both) == "pi-token-xyz"  # and the Pi reads only its own


def test_a_wrong_password_is_refused_without_a_session(signed):
    response, text = signed("POST", "/settings/login", {"password": "nope nope nope"})
    assert response.status == 401 and "isn't right" in text
    assert not (response.getheader("Set-Cookie") or "").startswith("dash_session=")


def test_repeated_wrong_passwords_lock_sign_in(signed):
    for _ in range(MAX_FAILURES):
        assert signed("POST", "/settings/login", {"password": "nope nope nope"})[0].status == 401
    response, text = signed("POST", "/settings/login", {"password": PASSWORD})
    assert response.status == 429 and "Too many attempts" in text


def test_signing_in_from_setup_goes_back_to_it(signed):
    response, _ = signed("GET", "/settings/setup/")
    assert response.status == 303 and response.getheader("Location") == "/settings/login?next=setup"
    _, page = signed("GET", "/settings/login?next=setup")
    assert 'name="next" value="setup"' in page
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "setup"})
    assert response.getheader("Location") == "/settings/setup/"
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "https://evil.example.com/"})
    assert response.getheader("Location") == "/settings"


def test_sign_out_ends_the_session(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings/logout", {"csrf": csrf}, cookie=cookie)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("dash_session=;")
    assert signed("GET", "/settings", cookie=cookie)[0].getheader("Location") == "/settings/login"


# The everyday form


def test_a_signed_in_save_with_the_form_token_works(signed):
    cookie, csrf = signed.sign_in()
    response, text = signed("POST", "/settings", {"csrf": csrf, "title": "Glossop"}, cookie=cookie)
    assert response.status == 200 and "Saved. The dashboard starts again" in text
    assert signed.page.saved == [{"title": "Glossop"}]


def test_save_errors_come_back_on_the_form(signed):
    cookie, csrf = signed.sign_in()
    response, text = signed("POST", "/settings", {"csrf": csrf, "title": "bad"}, cookie=cookie)
    assert response.status == 422 and "Too bad" in text and signed.page.saved == []


@pytest.mark.parametrize("csrf, origin", [
    ("forged", "same"),
    (None, "same"),
    ("right", "http://evil.example.com"),
    ("right", "null"),
    ("right", None),
])
def test_every_change_needs_the_same_origin_and_the_form_token(signed, csrf, origin):
    cookie, right = signed.sign_in()
    body = {"title": "x"} if csrf is None else {"csrf": right if csrf == "right" else csrf, "title": "x"}
    response, _ = signed("POST", "/settings", body, cookie=cookie, origin=origin)
    assert response.status == 403 and signed.page.saved == []


def test_a_save_without_a_session_goes_to_sign_in(signed):
    response, _ = signed("POST", "/settings", {"csrf": "x", "title": "x"})
    assert response.status == 303 and response.getheader("Location") == "/settings/login"


def test_bodies_must_be_plain_small_forms(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings", cookie=cookie, json_body={"csrf": csrf})
    assert response.status == 400
    response, _ = signed("POST", "/settings", {"csrf": csrf, "title": "x" * 70_000}, cookie=cookie)
    assert response.status == 413
    response, _ = signed("POST", "/settings", None, cookie=cookie, headers={"Transfer-Encoding": "chunked"})
    assert response.status == 400
    assert signed.page.saved == []


def test_other_methods_are_not_allowed(signed):
    response, _ = signed("PUT", "/settings", {"title": "x"})
    assert response.status == 405 and response.getheader("Allow") == "GET, HEAD, POST"


# A forgotten password, and the first claim, with a code from the log


def test_a_forgotten_password_is_reset_with_a_code_from_the_log(signed, caplog):
    _, page = signed("GET", "/settings/login")
    assert 'action="/settings/forgot"' in page and "Forgot password?" in page
    old, _ = signed.sign_in()
    form_cookie = ask_for_code(signed)
    _, page = signed("GET", "/settings/reset?asked", cookie=form_cookie)
    assert "docker logs departureboard-dashboard" in page and "Container Manager" in page
    code = code_from_the_log(caplog)
    response, _ = reset(signed, form_cookie, f"{code[:3]} {code[3:]}")
    assert response.status == 303 and response.getheader("Location") == "/settings"
    new = response.getheader("Set-Cookie")
    assert new.startswith("dash_session=") and "Path=/settings;" in new
    assert signed("GET", "/settings", cookie=new.split(";")[0])[0].status == 200
    assert signed("GET", "/settings", cookie=old)[0].status == 303  # every old session ended
    assert signed.area.password.check(NEW_PASSWORD) and not signed.area.password.check(PASSWORD)
    assert reset(signed, form_cookie, code, "yet another password")[0].status == 401  # once only
    assert not (signed.folder / "reset-display.json").exists()  # no board to hand it to
    assert_only_in_the_log(code, signed, caplog)


def test_the_dashboard_builds_its_sessions_on_its_password(dash):
    assert dash.area.sessions.password is dash.area.password
    assert dash.area.password.path == dash.folder / "web.json"


def test_a_password_set_from_the_command_line_signs_every_browser_out(signed, monkeypatch, capsys):
    import getpass

    from departureboard.cli import main

    old, _ = signed.sign_in()
    assert signed("GET", "/settings", cookie=old)[0].status == 200
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": NEW_PASSWORD)
    # Another process, as `docker exec … dashboard --set-password --state-dir /data` is.
    assert main(["dashboard", "--set-password", "--state-dir", str(signed.folder)]) == 0
    response, _ = signed("GET", "/settings", cookie=old)
    assert response.status == 303 and response.getheader("Location") == "/settings/login"
    assert signed("POST", "/settings/login", {"password": PASSWORD})[0].status == 401
    response, _ = signed("POST", "/settings/login", {"password": NEW_PASSWORD})
    assert response.status == 303 and response.getheader("Location") == "/settings"


def test_a_code_from_the_command_line_claims_the_running_dashboard(dash, capsys, caplog):
    from departureboard.cli import main

    assert main(["dashboard", "--reset-code", "--state-dir", str(dash.folder)]) == 0
    code = re.search(r"Settings reset code: (\d{3} \d{3})", capsys.readouterr().out).group(1)
    assert code.replace(" ", "") not in caplog.text  # printed, not put in the log
    form_cookie, _ = dash.form()
    response, _ = reset(dash, form_cookie, code)
    assert response.status == 303 and response.getheader("Location") == "/settings"
    assert dash.area.password.check(NEW_PASSWORD)


def test_a_wrong_code_changes_nothing_and_says_no_more(signed, caplog):
    form_cookie, _ = signed.form()
    response, without = reset(signed, form_cookie, "123456")
    assert response.status == 401 and WRONG_CODE in without
    ask_for_code(signed, form_cookie)
    code = code_from_the_log(caplog)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(5):
        response, text = reset(signed, form_cookie, wrong)
        assert response.status == 401 and WRONG_CODE in text
    assert reset(signed, form_cookie, code)[0].status == 401  # voided after five wrong
    assert signed.area.password.check(PASSWORD)
    assert_only_in_the_log(code, signed, caplog)


def test_the_first_claim_needs_a_code_from_the_log(dash, caplog):
    _, page = dash("GET", "/settings/login")
    assert "Claim this dashboard" in page and 'action="/settings/reset"' in page
    response, _ = dash("GET", "/settings")
    assert response.getheader("Location") == "/settings/login"
    assert dash("POST", "/settings/login", {"password": NEW_PASSWORD})[0].status == 403
    form_cookie, _ = dash.form()
    assert reset(dash, form_cookie, "000000")[0].status == 401
    assert not dash.area.password.is_set()
    ask_for_code(dash, form_cookie)
    code = code_from_the_log(caplog)
    response, _ = reset(dash, form_cookie, code)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("dash_session=")
    assert dash.area.password.check(NEW_PASSWORD)
    assert (dash.folder / "web.json").exists()
    _, page = dash("GET", "/settings/login")
    assert "Claim this dashboard" not in page
    assert_only_in_the_log(code, dash, caplog)


def test_asking_for_a_code_needs_this_browser_s_form_token(signed, caplog):
    form_cookie, token = signed.form()
    _, other_token = signed.form()
    for body, cookie, origin in [
        ({"form": token}, None, "same"),
        ({}, form_cookie, "same"),
        ({"form": other_token}, form_cookie, "same"),
        ({"form": token}, form_cookie, "http://evil.example.com"),
        ({"form": token}, form_cookie, None),
    ]:
        assert signed("POST", "/settings/forgot", body, cookie=cookie, origin=origin)[0].status == 403
    assert not any("Settings reset code:" in r.getMessage() for r in caplog.records)


# The setup wizard


def test_the_wizard_page_and_its_files_are_served_from_an_allowlist(signed):
    cookie, _ = signed.sign_in()
    response, page = signed("GET", "/settings/setup/", cookie=cookie)
    assert response.status == 200 and "wizard_static/wizard.js" in page
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    for path, content_type in [
        ("/settings/setup/wizard_static/wizard.js", "text/javascript; charset=utf-8"),
        ("/settings/setup/wizard_static/wizard.css", "text/css; charset=utf-8"),
        ("/settings/setup/dashboard_static/dom.js", "text/javascript; charset=utf-8"),
        ("/settings/setup/dashboard_static/vendor/leaflet-1.9.4/leaflet-src.esm.js", "text/javascript; charset=utf-8"),
    ]:
        response, _ = signed("GET", path)
        assert response.status == 200 and response.getheader("Content-Type") == content_type, path
    assert signed("GET", "/settings/setup", cookie=cookie)[0].getheader("Location") == "/settings/setup/"
    for path in [
        "/settings/setup/index.html",
        "/settings/setup/wizard_static/",
        "/settings/setup/wizard_static/nothing.js",
        "/settings/setup/dashboard_static/main.js",
        "/settings/setup/dashboard_static/vendor/leaflet-1.9.4/LICENSE",
        "/settings/setup/../web.json",
        "/settings/setup/%2e%2e/web.json",
        "/settings/web.json",
        "/settings/reset.json",
        "/settings/setup/api/begin",
    ]:
        assert signed("GET", path, cookie=cookie)[0].status == 404, path


def test_the_wizard_api_needs_the_session_and_its_csrf_token(signed):
    response, text = signed("GET", "/settings/setup/api/session")
    assert response.status == 401 and response.getheader("Content-Type") == "application/json"
    cookie, csrf = signed.sign_in()
    response, text = signed("GET", "/settings/setup/api/session", cookie=cookie)
    assert response.status == 200 and json.loads(text) == {"csrf": csrf}
    token = {"X-CSRF-Token": csrf}
    attempts = [
        (None, token, "same", 401),  # no session
        (cookie, {}, "same", 403),  # no token
        (cookie, {"X-CSRF-Token": "forged"}, "same", 403),
        (cookie, token, "http://evil.example.com", 403),
        (cookie, token, None, 403),
        (cookie, {**token, "Sec-Fetch-Site": "cross-site"}, "same", 403),
    ]
    for sent_cookie, headers, origin, status in attempts:
        response, _ = signed("POST", "/settings/setup/api/begin", cookie=sent_cookie, origin=origin,
                             headers=headers, json_body={})
        assert response.status == status, (sent_cookie, headers, origin)
    assert signed.setup.calls == []
    response, text = signed("POST", "/settings/setup/api/begin", cookie=cookie, headers=token, json_body={})
    assert response.status == 200 and json.loads(text) == {"step": "begin"}
    assert signed.setup.calls == [("begin", {})]


@pytest.mark.parametrize("name", ["Begin", "be-gin", "begin/x", "begin%20x", "a" * 21, "", "b%C3%A9gin"])
def test_a_step_name_is_plain_lowercase_checked_before_the_body(signed, name):
    cookie, csrf = signed.sign_in()
    token = {"X-CSRF-Token": csrf}
    response, text = signed("POST", f"/settings/setup/api/{name}", cookie=cookie, headers=token, json_body={})
    assert response.status == 404 and json.loads(text) == {"error": "No such setup step"}
    response, _ = post_framed(signed, f"/settings/setup/api/{name}",
                              [("Cookie", cookie), ("X-CSRF-Token", csrf), ("Content-Type", "application/json")])
    assert response.status == 404  # not 411: the name is checked before the body
    assert signed.setup.calls == []


def test_the_wizard_api_takes_only_small_json(signed):
    cookie, csrf = signed.sign_in()
    token = {"X-CSRF-Token": csrf}
    response, _ = signed("POST", "/settings/setup/api/begin", {"a": "1"}, cookie=cookie, headers=token)
    assert response.status == 415
    response, _ = signed("POST", "/settings/setup/api/begin", cookie=cookie, headers=token,
                         json_body={"q": "x" * 20_000})
    assert response.status == 413
    response, text = signed("POST", "/settings/setup/api/begin", cookie=cookie,
                            headers={**token, "Content-Type": "application/json"})
    assert response.status == 400 and json.loads(text) == {"error": "That request isn't JSON"}
    assert signed.setup.calls == []


def test_a_post_without_a_length_is_refused_unread(signed):
    conn = http.client.HTTPConnection("127.0.0.1", signed.port, timeout=10)
    conn.putrequest("POST", "/settings/login", skip_host=True)
    host = f"localhost:{signed.port}"
    for name, value in {"Host": host, "Origin": f"http://{host}",
                        "Content-Type": "application/x-www-form-urlencoded"}.items():
        conn.putheader(name, value)
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 411 and response.getheader("Connection") == "close"
    conn.close()


def post_framed(dash, path, pairs, origin="same"):
    """POST with exactly these extra (name, value) header pairs and no body."""
    conn = http.client.HTTPConnection("127.0.0.1", dash.port, timeout=10)
    conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    host = f"localhost:{dash.port}"
    conn.putheader("Host", host)
    if origin == "same":
        conn.putheader("Origin", f"http://{host}")
    elif origin:
        conn.putheader("Origin", origin)
    for name, value in pairs:
        conn.putheader(name, value)
    conn.endheaders()
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response, text


@pytest.mark.parametrize("path", ["/settings/login", "/settings", "/settings/setup/api/begin"])
@pytest.mark.parametrize("pairs", [[], [("Content-Length", "99999999")], [("Transfer-Encoding", "chunked")]])
def test_the_origin_is_checked_before_the_body_is_read(signed, path, pairs):
    for origin in ("http://evil.example.com", "null", None):
        response, _ = post_framed(signed, path, pairs, origin=origin)
        assert response.status == 403, (path, pairs, origin)
    assert signed.setup.calls == [] and signed.page.saved == []


# Headers


def csp(response):
    return response.getheader("Content-Security-Policy")


def test_settings_pages_allow_their_own_forms_and_no_scripts(signed):
    for path in ["/settings/login", "/settings/reset", "/settings", "/settings/style.css", "/settings/nowhere"]:
        response, _ = signed("GET", path)
        policy = csp(response)
        assert "form-action 'self'" in policy and "script-src" not in policy and "default-src 'none'" in policy
        assert "frame-ancestors 'none'" in policy and response.getheader("X-Frame-Options") == "DENY"
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Referrer-Policy") == "same-origin"
        assert len(response.getheaders()) == len({k.lower() for k, _ in response.getheaders()}), path


def test_only_the_wizard_may_run_scripts(signed):
    cookie, _ = signed.sign_in()
    for path in ["/settings/setup/", "/settings/setup/wizard_static/wizard.js", "/settings/setup/api/session",
                 "/settings/setup/nothing"]:
        response, _ = signed("GET", path, cookie=cookie)
        policy = csp(response)
        assert "script-src 'self'" in policy and "form-action 'none'" in policy, path
        assert "https://tile.openstreetmap.org" in policy
        assert response.getheader("Cache-Control") == "no-store"


def test_the_read_only_dashboard_keeps_its_own_headers(signed):
    for path in ["/", "/api/snapshot", "/healthz", "/settingsx", "/api/settings"]:
        response, _ = signed("GET", path)
        assert csp(response) == DASHBOARD_CSP, path
    response, _ = signed("POST", "/api/snapshot", {"a": "1"})
    assert response.status == 405 and response.getheader("Allow") == "GET, HEAD"


def test_outsiders_are_refused_before_the_settings(signed):
    assert signed("GET", "/settings/login", host="evil.example.com")[0].status == 403
    assert signed("POST", "/settings/login", {"password": PASSWORD}, host="evil.example.com")[0].status == 403


def test_without_a_settings_area_there_is_no_settings_page(tmp_path):
    app = DashboardApp(Hub("Glossop", WalkTimes()), StaticFiles({}, True), lambda: True)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        conn.request("GET", "/settings", headers={"Host": "localhost"})
        assert conn.getresponse().status == 404
        conn.close()
    finally:
        app.hub.close()
        server.shutdown()
        server.server_close()


# The real form, saving into the dashboard's own overlay

CONFIG = """
title = "Glossop"
[train]
crs = "GLO"
[bus]
source = "bods"
operators = ["BNML"]
[bus.stops]
A = "1000DGHS0900"
"""
ENV = {"RDM_API_KEY": "rail-key-1234567", "BODS_API_KEY": "bods-key-7654321"}


@pytest.fixture
def real(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    folder = tmp_path / "dash"
    folder.mkdir()
    store = Store.open(config, state_dir=folder)
    page = DashboardForm(store, lambda: store.env(ENV))
    running = Running(folder, page=page)
    running.area.password.set(PASSWORD)
    running.config = config
    yield running
    running.close()


def fields_from(page):
    values = dict(re.findall(r'<input id="([a-z_]+)" name="[a-z_]+" type="text" value="([^"]*)"', page))
    values |= {name: text for name, text in re.findall(r'<textarea id="([a-z_]+)"[^>]*>([^<]*)</textarea>', page)}
    for name, options in re.findall(r'<select id="([a-z_]+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
    return values


def test_the_form_saves_into_the_dashboard_s_folder_only(real):
    cookie, csrf = real.sign_in()
    _, page = real("GET", "/settings", cookie=cookie)
    assert "Currently set." in page and "rail-key-1234567" not in page and "bods-key-7654321" not in page
    fields = fields_from(page)
    assert fields["train_crs"] == "GLO" and fields["dashboard_train_rows"] == "10"
    assert fields["bus_stops"] == "A = 1000DGHS0900"
    fields.update(csrf=csrf, train_crs="man", dashboard_train_rows="6", RDM_DETAILS_API_KEY="details-key-0001")
    response, text = real("POST", "/settings", fields, cookie=cookie)
    assert response.status == 200 and "Saved." in text, text
    saved = tomllib.loads((real.folder / "settings.toml").read_text())
    assert saved["train"]["crs"] == "MAN" and saved["dashboard"]["train_rows"] == 6
    assert (real.folder / "secrets.env").read_text() == "RDM_DETAILS_API_KEY=details-key-0001\n"
    assert real.config.read_text() == CONFIG
    assert "details-key-0001" not in "".join(real.seen)


def test_the_form_says_what_is_wrong_and_saves_nothing(real):
    cookie, csrf = real.sign_in()
    fields = fields_from(real("GET", "/settings", cookie=cookie)[1])
    fields.update(csrf=csrf, dashboard_train_rows="50", bus_stops="A = not a code!", kb_incidents_url="http://x")
    response, text = real("POST", "/settings", fields, cookie=cookie)
    assert response.status == 422 and "Not saved: fix 3 things" in text
    assert 'href="#dashboard_train_rows"' in text and 'aria-invalid="true"' in text
    assert not (real.folder / "settings.toml").exists()


def test_a_settings_form_with_too_many_fields_is_refused_not_dropped(dash):
    response, _ = dash("POST", "/settings/login", {f"f{n}": "1" for n in range(301)})
    assert response.status == 413
