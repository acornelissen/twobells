"""Serves the settings site and the setup wizard on sample settings, for the screenshots in docs/images/.

    .venv/bin/python tools/serve_doc_settings.py [--port 8932] [--install pi] [--scheme light]

The pages are this checkout's: the real settings site (web/settingsite.py)
and the wizard at /settings/setup/, built here over a throwaway store in a
temporary folder holding examples/glossop.toml, made-up keys and a board
that last checked a minute ago. The folder goes when the server stops.

Signing in. The store gets a random password nobody is told, and this tool
makes one session and hands its cookie to the site with every request that
has none, so the browser is signed in without typing anything. That is all
here, around the site: nothing in the package can skip signing in. Signing
out ends the session; the next request gets a new one.

The wizard never reaches the internet from here: the postcode, address,
stops, walk-time, bus company and key-test lookups answer with Glossop's
sample answers (the stops from tests/fixtures' NaPTAN extract), and the
stations come from the bundled list as they always do. Its maps still load
OpenStreetMap's tiles in the browser.

It listens on 127.0.0.1 only. Open http://127.0.0.1:8932/settings and take
the pictures by hand. --scheme light (or dark) serves the pages with that
scheme's colours whatever the browser prefers, so the light pictures can be
taken from a browser set to dark.
"""

from __future__ import annotations

import argparse
import json
import secrets
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from twobells.settings import keyresults
from twobells.settings.installs import INSTALLS, Install
from twobells.settings.store import Store
from twobells.setup import discover
from twobells.setup import lookups as setup
from twobells.setup.api import SetupApi
from twobells.setup.keytest import KeyResult
from twobells.web.groupform import check_views
from twobells.web.server import DashboardApp, DashboardServer, StaticFiles, make_server
from twobells.web.settingsite import PREFIX, SettingsSite, wizard_files

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "glossop.toml"
FIXTURES = ROOT / "tests" / "fixtures"
STATIC = ROOT / "src" / "twobells" / "web"

# Both views on, so every tab shows; the weather so the dashboard has it.
EXTRA = """
[app]
views = ["board", "dashboard"]

[weather]
lat = 53.44
lon = -1.95
"""
# Made up, in the shape each service's keys take: the Keys page says they are set.
KEYS = {
    "RDM_API_KEY": "sample-rail-key-0001",
    "BODS_API_KEY": "sample-bus-key-0002",
    "RDM_DETAILS_API_KEY": "sample-details-key-0003",
}
HOME = setup.Place(53.4433, -1.9512, "SK13 8AB", "England", "High Peak")
ADDRESS = setup.Place(53.4433, -1.9512, "Henry Street, Glossop, High Peak, Derbyshire, England, SK13 8BW",
                      "England", "High Peak")
FOUND = {"BNML": ["237", "341"], "HIPK": ["61", "390"]}


def with_scheme(body: bytes, scheme: str | None) -> bytes:
    """A stylesheet or page with `scheme`'s colours whatever the browser
    prefers: its dark tokens' media query made to match never (light) or
    always (dark), and its color-scheme meta saying that one scheme."""
    if scheme is None:
        return body
    always = b"all" if scheme == "dark" else b"not all"
    body = body.replace(b"@media (prefers-color-scheme: dark)", b"@media " + always)
    return body.replace(b'content="light dark"', b'content="' + scheme.encode() + b'"')


class SampleStore(Store):
    """A store that leaves out this computer's environment: only the keys made up here."""

    def env(self, environ=None) -> dict[str, str]:
        return super().env({} if environ is None else environ)


class SampleJob:
    """The search for bus companies, found at once: Glossop's two, for whichever stops."""

    def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None):
        self.stops = dict(stops)
        self.state = {"state": "queued", "step": "", "done": 0, "total": 0, "message": "", "result": None}

    def start(self) -> None:
        lines = sorted({line for found in FOUND.values() for line in found})
        result = {
            "fragment": {"bus": {"source": "bods", "operators": sorted(FOUND), "routes": {}}},
            "lines": FOUND,
            "stop_lines": dict.fromkeys(self.stops, lines),
            "timetable_journeys": 412,
            "notes": [],
        }
        self.state.update(state="done", step=discover.STEP_TIMETABLE, done=1, total=1,
                          message="Found 2 bus companies", result=result)

    def status(self) -> dict:
        return json.loads(json.dumps(self.state))

    def cancel(self) -> None:
        pass


def sample_stops(lat: float, lon: float, *, within_m: float, limit: int) -> list[setup.Stop]:
    text = (FIXTURES / "naptan_setup_glossop.csv").read_text(encoding="utf-8")
    return setup.stops_near(text, lat, lon, within_m, limit)


def sample_walks(home, places) -> dict[str, tuple[int, str]]:
    return {key: (setup.estimate_minutes(home, place), "route") for key, place in places.items()}


def works(name, value, settings, env) -> KeyResult:
    return KeyResult("works", "It works: the service answered.")


class SampleHub:
    """The dashboard's snapshot for the Overview: the sample morning, each
    feed's last success moved to now, so they read as just checked."""

    closed = False

    def __init__(self) -> None:
        snapshot = json.loads((FIXTURES / "dashboard_snapshot.json").read_text(encoding="utf-8"))
        then = datetime.fromisoformat(snapshot["generated_at"])
        self._shift = datetime.now(then.tzinfo) - then
        self._body = json.dumps(self._moved(snapshot)).encode()

    def _moved(self, value):
        if isinstance(value, dict):
            return {key: (self._later(item) if key == "last_success" else self._moved(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [self._moved(item) for item in value]
        return value

    def _later(self, value):
        if not isinstance(value, str):
            return value
        return (datetime.fromisoformat(value) + self._shift).isoformat()

    def current(self) -> tuple[int, bytes]:
        return 1, self._body

    def wait(self, after_version: int, timeout: float) -> int:
        return 1

    def lookup(self, row_id: str) -> dict | None:
        return None

    def get(self, name: str) -> object | None:
        return None

    def touch(self) -> None:
        pass

    def active(self, within: float) -> bool:
        return True

    def age(self) -> float:
        return 0.0

    def close(self) -> None:
        pass


class SignedIn:
    """The settings site, with this tool's session cookie added to every
    request that has none: the browser is signed in without a password."""

    def __init__(self, site: SettingsSite) -> None:
        self.site = site
        self.token, _ = site.sessions.create()

    def handle(self, method, path, query, headers, client, stream):
        cookie = headers.get("Cookie") or ""
        if self.site.sessions.token_from(cookie) is None:
            if self.site.sessions.get(self.token) is None:  # signed out: start again
                self.token, _ = self.site.sessions.create()
            del headers["Cookie"]
            headers["Cookie"] = "; ".join(filter(None, [cookie, f"{self.site.sessions.cookie_name}={self.token}"]))
        return self.site.handle(method, path, query, headers, client, stream)


def make_store(folder: Path, install: Install) -> Store:
    """The throwaway store: the sample settings, made-up keys, a board that
    has just checked, and the keys' last tests."""
    config = folder / "config.toml"
    config.write_text(SAMPLE.read_text(encoding="utf-8") + EXTRA, encoding="utf-8")
    keys = folder / "secrets.env"
    keys.write_text("".join(f"{name}={value}\n" for name, value in KEYS.items()), encoding="utf-8")
    store = SampleStore.open(config, keys, state_dir=folder / "store", install=install.name, environ={})
    store.make()
    now = datetime.now().replace(microsecond=0)
    checked = (now - timedelta(minutes=1)).isoformat()
    store.state_file.write_text(json.dumps({
        "sources": {"train": {"last_success": checked}, "bus": {"last_success": checked}},
        "last_render": {"at": checked},
    }), encoding="utf-8")
    for name in ("RDM_API_KEY", "BODS_API_KEY"):
        keyresults.record(store.folder, name, "works", time.time() - 3600)
    return store


def make_site(store: Store, install: Install, scheme: str | None) -> SettingsSite:
    """The settings site and wizard as `twobells serve` builds them, with the
    wizard's internet lookups answered here."""
    target = store.target(lambda data, env: check_views(data, env, store.folder, install))
    view_links = {"dashboard": ("The dashboard", "/")} if "dashboard" in install.offered else {}
    wizard = SetupApi(
        target, store.env, store.folder,
        lookup_postcode=lambda postcode: setup.Place(HOME.lat, HOME.lon, setup.normalise_postcode(postcode),
                                                     HOME.country, HOME.admin_district),
        search_address=lambda query: [ADDRESS],
        nearby_stops=sample_stops,
        walk_minutes=sample_walks,
        make_job=SampleJob,
        test_key=works,
        install=install,
        links=[("Settings", PREFIX)],
        view_links=view_links,
        back=PREFIX,
    )
    files = {path: (with_scheme(body, scheme), kind) for path, (body, kind) in wizard_files().items()}
    site = SettingsSite(store, install, wizard, dashboard_on=lambda: True,
                        stylesheet=with_scheme((STATIC / "web.css").read_bytes(), scheme), files=files)
    site.password.set(secrets.token_urlsafe(24))  # nobody needs it: the tool signs the browser in
    site.hub = SampleHub()
    return site


def serve(folder: Path, install: Install, scheme: str | None = None, port: int = 8932) -> DashboardServer:
    """The server for the sample settings in `folder`, on 127.0.0.1 only."""
    site = make_site(make_store(folder, install), install, scheme)
    app = DashboardApp(None, StaticFiles({}, False), lambda: True, SignedIn(site))  # type: ignore[arg-type]
    return make_server(app, "127.0.0.1", port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8932)
    parser.add_argument("--install", choices=sorted(INSTALLS), default="pi",
                        help="whose settings: the Pi's has every tab")
    parser.add_argument("--scheme", choices=("light", "dark"), help="these colours, whatever the browser prefers")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="twobells-doc-settings-") as folder:
        server = serve(Path(folder), INSTALLS[args.install], args.scheme, args.port)
        print(f"The sample settings are on http://127.0.0.1:{args.port}{PREFIX}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
