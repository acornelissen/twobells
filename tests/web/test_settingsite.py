"""The settings site, end to end through the one server (dashserver), on a
Pi (codes on the board) and in Docker (codes in the log)."""

import http.client
import json
import re
import socket
import threading
import time
import tomllib
from urllib.parse import urlencode

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
    values = dict(re.findall(r'<input id="(\w+)" name="\w+" type="text" value="([^"]*)"', page))
    values |= dict(re.findall(r'<textarea id="(\w+)"[^>]*>([^<]*)</textarea>', page))
    for name, options in re.findall(r'<select id="(\w+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
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
    assert 'name="next" value="setup"' in page
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "setup"})
    assert response.getheader("Location") == "/settings/setup/"
    response, _ = signed("POST", "/settings/login", {"password": PASSWORD, "next": "https://evil.example.com/"})
    assert response.getheader("Location") == "/settings"


def test_sign_out_ends_the_session(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings/logout", {"csrf": csrf}, cookie=cookie)
    assert response.status == 303 and response.getheader("Set-Cookie").startswith("settings_session=;")
    assert signed("GET", "/settings", cookie=cookie)[0].getheader("Location") == "/settings/login"


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


# The tabs


def test_the_tabs_are_a_labelled_list_of_links_with_the_current_one_marked(signed):
    cookie, _ = signed.sign_in()
    for path, name in (("/settings", "General"), ("/settings/dashboard", "Dashboard")):
        response, page = signed("GET", path, cookie=cookie)
        assert response.status == 200, path
        nav = re.search(r'<nav aria-label="Settings"[^>]*><ul>(.*?)</ul></nav>', page).group(1)
        links = re.findall(r'<li><a href="([^"]+)"( aria-current="page")?>([^<]+)</a></li>', nav)
        assert [(href, label) for href, _, label in links] == [("/settings", "General"),
                                                               ("/settings/dashboard", "Dashboard")]
        assert [label for _, current, label in links if current] == [name]
        assert f"<title>{name} settings</title>" in page and f"<h1>{name} settings</h1>" in page
        assert page.index("<h1>") < page.index('<nav aria-label="Settings"') < page.index(f'action="{path}" novalidate')
        assert '<a href="/">Back to the dashboard</a>' in page


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
    page = pi("GET", "/settings", cookie=cookie)[1]
    assert 'href="/settings/board"' not in page and 'aria-current="page">General</a>' in page


def test_each_tab_shows_its_own_group(make):
    pi = make(install="pi")
    pi.store.make()
    cookie = pi.session()
    general = pi("GET", "/settings", cookie=cookie)[1]
    board = pi("GET", "/settings/board", cookie=cookie)[1]
    assert 'id="train_crs"' in general and 'id="RDM_API_KEY"' in general and 'id="theme"' not in general
    assert 'id="theme"' in board and 'id="TAPI_APP_KEY"' in board and 'id="train_crs"' not in board
    assert '<h2 id="status-title">Status</h2>' in board and "status-title" not in general
    assert board.index("<h1>") < board.index("Status</h2>") < board.index("<fieldset>")
    assert 'action="/settings/board"' in board and 'action="/settings"' in general


def test_the_general_tab_sums_up_the_place_and_links_to_setup(pi):
    cookie = pi.session()
    page = pi("GET", "/settings", cookie=cookie)[1]
    place = re.search(r'<section class="place"[^>]*>(.*?)</section>', page).group(1)
    assert '<h2 id="place-title">Your place</h2>' in place
    assert "<dt>Station</dt><dd>GLO</dd>" in place and "<dt>Bus stops</dt><dd>A</dd>" in place
    assert "<dt>Home</dt><dd>Not set</dd>" in place
    assert '<a href="/settings/setup/">Change with setup</a>' in place
    assert page.index("Your place") < page.index('<form method="post" action="/settings"')


def _place(page):
    return re.search(r'<section class="place"[^>]*>(.*?)</section>', page).group(1)


def test_the_general_tab_says_when_the_bus_timetable_was_saved_and_how_its_roads_are_getting_on(make):
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from departureboard.timetable import build as timetable

    docker = make(install="docker")  # whose dashboard starts again on saving: this page says how far they got
    cookie = docker.session()
    place = _place(docker("GET", "/settings", cookie=cookie)[1])
    assert "<dt>Bus timetable</dt><dd>Not built yet</dd>" in place and "Bus map roads" not in place
    saved = docker.folder / timetable.FILE_NAME
    saved.write_text("{}")  # only when it was saved is read
    when = time.time() - 180
    os.utime(saved, (when, when))
    clock = datetime.fromtimestamp(when, ZoneInfo("Europe/London"))
    place = _place(docker("GET", "/settings", cookie=cookie)[1])
    assert f"<dt>Bus timetable</dt><dd>Saved {clock:%H:%M}, 3 min ago</dd>" in place
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
def test_the_general_tab_says_nothing_of_a_bus_timetable_it_doesnt_use(make, config):
    docker = make(install="docker", config=config)
    assert "Bus timetable" not in docker("GET", "/settings", cookie=docker.session())[1]


def test_the_general_tab_says_which_version_is_running(pi):
    cookie = pi.session()
    general = pi("GET", "/settings", cookie=cookie)[1]
    wanted = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f'<p class="hint about">Running departureboard {wanted}.</p>' in general
    assert general.index("Your place") < general.index("Running departureboard") < general.index('<form method="post" action="/settings" novalidate')
    assert "Running departureboard" not in pi("GET", "/settings/board", cookie=cookie)[1]


def test_the_general_tab_says_plainly_when_the_version_is_unknown(pi):
    cookie = pi.session()
    pi.site.version = "unknown"
    assert '<p class="hint about">Running departureboard, version unknown.</p>' in pi("GET", "/settings", cookie=cookie)[1]
    pi.site.version = "<b>1</b>"
    assert "Running departureboard &lt;b&gt;1&lt;/b&gt;." in pi("GET", "/settings", cookie=cookie)[1]


def test_the_general_tab_says_why_the_dashboard_isnt_running(dash):
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


# Saving


def test_a_signed_in_save_with_the_form_token_works(signed):
    cookie, csrf = signed.sign_in()
    fields = fields_from(signed("GET", "/settings", cookie=cookie)[1])
    response, text = signed("POST", "/settings", fields | {"csrf": csrf, "title": "Our house"}, cookie=cookie)
    assert response.status == 200 and "Saved. The dashboard starts again" in text
    assert "<title>Saved – General settings</title>" in text
    assert signed.saved()["title"] == "Our house"


def test_a_save_on_the_pi_says_when_the_board_shows_it(pi_signed):
    cookie, csrf = pi_signed.sign_in("/settings/board")
    fields = fields_from(pi_signed("GET", "/settings/board", cookie=cookie)[1])
    response, text = pi_signed("POST", "/settings/board", fields | {"csrf": csrf, "board_train_rows": "2"},
                               cookie=cookie)
    assert response.status == 200, text
    assert "<title>Saved – Board settings</title>" in text
    assert "Saved. The board shows it within a few minutes; the screen within seconds." in text
    assert pi_signed.saved()["train"]["rows"] == 2


def test_save_errors_come_back_on_the_form(signed):
    cookie, csrf = signed.sign_in()
    fields = fields_from(signed("GET", "/settings", cookie=cookie)[1])
    response, text = signed("POST", "/settings", fields | {"csrf": csrf, "train_crs": "Glossop"}, cookie=cookie)
    assert response.status == 422 and "Not saved: fix 1 thing" in text and 'href="#train_crs"' in text
    assert "<title>General settings: fix the errors</title>" in text
    assert not (signed.folder / "settings.toml").exists()


@pytest.mark.parametrize("path", ["/settings", "/settings/dashboard"])
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
    for path in ("/settings", "/settings/dashboard"):
        response, _ = signed("POST", path, {"csrf": "x", "title": "x"})
        assert response.status == 303 and response.getheader("Location") == "/settings/login", path


def test_a_save_on_one_tab_leaves_the_others_settings_and_keys_alone(signed):
    signed.store.settings_file.write_text('title = "Our house"\ntheme = "plain"\n[dashboard]\nhistory_days = 30\n')
    signed.store.secrets_file.write_text("TAPI_APP_ID=kept\n")
    cookie, csrf = signed.sign_in("/settings/dashboard")
    fields = fields_from(signed("GET", "/settings/dashboard", cookie=cookie)[1])
    assert "title" not in fields and fields["history_days"] == "30"
    changed = fields | {"csrf": csrf, "dashboard_train_rows": "6", "RDM_DETAILS_API_KEY": "details-key-0001"}
    response, text = signed("POST", "/settings/dashboard", changed, cookie=cookie)
    assert response.status == 200, text
    saved = signed.saved()
    assert saved["title"] == "Our house" and saved["theme"] == "plain"
    assert saved["dashboard"]["train_rows"] == 6 and saved["dashboard"]["history_days"] == 30
    assert signed.store.secrets_file.read_text() == "TAPI_APP_ID=kept\nRDM_DETAILS_API_KEY=details-key-0001\n"
    assert signed.config.read_text() == CONFIG
    assert "details-key-0001" not in "".join(signed.seen)


def test_the_forms_save_into_the_store_and_never_show_a_key(signed):
    cookie, csrf = signed.sign_in()
    _, page = signed("GET", "/settings", cookie=cookie)
    assert "Currently set." in page and "rail-key-1234567" not in page and "bods-key-7654321" not in page
    fields = fields_from(page)
    assert fields["train_crs"] == "GLO" and fields["bus_stops"] == "A = 1000DGHS0900"
    response, text = signed("POST", "/settings", fields | {"csrf": csrf, "train_crs": "man"}, cookie=cookie)
    assert response.status == 200 and "Saved." in text, text
    assert signed.saved()["train"]["crs"] == "MAN"
    _, page = signed("GET", "/settings/dashboard", cookie=cookie)
    assert fields_from(page)["dashboard_train_rows"] == "10"


def test_the_dashboard_tab_says_what_is_wrong_and_saves_nothing(signed):
    cookie, csrf = signed.sign_in("/settings/dashboard")
    fields = fields_from(signed("GET", "/settings/dashboard", cookie=cookie)[1])
    fields.update(csrf=csrf, dashboard_train_rows="50", kb_incidents_url="http://x")
    response, text = signed("POST", "/settings/dashboard", fields, cookie=cookie)
    assert response.status == 422 and "Not saved: fix 2 things" in text
    assert 'href="#dashboard_train_rows"' in text and 'aria-invalid="true"' in text
    assert not (signed.folder / "settings.toml").exists()


def test_the_dashboard_tab_says_when_its_buses_cant_be_shown(make):
    running = make(install="docker", config=CONFIG.replace('source = "bods"', 'source = "transportapi"'))
    page = running("GET", "/settings/dashboard", cookie=running.session())[1]
    assert "only from the free BODS timetable" in page


def test_bodies_must_be_plain_small_forms(signed):
    cookie, csrf = signed.sign_in()
    response, _ = signed("POST", "/settings", cookie=cookie, json_body={"csrf": csrf})
    assert response.status == 400
    response, _ = signed("POST", "/settings", {"csrf": csrf, "title": "x" * 70_000}, cookie=cookie)
    assert response.status == 413
    response, _ = signed("POST", "/settings", None, cookie=cookie, headers={"Transfer-Encoding": "chunked"})
    assert response.status == 400
    assert not (signed.folder / "settings.toml").exists()


def test_a_form_with_too_many_fields_is_refused_not_dropped(dash):
    response, _ = dash("POST", "/settings/login", {f"f{n}": "1" for n in range(301)})
    assert response.status == 413


# Testing a key, on its tab


def press_test(running, path, name, typed=None):
    """Press a tab's test button for key `name`, with `typed` in its boxes."""
    cookie = running.session()
    fields = fields_from(running("GET", path, cookie=cookie)[1])
    return running("POST", path, fields | (typed or {}) | {"csrf": running.csrf(cookie), "test": name}, cookie=cookie)


def test_each_tab_has_a_test_button_for_each_of_its_keys(make):
    pi, docker = make(install="pi"), make(install="docker")
    expected = {
        (pi, "/settings"): ["RDM_API_KEY", "BODS_API_KEY"],
        (pi, "/settings/board"): ["TAPI_APP_KEY"],  # the app ID is tested with it
        (docker, "/settings"): ["RDM_API_KEY", "BODS_API_KEY"],
        (docker, "/settings/dashboard"): ["RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"],
    }
    for (running, path), names in expected.items():
        page = running("GET", path, cookie=running.session())[1]
        assert re.findall(r'<button type="submit" name="test" value="(\w+)"', page) == names, path
        # Save comes first, so Enter in any box saves rather than tests.
        assert page.index(">Save settings</button>") < page.index('name="test"'), path
        assert '<fieldset class="key-tests"><legend>Test a key</legend>' in page
    board = pi("GET", "/settings/board", cookie=pi.session())[1]
    assert ">Test the TransportAPI app ID and key</button>" in board
    assert re.search(r'<p class="hint" id="TAPI_APP_KEY-cost">Uses 1 of today\'s 30 TransportAPI lookups', board)
    assert 'value="TAPI_APP_KEY" class="quiet" aria-describedby="key-tests-hint TAPI_APP_KEY-cost"' in board


def test_testing_a_key_needs_signing_in(signed):
    response, _ = signed("POST", "/settings/dashboard", {"csrf": "x", "test": "RDM_KB_API_KEY"})
    assert response.status == 303 and response.getheader("Location") == "/settings/login"
    assert signed.setup.tests == []


@pytest.mark.parametrize("csrf, origin", [
    ("forged", "same"),
    (None, "same"),
    ("right", "http://evil.example.com"),
    ("right", "null"),
    ("right", None),
])
def test_testing_a_key_needs_the_same_origin_and_the_form_token(signed, csrf, origin):
    cookie = signed.session()
    right = signed.csrf(cookie)
    body = {"test": "RDM_KB_API_KEY", "RDM_KB_API_KEY": "typed-kb-key-1"}
    if csrf is not None:
        body["csrf"] = right if csrf == "right" else csrf
    response, _ = signed("POST", "/settings/dashboard", body, cookie=cookie, origin=origin)
    assert response.status == 403 and signed.setup.tests == []


def test_a_key_test_is_a_plain_small_form_like_a_save(signed):
    cookie = signed.session()
    csrf = signed.csrf(cookie)
    response, _ = signed("POST", "/settings", cookie=cookie, json_body={"csrf": csrf, "test": "RDM_API_KEY"})
    assert response.status == 400
    response, _ = signed("POST", "/settings", {"csrf": csrf, "test": "RDM_API_KEY", "title": "x" * 70_000},
                         cookie=cookie)
    assert response.status == 413 and signed.setup.tests == []


def test_a_key_is_tested_as_typed_else_as_saved_and_nothing_is_saved(signed):
    press_test(signed, "/settings/dashboard", "RDM_DETAILS_API_KEY", {"RDM_DETAILS_API_KEY": " typed-details-key-1 "})
    press_test(signed, "/settings/dashboard", "RDM_KB_API_KEY")  # its box empty: the saved one
    press_test(signed, "/settings", "RDM_API_KEY", {"title": "Not saved"})
    assert signed.setup.tests == [
        ("RDM_DETAILS_API_KEY", "typed-details-key-1", None),
        ("RDM_KB_API_KEY", None, None),
        ("RDM_API_KEY", None, None),
    ]
    assert not (signed.folder / "settings.toml").exists() and not signed.store.secrets_file.exists()


def test_transportapis_app_id_and_key_are_tested_together(pi_signed):
    press_test(pi_signed, "/settings/board", "TAPI_APP_KEY",
               {"TAPI_APP_ID": "typed-app-id", "TAPI_APP_KEY": "typed-app-key"})
    response, page = press_test(pi_signed, "/settings/board", "TAPI_APP_KEY", {"TAPI_APP_ID": "typed-app-id"})
    assert pi_signed.setup.tests == [("TAPI_APP_KEY", "typed-app-key", "typed-app-id"),
                                     ("TAPI_APP_KEY", None, "typed-app-id")]
    # The one result is by the app key's box, and both boxes point to it.
    assert page.count('id="TAPI_APP_KEY-test"') == 1
    for box in ("TAPI_APP_ID", "TAPI_APP_KEY"):
        assert re.search(r'<input id="%s"[^>]*aria-describedby="[^"]*TAPI_APP_KEY-test"' % box, page), box


def test_the_result_is_shown_by_its_keys_box_and_unsaved_changes_stay(signed):
    response, page = press_test(signed, "/settings/dashboard", "RDM_KB_API_KEY",
                                {"RDM_KB_API_KEY": "typed-kb-key-1", "history_days": "45"})
    assert response.status == 200 and "<title>Key tested – Dashboard settings</title>" in page
    box = re.search(r'<input id="RDM_KB_API_KEY"[^>]*>', page).group(0)
    assert 'aria-describedby="RDM_KB_API_KEY-hint RDM_KB_API_KEY-test"' in box and "value=" not in box
    result = ('<p class="key-result key-result-works" id="RDM_KB_API_KEY-test" role="status" tabindex="-1" '
              'autofocus><span aria-hidden="true">✓</span> <strong>Works:</strong> It works: the service answered.</p>')
    assert page[page.index(box) + len(box):].startswith(result)
    assert fields_from(page)["history_days"] == "45"
    assert not (signed.folder / "settings.toml").exists()


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
    _, page = press_test(signed, "/settings", "BODS_API_KEY")
    assert f'class="key-result key-result-{state}" id="BODS_API_KEY-test"' in page
    assert f"<strong>{words}:</strong> Said plainly.</p>" in page


def test_a_key_is_never_shown_or_logged(signed, caplog):
    typed = "typed-details-key-1"
    signed.setup.result = KeyResult("wrong_key", f"Refused {typed} and rail-key-1234567.")
    response, page = press_test(signed, "/settings/dashboard", "RDM_DETAILS_API_KEY", {"RDM_DETAILS_API_KEY": typed})
    assert response.status == 200 and "Refused … and …." in page
    for secret in (typed, "rail-key-1234567", "bods-key-7654321"):
        assert secret not in "".join(signed.seen) and secret not in caplog.text


def test_a_key_typed_wrongly_is_not_tested(signed):
    _, page = press_test(signed, "/settings/dashboard", "RDM_KB_API_KEY", {"RDM_KB_API_KEY": "has spaces in it"})
    assert signed.setup.tests == [] and "has spaces in it" not in page
    assert "<strong>Not tested:</strong> Paste the key exactly" in page


@pytest.mark.parametrize("path, name", [
    ("/settings/dashboard", "RDM_API_KEY"),
    ("/settings", "RDM_KB_API_KEY"),
    ("/settings/dashboard", "TAPI_APP_KEY"),
    ("/settings", "PASSWORD"),
    ("/settings", ""),
])
def test_a_tab_tests_only_its_own_keys(signed, path, name):
    response, _ = press_test(signed, path, name)
    assert response.status == 400 and signed.setup.tests == []


@pytest.mark.parametrize("failure, said", [
    (TooSoon("Wait 7 s before testing this key again"), "Wait 7 s before testing this key again"),
    (SetupError("The fetch service didn't answer in time"), "The fetch service didn't answer in time"),
    (RuntimeError("typed-kb-key-1 went wrong"), "Something went wrong while testing the key. Try again."),
])
def test_a_test_that_cant_run_says_why_plainly(signed, caplog, failure, said):
    signed.setup.result = failure
    response, page = press_test(signed, "/settings/dashboard", "RDM_KB_API_KEY", {"RDM_KB_API_KEY": "typed-kb-key-1"})
    assert response.status == 200 and f"<strong>Not tested:</strong> {said}" in page
    assert "typed-kb-key-1" not in page and "typed-kb-key-1" not in caplog.text


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
    _, page = press_test(on_a_pi, "/settings/board", "TAPI_APP_KEY",
                         {"TAPI_APP_ID": "typed-app-id", "TAPI_APP_KEY": "typed-app-key"})
    assert "<strong>Works:</strong> It works." in page
    assert on_a_pi.runner.tests == [("TAPI_APP_KEY", "typed-app-key", "typed-app-id")] and on_a_pi.here == []


def test_each_key_is_tested_at_most_once_every_ten_seconds_as_in_the_wizard(on_a_pi):
    press_test(on_a_pi, "/settings", "RDM_API_KEY")
    _, page = press_test(on_a_pi, "/settings", "RDM_API_KEY")
    assert "<strong>Not tested:</strong> Wait 10 s before testing this key again" in page
    press_test(on_a_pi, "/settings", "BODS_API_KEY")  # another key has its own limit
    assert on_a_pi.setup.handle("testkey", {"name": "BODS_API_KEY"})[0] == 429  # one limit with the wizard
    on_a_pi.now[0] += 10
    press_test(on_a_pi, "/settings", "RDM_API_KEY")
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
             ".css": "text/css; charset=utf-8"}
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
        for path in ["/settings/login", "/settings/reset", "/settings", "/settings/style.css", "/settings/nowhere",
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
