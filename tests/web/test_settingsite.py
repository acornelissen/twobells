"""The settings site, end to end through the one server (dashserver), on a
Pi (codes on the board) and in Docker (codes in the log)."""

import http.client
import json
import re
import socket
import threading
import time
import tomllib
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pytest

from departureboard.core.walk import WalkTimes
from departureboard.dashboard.snapshot import Hub
from departureboard.settings.installs import install_named
from departureboard.settings.store import Store
from departureboard.setup.api import PAGE_HEADERS, RateLimit, SetupApi, TooSoon, static_files
from departureboard.setup.keytest import KeyResult
from departureboard.setup.lookups import SetupError
from departureboard.web import settingsite
from departureboard.web.auth import FAILURE_WINDOW_S, MAX_FAILURES, PasswordStore, Sessions
from departureboard.web.icons import icon
from departureboard.web.server import DashboardApp, StaticFiles, make_server
from departureboard.web.settingsite import SettingsSite
from tests.paths import ROOT

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new password"
WRONG_CODE = "That code isn't right, or it has run out."

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
KEYS = "RDM_API_KEY=rail-key-1234567\nBODS_API_KEY=bods-key-7654321\n"

# The read-only dashboard's headers, as docs/dashboard-api.md has them.
DASHBOARD_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: "
    "https://tile.openstreetmap.org; connect-src 'self'; manifest-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


class FakeSetup:
    """setupapi.SetupApi's handle() and test_key(), recording what reached them."""

    def __init__(self):
        self.calls = []
        self.tests = []
        self.result = KeyResult("works", "It works: the service answered.")

    def handle(self, name, payload):
        self.calls.append((name, payload))
        if name == "nan":
            return 200, {"distance": float("nan")}
        if name != "begin":
            return 404, {"error": "No such setup step"}
        return 200, {"step": name}

    def test_key(self, name, value=None, app_id=None):
        self.tests.append((name, value, app_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class Running:
    """The server with a settings site for one install and its store, and
    every response it gave. With `dashboard`, the dashboard runs as well."""

    def __init__(self, folder, install="docker", config=CONFIG, dashboard=False, hosts=frozenset(), clock=None,
                 setup=None):
        folder.mkdir(parents=True)
        self.config = folder / "config.toml"
        if config is not None:
            self.config.write_text(config)
        self.secrets = folder / "secrets.env"
        self.secrets.write_text(KEYS)
        self.folder = folder / "store"
        self.store = Store.open(self.config, self.secrets, state_dir=self.folder, install=install, environ={})
        self.store.make()
        # `setup(store)` makes the wizard's API, when a test needs a real one.
        self.setup = FakeSetup() if setup is None else setup(self.store)
        self.clock = clock or Clock()
        self.on = {"now": dashboard}
        self.site = SettingsSite(self.store, install_named(install), self.setup, stylesheet=b"body{}",
                                 dashboard_on=lambda: self.on["now"], clock=self.clock)
        hub = Hub("Glossop", WalkTimes()) if dashboard else None
        self.app = DashboardApp(hub, StaticFiles({}, False), lambda: True, self.site,
                                dashboard_on=lambda: self.on["now"], allowed_hosts=hosts)
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

    def sign_in(self, path="/settings"):
        """A session cookie and its form token, signing in as a browser does."""
        response, _ = self("POST", "/settings/login", {"password": PASSWORD})
        assert response.status == 303 and response.getheader("Location") == "/settings"
        cookie = response.getheader("Set-Cookie").split(";")[0]
        _, page = self("GET", path, cookie=cookie)
        return cookie, re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def session(self):
        """A session cookie, made directly: signing in is tested above, and is slow on purpose."""
        token, _ = self.site.sessions.create()
        return f"settings_session={token}"

    def csrf(self, cookie):
        """The session's form token, as the wizard's page asks for it."""
        response, text = self("GET", "/settings/setup/api/session", cookie=cookie)
        assert response.status == 200 and response.getheader("Content-Type") == "application/json"
        return json.loads(text)["csrf"]

    def restart(self):
        """The server started again over the same store, as serve does after a
        save on a desktop or in Docker: a new process, nothing kept in memory."""
        self.server.shutdown()
        self.server.server_close()
        self.site = SettingsSite(self.store, self.site.install, self.setup, stylesheet=b"body{}",
                                 dashboard_on=lambda: self.on["now"], clock=self.clock)
        self.app = DashboardApp(self.app.hub, StaticFiles({}, False), lambda: True, self.site,
                                dashboard_on=lambda: self.on["now"], allowed_hosts=self.app.allowed_hosts)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def saved(self):
        return tomllib.loads((self.folder / "settings.toml").read_text())

    def close(self):
        if self.app.hub is not None:
            self.app.hub.close()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def make(tmp_path, caplog):
    caplog.set_level("DEBUG")
    made = []

    def build(**options):
        running = Running(tmp_path / f"install{len(made)}", **options)
        made.append(running)
        return running

    yield build
    for running in made:
        running.close()


@pytest.fixture
def dash(make):
    """Docker, with the dashboard running: codes in the log."""
    return make(install="docker", dashboard=True)


@pytest.fixture
def signed(dash):
    dash.site.password.set(PASSWORD)
    return dash


@pytest.fixture
def pi(make):
    """A Pi with its board: codes on the board, and no dashboard."""
    return make(install="pi")


@pytest.fixture
def pi_signed(pi):
    pi.site.password.set(PASSWORD)
    return pi


def fields_from(page):
    """What a browser would submit for the form as drawn."""
    values = dict(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="text" value="([^"]*)"', page))
    values |= dict(re.findall(r'<textarea id="(\w+)"[^>]*>([^<]*)</textarea>', page))
    for name, options in re.findall(r'<select id="(\w+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
    values |= dict(re.findall(r'<input type="radio" id="[\w-]+" name="(\w+)" value="([^"]*)" checked', page))
    values |= dict.fromkeys(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="checkbox" value="on" checked', page), "on")
    return values


def csp(response):
    return response.getheader("Content-Security-Policy")


# Signing in


def test_settings_need_signing_in(signed):
    response, _ = signed("GET", "/settings")
    assert response.status == 303 and response.getheader("Location") == "/settings/login"
    response, text = signed("GET", "/settings/login")
    assert response.status == 200 and 'action="/settings/login"' in text and 'type="password"' in text


def test_sign_in_sets_its_own_strict_cookies_for_the_settings_only(signed):
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD})
    cookie = response.getheader("Set-Cookie")
    assert cookie.startswith("settings_session=")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/settings;" in cookie
    form_cookie = signed("GET", "/settings/login")[0].getheader("Set-Cookie")
    assert form_cookie.startswith("settings_form=") and "Path=/settings;" in form_cookie
    assert "HttpOnly" in form_cookie and "SameSite=Strict" in form_cookie and "Max-Age=1800" in form_cookie


def test_the_old_pages_cookies_are_ignored_and_never_mistaken_for_ours(signed):
    cookie, _ = signed.sign_in()
    token = cookie.split("=", 1)[1]
    # The board's old page set `session` (and `form`) for Path=/, which reach /settings
    # too; the dashboard's old settings set dash_session.
    for old in (f"session={token}", f"dash_session={token}"):
        assert signed("GET", "/settings", cookie=old)[0].status == 303
    both = f"session=pi-token-xyz; dash_session=dash-token; {cookie}; form=old-form"
    assert signed("GET", "/settings", cookie=both)[0].status == 200
    assert signed("GET", "/", cookie=f"session=pi-token-xyz; {cookie}")[0].status == 200
    assert Sessions().token_from(both) == "pi-token-xyz"  # the old name reads only its own


def test_a_wrong_password_is_refused_without_a_session(signed):
    response, text = signed("POST", "/settings/login", {"password": "nope nope nope"})
    assert response.status == 401 and "isn't right" in text
    assert not (response.getheader("Set-Cookie") or "").startswith("settings_session=")


def test_repeated_wrong_passwords_lock_sign_in(signed):
    for _ in range(MAX_FAILURES):
        assert signed("POST", "/settings/login", {"password": "nope nope nope"})[0].status == 401
    response, text = signed("POST", "/settings/login", {"password": PASSWORD})
    assert response.status == 429 and "Too many attempts" in text


def test_the_lockout_lets_go_after_its_window(make):
    running = make(install="docker")
    running.site.password.set(PASSWORD)
    for _ in range(MAX_FAILURES):
        running("POST", "/settings/login", {"password": "nope nope nope"})
    assert running("POST", "/settings/login", {"password": PASSWORD})[0].status == 429
    running.clock.now += FAILURE_WINDOW_S + 1
    assert running("POST", "/settings/login", {"password": PASSWORD})[0].status == 303


def test_signing_in_from_setup_goes_back_to_it(signed):
    response, _ = signed("GET", "/settings/setup/")
    assert response.status == 303 and response.getheader("Location") == "/settings/login?next=setup"
    _, page = signed("GET", "/settings/login?next=setup")
    assert 'name="next" value="setup"' in page and 'name="step"' not in page
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "setup"})
    assert response.getheader("Location") == "/settings/setup/"
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "https://evil.example.com/"})
    assert response.getheader("Location") == "/settings"


@pytest.mark.parametrize("step", ["stops", "station"])
def test_signing_in_from_a_wizard_shortcut_goes_back_to_its_step(signed, step):
    response, _ = signed("GET", f"/settings/setup/?step={step}")
    assert response.status == 303
    assert response.getheader("Location") == f"/settings/login?next=setup&step={step}"
    _, page = signed("GET", f"/settings/login?next=setup&step={step}")
    assert f'<input type="hidden" name="step" value="{step}">' in page
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "setup", "step": step})
    assert response.getheader("Location") == f"/settings/setup/?step={step}"
    # A wrong password keeps the step for the next try.
    response, page = signed("POST", "/settings/login", {"password": "wrong", "next": "setup", "step": step})
    assert response.status == 401 and f'name="step" value="{step}"' in page


@pytest.mark.parametrize("step", ["keys", "//evil.example.com", "stops\r\nSet-Cookie: x=1", "STOPS", ""])
def test_only_the_shortcuts_steps_are_kept_and_never_lead_elsewhere(signed, step):
    response, _ = signed("GET", "/settings/setup/?" + urlencode({"step": step}))
    assert response.getheader("Location") in ("/settings/login?next=setup", None)  # None: the server refused it
    _, page = signed("GET", "/settings/login?" + urlencode({"next": "setup", "step": step}))
    assert 'name="step"' not in page
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "setup", "step": step})
    assert response.getheader("Location") == "/settings/setup/"
    # A step without the wizard to go back to is no step at all.
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "step": "stops"})
    assert response.getheader("Location") == "/settings"


def test_sign_out_ends_the_session(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings/logout", {"csrf": csrf}, cookie=cookie)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("settings_session=;")
    assert signed("GET", "/settings", cookie=cookie)[0].getheader("Location") == "/settings/login"


def test_a_save_and_the_restart_after_it_keep_the_browser_signed_in(signed):
    path = "/settings/trains-and-buses"
    cookie, csrf = signed.sign_in()
    fields = fields_from(signed("GET", path, cookie=cookie)[1])
    response, _ = signed("POST", path, fields | {"csrf": csrf, "title": "Our house"}, cookie=cookie)
    assert response.status == 303
    page = signed("GET", path + "?saved", cookie=cookie)[1]
    assert "Saved 1 change: Title." in page and "sign in again" not in page
    signed.restart()
    assert signed("GET", path, cookie=cookie)[0].status == 200
    # The page drawn before the restart still saves after it, with its form token.
    response, text = signed("POST", path, fields | {"csrf": csrf, "title": "Our home"}, cookie=cookie)
    assert response.status == 303, text
    assert signed.saved()["title"] == "Our home"


def test_a_signed_out_browser_stays_signed_out_after_a_restart(signed):
    cookie, csrf = signed.sign_in()
    signed("POST", "/settings/logout", {"csrf": csrf}, cookie=cookie)
    signed.restart()
    assert signed("GET", "/settings", cookie=cookie)[0].getheader("Location") == "/settings/login"


def test_the_store_keeps_sessions_only_as_hashes_in_private_files(signed):
    cookie, csrf = signed.sign_in()
    signed.form()  # a sign-in page's form, signed with the kept key
    token = cookie.split("=", 1)[1]
    raw =(signed.folder / "sessions.json").read_bytes()
    assert token.encode() not in raw and csrf.encode() not in raw
    for name in ("sessions.json", "form.key"):
        assert (signed.folder / name).stat().st_mode & 0o777 == 0o600, name


def test_a_form_opened_before_a_restart_still_posts_after_it(pi_signed):
    form_cookie, token = pi_signed.form()
    pi_signed.restart()
    response, _ = pi_signed("POST", "/settings/forgot", {"form": token}, cookie=form_cookie)
    assert response.status == 303 and response.getheader("Location") == "/settings/reset?asked"


def test_the_sessions_are_built_on_the_stores_one_password(dash):
    assert dash.site.sessions.password is dash.site.password
    assert dash.site.password.path == dash.folder / "web.json"


def test_without_a_password_the_page_says_how_to_set_one(make):
    for install, command in (("pi", "departureboard serve --install pi --set-password"),
                             ("docker", "departureboard serve --set-password --state-dir /data")):
        running = make(install=install)
        _, text = running("GET", "/settings/login")
        assert command in text, install
        assert running("POST", "/settings/login", {"password": "anything at all"})[0].status == 403


class SlowPassword(PasswordStore):
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
def slow(make, monkeypatch):
    monkeypatch.setattr(settingsite, "PasswordStore", SlowPassword)
    running = make(install="pi")
    running.site.password.set(PASSWORD)
    yield running
    running.site.password.go.set()


def test_simultaneous_guesses_cannot_beat_the_limit(slow):
    password = slow.site.password
    statuses = []

    def guess():
        for _ in range(5):  # a burst can overflow the listen queue; try again as a browser would
            try:
                response, _ = slow("POST", "/settings/login", {"password": "nope nope nope"})
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


def test_only_two_passwords_are_checked_at_once(slow):
    password = slow.site.password
    results = []

    def guess():
        results.append(slow("POST", "/settings/login", {"password": "nope nope nope"}))

    first = [threading.Thread(target=guess) for _ in range(2)]
    for thread in first:
        thread.start()
    assert password.inside.acquire(timeout=5) and password.inside.acquire(timeout=5)
    response, text = slow("POST", "/settings/login", {"password": "nope nope nope"})
    assert response.status == 429 and "Busy" in text
    password.go.set()
    for thread in first:
        thread.join(15)
    assert [r.status for r, _ in results] == [401, 401]
    assert password.checks == 2


# The pages


def test_the_tabs_are_a_labelled_list_of_links_with_the_current_one_marked(signed):
    cookie, _ = signed.sign_in()
    tabs = [("/settings", "Overview"), ("/settings/trains-and-buses", "Trains and buses"),
            ("/settings/dashboard", "Dashboard"), ("/settings/keys", "Keys"), ("/settings/general", "General")]
    for path, name in tabs:
        response, page = signed("GET", path, cookie=cookie)
        assert response.status == 200, path
        nav = re.search(r'<nav aria-label="Settings"[^>]*><ul>(.*?)</ul></nav>', page).group(1)
        links = re.findall(r'<li><a href="([^"]+)"( aria-current="page")?>([^<]+)</a></li>', nav)
        assert [(href, label) for href, _, label in links] == tabs
        assert [label for _, current, label in links if current] == [name]
        # Each page's title names it, then the site, by the place: the title's part before ">".
        assert f"<title>{name} – Glossop settings</title>" in page and f"<h1>{name}</h1>" in page
        assert page.index('<a href="/">Back to the dashboard</a>') < page.index("<h1>")
        assert page.index("<h1>") < page.index('action="/settings/logout"') < page.index('<nav aria-label="Settings"')
        if path != "/settings":
            assert page.index('<nav aria-label="Settings"') < page.index(f'action="{path}" novalidate')


def test_the_site_is_named_by_the_place_or_else_by_what_shows_the_departures(make):
    for config, install, name in (('title = "Glossop > Manchester"\n', "pi", "Glossop"),
                                  ('title = "Departures"\n', "pi", "Board"), ("", "docker", "Dashboard")):
        running = make(install=install, config=config)
        page = running("GET", "/settings/general", cookie=running.session())[1]
        assert f"<title>General – {name} settings</title>" in page, config


def test_a_tab_is_there_only_while_its_view_is_on(make):
    pi = make(install="pi")
    docker = make(install="docker", dashboard=True)
    for running, shown, hidden in ((pi, "/settings/board", "/settings/dashboard"),
                                   (docker, "/settings/dashboard", "/settings/board")):
        cookie = running.session()
        assert running("GET", shown, cookie=cookie)[0].status == 200
        assert running("GET", shown)[0].getheader("Location") == "/settings/login"
        page = running("GET", "/settings", cookie=cookie)[1]
        assert f'href="{shown}"' in page and f'href="{hidden}"' not in page
        # Not found, signed in or not, and nothing saved through it.
        assert running("GET", hidden, cookie=cookie)[0].status == 404
        assert running("GET", hidden)[0].status == 404
        csrf = running.csrf(cookie)
        assert running("POST", hidden, {"csrf": csrf, "title": "x"}, cookie=cookie)[0].status == 404
        assert not (running.folder / "settings.toml").exists()


def test_a_tab_goes_as_soon_as_its_view_is_turned_off(pi):
    cookie = pi.session()
    assert pi("GET", "/settings/board", cookie=cookie)[0].status == 200
    pi.store.settings_file.write_text("[app]\nviews = []\n")
    assert pi("GET", "/settings/board", cookie=cookie)[0].status == 404
    page = pi("GET", "/settings/general", cookie=cookie)[1]
    assert 'href="/settings/board"' not in page and 'aria-current="page">General</a>' in page


def test_each_page_shows_its_own_settings(make):
    pi = make(install="pi")
    pi.store.make()
    cookie = pi.session()
    pages = {path: pi("GET", path, cookie=cookie)[1] for path in (
        "/settings", "/settings/trains-and-buses", "/settings/board", "/settings/keys", "/settings/general")}
    overview, trains, board, keys, general = pages.values()
    assert "<form method=\"post\" action=\"/settings\"" not in overview and 'id="title"' not in overview
    assert 'id="train_crs"' in trains and 'id="title"' in trains and 'id="theme"' not in trains
    assert 'id="theme"' in board and 'id="train_crs"' not in board and 'id="TAPI_APP_KEY"' not in board
    assert 'id="RDM_API_KEY"' in keys and 'id="TAPI_APP_KEY"' in keys and 'id="train_crs"' not in keys
    assert 'id="bank_holidays"' in general and 'id="view_dashboard"' in general and 'id="timezone"' in general
    # A setting is on one page only.
    for name in ("title", "train_crs", "theme", "RDM_API_KEY", "bank_holidays"):
        assert sum(f'id="{name}"' in page for page in pages.values()) == 1, name
    assert '<h2 id="status-title">Status</h2>' in board and "status-title" not in trains
    assert board.index("<h1>") < board.index("Status</h2>") < board.index("<fieldset")
    assert 'action="/settings/board"' in board and 'action="/settings/trains-and-buses"' in trains


def test_the_overview_sums_up_the_place_in_names_and_links_to_setup(pi):
    cookie = pi.session()
    page = pi("GET", "/settings", cookie=cookie)[1]
    place = re.search(r'<section class="place card"[^>]*>(.*?)</section>', page).group(1)
    assert '<h2 id="place-title">Your place</h2>' in place
    assert "<dt>Station</dt><dd>Glossop (GLO)</dd>" in place
    assert '<dt>Bus stops</dt><dd><ul class="stops"><li>A: 1000DGHS0900</li></ul>' in place  # no timetable yet
    assert "<dt>Home</dt><dd>Not set</dd>" in place
    shortcuts = re.search(r'<section class="actions card"[^>]*>(.*?)</section>', page).group(1)
    assert re.findall(r'<a href="([^"]+)">', shortcuts) == [
        "/settings/setup/", "/settings/setup/?step=stops", "/settings/setup/?step=station"]
    assert page.index("Health</h2>") < page.index("Your place</h2>") < page.index("Keys</h2>") \
        < page.index("Shortcuts</h2>") < page.index("<footer")


def test_the_overview_saves_nothing(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings", {"csrf": csrf, "title": "x"}, cookie=cookie)
    assert response.status == 405 and response.getheader("Allow") == "GET, HEAD"
    assert not (signed.folder / "settings.toml").exists()


def _place(page):
    return re.search(r'<section class="place card"[^>]*>(.*?)</section>', page).group(1)


def test_the_overview_says_when_the_bus_timetable_was_saved_and_how_its_roads_are_getting_on(make):
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from departureboard.timetable import build as timetable
    from departureboard.timetable.model import Schedule

    docker = make(install="docker")  # whose dashboard starts again on saving: this page says how far they got
    cookie = docker.session()
    place = _place(docker("GET", "/settings", cookie=cookie)[1])
    assert "<dt>Bus timetable</dt><dd>Not built yet</dd>" in place and "Bus map roads" not in place
    when = time.time() - 180
    clock = datetime.fromtimestamp(when, ZoneInfo("Europe/London"))
    timetable.save(docker.folder, Schedule(clock.replace(tzinfo=None), None, {"A": "1000DGHS0900"}, (), {},
                                           ("BNML",), timetable.FORMAT))
    saved = docker.folder / timetable.FILE_NAME
    os.utime(saved, (when, when))
    place = _place(docker("GET", "/settings", cookie=cookie)[1])
    assert f"<dt>Bus timetable</dt><dd>Saved {clock:%H:%M}, 3 min ago</dd>" in place  # fresh: no warning
    assert place.index("Bus stops") < place.index("Bus timetable") < place.index("Home")
    progress = timetable.RoadsProgress(docker.folder)
    progress(12, 140)
    place = _place(docker("GET", "/settings", cookie=cookie)[1])
    assert "<dt>Bus map roads</dt><dd>Finding them (12 of 140)</dd>" in place
    assert place.index("Bus timetable") < place.index("Bus map roads")
    progress.clear()  # found: the timetable's line alone again
    assert "Bus map roads" not in _place(docker("GET", "/settings", cookie=cookie)[1])


@pytest.mark.parametrize("config", [
    CONFIG.replace('source = "bods"', 'source = "transportapi"'),  # no timetable to build
    CONFIG.replace('operators = ["BNML"]', 'operators = ["BNML"]\nenabled = false'),
    'title = "Glossop"\n[train]\ncrs = "GLO"\n',
])
def test_the_overview_says_nothing_of_a_bus_timetable_it_doesnt_use(make, config):
    docker = make(install="docker", config=config)
    assert "Bus timetable" not in docker("GET", "/settings", cookie=docker.session())[1]


def _health(page):
    return re.search(r'<section class="health card"[^>]*>(.*?)</section>', page).group(1)


def test_the_overview_says_how_the_dashboards_feeds_are_doing(dash):
    from datetime import datetime

    from departureboard.dashboard.snapshot import status_block

    dash.site.hub = dash.app.hub
    cookie = dash.session()
    health = _health(dash("GET", "/settings", cookie=cookie)[1])
    assert "Nothing checked yet" in health  # nothing published yet
    dash.app.hub.publish("trains", {"status": status_block(datetime.now().astimezone()), "positions": None})
    health = _health(dash("GET", "/settings", cookie=cookie)[1])
    assert "All working" in health and "<strong>Trains</strong>" in health
    assert '<a href="/settings">Reload</a>' in health
    failed = status_block(datetime.now().astimezone(), True, "HTTP Error 503 <i>down</i>")
    dash.app.hub.publish("weather", {"status": failed})
    health = _health(dash("GET", "/settings", cookie=cookie)[1])
    assert "1 problem" in health and "HTTP Error 503 &lt;i&gt;down&lt;/i&gt;" in health
    dash.site.healthy = lambda: False
    assert "2 problems" in _health(dash("GET", "/settings", cookie=cookie)[1])
    dash.on["now"] = False  # the dashboard stopped: its feeds aren't said
    assert "Nothing checked yet" in _health(dash("GET", "/settings", cookie=cookie)[1])


def test_the_overview_counts_the_dashboard_not_running_as_a_problem(dash):
    dash.on["now"] = False
    dash.site.problem = "[dashboard] train_rows must be a whole number"
    assert "1 problem" in _health(dash("GET", "/settings", cookie=dash.session())[1])


def test_on_a_pi_the_overview_says_how_the_board_is_doing(pi):
    import json as _json

    pi.store.state_file.write_text(_json.dumps({"last_render": {"at": "2026-10-08T07:58:00"}}))
    health = _health(pi("GET", "/settings", cookie=pi.session())[1])
    assert "<strong>Display</strong>" in health and "<dt>Last redrawn</dt>" in health
    assert "On the dashboard" not in health  # its view is off


def test_the_overview_gives_the_wall_tablets_address_on_the_host_used(dash):
    cookie = dash.session()
    page = dash("GET", "/settings", cookie=cookie, host=f"glossop.local:{dash.port}")[1]
    assert f"<code>http://glossop.local:{dash.port}/?kiosk</code>" in page
    assert '<a href="/">Open the dashboard</a>' in page
    page = dash("GET", "/settings", cookie=cookie)[1]  # localhost: no use to a tablet
    assert "http://localhost" not in page and "<code>/?kiosk</code>" in page


def test_the_overview_says_which_keys_are_set_never_what_they_are(signed):
    page = signed("GET", "/settings", cookie=signed.session())[1]
    keys = re.search(r'<section class="keys card"[^>]*>(.*?)</section>', page).group(1)
    assert "<dt>Rail Data Marketplace key</dt>" in keys and "Set" in keys
    assert "rail-key" not in page and "bods-key" not in page
    assert '<a href="/settings/keys">Change or test keys</a>' in keys


def test_every_page_says_which_version_is_running_in_its_footer(pi):
    cookie = pi.session()
    wanted = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    for path in ("/settings", "/settings/board", "/settings/general"):
        page = pi("GET", path, cookie=cookie)[1]
        assert f'<footer class="foot"><p>Running departureboard {wanted}.</p></footer>' in page, path
        assert page.index("</form>" if path != "/settings" else "</section>") < page.index("<footer"), path
    assert "Running departureboard" not in pi("GET", "/settings/login")[1]


def test_the_footer_says_plainly_when_the_version_is_unknown(pi):
    cookie = pi.session()
    pi.site.version = "unknown"
    assert "<p>Running departureboard, version unknown.</p>" in pi("GET", "/settings", cookie=cookie)[1]
    pi.site.version = "<b>1</b>"
    assert "Running departureboard &lt;b&gt;1&lt;/b&gt;." in pi("GET", "/settings", cookie=cookie)[1]


def test_the_overview_says_why_the_dashboard_isnt_running(dash):
    cookie = dash.session()
    assert "isn't running" not in dash("GET", "/settings", cookie=cookie)[1]
    dash.site.problem = "[dashboard] train_rows must be a whole number from 1 to 20, not <50>"
    page = dash("GET", "/settings", cookie=cookie)[1]
    alert = re.search(r'<div class="summary" role="alert"><h2>(.*?)</div>', page).group(1)
    assert "The dashboard isn't running" in alert and "not &lt;50&gt;" in alert
    assert "isn't running" not in dash("GET", "/settings/dashboard", cookie=cookie)[1]


def test_the_way_back_to_the_dashboard_is_there_only_while_it_runs(dash):
    cookie = dash.session()
    assert '<a href="/">Back to the dashboard</a>' in dash("GET", "/settings", cookie=cookie)[1]
    assert '<a href="/">Back to the dashboard</a>' in dash("GET", "/settings/login")[1]
    dash.on["now"] = False
    assert 'href="/"' not in dash("GET", "/settings", cookie=cookie)[1]
    assert 'href="/"' not in dash("GET", "/settings/login")[1]


# Sections and their cues


def test_sections_are_fieldsets_headed_in_their_legends_with_an_index_when_there_are_many(signed):
    page = signed("GET", "/settings/trains-and-buses", cookie=signed.session())[1]
    headings = re.findall(r'<fieldset class="card"><legend><h2 id="([\w-]+)">([^<]+)</h2>', page)
    assert headings == [("section-title", "Title"), ("section-trains", "Trains"), ("section-buses", "Buses"),
                        ("section-weather", "Weather")]
    index = re.search(r'<nav class="index" aria-label="On this page"><ul>(.*?)</ul></nav>', page).group(1)
    assert re.findall(r'<a href="#([\w-]+)">', index) == [name for name, _ in headings]
    assert page.index("<h1>") < page.index('class="index"') < page.index('<form method="post" action="/settings/t')
    # Three sections or fewer need no index.
    page = signed("GET", "/settings/dashboard", cookie=signed.session())[1]
    assert 'class="index"' not in page and page.count('<fieldset class="card">') == 2


def test_a_section_switched_off_says_so_with_its_fields_still_there(signed):
    signed.config.write_text(CONFIG.replace('operators = ["BNML"]', 'operators = ["BNML"]\nenabled = false'))
    page = signed("GET", "/settings/trains-and-buses", cookie=signed.session())[1]
    buses = page[page.index('<h2 id="section-buses">'):page.index('<div class="savebar">')]
    assert buses.startswith(f'<h2 id="section-buses">Buses</h2> <span class="pill pill-neutral">{icon("minus")} Off'
                            "</span></legend>")
    assert 'id="bus_directions"' in buses
    assert "pill" not in page[page.index('<h2 id="section-trains">'):page.index('<h2 id="section-buses">')]


def test_a_section_not_set_up_links_to_setup(make):
    running = make(install="docker", config='title = "Glossop"\n[train]\ncrs = "GLO"\n')
    page = running("GET", "/settings/trains-and-buses", cookie=running.session())[1]
    buses = page[page.index('<h2 id="section-buses">'):]
    buses = buses[:buses.index("</fieldset>")]
    assert "Not set up" in buses and 'id="bus_enabled"' not in buses
    assert '<p class="empty">Buses aren&#x27;t set up. <a href="/settings/setup/">Pick stops with setup</a>' in buses


def test_advanced_settings_wait_behind_a_disclosure_opened_by_their_errors(pi_signed):
    cookie, csrf = pi_signed.sign_in("/settings/board")
    page = pi_signed("GET", "/settings/board", cookie=cookie)[1]
    display = page[page.index('<h2 id="section-display">'):page.index('<h2 id="section-trains">')]
    assert '<details class="advanced"><summary>Advanced</summary>' in display
    assert display.index("<summary>") < display.index('id="min_redraw_minutes"')
    assert display.index('id="theme"') < display.index("<summary>")
    fields = fields_from(page) | {"csrf": csrf, "min_redraw_minutes": "0"}
    response, page = pi_signed("POST", "/settings/board", fields, cookie=cookie)
    assert response.status == 422
    assert '<details class="advanced" open><summary>Advanced</summary>' in page
    assert page.count("<details") - page.count("<details class=\"advanced\" open>") >= 1  # the others stay closed


def test_numbers_are_narrow_text_boxes_with_their_unit_beside_them_and_in_their_label(pi):
    page = pi("GET", "/settings/board", cookie=pi.session())[1]
    field = re.search(r'<div class="field">(<label for="min_redraw_minutes">.*?)</div></div>', page).group(1)
    assert field.startswith('<label for="min_redraw_minutes">Redraw at most every<span class="vh"> (minutes)</span>'
                            "</label>")
    assert ('<div class="with-unit"><input id="min_redraw_minutes" name="min_redraw_minutes" type="text" value="2" '
            'inputmode="numeric" autocomplete="off" class="w-3"') in field
    assert field.endswith('<span class="unit" aria-hidden="true">minutes</span>')


def test_codes_and_zones_get_the_keyboard_they_need(signed):
    cookie = signed.session()
    trains = signed("GET", "/settings/trains-and-buses", cookie=cookie)[1]
    assert re.search(r'<input id="train_crs" [^>]*class="w-4" autocapitalize="characters" autocorrect="off" '
                     r'spellcheck="false"', trains)
    assert "Currently Glossop (GLO)." in trains  # the name from the bundled station list
    general = signed("GET", "/settings/general", cookie=cookie)[1]
    assert re.search(r'<input id="timezone" [^>]*autocapitalize="none" autocorrect="off"', general)
    keys = signed("GET", "/settings/keys", cookie=cookie)[1]
    assert re.search(r'<input id="RDM_API_KEY" [^>]*autocapitalize="none" autocorrect="off" data-1p-ignore '
                     r'data-lpignore="true"', keys)


def test_the_display_is_shown_read_only_as_set_in_config_toml(pi):
    page = pi("GET", "/settings/board", cookie=pi.session())[1]
    shown = re.search(r'<dl class="readonly"><div><dt>Display</dt><dd>(.*?)</dd></div></dl>', page).group(1)
    assert shown.startswith("inky (250×122)") and "Set in config.toml</span>" in shown
    assert 'name="display"' not in page and "disabled" not in page


def test_the_save_bar_says_when_changes_take_effect_before_saving(make):
    for install, words in (("pi", "Changes show on the board and the dashboard within a few minutes"),
                           ("docker", "Saving starts the dashboard again")):
        running = make(install=install)
        page = running("GET", "/settings/general", cookie=running.session())[1]
        bar = re.search(r'<div class="savebar">(.*?)</div>', page).group(1)
        assert bar.startswith(f'<p class="applies" id="applies">{words}')
        assert bar.endswith('<button type="submit" class="primary" aria-describedby="applies">Save changes</button>')


# Saving


def test_a_signed_in_save_redirects_to_its_page_which_says_what_changed(signed):
    cookie, csrf = signed.sign_in()
    path = "/settings/trains-and-buses"
    fields = fields_from(signed("GET", path, cookie=cookie)[1])
    response, text = signed("POST", path, fields | {"csrf": csrf, "title": "Our house", "train_walk_minutes": "9"},
                            cookie=cookie)
    assert response.status == 303 and response.getheader("Location") == path + "?saved" and text == ""
    assert signed.saved()["title"] == "Our house"
    response, page = signed("GET", path + "?saved", cookie=cookie)
    assert "<title>Saved – Trains and buses – Our house settings</title>" in page
    notice = re.search(r'<p class="notice-ok" role="status" tabindex="-1" autofocus>(.*?)</p>', page).group(1)
    assert notice == (f"{icon('check')} Saved 2 changes: Title; Walk to the station, none to 9 minutes. "
                      "The dashboard starts again in a few seconds to use them.")
    assert page.index('<nav aria-label="Settings"') < page.index("notice-ok") < page.index(f'action="{path}"')
    assert "notice-ok" not in signed("GET", path + "?saved", cookie=cookie)[1]  # said once


def test_a_save_on_the_pi_says_when_the_board_shows_it(pi_signed):
    cookie, csrf = pi_signed.sign_in("/settings/board")
    fields = fields_from(pi_signed("GET", "/settings/board", cookie=cookie)[1])
    response, text = pi_signed("POST", "/settings/board", fields | {"csrf": csrf, "board_train_rows": "2"},
                               cookie=cookie)
    assert response.status == 303, text
    page = pi_signed("GET", response.getheader("Location"), cookie=cookie)[1]
    assert "<title>Saved – Board – Glossop settings</title>" in page
    assert ("Saved 1 change: Trains shown, 3 to 2. The board shows them within a few minutes, a screen within "
            "seconds.") in page
    assert pi_signed.saved()["train"]["rows"] == 2


def test_save_errors_come_back_on_the_form(signed):
    cookie, csrf = signed.sign_in()
    path = "/settings/trains-and-buses"
    fields = fields_from(signed("GET", path, cookie=cookie)[1])
    response, text = signed("POST", path, fields | {"csrf": csrf, "train_crs": "Glossop"}, cookie=cookie)
    assert response.status == 422 and "Not saved: fix 1 thing" in text and 'href="#train_crs"' in text
    assert "<title>Trains and buses – Glossop settings: fix the errors</title>" in text
    assert text.index('<nav aria-label="Settings"') < text.index('class="summary"') < text.index(f'action="{path}"')
    assert not (signed.folder / "settings.toml").exists()


@pytest.mark.parametrize("path", ["/settings/trains-and-buses", "/settings/dashboard"])
@pytest.mark.parametrize("csrf, origin", [
    ("forged", "same"),
    (None, "same"),
    ("right", "http://evil.example.com"),
    ("right", "null"),
    ("right", None),
])
def test_every_change_needs_the_same_origin_and_the_form_token(signed, path, csrf, origin):
    cookie = signed.session()
    right = signed.csrf(cookie)
    fields = fields_from(signed("GET", path, cookie=cookie)[1]) | {"title": "x", "dashboard_train_rows": "4"}
    body = fields if csrf is None else fields | {"csrf": right if csrf == "right" else csrf}
    response, _ = signed("POST", path, body, cookie=cookie, origin=origin)
    assert response.status == 403
    assert not (signed.folder / "settings.toml").exists()


def test_a_save_without_a_session_goes_to_sign_in(signed):
    for path in ("/settings/trains-and-buses", "/settings/dashboard", "/settings/keys", "/settings/general"):
        response, _ = signed("POST", path, {"csrf": "x", "title": "x"})
        assert response.status == 303 and response.getheader("Location") == "/settings/login", path


def test_the_forms_save_into_the_store_and_never_show_a_key(signed):
    cookie, csrf = signed.sign_in()
    _, page = signed("GET", "/settings/keys", cookie=cookie)
    assert "Saved, not tested</span>" in page and "rail-key-1234567" not in page and "bods-key-7654321" not in page
    _, page = signed("GET", "/settings/trains-and-buses", cookie=cookie)
    fields = fields_from(page)
    assert fields["train_crs"] == "GLO" and fields["bus_stops"] == "A = 1000DGHS0900"
    response, text = signed("POST", "/settings/trains-and-buses", fields | {"csrf": csrf, "train_crs": "man"},
                            cookie=cookie)
    assert response.status == 303, text
    assert signed.saved()["train"]["crs"] == "MAN"
    _, page = signed("GET", "/settings/dashboard", cookie=cookie)
    assert fields_from(page)["dashboard_train_rows"] == "10"


def test_the_dashboard_page_says_what_is_wrong_and_saves_nothing(signed):
    cookie, csrf = signed.sign_in("/settings/dashboard")
    fields = fields_from(signed("GET", "/settings/dashboard", cookie=cookie)[1])
    fields.update(csrf=csrf, dashboard_train_rows="50", history_days="0")
    response, text = signed("POST", "/settings/dashboard", fields, cookie=cookie)
    assert response.status == 422 and "Not saved: fix 2 things" in text
    assert 'href="#dashboard_train_rows"' in text and 'aria-invalid="true"' in text
    assert not (signed.folder / "settings.toml").exists()


def test_the_dashboard_page_says_when_its_buses_cant_be_shown(make):
    running = make(install="docker", config=CONFIG.replace('source = "bods"', 'source = "transportapi"'))
    page = running("GET", "/settings/dashboard", cookie=running.session())[1]
    assert "only from the free BODS timetable" in page


def test_bodies_must_be_plain_small_forms(signed):
    cookie, csrf = signed.sign_in()
    path = "/settings/trains-and-buses"
    response, _ = signed("POST", path, cookie=cookie, json_body={"csrf": csrf})
    assert response.status == 400
    response, _ = signed("POST", path, {"csrf": csrf, "title": "x" * 70_000}, cookie=cookie)
    assert response.status == 413
    response, _ = signed("POST", path, None, cookie=cookie, headers={"Transfer-Encoding": "chunked"})
    assert response.status == 400
    assert not (signed.folder / "settings.toml").exists()


def test_a_form_with_too_many_fields_is_refused_not_dropped(dash):
    response, _ = dash("POST", "/settings/login", {f"f{n}": "1" for n in range(301)})
    assert response.status == 413


# The Keys page: one small form per key, each saved and tested on its own


def key_form(page, name):
    """The form of key `name` on the Keys page, as drawn."""
    return re.search(r'<form method="post" action="/settings/keys" novalidate class="keyform">'
                     r'<input type="hidden" name="csrf" value="[^"]*">'
                     r'<input type="hidden" name="key" value="%s">.*?</form>' % name, page, re.S).group(0)


def press(running, name, do="save", typed=None, cookie=None, origin="same", csrf="right"):
    """Press button `do` of key `name`'s form, with `typed` in its boxes, as a
    browser posts that form alone (do=None: Enter in a box, before the browser
    adds the first button). (The redirect or refusal, the session's cookie.)"""
    cookie = cookie or running.session()
    form = key_form(running("GET", "/settings/keys", cookie=cookie)[1], name)
    fields = dict.fromkeys(re.findall(r'<input id="(\w+)" name="\w+" type="text" class="key-box"', form), "")
    fields |= fields_from(form) | {"key": name}
    if csrf is not None:
        fields["csrf"] = running.csrf(cookie) if csrf == "right" else csrf
    if do is not None:
        fields["do"] = do
    response, _ = running("POST", "/settings/keys", fields | (typed or {}), cookie=cookie, origin=origin)
    return response, cookie


def after(running, response, cookie):
    """The page a redirect goes to."""
    assert response.status == 303, response.status
    return running("GET", response.getheader("Location"), cookie=cookie)[1]


def results(running):
    return json.loads((running.folder / "keytests.json").read_text())


def pill_time(running):
    """When a test now is said to be, as the pill says it."""
    when = datetime.fromtimestamp(int(running.clock.now), ZoneInfo("Europe/London"))
    return f"{when.day} {when:%b %H:%M}"


def test_each_key_of_the_views_on_has_a_small_form_of_its_own(make):
    pi, docker = make(install="pi"), make(install="docker")
    expected = {
        pi: [("RDM_API_KEY", "Trains"), ("BODS_API_KEY", "Buses"), ("TAPI_APP_KEY", "Buses from TransportAPI")],
        docker: [("RDM_API_KEY", "Trains"), ("BODS_API_KEY", "Buses"), ("RDM_DETAILS_API_KEY", "Where each train is"),
                 ("RDM_KB_API_KEY", "Engineering works")],
    }
    for running, forms in expected.items():
        page = running("GET", "/settings/keys", cookie=running.session())[1]
        assert re.findall(r'<input type="hidden" name="key" value="(\w+)">', page) == [name for name, _ in forms]
        for name, feature in forms:
            form = key_form(page, name)
            assert re.search(r'<fieldset class="card"><legend><h2 id="section-[\w-]+">%s</h2> <span class="pill '
                             % feature, form), name
            assert form.count("<form") == 1 and form.count('name="csrf"') == 1
        for other in ("/settings/trains-and-buses", "/settings/general"):
            other_page = running("GET", other, cookie=running.session())[1]
            assert 'name="do"' not in other_page and "key-box" not in other_page, other


def test_save_key_comes_first_so_enter_saves_then_save_and_test_or_test_saved_key(signed):
    page = signed("GET", "/settings/keys", cookie=signed.session())[1]
    saved = key_form(page, "RDM_API_KEY")  # saved in the fixture
    buttons = re.findall(r'<button type="submit" name="do" value="(\w+)"([^>]*)>([^<]+)</button>', saved)
    assert buttons == [("save", ' class="primary"', "Save key"), ("save_test", ' class="if-typed"', "Save and test"),
                       ("test", ' class="if-empty"', "Test saved key")]
    not_set = key_form(page, "RDM_DETAILS_API_KEY")
    buttons = re.findall(r'<button type="submit" name="do" value="(\w+)"([^>]*)>([^<]+)</button>', not_set)
    assert buttons == [("save", ' class="primary"', "Save key"), ("save_test", "", "Save and test")]
    # Enter in a box sends the first button; a post without one saves too.
    response, cookie = press(signed, "RDM_DETAILS_API_KEY", None, {"RDM_DETAILS_API_KEY": "details-key-0001"})
    assert "Saved 1 change" in after(signed, response, cookie) and signed.setup.tests == []
    assert "RDM_DETAILS_API_KEY=details-key-0001" in signed.store.secrets_file.read_text()


def test_the_stylesheet_shows_save_and_test_or_test_saved_key_as_the_box_is_filled_or_empty():
    css = WEB_CSS.read_text()
    assert ".keyform:not(:has(.key-box:not(:placeholder-shown))) .if-typed { display: none; }" in css
    assert ".keyform:has(.key-box:not(:placeholder-shown)) .if-empty { display: none; }" in css


def test_key_boxes_are_plain_text_never_filled_and_left_alone_by_password_managers(signed):
    page = signed("GET", "/settings/keys", cookie=signed.session())[1]
    boxes = re.findall(r'<input id="\w+" name="\w+" type="text" class="key-box"[^>]*>', page)
    assert len(boxes) == 4 and 'type="password"' not in page
    for box in boxes:
        assert "value=" not in box
        for attribute in ('autocomplete="off"', 'spellcheck="false"', 'autocapitalize="none"', 'autocorrect="off"',
                          "data-1p-ignore", 'data-lpignore="true"', 'placeholder="'):
            assert attribute in box, (box, attribute)
    assert 'placeholder="Paste a new key to replace the saved one"' in key_form(page, "RDM_API_KEY")
    assert 'placeholder="Paste the key"' in key_form(page, "RDM_DETAILS_API_KEY")


def test_each_keys_state_is_a_pill_in_words(signed):
    signed.secrets.write_text("BODS_API_KEY=bods-key-7654321\n")  # trains are on, with no key
    cookie = signed.session()
    page = signed("GET", "/settings/keys", cookie=cookie)[1]
    pills = dict(re.findall(r'name="key" value="(\w+)">.*?<span class="pill pill-(\w+)">', page, re.S))
    assert pills == {"RDM_API_KEY": "warn", "BODS_API_KEY": "neutral", "RDM_DETAILS_API_KEY": "neutral",
                     "RDM_KB_API_KEY": "neutral"}
    assert f'{icon("triangle-alert")} Needed for trains</span>' in key_form(page, "RDM_API_KEY")
    assert f'{icon("info")} Saved, not tested</span>' in key_form(page, "BODS_API_KEY")
    assert f'{icon("minus")} Not set</span>' in key_form(page, "RDM_DETAILS_API_KEY")
    signed.setup.result = KeyResult("wrong_key", "The Bus Open Data Service refused this key.")
    after(signed, press(signed, "BODS_API_KEY", "test", cookie=cookie)[0], cookie)
    page = signed("GET", "/settings/keys", cookie=cookie)[1]
    assert f'<span class="pill pill-bad">{icon("x")} Refused, tested {pill_time(signed)}</span>' in page


def test_saving_one_key_leaves_every_other_key_and_setting_as_saved(signed):
    settings = 'title = "Our house"\ntheme = "plain"\n[dashboard]\nhistory_days = 30\n'
    signed.store.settings_file.write_text(settings)
    signed.store.secrets_file.write_text("TAPI_APP_ID=kept\nRDM_API_KEY=saved-rail-key-1\n")
    # Whatever else a forged post carries, only its form's key is saved.
    others = {"RDM_API_KEY": "sneaky-rail-key", "title": "x", "kb_incidents_url": "https://raildata.org.uk/x"}
    response, cookie = press(signed, "RDM_DETAILS_API_KEY", "save", others | {"RDM_DETAILS_API_KEY": "details-key-0001"})
    assert response.getheader("Location") == "/settings/keys?saved"
    assert signed.store.settings_file.read_text() == settings
    assert signed.store.secrets_file.read_text() == (
        "TAPI_APP_ID=kept\nRDM_API_KEY=saved-rail-key-1\nRDM_DETAILS_API_KEY=details-key-0001\n")
    assert signed.store.secrets_file.stat().st_mode & 0o777 == 0o600
    assert signed.config.read_text() == CONFIG and signed.secrets.read_text() == KEYS
    page = after(signed, response, cookie)
    assert "<title>Saved – Keys – Our house settings</title>" in page
    notice = re.search(r'<p class="notice-ok" role="status" tabindex="-1" autofocus>(.*?)</p>', page).group(1)
    assert notice == (f"{icon('check')} Saved 1 change: Rail Data Marketplace key for where each train is. "
                      "The dashboard starts again in a few seconds to use them.")
    for secret in ("details-key-0001", "saved-rail-key-1", "sneaky-rail-key"):
        assert secret not in "".join(signed.seen)


def test_the_first_key_can_be_saved_before_the_others(signed):
    signed.secrets.write_text("")  # trains and buses set up, and neither key: the settings don't load yet
    response, _ = press(signed, "RDM_API_KEY", "save", {"RDM_API_KEY": "first-rail-key"})
    assert response.getheader("Location") == "/settings/keys?saved"
    assert signed.store.secrets_file.read_text() == "RDM_API_KEY=first-rail-key\n"


def test_save_and_test_saves_then_tests_the_key_without_pasting_it_again(signed):
    response, cookie = press(signed, "RDM_DETAILS_API_KEY", "save_test", {"RDM_DETAILS_API_KEY": "details-key-0001"})
    assert response.getheader("Location") == "/settings/keys?saved"
    assert signed.store.secrets_file.read_text() == "RDM_DETAILS_API_KEY=details-key-0001\n"
    assert signed.setup.tests == [("RDM_DETAILS_API_KEY", None, None)]  # as saved
    page = after(signed, response, cookie)
    form = key_form(page, "RDM_DETAILS_API_KEY")
    result = ('<p class="key-result key-result-works" id="RDM_DETAILS_API_KEY-test" role="status" tabindex="-1" '
              f'autofocus>{icon("check")} Saved. <strong>Works:</strong> It works: the service answered.</p>')
    box = re.search(r'<input id="RDM_DETAILS_API_KEY"[^>]*>', form).group(0)
    assert form[form.index(box) + len(box):].startswith(result)
    assert 'aria-describedby="RDM_DETAILS_API_KEY-hint RDM_DETAILS_API_KEY-test"' in box
    # The result takes focus; the notice above still says what was saved.
    assert '<p class="notice-ok" role="status">' in page and page.count("autofocus") == 1
    assert f"Works, tested {pill_time(signed)}</span>" in form
    assert results(signed) == {"results": {"RDM_DETAILS_API_KEY": {"state": "works", "time": int(signed.clock.now)}}}
    assert "key-result" not in signed("GET", "/settings/keys?saved", cookie=cookie)[1]  # said once


@pytest.mark.parametrize("do", ["test", "save_test"])
def test_a_saved_key_is_tested_as_saved_with_its_box_empty_and_nothing_saved(signed, do):
    response, cookie = press(signed, "RDM_API_KEY", do)
    assert response.getheader("Location") == "/settings/keys?tested"
    assert signed.setup.tests == [("RDM_API_KEY", None, None)]
    assert not signed.store.settings_file.exists() and not signed.store.secrets_file.exists()
    page = after(signed, response, cookie)
    assert "<title>Key tested – Keys – Glossop settings</title>" in page and "notice-ok" not in page
    assert (f'autofocus>{icon("check")} <strong>Works:</strong> It works: the service answered.</p>'
            in key_form(page, "RDM_API_KEY"))
    page = signed("GET", "/settings/keys", cookie=cookie)[1]
    assert "key-result" not in page and f"Works, tested {pill_time(signed)}</span>" in key_form(page, "RDM_API_KEY")


def test_a_result_is_said_only_on_the_keys_page(signed):
    response, cookie = press(signed, "RDM_API_KEY", "test")
    for path in ("/settings/general?tested", "/settings/trains-and-buses?saved", "/settings?saved"):
        reply, page = signed("GET", path, cookie=cookie)
        assert reply.status == 200 and "key-result" not in page and "Key tested" not in page, path
    assert "<strong>Works:</strong>" in after(signed, response, cookie)


def test_test_saved_key_never_saves_what_is_typed(signed):
    response, cookie = press(signed, "RDM_DETAILS_API_KEY", "test", {"RDM_DETAILS_API_KEY": "typed-details-key-1"})
    assert response.getheader("Location") == "/settings/keys?tested"
    assert signed.setup.tests == [("RDM_DETAILS_API_KEY", None, None)] and not signed.store.secrets_file.exists()


def test_a_result_is_kept_as_its_state_and_time_only_in_a_private_file(signed):
    typed = "typed-details-key-1"
    signed.setup.result = KeyResult("not_subscribed", f"Refused {typed} and rail-key-1234567.")
    press(signed, "RDM_DETAILS_API_KEY", "save_test", {"RDM_DETAILS_API_KEY": typed})
    path = signed.folder / "keytests.json"
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_bytes()
    assert json.loads(raw) == {"results": {"RDM_DETAILS_API_KEY": {"state": "refused",
                                                                   "time": int(signed.clock.now)}}}
    for secret in (typed, "rail-key-1234567", "bods-key-7654321", "Refused"):
        assert secret.encode() not in raw


def test_an_inconclusive_test_keeps_the_last_result(signed):
    press(signed, "RDM_API_KEY", "test")
    first = results(signed)
    signed.clock.now += 60
    signed.setup.result = KeyResult("service_down", "The Rail Data Marketplace took too long to answer.")
    press(signed, "RDM_API_KEY", "test")
    assert results(signed) == first


def test_replacing_a_key_forgets_its_last_result(signed):
    press(signed, "RDM_API_KEY", "test")
    press(signed, "BODS_API_KEY", "test")
    response, cookie = press(signed, "RDM_API_KEY", "save", {"RDM_API_KEY": "new-rail-key-1"})
    assert list(results(signed)["results"]) == ["BODS_API_KEY"]
    page = after(signed, response, cookie)
    assert "Saved, not tested</span>" in key_form(page, "RDM_API_KEY")
    assert "Works, tested" in key_form(page, "BODS_API_KEY")


def test_a_key_pasted_wrongly_is_not_saved_or_tested_and_the_summary_says_so(signed):
    cookie = signed.session()
    for do in ("save", "save_test"):
        response, _ = press(signed, "RDM_KB_API_KEY", do, {"RDM_KB_API_KEY": "has spaces in it"}, cookie=cookie)
        assert response.status == 422
    page = signed.seen[-1]
    assert "<title>Keys – Glossop settings: fix the errors</title>" in page
    assert ('<a href="#RDM_KB_API_KEY">Engineering works – Rail Data Marketplace key for engineering works: '
            "Paste the key exactly") in page
    assert "The key you pasted wasn't saved. Paste it again once the rest is right." in page
    assert re.search(r'<div class="field has-error"><label for="RDM_KB_API_KEY">', page)
    assert "has spaces in it" not in "".join(signed.seen)
    assert signed.setup.tests == [] and not signed.store.secrets_file.exists()


@pytest.mark.parametrize("typed", ["abc\nRDM_API_KEY=evil", "abc\r\nx", "abc#x", "x" * 201])
def test_a_key_cannot_carry_another_line_or_anything_but_a_key(signed, typed):
    response, _ = press(signed, "BODS_API_KEY", "save_test", {"BODS_API_KEY": typed})
    assert response.status == 422 and "Paste the key exactly" in signed.seen[-1]
    assert not signed.store.secrets_file.exists() and signed.setup.tests == []


def test_saving_an_empty_box_says_to_paste_a_key(signed):
    for name, do in (("RDM_DETAILS_API_KEY", "save"), ("RDM_DETAILS_API_KEY", "save_test"), ("RDM_API_KEY", "save")):
        response, _ = press(signed, name, do)
        assert response.status == 422 and f'<a href="#{name}">' in signed.seen[-1], (name, do)
        assert "Nothing to save: paste a key first." in signed.seen[-1]
    assert signed.setup.tests == [] and not signed.store.secrets_file.exists()


def test_the_engineering_works_address_sits_with_its_key(signed):
    page = signed("GET", "/settings/keys", cookie=signed.session())[1]
    assert page.count('id="kb_incidents_url"') == 1 and 'id="kb_incidents_url"' in key_form(page, "RDM_KB_API_KEY")
    response, _ = press(signed, "RDM_KB_API_KEY", "save", {"kb_incidents_url": "https://api.example.test/kb"})
    assert response.status == 422 and not signed.store.settings_file.exists()
    assert '<a href="#kb_incidents_url">Engineering works – Engineering works address: ' in signed.seen[-1]
    assert 'value="https://api.example.test/kb"' in signed.seen[-1]  # what was typed, to put right
    url = "https://api1.raildata.org.uk/1010-knowledgebase-incidents/incidents"
    response, cookie = press(signed, "RDM_KB_API_KEY", "save", {"kb_incidents_url": url, "RDM_KB_API_KEY": "kb-key-0001"})
    assert signed.saved() == {"dashboard": {"kb_incidents_url": url}}
    assert signed.store.secrets_file.read_text() == "RDM_KB_API_KEY=kb-key-0001\n"
    page = after(signed, response, cookie)
    assert ("Saved 2 changes: Rail Data Marketplace key for engineering works; Engineering works address." in page)
    assert f'value="{url}"' in key_form(page, "RDM_KB_API_KEY")
    # The address alone, then nothing changed.
    press(signed, "RDM_KB_API_KEY", "test")
    assert "RDM_KB_API_KEY" in results(signed)["results"]
    response, cookie = press(signed, "RDM_KB_API_KEY", "save", {"kb_incidents_url": ""})
    assert "Saved 1 change: Engineering works address." in after(signed, response, cookie)
    assert "RDM_KB_API_KEY" not in results(signed)["results"]  # tested with the old address
    response, cookie = press(signed, "RDM_KB_API_KEY", "save")
    assert "Saved. Nothing had changed." in after(signed, response, cookie)


def test_transportapis_app_id_and_key_share_one_form_and_test(pi_signed):
    page = pi_signed("GET", "/settings/keys", cookie=pi_signed.session())[1]
    form = key_form(page, "TAPI_APP_KEY")
    assert re.findall(r'<input id="(\w+)" name="\w+" type="text" class="key-box"', form) == ["TAPI_APP_ID",
                                                                                              "TAPI_APP_KEY"]
    assert ('<p class="hint" id="TAPI_APP_KEY-cost">Testing uses 1 of today\'s 30 TransportAPI lookups, the same '
            "ones bus times use.</p>") in form
    assert 'value="save_test" aria-describedby="TAPI_APP_KEY-cost">Save and test</button>' in form
    response, cookie = press(pi_signed, "TAPI_APP_KEY", "save_test", {"TAPI_APP_ID": "typed-app-id"})
    assert pi_signed.store.secrets_file.read_text() == "TAPI_APP_ID=typed-app-id\n"
    assert pi_signed.setup.tests == [("TAPI_APP_KEY", None, None)]
    page = after(pi_signed, response, cookie)
    assert "Saved 1 change: TransportAPI app ID." in page
    form = key_form(page, "TAPI_APP_KEY")
    assert form.count('id="TAPI_APP_KEY-test"') == 1  # the one result, by the app key's box
    assert re.search(r'<input id="TAPI_APP_KEY"[^>]*aria-describedby="[^"]*TAPI_APP_KEY-test"', form)
    press(pi_signed, "TAPI_APP_KEY", "save", {"TAPI_APP_KEY": "typed-app-key"})
    assert pi_signed.store.secrets_file.read_text() == "TAPI_APP_ID=typed-app-id\nTAPI_APP_KEY=typed-app-key\n"
    assert "TAPI_APP_KEY" not in results(pi_signed)["results"]  # replacing either forgets the pair's result


def test_a_key_form_needs_signing_in(signed):
    for do in ("save", "save_test", "test"):
        response, _ = signed("POST", "/settings/keys", {"csrf": "x", "key": "RDM_API_KEY", "do": do,
                                                         "RDM_API_KEY": "new-rail-key-1"})
        assert response.status == 303 and response.getheader("Location") == "/settings/login"
    assert signed.setup.tests == [] and not signed.store.secrets_file.exists()


@pytest.mark.parametrize("do", ["save", "save_test", "test"])
@pytest.mark.parametrize("csrf, origin", [
    ("forged", "same"),
    (None, "same"),
    ("right", "http://evil.example.com"),
    ("right", "null"),
    ("right", None),
])
def test_a_key_form_needs_the_same_origin_and_the_form_token(signed, do, csrf, origin):
    response, _ = press(signed, "RDM_API_KEY", do, {"RDM_API_KEY": "new-rail-key-1"}, origin=origin, csrf=csrf)
    assert response.status == 403
    assert signed.setup.tests == [] and not signed.store.secrets_file.exists()
    assert not (signed.folder / "keytests.json").exists()


def test_a_key_form_is_a_plain_small_form_like_a_save(signed):
    cookie = signed.session()
    csrf = signed.csrf(cookie)
    response, _ = signed("POST", "/settings/keys", cookie=cookie,
                         json_body={"csrf": csrf, "key": "RDM_API_KEY", "do": "test"})
    assert response.status == 400
    response, _ = signed("POST", "/settings/keys", {"csrf": csrf, "key": "RDM_API_KEY", "do": "test",
                                                   "title": "x" * 70_000}, cookie=cookie)
    assert response.status == 413 and signed.setup.tests == []


@pytest.mark.parametrize("name, do", [
    ("TAPI_APP_KEY", "test"),  # the board's, and its view is off
    ("TAPI_APP_ID", "save"),  # a box, not a form
    ("PASSWORD", "save"),
    ("", "test"),
    ("RDM_API_KEY", "delete"),
])
def test_only_the_keys_of_the_forms_shown_are_saved_or_tested(signed, name, do):
    cookie = signed.session()
    body = {"csrf": signed.csrf(cookie), "key": name, "do": do, name or "x": "new-key-0001"}
    response, _ = signed("POST", "/settings/keys", body, cookie=cookie)
    assert response.status == 400 and signed.setup.tests == [] and not signed.store.secrets_file.exists()


@pytest.mark.parametrize("path", ["/settings/dashboard", "/settings/trains-and-buses", "/settings/general"])
def test_no_other_page_saves_or_tests_a_key(signed, path):
    cookie = signed.session()
    fields = fields_from(signed("GET", path, cookie=cookie)[1])
    body = fields | {"csrf": signed.csrf(cookie), "key": "RDM_API_KEY", "do": "save_test",
                     "RDM_API_KEY": "new-rail-key-1", "test": "RDM_API_KEY"}
    response, _ = signed("POST", path, body, cookie=cookie)
    assert response.status == 303 and signed.setup.tests == [] and not signed.store.secrets_file.exists()


def test_the_overview_tests_nothing(signed):
    cookie = signed.session()
    response, _ = signed("POST", "/settings", {"csrf": signed.csrf(cookie), "key": "RDM_API_KEY", "do": "test"},
                         cookie=cookie)
    assert response.status == 405 and signed.setup.tests == []


@pytest.mark.parametrize("state, words", [
    ("works", "Works"),
    ("wrong_key", "Wrong key"),
    ("not_subscribed", "Not subscribed"),
    ("service_down", "Service not answering"),
    ("missing", "No key"),
    ("unknown", "Can't tell"),
])
def test_every_result_is_said_in_words_not_colour_alone(signed, state, words):
    signed.setup.result = KeyResult(state, "Said plainly.")
    response, cookie = press(signed, "BODS_API_KEY", "test")
    page = after(signed, response, cookie)
    assert f'class="key-result key-result-{state}" id="BODS_API_KEY-test"' in page
    assert f"<strong>{words}:</strong> Said plainly.</p>" in page


def test_no_key_is_ever_shown_or_logged(signed, caplog):
    typed = "typed-details-key-1"
    signed.setup.result = KeyResult("wrong_key", f"Refused {typed} and rail-key-1234567.")
    response, cookie = press(signed, "RDM_DETAILS_API_KEY", "save_test", {"RDM_DETAILS_API_KEY": typed})
    assert "Refused … and …." in after(signed, response, cookie)
    after(signed, *press(signed, "RDM_API_KEY", "test", {"RDM_API_KEY": "typed-rail-key-2"}))
    response, _ = press(signed, "BODS_API_KEY", "save", {"BODS_API_KEY": "bad bods key 3"})
    assert response.status == 422
    for secret in (typed, "typed-rail-key-2", "bad bods key 3", "rail-key-1234567", "bods-key-7654321"):
        assert secret not in "".join(signed.seen) and secret not in caplog.text


@pytest.mark.parametrize("failure, said", [
    (TooSoon("Wait 7 s before testing this key again"), "Wait 7 s before testing this key again"),
    (SetupError("The fetch service didn't answer in time"), "The fetch service didn't answer in time"),
    (RuntimeError("rail-key-1234567 went wrong"), "Something went wrong while testing the key. Try again."),
])
def test_a_test_that_cant_run_says_why_plainly(signed, caplog, failure, said):
    signed.setup.result = failure
    response, cookie = press(signed, "RDM_API_KEY", "test")
    page = after(signed, response, cookie)
    assert f"<strong>Not tested:</strong> {said}" in page
    assert "rail-key-1234567" not in page and "rail-key-1234567" not in caplog.text
    assert not (signed.folder / "keytests.json").exists()


class KeyRunner:
    """A setupapi.Runner that only tests keys, as the Pi's fetch service does them."""

    def __init__(self):
        self.tests = []

    def test_key(self, name, value, app_id):
        self.tests.append((name, value, app_id))
        return KeyResult("works", "It works.")

    def lookup_postcode(self, postcode):
        raise AssertionError("not a lookup")

    search_address = nearby_stops = walk_minutes = make_job = lookup_postcode


@pytest.fixture
def on_a_pi(make):
    """A Pi whose wizard's API is the real one, with its runner: no key is tested in this process."""
    runner, here, now = KeyRunner(), [], [100.0]

    def wizard(store):
        return SetupApi(store.target(), store.env, store.folder, install=install_named("pi"), runner=runner,
                        test_key=lambda *args: here.append(args), key_tests=RateLimit(10.0, clock=lambda: now[0]))

    running = make(install="pi", setup=wizard)
    running.runner, running.here, running.now = runner, here, now
    return running


def test_on_a_pi_the_test_runs_in_the_setup_service_never_here(on_a_pi):
    response, cookie = press(on_a_pi, "TAPI_APP_KEY", "save_test",
                             {"TAPI_APP_ID": "typed-app-id", "TAPI_APP_KEY": "typed-app-key"})
    assert "<strong>Works:</strong> It works." in after(on_a_pi, response, cookie)
    # Saved first, so the service reads the pair as saved: no key is handed to it.
    assert on_a_pi.runner.tests == [("TAPI_APP_KEY", None, None)] and on_a_pi.here == []


def test_each_key_is_tested_at_most_once_every_ten_seconds_as_in_the_wizard(on_a_pi):
    press(on_a_pi, "RDM_API_KEY", "test")
    response, cookie = press(on_a_pi, "RDM_API_KEY", "test")
    assert "<strong>Not tested:</strong> Wait 10 s before testing this key again" in after(on_a_pi, response, cookie)
    press(on_a_pi, "BODS_API_KEY", "test")  # another key has its own limit
    assert on_a_pi.setup.handle("testkey", {"name": "BODS_API_KEY"})[0] == 429  # one limit with the wizard
    on_a_pi.now[0] += 10
    press(on_a_pi, "RDM_API_KEY", "test")
    assert [name for name, _, _ in on_a_pi.runner.tests] == ["RDM_API_KEY", "BODS_API_KEY", "RDM_API_KEY"]


def framed_post(running, path, pairs, body=b"", origin="same"):
    """POST with exactly these extra (name, value) header pairs, repeats and
    all, and no Content-Length unless they hold one."""
    conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=10)
    conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    host = f"localhost:{running.port}"
    conn.putheader("Host", host)
    if origin == "same":
        conn.putheader("Origin", f"http://{host}")
    elif origin:
        conn.putheader("Origin", origin)
    for name, value in pairs:
        conn.putheader(name, value)
    conn.endheaders()
    if body:
        conn.send(body)
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response, text


FORM = ("Content-Type", "application/x-www-form-urlencoded")
BAD_FRAMING = [
    ([("Transfer-Encoding", "chunked")], 400),
    ([("Transfer-Encoding", "chunked"), ("Content-Length", "3")], 400),
    ([("Content-Length", "3"), ("Content-Length", "3")], 400),
    ([], 411),
    ([("Content-Length", "three")], 411),
    ([("Content-Length", "-1")], 411),
    ([("Content-Length", str(10**7))], 413),
]


@pytest.mark.parametrize("pairs, status", BAD_FRAMING)
def test_a_form_framed_any_way_but_one_plain_length_is_refused_unread(pi_signed, pairs, status):
    cookie = pi_signed.session()
    response, text = framed_post(pi_signed, "/settings/board", [FORM, ("Cookie", cookie), *pairs])
    assert response.status == status and response.getheader("Connection") == "close"
    assert "default-src 'none'" in csp(response) and "script-src" not in csp(response)
    assert "<html" not in text.lower() and not (pi_signed.folder / "settings.toml").exists()


@pytest.mark.parametrize("path", ["/settings/login", "/settings", "/settings/board", "/settings/setup/api/begin"])
@pytest.mark.parametrize("pairs", [[], [("Content-Length", "99999999")], [("Transfer-Encoding", "chunked")]])
def test_the_origin_is_checked_before_the_body_is_read(pi_signed, path, pairs):
    for origin in ("http://evil.example.com", "null", None):
        response, _ = framed_post(pi_signed, path, pairs, origin=origin)
        assert response.status == 403, (path, pairs, origin)
    assert pi_signed.setup.calls == [] and not (pi_signed.folder / "settings.toml").exists()


def test_a_null_origin_without_a_referer_is_refused(signed):
    assert signed("POST", "/settings/login", {"password": PASSWORD}, origin="null")[0].status == 403


def test_other_methods_are_not_allowed(make):
    running = make(install="pi")
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "BREW"):
        for path in ("/settings", "/settings/login", "/settings/nowhere"):
            response, text = running(method, path, {"title": "x"})
            assert response.status == 405 and response.getheader("Allow") == "GET, HEAD, POST", (method, path)
            assert "script-src" not in csp(response) and response.getheader("X-Frame-Options") == "DENY"
            assert response.getheader("Content-Type") == "text/plain; charset=utf-8" and "<html" not in text.lower()
        for path in ("/", "/nowhere", "/login"):
            response, _ = running(method, path)
            assert response.status == 405 and response.getheader("Allow") == "GET, HEAD", (method, path)
            assert "script-src" not in csp(response), (method, path)
        assert running(method, "/settings", host="evil.example.com")[0].status == 403  # outsiders first
    assert not (running.folder / "settings.toml").exists()


# A forgotten password, and the first claim, with a code from the board (a Pi)


def board_code(running):
    return json.loads((running.folder / "reset-display.json").read_text())["code"]


def ask_for_code(running, cookie=None):
    form_cookie, token = running.form(cookie=cookie)
    response, _ = running("POST", "/settings/forgot", {"form": token}, cookie=form_cookie)
    assert response.status == 303 and response.getheader("Location") == "/settings/reset?asked"
    return form_cookie


def reset(running, form_cookie, code, new=NEW_PASSWORD, again=None):
    _, token = running.form("/settings/reset?asked", cookie=form_cookie)
    return running("POST", "/settings/reset", {"form": token, "code": code, "password": new, "again": again or new},
                   cookie=form_cookie)


def assert_never_shown(code, running, caplog):
    # Random tokens first, which could hold any six digits by chance.
    tokens = re.compile(r'(name="form" value=|name="csrf" value=|settings_form=|settings_session=|"csrf":)[^";\s]+')
    for text in running.seen:
        text = tokens.sub(r"\1(token)", text)
        assert code not in text and f"{code[:3]} {code[3:]}" not in text
    for record in caplog.records:
        assert code not in record.getMessage() and f"{code[:3]} {code[3:]}" not in record.getMessage()


def test_a_forgotten_password_is_reset_with_the_code_from_the_board(pi_signed, caplog):
    pi = pi_signed
    _, sign_in_page = pi("GET", "/settings/login")
    assert 'action="/settings/forgot"' in sign_in_page and "Forgot password?" in sign_in_page
    old, _ = pi.sign_in()
    form_cookie = ask_for_code(pi)
    _, page = pi("GET", "/settings/reset?asked", cookie=form_cookie)
    assert "Look at the board" in page and "Code from the board" in page
    assert 'name="code"' in page and 'autocomplete="new-password"' in page
    code = board_code(pi)
    response, _ = reset(pi, form_cookie, f"{code[:3]} {code[3:]}")
    assert response.status == 303 and response.getheader("Location") == "/settings"
    new = response.getheader("Set-Cookie")
    assert new.startswith("settings_session=") and "HttpOnly" in new and "SameSite=Strict" in new
    assert pi("GET", "/settings", cookie=new.split(";")[0])[0].status == 200
    assert pi("GET", "/settings", cookie=old)[0].getheader("Location") == "/settings/login"  # every old one ended
    assert pi.site.password.check(NEW_PASSWORD) and not pi.site.password.check(PASSWORD)
    assert not (pi.folder / "reset-display.json").exists()  # used, so off the board
    assert reset(pi, form_cookie, code, "yet another password")[0].status == 401  # and once only
    assert_never_shown(code, pi, caplog)


def test_a_wrong_code_changes_nothing_and_says_no_more_than_that(pi_signed, caplog):
    pi = pi_signed
    form_cookie, _ = pi.form()
    # No code asked for yet: the same answer as for a wrong one.
    response, without = reset(pi, form_cookie, "123456")
    assert response.status == 401 and WRONG_CODE in without
    ask_for_code(pi, form_cookie)
    code = board_code(pi)
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    error = re.compile(r'<p class="error"[^>]*>(.*?)</p>')
    answers = set()
    for _ in range(5):
        response, text = reset(pi, form_cookie, wrong)
        assert response.status == 401 and not response.getheader("Set-Cookie", "").startswith("settings_session=")
        answers.add(error.search(text).group(1))
    response, text = reset(pi, form_cookie, code)  # voided after five wrong
    assert response.status == 401
    answers.add(error.search(text).group(1))
    answers.add(error.search(without).group(1))
    assert len(answers) == 1
    assert 'id="code"' in text and 'aria-invalid="true"' in text
    assert pi.site.password.check(PASSWORD)
    assert_never_shown(code, pi, caplog)


def test_a_typo_in_the_new_password_does_not_use_up_the_code(pi_signed, caplog):
    pi = pi_signed
    form_cookie = ask_for_code(pi)
    code = board_code(pi)
    response, text = reset(pi, form_cookie, code, NEW_PASSWORD, NEW_PASSWORD + "!")
    assert response.status == 422 and "don't match" in text
    response, text = reset(pi, form_cookie, code, "short")
    assert response.status == 422 and "at least 10" in text
    assert reset(pi, form_cookie, code)[0].status == 303
    assert_never_shown(code, pi, caplog)


def test_the_first_claim_needs_a_code_from_the_board(pi, caplog):
    _, page = pi("GET", "/settings/login")
    assert "Claim this board" in page and 'action="/settings/forgot"' in page and 'action="/settings/reset"' in page
    assert "--set-password" in page and "--reset-code" in page
    response, _ = pi("GET", "/settings")
    assert response.getheader("Location") == "/settings/login"
    form_cookie, _ = pi.form()
    assert reset(pi, form_cookie, "000000")[0].status == 401
    assert not pi.site.password.is_set()
    assert pi("POST", "/settings/login", {"password": NEW_PASSWORD})[0].status == 403
    ask_for_code(pi, form_cookie)
    code = board_code(pi)
    response, _ = reset(pi, form_cookie, code)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("settings_session=")
    assert pi.site.password.check(NEW_PASSWORD) and (pi.folder / "web.json").exists()
    _, page = pi("GET", "/settings/login")
    assert "Claim this board" not in page  # claimed: an ordinary sign-in now
    assert_never_shown(code, pi, caplog)


def test_asking_for_a_code_needs_this_browsers_form_token(pi_signed):
    pi = pi_signed
    form_cookie, token = pi.form()
    other_cookie, other_token = pi.form()
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
        response, _ = pi("POST", "/settings/forgot", body, cookie=cookie, origin=origin)
        assert response.status == 403, (body, cookie, origin)
    assert not (pi.folder / "reset-display.json").exists()
    pi.clock.now += 31 * 60  # the token has run out
    assert pi("POST", "/settings/forgot", {"form": token}, cookie=form_cookie)[0].status == 403
    assert not (pi.folder / "reset-display.json").exists()
    form_cookie, token = pi.form()
    assert pi("POST", "/settings/forgot", {"form": token}, cookie=form_cookie)[0].status == 303
    assert (pi.folder / "reset-display.json").exists()


def test_a_reset_needs_the_form_token_too(pi_signed):
    pi = pi_signed
    form_cookie = ask_for_code(pi)
    code = board_code(pi)
    response, _ = pi("POST", "/settings/reset", {"form": "1.bad", "code": code, "password": NEW_PASSWORD,
                                                 "again": NEW_PASSWORD}, cookie=form_cookie)
    assert response.status == 403 and pi.site.password.check(PASSWORD)
    assert reset(pi, form_cookie, code)[0].status == 303  # the code wasn't spent on it


def test_asking_again_too_soon_looks_the_same_and_keeps_the_code(pi_signed):
    form_cookie = ask_for_code(pi_signed)
    code = board_code(pi_signed)
    ask_for_code(pi_signed, form_cookie)  # within the minute: limited, but answered the same
    assert board_code(pi_signed) == code


def test_the_form_cookie_keeps_its_value_so_two_tabs_both_work(pi):
    cookie = pi("GET", "/settings/login")[0].getheader("Set-Cookie")
    again = pi("GET", "/settings/reset", cookie=cookie.split(";")[0])[0].getheader("Set-Cookie")
    assert again.split(";")[0] == cookie.split(";")[0]


# With no board to show it, a code goes to the log (Docker; a Pi with its board view off)


def code_from_the_log(caplog):
    codes = [re.search(r"Settings reset code: (\d{3}) (\d{3})", r.getMessage()) for r in caplog.records]
    found = [m.group(1) + m.group(2) for m in codes if m]
    assert found, "no code in the log"
    return found[-1]


def assert_only_in_the_log(code, running, caplog):
    tokens = re.compile(r'(name="form" value=|name="csrf" value=|settings_form=|settings_session=|"csrf":)[^";\s]+')
    for text in running.seen:
        text = tokens.sub(r"\1(token)", text)
        assert code not in text and f"{code[:3]} {code[3:]}" not in text
    logged = [r for r in caplog.records if code[:3] in r.getMessage() and code[3:] in r.getMessage()]
    assert all(r.getMessage().startswith("Settings reset code: ") for r in logged)


def test_a_forgotten_password_is_reset_with_a_code_from_the_log(signed, caplog):
    old, _ = signed.sign_in()
    form_cookie = ask_for_code(signed)
    _, page = signed("GET", "/settings/reset?asked", cookie=form_cookie)
    assert "docker logs departureboard<" in page and "Container Manager" in page
    assert "Code from the log" in page
    code = code_from_the_log(caplog)
    response, _ = reset(signed, form_cookie, f"{code[:3]} {code[3:]}")
    assert response.status == 303 and response.getheader("Location") == "/settings"
    new = response.getheader("Set-Cookie")
    assert new.startswith("settings_session=") and "Path=/settings;" in new
    assert signed("GET", "/settings", cookie=new.split(";")[0])[0].status == 200
    assert signed("GET", "/settings", cookie=old)[0].status == 303  # every old session ended
    assert signed.site.password.check(NEW_PASSWORD) and not signed.site.password.check(PASSWORD)
    assert reset(signed, form_cookie, code, "yet another password")[0].status == 401  # once only
    assert not (signed.folder / "reset-display.json").exists()  # no board to hand it to
    assert_only_in_the_log(code, signed, caplog)


def test_a_wrong_code_from_the_log_changes_nothing(signed, caplog):
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
    assert signed.site.password.check(PASSWORD)
    assert_only_in_the_log(code, signed, caplog)


def test_the_first_claim_in_docker_needs_a_code_from_the_log(dash, caplog):
    _, page = dash("GET", "/settings/login")
    assert "Claim this dashboard" in page and 'action="/settings/reset"' in page
    assert "docker exec -it departureboard python" in page
    assert dash("POST", "/settings/login", {"password": NEW_PASSWORD})[0].status == 403
    form_cookie, _ = dash.form()
    assert reset(dash, form_cookie, "000000")[0].status == 401
    ask_for_code(dash, form_cookie)
    code = code_from_the_log(caplog)
    response, _ = reset(dash, form_cookie, code)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("settings_session=")
    assert dash.site.password.check(NEW_PASSWORD)
    assert "Claim this dashboard" not in dash("GET", "/settings/login")[1]
    assert_only_in_the_log(code, dash, caplog)


def test_with_the_board_view_off_a_pis_codes_go_to_the_log_and_its_pages_say_so(pi_signed, caplog):
    pi = pi_signed
    form_cookie = ask_for_code(pi)
    assert "Look at the board" in pi("GET", "/settings/reset?asked", cookie=form_cookie)[1]
    assert (pi.folder / "reset-display.json").exists()
    pi.store.settings_file.write_text("[app]\nviews = []\n")  # turned off while it runs
    pi.clock.now += 61
    form_cookie = ask_for_code(pi)
    page = pi("GET", "/settings/reset?asked", cookie=form_cookie)[1]
    assert "Code from the log" in page and "journalctl -u departureboard-web" in page and "Look at the board" not in page
    assert not (pi.folder / "reset-display.json").exists()  # the board's code is void
    code = code_from_the_log(caplog)
    assert reset(pi, form_cookie, code)[0].status == 303
    assert_only_in_the_log(code, pi, caplog)


def test_asking_for_a_code_from_the_log_needs_this_browsers_form_token(signed, caplog):
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


def test_a_password_set_from_the_command_line_signs_every_browser_out(signed, monkeypatch, capsys):
    import getpass

    from departureboard.cli.main import main

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


def test_a_code_from_the_command_line_claims_the_running_server(dash, capsys, caplog):
    from departureboard.cli.main import main

    assert main(["dashboard", "--reset-code", "--state-dir", str(dash.folder)]) == 0
    code = re.search(r"Settings reset code: (\d{3} \d{3})", capsys.readouterr().out).group(1)
    assert code.replace(" ", "") not in caplog.text  # printed, not put in the log
    form_cookie, _ = dash.form()
    response, _ = reset(dash, form_cookie, code)
    assert response.status == 303 and response.getheader("Location") == "/settings"
    assert dash.site.password.check(NEW_PASSWORD)


# Until there is something to show, a new session starts in the setup wizard


def test_until_the_settings_are_set_up_signing_in_opens_the_wizard(make):
    running = make(install="pi", config=None)  # no config.toml and nothing saved
    running.site.password.set(PASSWORD)
    response, _ = running("POST", "/settings/login", {"password": PASSWORD})
    assert response.status == 303 and response.getheader("Location") == "/settings/setup/"
    # The settings themselves are still there, for the wizard's link back.
    cookie = response.getheader("Set-Cookie").split(";")[0]
    assert running("GET", "/settings", cookie=cookie)[0].status == 200
    running.config.write_text(CONFIG)
    response, _ = running("POST", "/settings/login", {"password": PASSWORD})
    assert response.getheader("Location") == "/settings"


def test_claiming_settings_not_yet_set_up_opens_the_wizard(make):
    running = make(install="pi", config=None)
    form_cookie = ask_for_code(running)
    response, _ = reset(running, form_cookie, board_code(running))
    assert response.status == 303 and response.getheader("Location") == "/settings/setup/"
    assert response.getheader("Set-Cookie").startswith("settings_session=")


def test_a_fault_telling_whether_it_is_set_up_opens_the_settings(pi_signed, monkeypatch):
    def broken(store):
        raise PermissionError("config.toml")

    monkeypatch.setattr(settingsite, "needs_setup", broken)
    response, _ = pi_signed("POST", "/settings/login", {"password": PASSWORD})
    assert response.status == 303 and response.getheader("Location") == "/settings"


# The setup wizard


def step(running, name, body=b"{}", cookie=None, csrf=None, origin="same", content_type="application/json",
         length=None, extra=None):
    """POST a wizard step as the page does: JSON, with the form token in a header."""
    pairs = []
    if cookie:
        pairs.append(("Cookie", cookie))
    if csrf is not None:
        pairs.append(("X-CSRF-Token", csrf))
    if content_type:
        pairs.append(("Content-Type", content_type))
    if length != "none":
        pairs.append(("Content-Length", str(len(body) if length is None else length)))
    pairs += list((extra or {}).items())
    return framed_post(running, f"/settings/setup/api/{name}", pairs, body, origin=origin)


def test_the_wizard_page_and_its_api_need_signing_in_and_its_files_do_not(pi):
    response, _ = pi("GET", "/settings/setup/")
    assert response.status == 303 and response.getheader("Location") == "/settings/login?next=setup"
    response, text = pi("GET", "/settings/setup/api/session")
    assert response.status == 401 and "csrf" not in json.loads(text)
    assert response.getheader("Content-Type") == "application/json"
    response, _ = step(pi, "begin", csrf="anything")
    assert response.status == 401
    response, _ = step(pi, "begin", cookie="settings_session=guessed", csrf="anything")
    assert response.status == 401 and pi.setup.calls == []
    # Its scripts and styles are public, as the dashboard's are; they hold nothing of this install's.
    assert pi("GET", "/settings/setup/wizard_static/wizard.js")[0].status == 200


def test_the_wizard_page_and_its_files_are_served_from_an_allowlist(pi):
    cookie = pi.session()
    response, page = pi("GET", "/settings/setup/", cookie=cookie)
    assert response.status == 200 and "wizard_static/wizard.js" in page
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8", ".woff2": "font/woff2"}
    for relative, path in static_files().items():
        conn = http.client.HTTPConnection("127.0.0.1", pi.port, timeout=10)
        conn.request("GET", f"/settings/setup/{relative}", headers={"Host": f"localhost:{pi.port}"})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        assert response.status == (303 if relative == "" else 200), relative
        if relative:
            assert response.getheader("Content-Type") == types[path.suffix], relative
            assert body == path.read_bytes() and response.getheader("X-Content-Type-Options") == "nosniff"
    assert pi("GET", "/settings/setup", cookie=cookie)[0].getheader("Location") == "/settings/setup/"
    for path in [
        "/settings/setup/index.html",  # the page is served at the prefix only
        "/settings/setup/wizard_static/",
        "/settings/setup/wizard_static/index.html",
        "/settings/setup/wizard_static/nothing.js",
        "/settings/setup/wizard_static/wizard.js/",
        "/settings/setup/dashboard_static/main.js",  # the dashboard's own, not the wizard's
        "/settings/setup/dashboard_static/app.js",
        "/settings/setup/dashboard_static/vendor/leaflet-1.9.4/LICENSE",
        "/settings/setup/dashboard_static/vendor/leaflet-1.9.4/images/marker-icon.png",
        "/settings/setup/../web.json",
        "/settings/setup/%2e%2e/web.json",
        "/settings/setup/wizard_static/../setupapi.py",
        "/settings/setup/setupapi.py",
        "/settings/web.json",
        "/settings/reset.json",
        "/settings/setup/api/begin",  # steps are POST only
    ]:
        assert pi("GET", path, cookie=cookie)[0].status == 404, path


def test_a_file_of_an_unknown_kind_is_left_out_and_the_log_says_so(tmp_path, caplog):
    (tmp_path / "picture.svg").write_text("<svg/>")
    (tmp_path / "page.html").write_text("<p>")
    found = settingsite.wizard_files({"": tmp_path / "page.html", "picture.svg": tmp_path / "picture.svg"})
    assert set(found) == {""} and found[""] == (b"<p>", "text/html; charset=utf-8")
    assert "picture.svg left out" in caplog.text


def test_the_wizard_api_needs_the_session_and_its_csrf_token(pi):
    cookie = pi.session()
    csrf = pi.csrf(cookie)
    other = pi.csrf(pi.session())
    token = {"X-CSRF-Token": csrf}
    attempts = [
        (None, token, "same", 401),  # no session
        (cookie, {}, "same", 403),  # no token
        (cookie, {"X-CSRF-Token": "forged"}, "same", 403),
        (cookie, {"X-CSRF-Token": ""}, "same", 403),
        (cookie, {"X-CSRF-Token": other}, "same", 403),  # another session's
        (cookie, {"X-CSRF-Token": csrf + "x"}, "same", 403),
        (cookie, token, "http://evil.example.com", 403),
        (cookie, token, "null", 403),
        (cookie, token, None, 403),
        (cookie, {**token, "Sec-Fetch-Site": "cross-site"}, "same", 403),
    ]
    for sent_cookie, headers, origin, status in attempts:
        response, text = pi("POST", "/settings/setup/api/begin", cookie=sent_cookie, origin=origin,
                            headers=headers, json_body={})
        assert response.status == status, (sent_cookie, headers, origin)
        assert origin != "same" or "error" in json.loads(text)
    assert pi.setup.calls == []
    response, text = pi("POST", "/settings/setup/api/begin", cookie=cookie, headers=token, json_body={"x": [1, 2]})
    assert response.status == 200 and json.loads(text) == {"step": "begin"}
    assert response.getheader("Content-Type") == "application/json"
    assert pi.setup.calls == [("begin", {"x": [1, 2]})]


def test_todays_choices_for_the_wizards_shortcuts_need_the_session_and_its_token_too(pi):
    cookie = pi.session()
    csrf = pi.csrf(cookie)
    for sent_cookie, headers, origin, status in [
        (None, {"X-CSRF-Token": csrf}, "same", 401),
        (cookie, {}, "same", 403),
        (cookie, {"X-CSRF-Token": pi.csrf(pi.session())}, "same", 403),
        (cookie, {"X-CSRF-Token": csrf}, "http://evil.example.com", 403),
    ]:
        response, _ = pi("POST", "/settings/setup/api/saved", cookie=sent_cookie, origin=origin, headers=headers,
                         json_body={})
        assert response.status == status, (sent_cookie, headers, origin)
    assert pi("GET", "/settings/setup/api/saved", cookie=cookie)[0].status != 200  # steps are POST only
    assert pi.setup.calls == []
    pi("POST", "/settings/setup/api/saved", cookie=cookie, headers={"X-CSRF-Token": csrf}, json_body={})
    assert pi.setup.calls == [("saved", {})]


def test_the_wizard_api_refuses_another_sites_page_outright(pi):
    cookie = pi.session()
    csrf = pi.csrf(cookie)
    cross = {"Sec-Fetch-Site": "cross-site"}
    for method, path in (("GET", "/settings/setup/api/session"), ("GET", "/settings/setup/api/begin"),
                         ("HEAD", "/settings/setup/api/session"), ("PUT", "/settings/setup/api/begin")):
        response, text = pi(method, path, cookie=cookie, headers=cross)
        assert response.status == 403, (method, path)
        assert csp(response) == PAGE_HEADERS["Content-Security-Policy"]
        assert method == "HEAD" or (json.loads(text) == {"error": "Not from another site"} and csrf not in text)
    response, text = step(pi, "begin", cookie=cookie, csrf=csrf, extra={"Sec-Fetch-Site": "Cross-Site "})
    assert response.status == 403 and json.loads(text) == {"error": "Not from another site"}
    assert pi.setup.calls == []
    for same in ("same-origin", "none"):  # the page's own fetches, and a typed-in address
        assert pi("GET", "/settings/setup/api/session", cookie=cookie, headers={"Sec-Fetch-Site": same})[0].status == 200


def test_other_methods_on_the_wizard_get_its_headers(pi):
    cookie = pi.session()
    for path in ("/settings/setup/", "/settings/setup/api/begin"):
        response, _ = pi("PUT", path, cookie=cookie)
        assert response.status == 405 and response.getheader("Allow") == "GET, HEAD, POST", path
        assert csp(response) == PAGE_HEADERS["Content-Security-Policy"], path
    assert pi.setup.calls == []


@pytest.mark.parametrize("change, status", [
    ({"content_type": "application/x-www-form-urlencoded", "body": b"a=1"}, 415),
    ({"content_type": "text/plain"}, 415),
    ({"content_type": None}, 415),
    ({"body": b"{" + b" " * 20000 + b"}"}, 413),
    ({"length": 10**7}, 413),
    ({"length": -1}, 411),
    ({"length": "none"}, 411),
    ({"body": b"not json"}, 400),
    ({"body": b"[" * 50 + b"]" * 50}, 400),  # nested too deep
    ({"body": b'{"n": NaN}'}, 400),
])
def test_a_step_must_be_json_of_a_sensible_size(pi, change, status):
    cookie = pi.session()
    response, text = step(pi, "begin", cookie=cookie, csrf=pi.csrf(cookie), **change)
    assert response.status == status and "error" in json.loads(text)
    assert pi.setup.calls == []


def test_a_step_that_isnt_json_says_so(pi):
    cookie = pi.session()
    response, text = pi("POST", "/settings/setup/api/begin", cookie=cookie,
                        headers={"X-CSRF-Token": pi.csrf(cookie), "Content-Type": "application/json"})
    assert response.status == 400 and json.loads(text) == {"error": "That request isn't JSON"}


@pytest.mark.parametrize("pairs, status", BAD_FRAMING)
def test_a_step_framed_any_way_but_one_plain_length_is_refused_unread(pi, pairs, status):
    cookie = pi.session()
    response, text = framed_post(pi, "/settings/setup/api/begin",
                                 [("Cookie", cookie), ("Content-Type", "application/json"),
                                  ("X-CSRF-Token", pi.csrf(cookie)), *pairs])
    assert response.status == status and response.getheader("Connection") == "close"
    assert csp(response) == PAGE_HEADERS["Content-Security-Policy"]
    assert "<html" not in text.lower() and pi.setup.calls == []


@pytest.mark.parametrize("name", ["Begin", "be-gin", "begin/x", "begin%20x", "a" * 21, "", "b%C3%A9gin"])
def test_a_step_name_is_plain_lowercase_checked_before_the_body(pi, name):
    cookie = pi.session()
    csrf = pi.csrf(cookie)
    response, text = step(pi, name, cookie=cookie, csrf=csrf)
    assert response.status == 404 and json.loads(text) == {"error": "No such setup step"}
    response, _ = framed_post(pi, f"/settings/setup/api/{name}",
                              [("Cookie", cookie), ("X-CSRF-Token", csrf), ("Content-Type", "application/json")])
    assert response.status == 404  # not 411: the name is checked before the body
    assert pi.setup.calls == []


def test_an_unknown_step_is_the_apis_to_refuse(pi):
    cookie = pi.session()
    response, _ = step(pi, "nosuch", cookie=cookie, csrf=pi.csrf(cookie))
    assert response.status == 404 and pi.setup.calls == [("nosuch", {})]


def test_a_reply_that_is_not_strict_json_is_never_sent(pi):
    # A plain 500 rather than NaN on the wire (or a dropped connection).
    cookie = pi.session()
    response, body = step(pi, "nan", cookie=cookie, csrf=pi.csrf(cookie))
    assert response.status == 500 and "nan" not in body.lower()
    assert pi.setup.calls == [("nan", {})]


# Headers


def test_settings_pages_allow_their_own_forms_and_no_scripts(make):
    for running in (make(install="docker", dashboard=True), make(install="pi")):
        cookie = running.session()
        for path in ["/settings/login", "/settings/reset", "/settings", "/settings/keys", "/settings/style.css",
                     "/settings/trains-and-buses?saved", "/settings/nowhere",
                     "/login", "/setup/"]:
            response, _ = running("GET", path, cookie=cookie)
            policy = csp(response)
            assert "form-action 'self'" in policy and "script-src" not in policy and "default-src 'none'" in policy
            assert "frame-ancestors 'none'" in policy and response.getheader("X-Frame-Options") == "DENY"
            assert response.getheader("X-Content-Type-Options") == "nosniff"
            assert response.getheader("Cache-Control") == "no-store"
            assert response.getheader("Referrer-Policy") == "same-origin"  # no-referrer would send Origin: null
            assert response.getheader("Server") == "departureboard"
            assert len(response.getheaders()) == len({k.lower() for k, _ in response.getheaders()}), path


def test_only_the_wizard_may_run_scripts(pi):
    cookie = pi.session()
    for path in ["/settings/setup/", "/settings/setup/wizard_static/wizard.js", "/settings/setup/api/session",
                 "/settings/setup/nothing"]:
        response, _ = pi("GET", path, cookie=cookie)
        policy = csp(response)
        assert "script-src 'self'" in policy and "form-action 'none'" in policy, path
        assert "https://tile.openstreetmap.org" in policy
        for name, value in PAGE_HEADERS.items():
            if name != "Cache-Control":
                assert response.getheader(name) == value, (path, name)
        assert response.getheader("Cache-Control") == "no-store"
    response, _ = step(pi, "begin", cookie=cookie, csrf=pi.csrf(cookie))
    assert csp(response) == PAGE_HEADERS["Content-Security-Policy"]


def test_the_read_only_dashboard_keeps_its_own_headers(signed):
    for path in ["/", "/api/snapshot", "/healthz", "/settingsx", "/api/settings"]:
        response, _ = signed("GET", path)
        assert csp(response) == DASHBOARD_CSP, path
    response, _ = signed("POST", "/api/snapshot", {"a": "1"})
    assert response.status == 405 and response.getheader("Allow") == "GET, HEAD"


def raw_exchange(running, data):
    """Send `data` as it is and return everything the server says back."""
    with socket.create_connection(("127.0.0.1", running.port), timeout=5) as raw:
        raw.sendall(data)
        reply = b""
        while chunk := raw.recv(4096):
            reply += chunk
    return reply.decode("latin-1")


@pytest.mark.parametrize("data, status", [
    (b"GET /settings HTTP/1.0\r\n" + b"X: y\r\n" * 101 + b"\r\n", "431"),  # too many headers
    (b"GET /" + b"a" * 70000 + b" HTTP/1.0\r\n\r\n", "414"),
], ids=["too-many-headers", "path-too-long"])
def test_http_servers_own_errors_carry_the_headers_and_no_html(pi, data, status):
    reply = raw_exchange(pi, data)
    head = reply.split("\r\n\r\n", 1)[0]
    assert head.startswith(f"HTTP/1.0 {status}")
    assert "Content-Security-Policy: default-src 'none'" in head and "X-Frame-Options: DENY" in head
    assert "script-src" not in head and "Server: departureboard\r\n" in head
    assert "<html" not in reply.lower() and "python" not in reply.lower()


@pytest.mark.parametrize("data, phrase", [
    (b"GET /settings HTTP/9.9\r\n\r\n", "HTTP Version Not Supported"),
    (b"GET\r\n\r\n", "Bad Request"),
], ids=["unknown-version", "bad-request-line"])
def test_a_broken_request_line_gets_our_headers_on_every_python(pi, data, phrase):
    # http.server would answer these HTTP/0.9 style (a bare phrase) on Python
    # 3.12 but with headers on 3.13; the server answers the same way on both.
    head, body = raw_exchange(pi, data).split("\r\n\r\n", 1)
    assert head.startswith("HTTP/1.0 ") and body == phrase
    assert "Content-Security-Policy: default-src 'none'" in head and "X-Frame-Options: DENY" in head


# Who may ask


def test_outsiders_are_refused_before_the_settings(make):
    running = make(install="pi", hosts=frozenset({"departures.example.com"}))
    running.site.password.set(PASSWORD)
    for host in ("evil.example.com", "8.8.8.8", "board.local.evil.com"):
        assert running("GET", "/settings/login", host=host)[0].status == 403, host
        assert running("POST", "/settings/login", {"password": PASSWORD}, host=host)[0].status == 403, host
        assert running("GET", "/settings/setup/api/session", host=host)[0].status == 403, host
    response, _ = running("GET", "/settings/login", host="evil.example.com")
    assert "script-src" not in csp(response) and response.getheader("X-Frame-Options") == "DENY"
    assert running("GET", "/settings/login", host="departures.example.com")[0].status == 200  # allowed_hosts
    assert running("GET", "/settings/login", host="raspberrypi.local:8080")[0].status == 200


def test_slow_clients_are_dropped():
    from departureboard.web.server import _Handler

    assert _Handler.timeout and _Handler.timeout <= 10  # a client that stalls lets go of its thread


def test_a_client_hanging_up_early_is_not_reported(capsys, caplog):
    from departureboard.web.server import DashboardServer

    server = DashboardServer.__new__(DashboardServer)
    try:
        raise BrokenPipeError(32, "Broken pipe")
    except BrokenPipeError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == "" and "Broken" not in caplog.text


def test_one_host_header_only(pi):
    conn = http.client.HTTPConnection("127.0.0.1", pi.port, timeout=5)
    conn.putrequest("GET", "/settings/login", skip_host=True)
    conn.putheader("Host", "localhost")
    conn.putheader("Host", "evil.example.com")
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()


# Where things are


@pytest.mark.parametrize("old, new", [
    ("/login", "/settings/login"),
    ("/login?next=setup", "/settings/login"),
    ("/reset", "/settings/reset"),
    ("/setup", "/settings/setup/"),
    ("/setup/", "/settings/setup/"),
    ("/setup/wizard_static/wizard.js", "/settings/setup/"),
])
def test_the_board_pages_old_addresses_go_to_their_new_ones(pi, old, new):
    response, _ = pi("GET", old)
    assert response.status == 303 and response.getheader("Location") == new


def test_settings_with_a_slash_goes_to_settings(pi):
    response, _ = pi("GET", "/settings/")
    assert response.status == 303 and response.getheader("Location") == "/settings"


def test_the_front_page_is_the_dashboard_while_it_runs_else_the_settings(make):
    pi = make(install="pi")
    response, _ = pi("GET", "/")
    assert response.status == 303 and response.getheader("Location") == "/settings"
    dash = make(install="docker", dashboard=True)
    response, text = dash("GET", "/")
    assert response.status == 200 and "Front end not built" in text
    dash.on["now"] = False  # its view turned off, or it couldn't start
    assert dash("GET", "/")[0].getheader("Location") == "/settings"


def test_without_the_dashboard_its_api_is_not_found_and_the_health_check_is_ok(pi):
    for path in ("/api/snapshot", "/api/events", "/api/area", "/static/app.css", "/manifest.webmanifest"):
        assert pi("GET", path)[0].status == 404, path
    response, text = pi("GET", "/healthz")
    assert response.status == 200 and text == "ok"


# The look: the dashboard's colours, Inter and icons


WEB_CSS = ROOT / "src" / "departureboard" / "web" / "web.css"
DASHBOARD_STATIC = ROOT / "src" / "departureboard" / "dashboard_static"


def _palette(path, name):
    css = path.read_text(encoding="utf-8")
    match = re.search(r"/\* theme: %s \*/[^{]*\{(?:[^{}]*\{)?([^{}]*)\}" % name, css)
    assert match, name
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", match.group(1)))


def _palettes():
    light, dark = (_palette(WEB_CSS, name) for name in ("light", "dark"))
    return {"light": light, "dark": {**light, **dark}}


def _contrast(a, b):
    def luminance(colour):
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_colours_are_the_dashboards():
    dashboard = {theme: _palette(DASHBOARD_STATIC / "app.css", f"modern-{theme}") for theme in ("light", "dark")}
    for theme, tokens in _palettes().items():
        assert tokens == {name: dashboard[theme][name] for name in tokens}, theme


TEXT_PAIRS = [
    ("fg", "bg"), ("fg", "surface"), ("muted", "surface"), ("muted", "bg"), ("accent", "surface"), ("accent", "bg"),
    ("accent-fg", "accent"), ("ok-fg", "surface"), ("bad-fg", "surface"), ("bad-fg", "bg"),
    # The problem summary and a refused key on their red tint, a key that can't be told on amber.
    ("fg", "bad-bg"), ("bad-fg", "bad-bg"), ("late-fg", "late-bg"),
    # The neutral pill on its tint.
    ("fg", "surface-2"),
]
OUTLINE_PAIRS = [("focus", "bg"), ("focus", "surface"), ("edge", "surface"), ("edge", "bg"), ("bad-fg", "surface")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_theme_contrast(theme):
    tokens = _palettes()[theme]
    for pairs, need in ((TEXT_PAIRS, 4.5), (OUTLINE_PAIRS, 3.0)):
        for fg, bg in pairs:
            ratio = _contrast(tokens[fg], tokens[bg])
            assert ratio >= need, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1"


def test_the_stylesheet_takes_inter_from_the_wizards_files():
    # One copy, the dashboard's, served with the wizard's files and without a session.
    css = WEB_CSS.read_text(encoding="utf-8")
    [src] = re.findall(r'src: url\("([^"]+)"\) format\("woff2"\)', css)
    assert src.startswith(settingsite.SETUP)
    served = static_files()[src.removeprefix(settingsite.SETUP)]
    assert served.resolve() == (DASHBOARD_STATIC / "vendor" / "inter-4.1" / "InterVariable.woff2").resolve()
    assert re.search(r'--font: "Inter", system-ui', css)
    assert "font-src 'self'" in settingsite.SETTINGS_HEADERS["Content-Security-Policy"]
    assert "font-src 'self'" in settingsite.WIZARD_HEADERS["Content-Security-Policy"]


def test_buttons_are_quiet_but_each_forms_main_action(make):
    running = make(install="pi")
    css = WEB_CSS.read_text(encoding="utf-8")
    assert re.search(r"\nbutton \{[^}]*background: transparent", css)
    assert re.search(r"button\.primary \{[^}]*background: var\(--accent\)", css)
    page = running("GET", "/settings/general", cookie=running.session())[1]
    assert re.findall(r'<button type="submit" class="primary"[^>]*>([^<]*)</button>', page) == ["Save changes"]
    assert '<button type="submit">Sign out</button>' in page
    for path in ("/settings/login", "/settings/reset"):
        page = running("GET", path)[1]
        assert len(re.findall(r'<button type="submit" class="primary">', page)) == 1, path
        assert re.search(r'<button type="submit">[^<]+</button>', page), path  # asking for a code
