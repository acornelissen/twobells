"""The setup wizard's input checks, and the parts of its replies.

Every field a request to setup/api.py sends is checked here, strictly:
unknown fields, wrong types, overlong text and positions outside the UK are
refused with Invalid, whose text names the field and never the value. The
spool's checks (setup/spool/checks.py) check the stops the same way.

Also here: what `begin` offers for the views, what they show and the modes
they show (view_choices, show_choices, mode_choices); what `saved` reads
from the settings in force (table, saved_station, saved_stops and the
rest), only in the shapes the wizard saves; and the replies' places,
stations, roads and errors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..core.bounded import finite
from ..core.errors import ConfigError, safe_message, scrub_secrets
from ..core.places import _CONTROL, LONDON_STOP, TRAM_STOP, in_uk, tram_stop_names
from ..settings import modes, schema, show
from ..settings.installs import Install
from ..settings.schema import ATCO as _ATCO
from ..settings.schema import CRS as _CRS
from ..settings.schema import KEY_VALUE as _KEY_VALUE
from ..settings.schema import LABEL as _LABEL
from . import discover
from . import lookups as setup
from .lookups import Place, Station, Stop

MAX_TRAM_STOPS = schema.MAX_TRAM_STOPS
MAX_BOARD_STOPS = setup.MAX_BOARD_STOPS
MAX_WALK_MINUTES = 60
_COUNTRIES = ("England", "Wales", "Scotland", "Northern Ireland")

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


# Checking input. Messages name the field, never echo the value.


def fields(payload, required: Sequence[str] = (), optional: Sequence[str] = ()) -> dict:
    if not isinstance(payload, dict):
        raise Invalid("Send a JSON object")
    unknown = set(payload) - set(required) - set(optional)
    if unknown:
        raise Invalid("That request has fields it shouldn't", sorted(unknown)[0][:40])
    for name in required:
        if name not in payload:
            raise Invalid(f"That request needs {name}", name)
    return payload


def text(value, field: str, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise Invalid("Send text", field)
    value = value.strip()
    if not low <= len(value) <= high or _CONTROL.search(value):
        raise Invalid(f"Use {low} to {high} ordinary characters", field)
    return value


def _number(value, field: str) -> float:
    number = finite(value)
    if number is None:
        raise Invalid("A position must be two numbers", field)
    return number


def point(value, field: str = "home") -> Place:
    """A position in the UK, with an optional country for the bank holidays."""
    sent = fields(value, ("lat", "lon"), ("country",))
    lat, lon = _number(sent["lat"], field), _number(sent["lon"], field)
    if not in_uk(lat, lon):
        raise Invalid("That position is outside the UK", field)
    country = sent.get("country", "")
    if country not in ("", *_COUNTRIES):
        raise Invalid("That isn't a UK country", field)
    return Place(lat, lon, "", country)


def flag(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise Invalid("Send true or false", field)
    return value


def minutes(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WALK_MINUTES:
        raise Invalid(f"A walk is a whole number of minutes from 0 to {MAX_WALK_MINUTES}", field)
    return value


def stops(value, field: str = "stops", least: int = 0) -> dict[str, str]:
    """{label: ATCO code}: labels as the settings site allows, each stop once."""
    if not isinstance(value, dict) or not least <= len(value) <= MAX_BOARD_STOPS:
        raise Invalid(f"Pick {least} to {MAX_BOARD_STOPS} stops", field)
    stops = {}
    for label, atco in value.items():
        if not _LABEL.fullmatch(label):
            raise Invalid("A stop label is 1 to 3 letters or digits", field)
        if not isinstance(atco, str) or not _ATCO.fullmatch(atco):
            raise Invalid(f"Stop {label} needs a NaPTAN stop code", field)
        stops[label] = atco
    if len({label.upper() for label in stops}) != len(stops):
        raise Invalid("Two stops have the same label", field)
    if len(set(stops.values())) != len(stops):
        raise Invalid("Each stop must be a different stop", field)
    return stops


def trams(value, field: str = "trams") -> dict[str, str]:
    """{label: tram stop code}: Metrolink stops, each once, labelled as the settings site allows."""
    if not isinstance(value, dict) or len(value) > MAX_TRAM_STOPS:
        raise Invalid(f"Pick at most {MAX_TRAM_STOPS} tram stops", field)
    trams = {}
    for label, code in value.items():
        if not _LABEL.fullmatch(label):
            raise Invalid("A tram stop label is 1 to 3 letters or digits", field)
        if (not isinstance(code, str) or not (TRAM_STOP.fullmatch(code) or LONDON_STOP.fullmatch(code))
                or code not in tram_stop_names()):
            raise Invalid(f"Tram stop {label} needs a Metrolink stop code, or TfL's for a London stop", field)
        trams[label] = code
    if len({bool(LONDON_STOP.fullmatch(code)) for code in trams.values()}) > 1:
        raise Invalid("Pick Metrolink stops or London ones, not both", field)
    if len({label.upper() for label in trams}) != len(trams) or len(set(trams.values())) != len(trams):
        raise Invalid("Pick each tram stop once, with a label of its own", field)
    return trams


def key(value, field: str) -> str:
    """A pasted key in the shape of one, or "" for none."""
    if not isinstance(value, str):
        raise Invalid("Paste the key as text", field)
    key = value.strip()
    if key and not _KEY_VALUE.fullmatch(key):
        raise Invalid("Paste the key exactly; it has only letters, digits and . _ ~ + / = -", field)
    return key


def views(value, offered: Sequence[str]) -> list[str]:
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


def presets(value, on: Sequence[str]) -> dict[str, str]:
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


def has_show_list(data: Mapping, view: str) -> bool:
    section = data.get(view)
    return isinstance(section, dict) and "show" in section


def show_choices(offered: Sequence[str], data: Mapping, new: bool) -> dict:
    """The presets, and for each view offered what each shows there and the
    one to start at (`now`), by settings `data`; `new` if the install is
    still to be set up."""
    views = {}
    for view in offered:
        now = show.NEW_INSTALL if new else KEEP
        if has_show_list(data, view):
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


def view_modes(value, on: Sequence[str], picked: Sequence[str], data: Mapping) -> dict[str, list[str]]:
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


def keys(value, names: Sequence[str]) -> dict[str, str]:
    """Keys to save: only the host's (`names`), each in the shape of a key; empty ones skipped."""
    if not isinstance(value, dict):
        raise Invalid("Send keys as an object", "keys")
    found = {}
    for name, pasted in value.items():
        if name not in names:
            raise Invalid("That isn't one of the keys set up here", "keys")
        pasted = key(pasted, name)
        if pasted:
            found[name] = pasted
    return found


def place(place: Place) -> dict:
    return {"lat": place.lat, "lon": place.lon, "label": place.label, "country": place.country,
            "district": place.admin_district}


def station(station: Station) -> dict:
    return {"name": station.name, "crs": station.crs, "lat": station.lat, "lon": station.lon,
            "distance_m": station.distance_m}


def suggested_labels(stops: Sequence[Stop]) -> list[str]:
    """A label for each stop found, as setup.stop_labels picks them: the letter
    on its sign where it has one of its own, else one on no sign here."""
    return setup.default_labels(stops)


# The settings in force, as `saved` reads them; and the replies' roads and errors.


def table(data: Mapping, name: str) -> dict:
    """A table of the settings, or empty if it isn't one."""
    table = data.get(name)
    return table if isinstance(table, dict) else {}


def saved_number(value) -> float | None:
    """A saved number as a float, or None if it isn't a finite one."""
    return finite(value)


def saved_station(train: Mapping) -> str:
    """[train] crs, upper case, if trains are on and it is a station code;
    else "" (the template's CHANGE_ME, say)."""
    crs = train.get("crs")
    if train.get("enabled") is False or not isinstance(crs, str) or not _CRS.fullmatch(crs):
        return ""
    return crs.upper()


def saved_stops(bus: Mapping) -> dict[str, str]:
    """[bus] stops, {label: ATCO code}, if buses are on: only those shaped as
    the wizard saves them, at most MAX_BOARD_STOPS."""
    stops = bus.get("stops")
    if bus.get("enabled") is False or not isinstance(stops, dict):
        return {}
    found = [(label, atco) for label, atco in stops.items()
             if isinstance(label, str) and _LABEL.fullmatch(label) and isinstance(atco, str) and _ATCO.fullmatch(atco)]
    return dict(found[:MAX_BOARD_STOPS])


def saved_trams(metro: Mapping) -> dict[str, str]:
    """[metro] stops, {label: tram stop code}, if trams are on: only those
    shaped as the wizard saves them, at most MAX_TRAM_STOPS."""
    stops = metro.get("stops")
    if metro.get("enabled") is False or not isinstance(stops, dict):
        return {}
    found = [(label, code) for label, code in stops.items() if isinstance(label, str) and _LABEL.fullmatch(label)
             and isinstance(code, str) and (TRAM_STOP.fullmatch(code) or LONDON_STOP.fullmatch(code))]
    return dict(found[:MAX_TRAM_STOPS])


def saved_minutes(value) -> int | None:
    """A saved walk time the page can show, or None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_WALK_MINUTES:
        return None
    return value


def roads(roads: Mapping) -> dict:
    """A done job's roads for the page: running, done or cancelled, and two
    counts. Anything else reads as done, so the page never waits for it."""
    state = roads.get("state")
    done, total = (n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0
                   for n in (roads.get("done"), roads.get("total")))
    return {"state": state if state in discover.ROADS_STATES else "done", "done": done, "total": total}


def error(error: BaseException, secrets: Sequence[str], field: str | None = None) -> dict:
    # Scrubbed before safe_message shortens it, so a cut can't leave part of a key.
    out = {"error": safe_message(scrub_secrets(str(error) or type(error).__name__, secrets))}
    if field:
        out["field"] = field
    return out
