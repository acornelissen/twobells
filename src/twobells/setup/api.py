"""The setup wizard's JSON API, for any host to mount behind its sign-in.

Framework-free: `SetupApi.handle(name, payload)` takes the name of a handler
and the request's JSON body, already decoded (see `decode`), from a request
the host has already signed in and checked (its session, its CSRF token and
that it is same-origin), and returns (HTTP status, JSON-ready dict). Every
input is checked here, strictly: unknown fields, wrong types, overlong text
and positions outside the UK are refused with 400.

Privacy. Home's exact position lives in the browser and in this object's
memory while setup runs (the stops found near it, cleared on finishing); it
goes to postcodes.io or Nominatim to find it and to the foot router for
walk times, as setup/lookups.py describes, and is never logged or saved. With a
spool runner it passes through a request file in the private spool folder,
held in memory on the Pi, never on the SD card, and deleted as soon as the
fetch service reads it (setup/spool/). Finishing
writes only what setup.config_fragment allows: the weather position rounded
to 2 places (about a kilometre), the station, the stops and walk times, the
bank holiday list, and [bus] source, operators and routes from the search;
and the views and what each shows.

Keys are write-only: the API says which are set, never their values, and
no error repeats one. `testkey` tries one key, saved or pasted, with one
harmless call to its service (setup/keytest.py), at most once per key every
KEY_TEST_EVERY seconds across the whole process. The settings tabs test
the other keys through SetupApi.test_key, under the same limit and runner.

Where the internet work runs. By default the lookups, the key tests and the
search run here, in this process (the dashboard; a laptop). A host that
mustn't reach the internet itself passes a `runner` (see Runner) that does
them elsewhere: on the Pi, spool.runner.SpoolRunner hands them to a separate
fetch service, as the settings site's own service is kept off the internet.
A runner is given only what was typed or pasted, never a saved key or the
saved settings; it reads those itself.

One wizard for every install: `begin` answers with the page's title, the
word for what shows the departures there ("board" on a Pi, "dashboard"
elsewhere; settings/installs.py), the keys every view uses (schema.keys of the
general group: each view's own keys are on its settings tab), and the
views the install offers with those on now. Only those keys can be saved
or tested through it. `finish` takes the views to turn on, at least one
and only those offered, saves them as [app] views, and the settings are
checked as each of those views loads them before anything is saved.

What to show. `begin` also offers show.PRESETS for each view offered, with
what each preset shows there and the one to start at: the preset a saved
show list matches; "keep" for a list that matches none, or for an install
already set up without one (it shows what it always has); The usual
(show.NEW_INSTALL) for a new install. `finish` takes a preset for any of
the views on, saved as [board] show or [dashboard] show through the
settings form's check; a view left out keeps its list, except that on a new
install a view without one gets The usual.

Modes of transport. `begin` also gives the modes setup sets up (trains
from a station, buses from stops: SETUP_MODES), and for each view offered
how many it has room for (the board's display, in its look and text size;
the dashboard has room for them all), why, in words, and the modes its list names now (None without
one). `finish` takes, for any of the views on, the modes it shows, each one
of those this finish sets up, saved as [board] modes or [dashboard] modes
through the settings form's check: at least one, and no more than there is
room for. A view left out keeps its list; one naming none of the modes set
up shows every mode set up (settings/modes.py).

Shortcuts. The page can start at the station or the stops step instead of
home ("Change station only", "Change stops only"), filled in with today's
choices: `saved` answers them from the settings in force and local files
only (roughly where home is, from [weather]; the station and the stops,
with names from the bundled station list and the bus timetable; the walk
times), never a key. `finish` with `only` then saves that part alone,
keeping everything else as saved. `back`, if the host gives one, is where
Back on a shortcut's first step goes.

Serving the page. The host serves, under one prefix ending in a slash (say
/setup/), the files `static_files()` lists by their path under that prefix:
the page itself at the prefix, wizard_static/* beside it and the dashboard's
dom.js, icons.js, Leaflet and Inter under dashboard_static/, so the page's
relative imports resolve. The API is at <prefix>api/<name>, POST with a JSON
body, CSRF token in the X-CSRF-Token header; and the host's own GET
<prefix>api/session answers {"csrf": token} for a signed-in session. Send the
page and its files with PAGE_HEADERS.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Protocol, overload

from ..core.errors import safe_message
from ..settings import keyresults, modes, schema, show
from ..settings.config import ConfigError, still_to_set_up
from ..settings.installs import INSTALLS, Install
from ..settings.overlay import OverlayError, Target, save_to
from ..settings.schema import ATCO as _ATCO
from ..settings.schema import CRS as _CRS
from ..settings.schema import KEY_VALUE as _KEY_VALUE
from ..settings.schema import LABEL as _LABEL
from ..timetable import build as timetable
from . import discover, keytest
from . import lookups as setup
from .lookups import Place, SetupError, Station, Stop

log = logging.getLogger(__name__)

MAX_BODY = 16 * 1024  # the biggest request, finish with keys, is about 2 kB
MAX_DEPTH = 6
STATIONS_SHOWN = 5
STATIONS_WITHIN_KM = 40
STOPS_SHOWN = 15
STOPS_WITHIN_M = 1000
TRAM_STOPS_SHOWN = 5
# London's stations are close together, on many lines: a few more of them.
LONDON_STOPS_SHOWN = 8
MAX_TRAM_STOPS = schema.MAX_TRAM_STOPS
# Labels for the tram stops picked, in order: T is for tram.
TRAM_LABELS = ("T", "U", "V", "W")
MAX_BOARD_STOPS = setup.MAX_BOARD_STOPS
MAX_WALK_MINUTES = 60
_CACHED_STOPS = 200
MIN_SCRUBBED = 6
KEY_TEST_EVERY = 10.0  # seconds between tests of one key

PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

_POSTCODE_LENGTH = 16
_LINK = re.compile(r"^(/(?!/)[A-Za-z0-9._~/-]*|https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?)$")
# The keys every view uses; each view's own are on its settings tab.
GENERAL_KEYS = schema.keys("general")
TITLE = "Set up your departures"
_TITLE_LENGTH = 60
_NOUN = re.compile(r"[a-z]{1,20}")  # one plain word, to sit in a sentence: "No trains on this board"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
KEY_DOCS = "https://github.com/acornelissen/twobells/blob/main/docs/keys.md"
_KEY_ANCHORS = {
    "RDM_API_KEY": "#rail-data-marketplace-live-departure-board",
    "RDM_DETAILS_API_KEY": "#rail-data-marketplace-service-details",
    "RDM_KB_API_KEY": "#rail-data-marketplace-knowledgebase-incidents",
    "BODS_API_KEY": "#bus-open-data-service",
    "TAPI_APP_ID": "#transportapi",
    "TAPI_APP_KEY": "#transportapi",
    "TFL_APP_KEY": "#transport-for-london",
}
_COUNTRIES = ("England", "Wales", "Scotland", "Northern Ireland")
_NAME_LENGTH = 100  # a stop's name from the timetable, as the page shows it
# What finish takes, (required, optional): for the whole wizard (no `only`),
# or for one part of it, the rest kept as saved.
PARTS = ("station", "stops")
_FINISH_FIELDS = {
    None: (("home", "stops", "walk"), ("station_crs", "filter_crs", "trams", "keys", "views", "show", "modes")),
    "station": (("only", "walk"), ("station_crs", "filter_crs", "keys")),
    "stops": (("only", "stops", "walk"), ("keys",)),
}


# What begin says to start a view's What to show at when its show list is no preset's.
KEEP = "keep"
# The modes setup sets up, a step each: trains from the station, buses from
# the stops, trams from the tram stops (Manchester's Metrolink, where home is near one).
SETUP_MODES = ("train", "bus", "metro")


class Invalid(Exception):
    """A request that doesn't fit; the text is safe to show and names no value."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


class Conflict(Exception):
    """A request that can't be done now, like a second search while one runs."""


class TooSoon(Exception):
    """A request made again too soon after the last, like testing one key twice in a row."""


class RateLimit:
    """At most one go per `every` seconds for each name. Thread-safe; the
    names are a fixed few, so it never grows."""

    def __init__(self, every: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._every = every
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def take(self, name: str) -> float:
        """0 if `name` may go now, and that go is counted; else the seconds to wait."""
        with self._lock:
            now = self._clock()
            last = self._last.get(name)
            if last is not None and now - last < self._every:
                return self._every - (now - last)
            self._last[name] = now
            return 0.0


# Shared by every SetupApi in the process, so two hosts can't double the rate.
KEY_TESTS = RateLimit(KEY_TEST_EVERY)


Reply = tuple[int, dict]


class Job(Protocol):
    """A search for bus companies: a discover.SetupJob, or on a Pi the runner's stand-in for one."""

    def start(self) -> None: ...

    def status(self) -> dict: ...

    def cancel(self) -> None: ...


MakeJob = Callable[..., Job]  # (folder, stops, api_key, *, routes, walk_minutes) -> a discover.SetupJob


class Runner(Protocol):
    """Setup's internet work, done somewhere other than this process. Each
    lookup takes and gives what setup/lookups.py's does, raising SetupError when it
    can't. make_job's job has a discover.SetupJob's start, status and
    cancel; given no `pasted_key`, it uses the saved Bus Open Data Service
    key, as test_key uses the saved key `name` without a `value`."""

    def lookup_postcode(self, postcode: str) -> Place: ...

    def search_address(self, query: str) -> list[Place]: ...

    def nearby_stops(self, lat: float, lon: float, *, within_m: float, limit: int) -> list[Stop]: ...

    def walk_minutes(self, home: Place, places: Mapping[str, Station | Stop]) -> dict[str, tuple[int, str]]: ...

    def make_job(self, stops: Mapping[str, str], pasted_key: str, *, routes: bool,
                 walk_minutes: Mapping[str, int]) -> Job: ...

    def test_key(self, name: str, value: str | None, app_id: str | None) -> keytest.KeyResult: ...


def decode(body: bytes) -> object:
    """A request body as JSON. Invalid if it is too big, not UTF-8 JSON, or nested too deep."""
    if len(body) > MAX_BODY:
        raise Invalid("That request is too big")
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise Invalid("That request isn't JSON") from None
    if too_deep(value, MAX_DEPTH):
        raise Invalid("That request is nested too deeply")
    return value


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number")


def too_deep(value, limit: int) -> bool:
    """Whether anything in `value` is nested more than `limit` deep. Without
    recursion: json reads deeper than Python can recurse."""
    waiting = [(value, 1)]
    while waiting:
        item, level = waiting.pop()
        if level > limit:
            return True
        if isinstance(item, dict):
            waiting.extend((v, level + 1) for v in item.values())
        elif isinstance(item, list):
            waiting.extend((v, level + 1) for v in item)
    return False


def static_files() -> dict[str, Path]:
    """The files a host serves for the wizard, by their path under its prefix
    ("" is the page). Nothing else from either folder is needed. The settings
    site's stylesheet takes Inter from here too, so both load the one copy."""
    # Siblings, so the page's relative imports resolve the same on disk as served.
    root = Path(str(files("twobells")))
    wizard, dashboard = root / "wizard_static", root / "dashboard_static"
    leaflet, inter = "vendor/leaflet-1.9.4", "vendor/inter-4.1/InterVariable.woff2"
    # wizard.js and the modules it imports, each named: anything else is a 404.
    scripts = ("wizard", "api", "say", "state", "steps", "maps", "home", "station", "stops", "trams", "walk", "keys",
               "views", "presets", "finish", "roads")
    return {
        "": wizard / "index.html",
        **{f"wizard_static/{name}.js": wizard / f"{name}.js" for name in scripts},
        "wizard_static/wizard.css": wizard / "wizard.css",
        "dashboard_static/dom.js": dashboard / "dom.js",
        "dashboard_static/icons.js": dashboard / "icons.js",
        f"dashboard_static/{inter}": dashboard / inter,
        f"dashboard_static/{leaflet}/leaflet-src.esm.js": dashboard / leaflet / "leaflet-src.esm.js",
        f"dashboard_static/{leaflet}/leaflet.css": dashboard / leaflet / "leaflet.css",
    }


# Checking input. Messages name the field, never echo the value.


def _fields(payload, required: Sequence[str] = (), optional: Sequence[str] = ()) -> dict:
    if not isinstance(payload, dict):
        raise Invalid("Send a JSON object")
    unknown = set(payload) - set(required) - set(optional)
    if unknown:
        raise Invalid("That request has fields it shouldn't", sorted(unknown)[0][:40])
    for name in required:
        if name not in payload:
            raise Invalid(f"That request needs {name}", name)
    return payload


def _text(value, field: str, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise Invalid("Send text", field)
    value = value.strip()
    if not low <= len(value) <= high or setup._CONTROL.search(value):
        raise Invalid(f"Use {low} to {high} ordinary characters", field)
    return value


def _number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Invalid("A position must be two numbers", field)
    return float(value)


def _point(value, field: str = "home") -> Place:
    """A position in the UK, with an optional country for the bank holidays."""
    fields = _fields(value, ("lat", "lon"), ("country",))
    lat, lon = _number(fields["lat"], field), _number(fields["lon"], field)
    if not setup.in_uk(lat, lon):
        raise Invalid("That position is outside the UK", field)
    country = fields.get("country", "")
    if country not in ("", *_COUNTRIES):
        raise Invalid("That isn't a UK country", field)
    return Place(lat, lon, "", country)


def _flag(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise Invalid("Send true or false", field)
    return value


def _minutes(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WALK_MINUTES:
        raise Invalid(f"A walk is a whole number of minutes from 0 to {MAX_WALK_MINUTES}", field)
    return value


def _stops(value, field: str = "stops", least: int = 0) -> dict[str, str]:
    """{label: ATCO code}: labels as the settings site allows, each stop once."""
    if not isinstance(value, dict) or not least <= len(value) <= MAX_BOARD_STOPS:
        raise Invalid(f"Pick {least} to {MAX_BOARD_STOPS} stops", field)
    stops = {}
    for label, atco in value.items():
        if not _LABEL.match(label):
            raise Invalid("A stop label is 1 to 3 letters or digits", field)
        if not isinstance(atco, str) or not _ATCO.match(atco):
            raise Invalid(f"Stop {label} needs a NaPTAN stop code", field)
        stops[label] = atco
    if len({label.upper() for label in stops}) != len(stops):
        raise Invalid("Two stops have the same label", field)
    if len(set(stops.values())) != len(stops):
        raise Invalid("Each stop must be a different stop", field)
    return stops


def _trams(value, field: str = "trams") -> dict[str, str]:
    """{label: tram stop code}: Metrolink stops, each once, labelled as the settings site allows."""
    if not isinstance(value, dict) or len(value) > MAX_TRAM_STOPS:
        raise Invalid(f"Pick at most {MAX_TRAM_STOPS} tram stops", field)
    trams = {}
    for label, code in value.items():
        if not _LABEL.match(label):
            raise Invalid("A tram stop label is 1 to 3 letters or digits", field)
        if (not isinstance(code, str) or not (setup.TRAM_STOP.match(code) or setup.LONDON_STOP.match(code))
                or code not in setup.tram_stop_names()):
            raise Invalid(f"Tram stop {label} needs a Metrolink stop code, or TfL's for a London stop", field)
        trams[label] = code
    if len({bool(setup.LONDON_STOP.match(code)) for code in trams.values()}) > 1:
        raise Invalid("Pick Metrolink stops or London ones, not both", field)
    if len({label.upper() for label in trams}) != len(trams) or len(set(trams.values())) != len(trams):
        raise Invalid("Pick each tram stop once, with a label of its own", field)
    return trams


def _key(value, field: str) -> str:
    """A pasted key in the shape of one, or "" for none."""
    if not isinstance(value, str):
        raise Invalid("Paste the key as text", field)
    key = value.strip()
    if key and not _KEY_VALUE.match(key):
        raise Invalid("Paste the key exactly; it has only letters, digits and . _ ~ + / = -", field)
    return key


def _views(value, offered: Sequence[str]) -> list[str]:
    """The views to turn on: at least one, each once, only those offered;
    in the order offered."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(offered):
        raise Invalid("Pick at least one of the views offered", "views")
    if not all(isinstance(view, str) for view in value) or len(set(value)) != len(value):
        raise Invalid("Pick each view once", "views")
    if any(view not in offered for view in value):
        raise Invalid("That isn't one of the views offered here", "views")
    return [view for view in offered if view in value]


def view_choices(offered: Sequence[str]) -> list[dict]:
    """Each view offered, with its name and what it is as the General page says them."""
    fields = {f.name: f for f in schema.FIELDS}
    return [{"name": view, "label": fields[f"view_{view}"].label, "hint": fields[f"view_{view}"].hint}
            for view in offered]


def _show(value, on: Sequence[str]) -> dict[str, str]:
    """What to show: a preset's name for any of the views `on`."""
    names = [name for name, _ in show.PRESETS]
    if not isinstance(value, dict):
        raise Invalid("Send what to show as a preset for each view", "show")
    if any(view not in on for view in value):
        raise Invalid("Say what to show only for the views on", "show")
    if any(name not in names for name in value.values()):
        raise Invalid("Pick Just the times, The usual or Everything", "show")
    return {view: value[view] for view in on if view in value}


def show_list(view: str, name: str) -> list[str]:
    """Preset `name`'s show list for `view`, checked as the settings form checks it."""
    return show.check(view)(" ".join(show.preset(view, name)))


def preset_words(view: str, name: str) -> str:
    """What preset `name` shows in `view`, from the one before it:
    "The usual, and also Calling at and Clock page."."""
    names = [preset for preset, _ in show.PRESETS]
    at = names.index(name)
    before = set(show.preset(view, names[at - 1])) if at else set()
    added = [item.label for item in show.items(view) if item.key in show.preset(view, name) and item.key not in before]
    if not at:
        return f"The departures' times, and {_listed(added)}." if added else "The departures' times only."
    start = dict(show.PRESETS)[names[at - 1]]
    return f"{start}, and also {_listed(added)}." if added else f"The same as {start}."


def _listed(words: Sequence[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _has_show_list(data: Mapping, view: str) -> bool:
    section = data.get(view)
    return isinstance(section, dict) and "show" in section


def show_choices(offered: Sequence[str], data: Mapping, new: bool) -> dict:
    """The presets, and for each view offered what each shows there and the
    one to start at (`now`), by settings `data`; `new` if the install is
    still to be set up."""
    views = {}
    for view in offered:
        now = show.NEW_INSTALL if new else KEEP
        if _has_show_list(data, view):
            try:
                now = show.preset_of(view, show.shown(data, view)) or KEEP
            except ConfigError:
                now = KEEP
        views[view] = {"now": now, "says": {name: preset_words(view, name) for name, _ in show.PRESETS}}
    return {"presets": [{"name": name, "label": label} for name, label in show.PRESETS], "views": views}


def mode_choices(offered: Sequence[str], data: Mapping) -> dict:
    """The modes setup sets up, and for each view offered how many it has
    room for (None for as many as there are), why, and the modes its list
    names now (None without one, or with one that doesn't load), by
    settings `data`."""
    views = {}
    for view in offered:
        try:
            now = modes.listed(data, view)
        except ConfigError:
            now = None
        if view == "board":
            look = _board_look(data)
            most, says = schema.board_room(*look)[0], schema.room_words(*look)
        else:
            most, says = None, "The dashboard lists them one after another."
        views[view] = {"now": list(now) if now is not None else None, "most": most, "says": says}
    return {"offered": [{"name": key, "label": modes.mode(key).label} for key in SETUP_MODES if modes.mode(key).ready],
            "views": views}


def _board_look(data: Mapping) -> tuple[str, str | None, int | None]:
    """(display, look, text size) of the board by settings `data`, which
    say how many modes it has room for (schema.board_room); a look or text
    size that isn't one counts as none, for the loader to refuse."""
    display = str(data.get("display", schema.BOARD_DEFAULTS["display"]))
    look = data.get("theme", schema.BOARD_DEFAULTS["theme"])
    size = data.get("text_size", schema.BOARD_DEFAULTS["text_size"])
    return (display, look if isinstance(look, str) else None,
            size if isinstance(size, int) and not isinstance(size, bool) else None)


def _modes(value, on: Sequence[str], picked: Sequence[str], data: Mapping) -> dict[str, list[str]]:
    """The modes to show: for any of the views `on`, at least one of those
    `picked` (this finish sets up), and no more than the view has room for."""
    if not isinstance(value, dict) or any(view not in on for view in value):
        raise Invalid("Say which modes to show only for the views on", "modes")
    chosen = {}
    for view in on:
        if view not in value:
            continue
        keys = value[view]
        if (not isinstance(keys, list) or len(keys) > len(modes.KEYS)
                or not all(isinstance(key, str) and key in picked for key in keys)):
            raise Invalid("Pick only the modes being set up", "modes")
        if view == "board":
            look = _board_look(data)
            check = modes.check(*schema.board_room(*look), other=schema.room_other(*look))
        else:
            check = modes.check()
        try:
            chosen[view] = check(" ".join(keys))
        except schema.Invalid as error:
            raise Invalid(str(error), "modes") from None
    return chosen


def saved_message(install: Install, views: Sequence[str]) -> str:
    """What happens once setup saves, with `views` on, on `install`."""
    said = ["Saved."]
    if "board" in views:
        said.append("The board shows the new settings within a few minutes.")
    if "dashboard" in views:
        if install.fetch == "process":  # serve starts again with the new settings (cli/serve.py, _SettingsWatch)
            said.append("The dashboard starts again in a few seconds to use them.")
        else:
            said.append("The dashboard shows them within a few minutes.")
    return " ".join(said)


def _keys(value, names: Sequence[str]) -> dict[str, str]:
    """Keys to save: only the host's (`names`), each in the shape of a key; empty ones skipped."""
    if not isinstance(value, dict):
        raise Invalid("Send keys as an object", "keys")
    keys = {}
    for name, key in value.items():
        if name not in names:
            raise Invalid("That isn't one of the keys set up here", "keys")
        key = _key(key, name)
        if key:
            keys[name] = key
    return keys


def _place(place: Place) -> dict:
    return {"lat": place.lat, "lon": place.lon, "label": place.label, "country": place.country,
            "district": place.admin_district}


def _station(station: Station) -> dict:
    return {"name": station.name, "crs": station.crs, "lat": station.lat, "lon": station.lon,
            "distance_m": station.distance_m}


def suggested_labels(stops: Sequence[Stop]) -> list[str]:
    """A label for each stop found, as setup.stop_labels picks them: the letter
    on its sign where it has one of its own, else one on no sign here."""
    return setup.default_labels(stops)


class SetupApi:
    """The wizard's handlers, for one host. Holds the search job (one at a
    time) and the stops found lately, in memory only. Thread-safe."""

    def __init__(
        self,
        target: Target,
        load_secrets: Callable[[], Mapping[str, str]],
        job_folder: Path | str,
        *,
        links: Sequence[tuple[str, str]] = (),
        lookup_postcode: Callable[[str], Place] = setup.lookup_postcode,
        search_address: Callable[[str], list[Place]] = setup.search_address,
        nearby_stations: Callable[..., list[Station]] = setup.nearby_stations,
        nearby_stops: Callable[..., list[Stop]] = setup.nearby_stops,
        walk_minutes: Callable[..., dict[str, tuple[int, str]]] = setup.walk_minutes,
        stations: Callable[[], Sequence[tuple[str, str, float, float]]] = setup.bundled_stations,
        make_job: MakeJob = discover.SetupJob,
        test_key: Callable[..., keytest.KeyResult] = keytest.test_key,
        key_tests: RateLimit = KEY_TESTS,
        install: Install = INSTALLS["pi"],
        view_links: Mapping[str, tuple[str, str]] | None = None,
        saved_message: str | None = None,
        title: str = TITLE,
        noun: str | None = None,
        keys: Sequence[tuple[str, str, str]] = GENERAL_KEYS,
        runner: Runner | None = None,
        back: str | None = None,
    ) -> None:
        """`target` is the overlay finishing saves into (store.Store.target),
        checked as each view on loads its settings. `load_secrets` gives
        the environment with every saved key, as the settings site's does;
        `job_folder` is where the search builds the bus timetable (the
        store, where a key pasted forgets its last test). `install` says which views can be turned on (its offered,
        and its default views while none are saved), what shows the
        departures (its noun, unless `noun` says) and how a save takes
        effect. The finished page offers `links`, (label, href) pairs like
        ("Settings", "/settings"), after `view_links`' link for each view
        on, like {"dashboard": ("The dashboard", "/")}; `saved_message`,
        if given, says what happens next instead of saved_message().
        `title` heads the page, `noun` is what its text calls what shows
        the departures ("board": "No trains on this board"), and `keys`,
        (name, label, hint) like schema.keys(), are the keys offered, saved
        and tested in that order, and no others. `runner`, if given, does
        the lookups that need the internet, the key tests and the search
        instead of this process (and the functions passed for them).
        `back`, a plain path like /settings, is where Back on a shortcut's
        first step goes; without it, that step has no Back."""
        noun = install.noun if noun is None else noun
        view_links = dict(view_links or {})
        for label, href in [*links, *view_links.values()]:
            if not label or len(label) > 40 or not _LINK.match(href):
                raise ValueError("A link is a short label and a plain path or http(s) address")
        if set(view_links) - set(schema.VIEWS):
            raise ValueError("A view's link is for a view")
        if not 0 < len(title) <= _TITLE_LENGTH or not title.isprintable():
            raise ValueError(f"A title is 1 to {_TITLE_LENGTH} ordinary characters")
        if not _NOUN.fullmatch(noun):
            raise ValueError("A noun is one lower-case word, like board")
        if not keys or any(not _ENV_NAME.match(name) for name, _, _ in keys):
            raise ValueError("Keys are named like environment variables")
        if not install.offered or set(install.offered) - set(schema.VIEWS):
            raise ValueError("An install offers at least one view, and only views")
        if back is not None and not (back.startswith("/") and _LINK.match(back)):
            raise ValueError("Back is a plain path on this site")
        self._back = back or ""
        self._title = title
        self._noun = noun
        self._install = install
        self._keys = tuple((name, label, hint) for name, label, hint in keys)
        self._key_names = tuple(name for name, _, _ in self._keys)
        self._load_secrets = load_secrets
        self._job_folder = Path(job_folder)
        self._links = [{"label": label, "href": href} for label, href in links]
        self._view_links = {view: {"label": label, "href": href} for view, (label, href) in view_links.items()}
        if runner is not None:
            lookup_postcode, search_address = runner.lookup_postcode, runner.search_address
            nearby_stops, walk_minutes = runner.nearby_stops, runner.walk_minutes
        self._runner = runner
        self._lookup_postcode = lookup_postcode
        self._search_address = search_address
        # The stations are a bundled list, so finding them never needs the runner.
        self._nearby_stations = nearby_stations
        self._nearby_stops = nearby_stops
        self._walk_minutes = walk_minutes
        self._stations = stations
        self._make_job = make_job
        self._test_key = test_key
        self._key_tests = key_tests
        self._target = target
        self._saved_message = saved_message
        self._lock = threading.Lock()
        self._found_stops: dict[str, Stop] = {}
        self._job: Job | None = None
        self._job_stops: dict[str, str] = {}
        self._job_routes = False
        self._job_key = ""
        self._handlers: dict[str, Callable[[object], dict]] = {
            "begin": self.begin,
            "saved": self.saved,
            "postcode": self.postcode,
            "address": self.address,
            "stations": self.stations,
            "station": self.station,
            "stops": self.stops,
            "trams": self.trams,
            "walk": self.walk,
            "start": self.start,
            "job": self.job,
            "cancel": self.cancel,
            "finish": self.finish,
            "testkey": self.testkey,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def handle(self, name: str, payload: object) -> Reply:
        """(status, body) for one request. 200 done; 400 bad input; 404 no
        such handler; 409 not now; 422 setup couldn't (a lookup failed, the
        settings wouldn't load); 429 too soon after the last; 500 a fault,
        said plainly."""
        handler = self._handlers.get(name)
        if handler is None:
            return 404, {"error": "No such setup step"}
        secrets = self._secret_values(payload)
        try:
            status, reply = 200, handler(payload)
        except Invalid as error:
            status, reply = 400, _error(error, secrets, error.field)
        except Conflict as error:
            status, reply = 409, _error(error, secrets)
        except TooSoon as error:
            status, reply = 429, _error(error, secrets)
        except (SetupError, OverlayError, discover.DiscoveryError) as error:
            status, reply = 422, _error(error, secrets)
        except Exception as error:  # noqa: BLE001 - a fault, shown without detail
            log.warning("Setup step %s failed: %s", name, type(error).__name__)
            status, reply = 500, {"error": "Something went wrong; try again"}
        # Belt and braces: no key, saved or just pasted, leaves in any answer.
        return status, _scrub(reply, secrets)

    def _secret_values(self, payload: object) -> list[str]:
        try:
            env = self._load_secrets()
            # Every key any host knows, not just this one's: scrubbing more does no harm.
            values = [str(env.get(name, "")).strip() for name in {*self._key_names, *keytest.KEY_NAMES}]
        except Exception:  # noqa: BLE001 - the step itself says if secrets are unreadable
            values = []
        with self._lock:
            values.append(self._job_key)  # pasted for the search, perhaps never saved
        if isinstance(payload, dict):
            pasted = payload.get("keys")
            if isinstance(pasted, dict):
                values += [v.strip() for v in pasted.values() if isinstance(v, str)]
            # A key pasted to test it, and an app ID pasted to test with it.
            values += [payload[f].strip() for f in ("value", "app_id") if isinstance(payload.get(f), str)]
        # A value this short can't be told from ordinary text, and isn't a usable key.
        return sorted({v for v in values if len(v) >= MIN_SCRUBBED}, key=len, reverse=True)

    # Finding home.

    def begin(self, payload) -> dict:
        """What the page needs first: its title, what it calls what shows
        the departures, the keys and which are set (never their values),
        the views offered with those on now, the links to show at the end,
        and any search already running."""
        _fields(payload)
        env = self._load_secrets()
        keys = [{"name": name, "label": label, "hint": hint, "set": bool(str(env.get(name, "")).strip()),
                 "doc": KEY_DOCS + _KEY_ANCHORS.get(name, "")} for name, label, hint in self._keys]
        on = self._views_now()
        data, new = self._settings_now()
        return {"title": self._title, "noun": self._noun, "keys": keys,
                "views": {"offered": view_choices(self._install.offered), "on": list(on)},
                "show": show_choices(self._install.offered, data, new),
                "modes": mode_choices(self._install.offered, data),
                "links": self._links_for(on), "back": self._back, "job": self._job_status()}

    def saved(self, payload) -> dict:
        """Today's choices, for the page to start from: roughly where home
        is (the weather's position, to about a kilometre), the station and
        the one trains must call at, by code and name, and the bus stops by
        label, with names where the bus timetable has them; each with its
        walk time, or None. A part switched off, or not set up, is None (or
        no stops). Only what the wizard asks about, never a key, and from
        local files only: the settings in force, the bundled station list
        and the timetable."""
        _fields(payload)
        data = self._settings()
        train, bus, weather = (_table(data, name) for name in ("train", "bus", "weather"))
        names = {code: name for name, code, _, _ in self._stations()}
        out: dict = {"home": None, "station": None, "filter": None, "stops": [], "trams": []}
        lat, lon = _saved_number(weather.get("lat")), _saved_number(weather.get("lon"))
        if lat is not None and lon is not None and setup.in_uk(lat, lon):
            out["home"] = {"lat": round(lat, 2), "lon": round(lon, 2)}
        station = _saved_station(train)
        if station in names:
            out["station"] = {"crs": station, "name": names[station], "walk": _saved_minutes(train.get("walk_minutes"))}
            towards = train.get("filter_crs")
            if isinstance(towards, str) and towards.upper() in names:
                out["filter"] = {"crs": towards.upper(), "name": names[towards.upper()]}
        metro = _table(data, "metro")
        names, london = setup.tram_stop_names(), setup.london_stops_by_code()
        walks = _table(metro, "walk_minutes")
        for label, code in _saved_trams(metro).items():
            kind = london[code].kind if code in london else "metrolink"
            out["trams"].append({"label": label, "code": code, "name": london[code].name if code in london
                                 else names.get(code, ""), "kind": kind, "walk": _saved_minutes(walks.get(label))})
        stops = _saved_stops(bus)
        if stops:
            walks = _table(bus, "walk_minutes")
            known = self._stop_names()
            for label, atco in stops.items():
                name, indicator = known.get(atco, ("", ""))
                out["stops"].append({"label": label, "atco": atco, "name": name, "indicator": indicator,
                                     "walk": _saved_minutes(walks.get(label))})
        return out

    def _stop_names(self) -> dict[str, tuple[str, str]]:
        """{ATCO code: (name, the word on its sign)} from the bus timetable in
        the store; empty if there is none. Read without its journeys, which a
        Pi's small web server hasn't the memory to hold."""
        schedule = timetable.load(self._job_folder, journeys=False)
        if schedule is None:
            return {}
        return {atco: (str(place.name)[:_NAME_LENGTH], str(place.indicator or "")[:_NAME_LENGTH])
                for atco, place in schedule.positions.items()}

    def _settings_now(self) -> tuple[dict, bool]:
        """The settings in force, and whether the install is still to be set
        up; ({}, False) if they don't read, so nothing is taken as new."""
        try:
            data = self._target.current()
        except (OSError, ConfigError):
            return {}, False
        return data, still_to_set_up(data)

    def _views_now(self) -> tuple[str, ...]:
        """The views on in the settings in force; the install's defaults if they don't read."""
        try:
            return schema.views(self._target.current(), self._install)
        except (OSError, ConfigError):
            return self._install.default_views

    def _links_for(self, views: Sequence[str]) -> list[dict]:
        """The finished page's links: each view's on, then the rest."""
        found = [dict(self._view_links[view]) for view in views if view in self._view_links]
        return found + [dict(link) for link in self._links]

    def postcode(self, payload) -> dict:
        fields = _fields(payload, ("postcode",))
        return {"place": _place(self._lookup_postcode(_text(fields["postcode"], "postcode", 5, _POSTCODE_LENGTH)))}

    def address(self, payload) -> dict:
        fields = _fields(payload, ("q",))
        query = _text(fields["q"], "q", *setup.ADDRESS_LENGTH)
        return {"places": [_place(p) for p in self._search_address(query)]}

    # Stations and stops.

    def stations(self, payload) -> dict:
        home = _point(_fields(payload, ("lat", "lon")))
        found = self._nearby_stations(home.lat, home.lon, limit=STATIONS_SHOWN, within_km=STATIONS_WITHIN_KM)
        return {"stations": [_station(s) for s in found]}

    def _known_station(self, value, field: str) -> Station:
        if not isinstance(value, str) or not _CRS.match(value.strip()):
            raise Invalid("A station code is 3 letters, like GLO", field)
        crs = value.strip().upper()
        for name, code, lat, lon in self._stations():
            if code == crs:
                return Station(name, code, lat, lon, 0)
        raise Invalid("No National Rail station has that code", field)

    def station(self, payload) -> dict:
        """A station by its code, to check one typed in."""
        fields = _fields(payload, ("crs",))
        return {"station": _station(self._known_station(fields["crs"], "crs"))}

    def stops(self, payload) -> dict:
        home = _point(_fields(payload, ("lat", "lon")))
        found = self._nearby_stops(home.lat, home.lon, within_m=STOPS_WITHIN_M, limit=STOPS_SHOWN)
        with self._lock:
            for stop in found:
                self._found_stops.pop(stop.atco, None)
                self._found_stops[stop.atco] = stop
            while len(self._found_stops) > _CACHED_STOPS:
                self._found_stops.pop(next(iter(self._found_stops)))
        labels = suggested_labels(found)
        return {"stops": [
            {"atco": s.atco, "name": s.name, "indicator": s.indicator, "bearing": s.bearing, "lat": s.lat,
             "lon": s.lon, "distance_m": s.distance_m, "label": label}
            for s, label in zip(found, labels, strict=True)
        ]}

    def trams(self, payload) -> dict:
        """The Metrolink tram stops nearest home, or London's Underground,
        DLR, tram, cable car and river bus stops, from the bundled lists (no
        network): none for a home far from both, and then the page skips
        the step. Each with its kind: "metrolink", or a London one's
        (setup.LONDON_KINDS)."""
        home = _point(_fields(payload, ("lat", "lon")))
        found = [*setup.nearby_tram_stops(home.lat, home.lon, limit=TRAM_STOPS_SHOWN),
                 *setup.nearby_london_stops(home.lat, home.lon, limit=LONDON_STOPS_SHOWN)]
        found.sort(key=lambda s: (s.distance_m, s.code))
        return {"trams": [{"code": s.code, "name": s.name, "kind": s.kind, "lat": s.lat, "lon": s.lon,
                           "distance_m": s.distance_m} for s in found]}

    def walk(self, payload) -> dict:
        """Minutes from home to the station, each stop and each tram stop:
        {key: {minutes, source}} by CRS, ATCO or tram stop code, source
        "route" or "estimate". A stop this setup hasn't found is left out,
        for the person to type."""
        fields = _fields(payload, ("home",), ("station_crs", "stop_atcos", "tram_codes"))
        home = _point(fields["home"])
        places: dict = {}
        if fields.get("station_crs") is not None:
            station = self._known_station(fields["station_crs"], "station_crs")
            places[station.crs] = station
        atcos = fields.get("stop_atcos", [])
        if not isinstance(atcos, list) or len(atcos) > MAX_BOARD_STOPS:
            raise Invalid(f"Ask about at most {MAX_BOARD_STOPS} stops", "stop_atcos")
        for atco in atcos:
            if not isinstance(atco, str) or not _ATCO.match(atco):
                raise Invalid("A stop is its NaPTAN code", "stop_atcos")
            with self._lock:
                stop = self._found_stops.get(atco)
            if stop is not None:
                places[atco] = stop
        codes = fields.get("tram_codes", [])
        if not isinstance(codes, list) or len(codes) > MAX_TRAM_STOPS:
            raise Invalid(f"Ask about at most {MAX_TRAM_STOPS} tram stops", "tram_codes")
        trams = {code: (name, lat, lon) for name, code, lat, lon in setup.bundled_tram_stops()}
        trams.update((s.code, (s.name, s.lat, s.lon)) for s in setup.bundled_london_stops())
        for code in codes:
            if not isinstance(code, str) or code not in trams:
                raise Invalid("A tram stop is its Metrolink stop code, or TfL's for a London stop", "tram_codes")
            name, lat, lon = trams[code]
            places[code] = setup.TramStop(code, name, lat, lon, 0)
        found = self._walk_minutes(home, places)
        return {"walk": {key: {"minutes": minutes, "source": source} for key, (minutes, source) in found.items()}}

    # The search for bus companies, which builds the timetable too.

    def _job_status(self) -> dict:
        with self._lock:
            job, stops = self._job, dict(self._job_stops)
        if job is None:
            return {"state": "idle"}
        status = job.status()
        out = {key: status.get(key) for key in ("state", "step", "done", "total")}
        out["message"] = safe_message(status["message"]) if status.get("message") else ""
        out["stops"] = stops
        result = status.get("result")
        if status.get("state") == "done" and isinstance(result, dict):
            out["found"] = {
                "operators": list(result["fragment"]["bus"]["operators"]),
                "lines": result.get("lines", {}),
                "stop_lines": result.get("stop_lines", {}),
                "routes": sorted(result["fragment"]["bus"].get("routes", {})),
                "timetable_journeys": result.get("timetable_journeys", 0),
                "notes": [safe_message(note) for note in result.get("notes", [])],
            }
            if isinstance(status.get("roads"), dict):
                out["roads"] = _roads(status["roads"])
        return out

    def start(self, payload) -> dict:
        """Start the search for `stops`; with `routes`, route strips too. The
        Bus Open Data Service key is the one pasted (in `keys`) or else the one
        saved. One search at a time; one done but still finding the roads for
        the maps (its status's `roads`) has them stopped."""
        fields = _fields(payload, ("stops", "routes"), ("walk", "keys"))
        stops = _stops(fields["stops"], least=1)
        routes = _flag(fields["routes"], "routes")
        walk = fields.get("walk", {})
        if not isinstance(walk, dict) or set(walk) - set(stops):
            raise Invalid("Walk times are by stop label", "walk")
        walk = {label: _minutes(m, "walk") for label, m in walk.items()}
        pasted = _keys(fields.get("keys", {}), self._key_names).get("BODS_API_KEY")
        api_key = pasted or str(self._load_secrets().get("BODS_API_KEY", "")).strip()
        if not api_key:
            raise Invalid("Paste your Bus Open Data Service key first; finding bus companies needs it",
                          "BODS_API_KEY")
        with self._lock:
            last = self._job.status() if self._job is not None else {}
            if last.get("state") in ("queued", "running"):
                raise Conflict("A search is already running; wait for it or cancel it")
            if self._job is not None and discover.roads_running(last):
                # Its timetable is about to be replaced. The roads it found are kept
                # for the new one, which waits for it to stop (the timetable's lock).
                self._job.cancel()
            if self._runner is not None:
                # The runner reads the saved key itself: only a pasted one goes to it.
                job = self._runner.make_job(stops, pasted or "", routes=routes, walk_minutes=walk)
            else:
                job = self._make_job(self._job_folder, stops, api_key, routes=routes, walk_minutes=walk)
            try:
                job.start()
            except discover.SetupBusy:
                # Another search on this folder, perhaps from the command line.
                raise Conflict(f"A setup search is already running for this {self._noun}") from None
            self._job, self._job_stops, self._job_routes, self._job_key = job, stops, routes, api_key
        return {"job": self._job_status()}

    def job(self, payload) -> dict:
        _fields(payload)
        return {"job": self._job_status()}

    def cancel(self, payload) -> dict:
        _fields(payload)
        with self._lock:
            job = self._job
        if job is not None:
            job.cancel()
        return {"job": self._job_status()}

    # Testing a key.

    def testkey(self, payload) -> dict:
        """Try one key with one harmless call: `value` if pasted (not saved),
        else the saved one. TransportAPI's app ID and key go together: with
        `app_id`, a pasted app ID, the app key is tested with it. That test
        spends one of TransportAPI's 30 lookups a day, so the page asks
        first. {state, message}, as keytest.KeyResult; 429 within
        KEY_TEST_EVERY seconds of the last test of that key."""
        fields = _fields(payload, ("name",), ("value", "app_id"))
        name = fields["name"]
        # Only this host's keys: the dashboard never spends a TransportAPI lookup.
        if not isinstance(name, str) or name not in self._key_names or name not in keytest.KEY_NAMES:
            raise Invalid("That isn't one of the keys set up here", "name")
        value = _key(fields.get("value", ""), name) or None
        app_id = None
        if "app_id" in fields:
            if name != "TAPI_APP_KEY":
                raise Invalid("Only the TransportAPI app key is tested with an app ID", "app_id")
            app_id = _key(fields["app_id"], "TAPI_APP_ID") or None
        result = self.test_key(name, value, app_id)
        return {"state": result.state, "message": result.message}

    def test_key(self, name: str, value: str | None = None, app_id: str | None = None) -> keytest.KeyResult:
        """Try key `name`, any keytest.KEY_NAMES, as testkey does: `value`
        if pasted, else the saved one, and `app_id` with TransportAPI's app
        key. The settings tabs test their own keys through here, so each
        key has one limit and, given a runner, the test runs there. TooSoon
        within KEY_TEST_EVERY seconds of the last test of that key; the
        runner's SetupError if it can't. The caller checks the values' shape."""
        if name not in keytest.KEY_NAMES:
            raise Invalid("That isn't a key this can test", "name")
        # TransportAPI's two keys are one test, so share one limit.
        wait = self._key_tests.take("TAPI" if name in keytest.COSTS_A_LOOKUP else name)
        if wait:
            raise TooSoon(f"Wait {math.ceil(wait)} s before testing this key again")
        if self._runner is not None:
            # The runner reads the saved keys and settings itself; only what was pasted goes to it.
            return self._runner.test_key(name, value, app_id)
        env = dict(self._load_secrets())
        if app_id:
            env["TAPI_APP_ID"] = app_id
        return self._test_key(name, value, self._settings(), env)

    def _settings(self) -> dict:
        """The settings in force for this host (its saved overlay included),
        for what a key test asks about; empty if they don't read."""
        try:
            return self._target.current()
        except (OSError, ConfigError, ValueError, TypeError):
            return {}

    # Saving.

    def finish(self, payload) -> dict:
        """Save what setup found into the settings overlay, merged with what is
        there, and any pasted keys into the secrets overlay. With `only`,
        "station" or "stops", save that part alone (a shortcut's): the
        station, where trains must call and its walk, or the stops and their
        walks; home, the views, what they show and the other part stay as
        saved."""
        only = payload.get("only") if isinstance(payload, dict) else None
        if only is not None and only not in PARTS:
            raise Invalid("Save the station or the stops", "only")
        fields = _fields(payload, *_FINISH_FIELDS[only])
        home = _point(fields["home"]) if only is None else None
        station = None
        if fields.get("station_crs") is not None:
            station = self._known_station(fields["station_crs"], "station_crs")
        filter_crs = ""
        if fields.get("filter_crs") not in (None, ""):
            filter_crs = self._known_station(fields["filter_crs"], "filter_crs").crs
            if station is None:
                raise Invalid("Pick a station before saying where you go from it", "filter_crs")
        stops = _stops(fields["stops"]) if only != "station" else {}
        trams = _trams(fields["trams"]) if fields.get("trams") is not None else {}
        current = self._settings() if only is not None else {}
        if only == "station" and station is None and not _saved_stops(_table(current, "bus")):
            raise Invalid("Pick a station: with no bus stops either, there is nothing to show", "station_crs")
        if only == "stops" and not stops and not _saved_station(_table(current, "train")):
            raise Invalid("Pick at least one stop: with no station either, there is nothing to show", "stops")
        if only is None and station is None and not stops and not trams:
            raise Invalid("Pick a station, a bus stop or a tram stop", "stops")
        walk = fields["walk"]
        if not isinstance(walk, dict):
            raise Invalid("Walk times are by station code and stop code", "walk")
        allowed = set(stops.values()) | set(trams.values()) | ({station.crs} if station else set())
        if set(walk) - allowed:
            raise Invalid("Walk times are only for your station and stops", "walk")
        walk = {key: (_minutes(m, "walk"), "given") for key, m in walk.items()}
        keys = _keys(fields.get("keys", {}), self._key_names)
        views = _views(fields["views"], self._install.offered) if "views" in fields else None
        on = tuple(views) if views is not None else self._views_now()
        presets = _show(fields["show"], on) if "show" in fields else {}
        picked = [key for key, chosen in (("train", station), ("bus", stops), ("metro", trams)) if chosen]
        shown_modes = {}
        if "modes" in fields:
            shown_modes = _modes(fields["modes"], on, picked, self._settings())

        changes = setup.config_fragment(home, station, [(label, self._stop(atco)) for label, atco in stops.items()],
                                        walk)
        # What isn't chosen is switched off rather than removed, which an overlay
        # can't do: config.toml's [train] or [bus] (the template's placeholders,
        # say) would otherwise stay in force. Choosing it again switches it on.
        if only != "stops":
            if station is not None:
                changes["train"].setdefault("walk_minutes", "")  # "" clears an old one, as the settings site does
                changes["train"]["filter_crs"] = filter_crs
                changes["train"]["enabled"] = True
            else:
                changes["train"] = {"enabled": False}
        if only != "station":
            if stops:
                changes["bus"].setdefault("walk_minutes", {})
                # A shortcut keeping the stops as saved, by the same labels, needs no
                # new search: their bus companies and route strips stay as they are.
                if only is None or stops != _saved_stops(_table(current, "bus")):
                    changes["bus"].update(self._found_bus(stops))
                changes["bus"]["enabled"] = True
            else:
                changes["bus"] = {"enabled": False}
        if only is None:
            # Trams: switched off rather than removed, as trains and buses are, when there were some.
            if trams:
                metro: dict = {"stops": trams, "enabled": True,
                               "walk_minutes": {label: walk[code][0] for label, code in trams.items() if code in walk}}
                changes["metro"] = metro
            elif "metro" in self._settings():
                changes["metro"] = {"enabled": False}
        if home is not None:
            division = setup.bank_holiday_division(home.country)
            if division:
                changes["bank_holiday_division"] = division
        if views is not None:
            # The target checks the settings as each of these views loads them.
            changes["app"] = {"views": views}
        if only is None:
            # A new install the page said nothing for (an older page, say) gets The usual.
            data, new = self._settings_now()
            for view in on:
                if view not in presets and new and not _has_show_list(data, view):
                    presets[view] = show.NEW_INSTALL
        for view, name in presets.items():
            changes.setdefault(view, {})["show"] = show_list(view, name)
        for view, chosen in shown_modes.items():
            changes.setdefault(view, {})["modes"] = chosen
        env = dict(self._load_secrets())
        save_to(self._target, changes, keys, env)
        keyresults.forget(self._job_folder, keys)  # a test of the old key says nothing of the new
        with self._lock:
            self._found_stops.clear()
        message = self._saved_message or saved_message(self._install, on)
        return {"saved": True, "message": message, "links": self._links_for(on)}

    def _stop(self, atco: str) -> Stop:
        with self._lock:
            found = self._found_stops.get(atco)
        # Every stop arrives labelled, so config_fragment needs only its code.
        return found or Stop(atco, atco, "", "", 0.0, 0.0, 0)

    def _found_bus(self, stops: dict[str, str]) -> dict:
        """[bus] source, operators and routes from the finished search for these stops."""
        with self._lock:
            job, searched, routes_asked = self._job, dict(self._job_stops), self._job_routes
        status = job.status() if job is not None else {}
        if status.get("state") != "done" or searched != stops:
            raise Conflict("Find the bus companies for these stops first")
        bus = status["result"]["fragment"]["bus"]
        found = {"source": bus["source"], "operators": list(bus["operators"])}
        if routes_asked:
            found["routes"] = dict(bus.get("routes", {}))
        else:
            # Route strips set up before stay if their stops still are; else they'd stop the board loading.
            current = self._current_routes()
            if any(not isinstance(r, dict) or r.get("stop") not in stops for r in current.values()):
                found["routes"] = {}
        return found

    def _current_routes(self) -> dict:
        try:
            routes = self._target.current().get("bus", {}).get("routes", {})
        except (OSError, ConfigError, AttributeError):
            return {}
        return routes if isinstance(routes, dict) else {}


def _table(data: Mapping, name: str) -> dict:
    """A table of the settings, or empty if it isn't one."""
    table = data.get(name)
    return table if isinstance(table, dict) else {}


def _saved_number(value) -> float | None:
    """A saved number as a float, or None if it isn't a finite one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _saved_station(train: Mapping) -> str:
    """[train] crs, upper case, if trains are on and it is a station code;
    else "" (the template's CHANGE_ME, say)."""
    crs = train.get("crs")
    if train.get("enabled") is False or not isinstance(crs, str) or not _CRS.match(crs):
        return ""
    return crs.upper()


def _saved_stops(bus: Mapping) -> dict[str, str]:
    """[bus] stops, {label: ATCO code}, if buses are on: only those shaped as
    the wizard saves them, at most MAX_BOARD_STOPS."""
    stops = bus.get("stops")
    if bus.get("enabled") is False or not isinstance(stops, dict):
        return {}
    found = [(label, atco) for label, atco in stops.items()
             if isinstance(label, str) and _LABEL.match(label) and isinstance(atco, str) and _ATCO.match(atco)]
    return dict(found[:MAX_BOARD_STOPS])


def _saved_trams(metro: Mapping) -> dict[str, str]:
    """[metro] stops, {label: tram stop code}, if trams are on: only those
    shaped as the wizard saves them, at most MAX_TRAM_STOPS."""
    stops = metro.get("stops")
    if metro.get("enabled") is False or not isinstance(stops, dict):
        return {}
    found = [(label, code) for label, code in stops.items() if isinstance(label, str) and _LABEL.match(label)
             and isinstance(code, str) and (setup.TRAM_STOP.match(code) or setup.LONDON_STOP.match(code))]
    return dict(found[:MAX_TRAM_STOPS])


def _saved_minutes(value) -> int | None:
    """A saved walk time the page can show, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WALK_MINUTES:
        return None
    return value


def _roads(roads: Mapping) -> dict:
    """A done job's roads for the page: running, done or cancelled, and two
    counts. Anything else reads as done, so the page never waits for it."""
    state = roads.get("state")
    done, total = (n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
                   for n in (roads.get("done"), roads.get("total")))
    return {"state": state if state in discover.ROADS_STATES else "done", "done": done, "total": total}


def _error(error: BaseException, secrets: Sequence[str], field: str | None = None) -> dict:
    # Scrubbed before safe_message shortens it, so a cut can't leave part of a key.
    out = {"error": safe_message(_scrub(str(error) or type(error).__name__, secrets))}
    if field:
        out["field"] = field
    return out


@overload
def _scrub(value: str, secrets: Sequence[str]) -> str: ...
@overload
def _scrub(value: dict, secrets: Sequence[str]) -> dict: ...
@overload
def _scrub(value: object, secrets: Sequence[str]) -> object: ...


def _scrub(value, secrets: Sequence[str]):
    """`value` with every secret replaced in every string, keys included."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "…")
        return value
    if isinstance(value, dict):
        return {_scrub(k, secrets): _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value
