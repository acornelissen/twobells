"""tools/serve_doc_settings.py: the real settings site and wizard on sample
settings, signed in by the tool alone, listening on 127.0.0.1 and never
reaching the internet for the wizard's lookups."""

import http.client
import importlib.util
import json
import re
import socket
import threading

import pytest

from tests.paths import ROOT
from twobells.settings.installs import INSTALLS
from twobells.web.server import DashboardApp, StaticFiles, make_server

_SCRIPT = ROOT / "tools" / "serve_doc_settings.py"
HOME = {"lat": 53.4433, "lon": -1.9512}


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("serve_doc_settings", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Served:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()

    def __call__(self, method, path, body=None, headers=None):
        sent = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}", **(headers or {})}
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body, sent)
        response = conn.getresponse()
        text = response.read().decode()
        conn.close()
        return response, text

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def served(tool, tmp_path):
    running = Served(tool.serve(tmp_path, INSTALLS["pi"], scheme="light", port=0))
    yield running
    running.close()


def test_it_listens_on_this_computer_only(served):
    assert served.server.server_address[0] == "127.0.0.1"


def test_every_tab_opens_signed_in(served):
    for path in ("/settings", "/settings/general", "/settings/board", "/settings/dashboard", "/settings/keys",
                 "/settings/trains-and-buses", "/settings/setup/"):
        response, text = served("GET", path)
        assert response.status == 200, path
    response, text = served("GET", "/settings/general")
    assert "Sign out" in text and re.search(r'name="csrf" value="[^"]+"', text)


def test_a_save_still_needs_its_csrf_token(served):
    response, _ = served("POST", "/settings/general", "title=Elsewhere",
                         {"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status != 303  # refused: the form's token is missing


def test_the_sample_settings_are_glossops(served):
    _, text = served("GET", "/settings")
    assert "Glossop (GLO)" in text and "Keys are never shown here." in text


def test_signing_out_then_back_in_by_the_tool(served):
    _, page = served("GET", "/settings")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    response, _ = served("POST", "/settings/logout", f"csrf={csrf}",
                         {"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status == 303
    response, _ = served("GET", "/settings")
    assert response.status == 200


def test_the_package_site_alone_asks_for_the_password(tool, tmp_path):
    """The signing in is the tool's: the site it builds, served as serve does, redirects to sign in."""
    install = INSTALLS["pi"]
    site = tool.make_site(tool.make_store(tmp_path, install), install, None)
    running = Served(make_server(DashboardApp(None, StaticFiles({}, False), lambda: True, site), "127.0.0.1", 0))
    try:
        response, _ = running("GET", "/settings")
        assert response.status == 303 and response.getheader("Location").startswith("/settings/login")
        response, _ = running("GET", "/settings/setup/")
        assert response.status == 303
    finally:
        running.close()


def test_nothing_in_the_package_knows_the_tool():
    for path in (ROOT / "src" / "twobells").rglob("*"):
        if path.suffix in (".py", ".js", ".html"):
            text = path.read_text(encoding="utf-8")
            assert "serve_doc" not in text and "SignedIn" not in text, path


def test_the_wizard_never_connects_out(tool, tmp_path, monkeypatch):
    install = INSTALLS["pi"]
    site = tool.make_site(tool.make_store(tmp_path, install), install, None)

    def refuse(*args, **kwargs):
        raise AssertionError("the wizard tried to connect out")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    wizard = site.setup
    status, found = wizard.handle("postcode", {"postcode": "sk13 8ab"})
    assert status == 200 and found["place"]["label"] == "SK13 8AB"
    assert wizard.handle("address", {"q": "Henry Street"})[0] == 200
    status, found = wizard.handle("stations", HOME)
    assert status == 200 and found["stations"][0]["crs"] == "GLO"
    status, found = wizard.handle("stops", HOME)
    assert status == 200 and "1000DGHS0900" in [stop["atco"] for stop in found["stops"]]
    status, found = wizard.handle("walk", {"home": HOME, "station_crs": "GLO", "stop_atcos": ["1000DGHS0900"]})
    assert status == 200 and set(found["walk"]) == {"GLO", "1000DGHS0900"}
    status, found = wizard.handle("start", {"stops": {"A": "1000DGHS0900"}, "routes": False})
    assert status == 200 and found["job"]["state"] == "done"
    status, found = wizard.handle("testkey", {"name": "RDM_API_KEY"})
    assert status == 200 and found["state"] == "works"
    status, found = wizard.handle("finish", {
        "home": HOME, "station_crs": "GLO", "filter_crs": "MAN", "stops": {"A": "1000DGHS0900"},
        "walk": {"GLO": 10, "1000DGHS0900": 8}, "views": ["board", "dashboard"],
    })
    assert status == 200 and found["saved"] is True, found


def test_the_keys_are_made_up_whatever_this_computer_has(tool, tmp_path, monkeypatch):
    monkeypatch.setenv("RDM_KB_API_KEY", "a-real-key-from-the-shell")
    store = tool.make_store(tmp_path, INSTALLS["pi"])
    assert "RDM_KB_API_KEY" not in store.env()
    assert store.env()["RDM_API_KEY"] == tool.KEYS["RDM_API_KEY"]


@pytest.mark.parametrize(("scheme", "media"), [("light", b"@media not all"), ("dark", b"@media all")])
def test_a_scheme_is_served_whatever_the_browser_prefers(tool, scheme, media):
    css = b":root { --bg: #fff; }\n@media (prefers-color-scheme: dark) {\n  :root { --bg: #000; }\n}\n"
    page = b'<meta name="color-scheme" content="light dark">'
    assert tool.with_scheme(css, scheme) == css.replace(b"@media (prefers-color-scheme: dark)", media)
    assert tool.with_scheme(page, scheme) == f'<meta name="color-scheme" content="{scheme}">'.encode()
    assert tool.with_scheme(css, None) == css


def test_the_light_scheme_reaches_the_pages(served):
    _, css = served("GET", "/settings/style.css")
    _, wizard_css = served("GET", "/settings/setup/wizard_static/wizard.css")
    _, wizard = served("GET", "/settings/setup/")
    for text in (css, wizard_css):
        assert "prefers-color-scheme" not in text and "@media not all" in text
    assert 'content="light"' in wizard


def test_the_wizard_api_answers_through_the_server(served):
    response, text = served("GET", "/settings/setup/api/session")
    csrf = json.loads(text)["csrf"]
    response, text = served("POST", "/settings/setup/api/begin", "{}",
                            {"Content-Type": "application/json", "X-CSRF-Token": csrf})
    assert response.status == 200 and json.loads(text)["noun"] == "board"


def test_the_dashboard_tool_serves_a_scheme_alike(tool):
    """tools/serve_doc_dashboard.py has the same --scheme, for the dashboard's light pictures."""
    spec = importlib.util.spec_from_file_location("serve_doc_dashboard", ROOT / "tools" / "serve_doc_dashboard.py")
    dashboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dashboard)
    css = (ROOT / "src" / "twobells" / "dashboard_static" / "app.css").read_bytes()
    for scheme in ("light", "dark", None):
        assert dashboard.with_scheme(css, scheme) == tool.with_scheme(css, scheme)
    assert b"prefers-color-scheme" not in dashboard.with_scheme(css, "light")
