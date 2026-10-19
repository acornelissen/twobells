"""The settings' Overview: how each feed is doing, your place in names, the
keys' state, and the ways to change things or open the dashboard.

Drawn on the server from local data only, as the rest of the settings
site is: the dashboard's snapshot (the pollers' Hub on Docker and a
desktop, the fetch service's mirrored copy on a Pi), the board's state
file on a Pi, the settings, the bundled station list and the bus
timetable in the store. Nothing here connects out. The snapshot and the
state file are written by other code from feeds on the internet, and a
Pi's comes through a shared folder, so every value is checked as it is
read, errors are made safe again (safe_message), and everything shown is
escaped.
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from ..core.errors import safe_message
from ..core.polling import PollPolicy
from ..settings import schema
from ..sources import tfl
from ..timetable import build as timetable
from ..timetable import trams
from . import places
from .icons import icon
from .keystate import KeyState, pill
from .status import SOURCES, as_dict, poll_policy, positive, switched_off, time_ago

# A feed's state: its pill's look, icon and words.
STATES = {
    "ok": ("pill-ok", "check", "Working"),
    "problem": ("pill-bad", "x", "Problem"),
    "waiting": ("pill-neutral", "info", "Waiting"),
    "resting": ("pill-neutral", "minus", "Resting"),
    "off": ("pill-neutral", "minus", "Not shown"),
}
# The longest error or note shown from a feed, as safe_message keeps them.
_TEXT_LENGTH = 160
# What a problem with each feed means for you.
MEANINGS = {
    "board_train": "The board's train times may be out of date.",
    "board_bus": "The board's bus times may be out of date.",
    "board_metro": "The board's tram times may be missing: they come from the tram timetable.",
    "trams": "The dashboard shows no trams until there is a tram timetable.",
    "board_metro_live": "The board's London times may be out of date: they come live from TfL.",
    "trams_live": "The dashboard's London times may be out of date: they come live from TfL.",
    "board_vehicles": "The board's bus times are from the timetable only, without live positions.",
    "trains": "The dashboard's train times may be out of date.",
    "train_positions": "The dashboard lists trains without saying where each one is.",
    "bus_timetable": "The dashboard shows no buses until there is a bus timetable.",
    "bus_times": "The dashboard's bus times may be out of date.",
    "bus_positions": "The dashboard's bus times are from the timetable only, without live positions.",
    "weather": "No rain warning, and the forecast may be out of date.",
    "engineering": "Engineering works may be missing from the dashboard.",
    "bus_disruptions": "Bus disruptions may be missing from the dashboard.",
    "updates": "The dashboard shows what it last had until they start again. Restarting the device usually helps.",
    "budget": "The board checks for buses again after midnight.",
}
# The web server's network guard (netguard.py), checked on a Pi as it starts
# and again as the Overview is drawn: its state, pill and what it means.
GUARD = {
    "on": ("ok", ("pill-ok", "check", "On"), "This server can't connect out to the internet."),
    "off": ("problem", ("pill-bad", "x", "Off"),
            "The server still refuses visitors outside your home network itself; nothing stops it connecting out."),
    "unknown": ("waiting", ("pill-neutral", "info", "Can't tell"),
                "The check couldn't see whether anything stops this server connecting out, most often as "
                "there was no route out yet. It looks again when this page is drawn, at most once a minute."),
}
# Loading the guard again empties its sets, so the services it holds restart after it (docs/security.md).
_GUARD_FIX = ("What to do", "On the Pi, load the guard again and restart the services it holds: sudo systemctl "
              "restart twobells-guard twobells-web twobells-fetch. If it stays off, sudo journalctl "
              "-u twobells-guard -u twobells-web says why.")
# A Host header as the server let it in: a name or an address, and a port.
_HOST = re.compile(r"(?:\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)(?::[0-9]{1,5})?")


@dataclass(frozen=True)
class Feed:
    """One feed's health, in words: its name, a STATES key, when it last
    updated, whether it is being checked now, and its last error, note and
    what a problem means, each "" for none. `extra` are more (term, words).
    `pill` is its pill's look, icon and words, when not its state's."""

    name: str
    state: str
    updated: str
    checking: str
    error: str = ""
    note: str = ""
    meaning: str = ""
    extra: tuple[tuple[str, str], ...] = ()
    pill: tuple[str, str, str] | None = None


# Reading what others wrote


def _text(value) -> str:
    """Words from a feed, made safe again and kept short; "" unless text."""
    if not isinstance(value, str):
        return ""
    return safe_message(value)[:_TEXT_LENGTH]


def _moment(value, zone: tzinfo | None) -> datetime | None:
    """A saved time as a naive local one: the board's are local already, the
    snapshot's carry their offset and are put in `zone`."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(zone).replace(tzinfo=None)
    return moment


def _updated(moment: datetime | None, now: datetime) -> str:
    return time_ago(moment, now) if moment is not None else "Never"


def _from_status(name: str, block, now: datetime, zone: tzinfo | None, checking: str, meaning: str,
                 resting: bool = False) -> Feed:
    """A feed from one of the snapshot's status blocks (snapshot.status_block)."""
    block = as_dict(block)
    success = _moment(block.get("last_success"), zone)
    error, note = _text(block.get("error")), _text(block.get("note"))
    if error or block.get("stale") is True:
        state = "problem"
    elif success is None:
        state = "waiting"
    else:
        state = "resting" if resting else "ok"
    return Feed(name, state, _updated(success, now), checking, error, note, meaning if state == "problem" else "")


def dashboard_feeds(snapshot: Mapping | None, now: datetime, zone: tzinfo | None, updating: bool = True) -> list[Feed]:
    """The dashboard's feeds from its snapshot (docs/dashboard-api.md); none
    without one. `updating` is False when its pollers have stopped."""
    if not isinstance(snapshot, Mapping):
        return []
    quiet = snapshot.get("quiet") is True
    checking = "No, it rests in its quiet hours unless the dashboard is open" if quiet else "Yes"
    found = []

    def add(name: str, block, meaning: str) -> None:
        found.append(_from_status(name, block, now, zone, checking, meaning, resting=quiet))

    if not updating:
        found.append(Feed("Dashboard updates", "problem", "Stopped", "No",
                          "The dashboard's checks have stopped", meaning=MEANINGS["updates"]))
    trains = snapshot.get("trains")
    if isinstance(trains, Mapping):
        add("Trains", trains.get("status"), MEANINGS["trains"])
        if isinstance(trains.get("positions"), Mapping):
            add("Where each train is", trains["positions"], MEANINGS["train_positions"])
    buses = snapshot.get("buses")
    if isinstance(buses, Mapping):
        found.extend(_bus_feeds(buses, now, zone, checking, quiet))
    trams = snapshot.get("metro")
    if isinstance(trams, Mapping):
        live = trams.get("live") is True
        # London's heading ("Underground"), one of those TfL's source gives; anything else is "Trams".
        label = trams.get("label")
        name = label if live and label in (*tfl.LABELS.values(), tfl.MIXED) else "Trams"
        add(str(name), trams.get("status"), MEANINGS["trams_live" if live else "trams"])
    weather = snapshot.get("weather")
    if isinstance(weather, Mapping):
        add("Weather", weather.get("status"), MEANINGS["weather"])
    disruptions = as_dict(as_dict(snapshot.get("disruptions")).get("status"))
    if isinstance(disruptions.get("train"), Mapping):
        add("Engineering works", disruptions["train"], MEANINGS["engineering"])
    if isinstance(disruptions.get("bus"), Mapping):
        add("Bus disruptions", disruptions["bus"], MEANINGS["bus_disruptions"])
    return found


def _bus_feeds(buses: Mapping, now: datetime, zone: tzinfo | None, checking: str, quiet: bool) -> list[Feed]:
    """The bus timetable and the live positions; or, where the buses come
    from TransportAPI (which the dashboard doesn't show), why there are none."""
    status = as_dict(buses.get("status"))
    positions = buses.get("positions")
    table = as_dict(buses.get("timetable"))
    if not isinstance(positions, Mapping) and table.get("state") == "missing" and not status.get("error"):
        return [Feed("Buses", "off", "Never", "No", note=_text(status.get("note")))]
    times = _from_status("Bus times", status, now, zone, checking, MEANINGS["bus_times"], resting=quiet)
    state, error, note, meaning = times.state, times.error, times.note, times.meaning
    if note.startswith("Timetabled times only"):
        note = ""  # the live positions' feed says so
    if table.get("state") == "building" and state != "problem":
        state = "waiting"
    elif table.get("state") == "missing":
        state, meaning = "problem", MEANINGS["bus_timetable"]
        error, note = error or note or "There is no bus timetable yet", ""
    built = _moment(table.get("built"), zone)
    extra = (("Timetable built", time_ago(built, now)),) if built is not None else ()
    found = [Feed("Bus times", state, times.updated, checking, error, note, meaning, extra)]
    if isinstance(positions, Mapping):
        found.append(_from_status("Live bus positions", positions, now, zone, checking, MEANINGS["bus_positions"],
                                  resting=quiet))
    return found


def board_feeds(data: dict, state: dict | None, now: datetime,
                holidays: frozenset[date] = frozenset()) -> list[Feed]:
    """The board's feeds and its display, from the state file it saves
    (None if it can't be read), as the Board page's status panel reads it."""
    if state is None:
        return [Feed("Board", "problem", "Can't tell", "Can't tell", "The board's state file can't be read",
                     meaning="The board may not be showing departures.")]
    if not state:
        return [Feed("Board", "waiting", "Never", "Can't tell",
                     note="The board saves its state on its first run, within a minute or two of starting.")]
    if data.get("bank_holidays", True) is not True:
        holidays = frozenset()
    entries = as_dict(state.get("sources"))
    found = []
    for name, section_name, heading, stale_default in SOURCES:
        if (section_name not in data and name not in entries) or switched_off(data, section_name):
            continue
        section = as_dict(data.get(section_name))
        # No windows saved: the board checks at its defaults (BODS buses all day).
        defaults = schema.bus_defaults(schema.bus_source(section)) if name == "bus" else schema.BOARD_DEFAULTS[name]
        policy = poll_policy({"windows": list(defaults["windows"]), **section}, holidays)
        stale_after = positive(section, "stale_after_minutes", stale_default)
        extra, over = (), False
        if name == "bus" and schema.bus_source(data.get("bus")) == "transportapi":
            extra, over = _budget(data, state, now)
        meaning = MEANINGS["board_metro_live" if name == "metro" and schema.london_metro(data) else f"board_{name}"]
        found.append(_board_feed(heading, as_dict(entries.get(name)), policy, stale_after, now, meaning, extra, over))
    vehicles = as_dict(state.get("vehicles"))
    if vehicles and not switched_off(data, "bus"):
        # Saved only alongside fetched departures, so too old to judge "not updating" by.
        bus_policy = poll_policy(as_dict(data.get("bus")), holidays)
        found.append(_board_feed("Live bus positions", vehicles, bus_policy, None, now, MEANINGS["board_vehicles"]))
    drawn = _moment(as_dict(state.get("last_render")).get("at"), None)
    found.append(Feed("Display", "ok" if drawn else "waiting", _updated(drawn, now), ""))
    return found


def _board_feed(heading: str, entry: dict, policy: PollPolicy | None, stale_after: int | None, now: datetime,
                meaning: str, extra: tuple[tuple[str, str], ...] = (), over: bool = False) -> Feed:
    success = _moment(entry.get("last_success"), None)
    active = policy is not None and policy.is_active(now)
    if policy is None:
        checking = "Can’t tell: its checking times don’t read"
    elif active:
        checking = "Yes"
    else:
        checking = "No, outside its checking times"
    error = ""
    last_error = as_dict(entry.get("last_error"))
    failed = _moment(last_error.get("at"), None)
    if failed and (success is None or failed > success):
        error = f"{time_ago(failed, now)}: {_text(last_error.get('message')) or 'no details'}"
    if active and stale_after is not None and (success is None or now - success > timedelta(minutes=stale_after)):
        error = error or f"Not updating: nothing new for over {stale_after} min"
    if over:
        error = error or "Every TransportAPI lookup for today is used"
        meaning = MEANINGS["budget"]
    if error:
        state = "problem"
    elif success is None:
        state = "waiting"
    else:
        state = "ok" if active else "resting"
    return Feed(heading, state, _updated(success, now), checking, error, _text(entry.get("note")),
                meaning if error else "", extra)


def _budget(data: dict, state: dict, now: datetime) -> tuple[tuple[tuple[str, str], ...], bool]:
    """The TransportAPI lookups used today, and whether they are all used."""
    limit = positive(as_dict(data.get("bus")), "daily_budget", 28)
    spent = as_dict(as_dict(state.get("budgets")).get("bus"))
    used = spent.get("used", 0) if spent.get("day") == now.date().isoformat() else 0
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return (), False
    return (("TransportAPI lookups today", f"{used} of {limit}"),), used >= limit


def guard_feed(found: str, checked: datetime, now: datetime) -> Feed:
    """The network guard's line in the health, from netguard.probe() when it
    was `checked` (local, as `now`): On is working, Off a problem, and Can't
    tell neither."""
    state, pill, meaning = GUARD.get(found, GUARD["unknown"])
    when = ("Checked", time_ago(checked, now))
    extra = (when, _GUARD_FIX) if state == "problem" else (when,)
    return Feed("Network guard", state, "", "", meaning=meaning, extra=extra, pill=pill)


# Drawing


def _pill(look: str, shape: str, words: str) -> str:
    return f'<span class="pill {look}">{icon(shape)} {html.escape(words)}</span>'


def _row(term: str, detail: str) -> str:
    """A <div><dt><dd> of a dl.readonly; `detail` is already HTML."""
    return f"<div><dt>{html.escape(term)}</dt><dd>{detail}</dd></div>"


def _feed(feed: Feed) -> str:
    e = html.escape
    rows = []
    if feed.updated:
        rows.append(_row("Last redrawn" if feed.name == "Display" else "Last update", e(feed.updated)))
    if feed.checking:
        rows.append(_row("Checking now", e(feed.checking)))
    if feed.error:
        rows.append(_row("Last error", e(feed.error)))
    if feed.note:
        rows.append(_row("Note", e(feed.note)))
    rows.extend(_row(term, e(words)) for term, words in feed.extra)
    if feed.meaning:
        rows.append(_row("What it means", e(feed.meaning)))
    return (f'<li class="feed feed-{feed.state}"><p class="feed-name"><strong>{e(feed.name)}</strong> '
            f'{_pill(*(feed.pill or STATES[feed.state]))}</p><dl class="readonly">{"".join(rows)}</dl></li>')


def render_health(groups: Sequence[tuple[str, list[Feed]]], now: datetime, reload: str, problems: int = 0,
                  note: str = "") -> str:
    """The health card: one line saying whether all is working, as of when,
    with a link to reload, and `note` (words) under it; then each group's
    feeds, under the group's name when there is more than one. While all is
    working the feeds wait behind Show each feed, so the rest of the page
    comes up; with a problem they show. `problems` counts any found elsewhere."""
    feeds = [feed for _, group in groups for feed in group]
    problems += sum(feed.state == "problem" for feed in feeds)
    if problems:
        line = _pill("pill-bad", "triangle-alert", f"{problems} problem{'s' if problems > 1 else ''}")
    elif feeds:
        line = _pill("pill-ok", "check", "All working")
    else:
        line = _pill("pill-neutral", "info", "Nothing checked yet")
    out = ['<section class="health card" aria-labelledby="health-title"><h2 id="health-title">Health</h2>',
           f'<p class="health-line">{line} <span>As of {now:%H:%M}.</span> '
           f'<a href="{html.escape(reload)}">Reload</a></p>']
    if note:
        out.append(f'<p class="hint">{html.escape(note)}</p>')
    shown = [(name, group) for name, group in groups if group]
    folded = bool(shown) and not problems
    if folded:
        out.append('<details class="feeds-more"><summary>Show each feed</summary>')
    for name, group in shown:
        if len(shown) > 1:
            out.append(f"<h3>{html.escape(name)}</h3>")
        out.append(f'<ul class="feeds">{"".join(_feed(feed) for feed in group)}</ul>')
    if folded:
        out.append("</details>")
    if not shown:
        out.append("<p>Nothing has been checked yet. This page shows how each feed is doing once there is "
                   "something to show.</p>")
    out.append("</section>")
    return "".join(out)


def _minutes(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 60:
        return None
    return value


def _walk(minutes: int | None) -> str:
    return f", {minutes} min walk" if minutes is not None else ""


def _off(section: dict) -> bool:
    return section.get("enabled") is False


def _station_line(train: dict) -> str:
    """"Glossop (GLO), 12 min walk; only trains calling at Manchester Piccadilly (MAN)"."""
    crs = train.get("crs")
    if not isinstance(crs, str) or not schema.CRS.fullmatch(crs):
        return ""
    line = places.station(crs.upper()) + _walk(_minutes(train.get("walk_minutes")))
    towards = train.get("filter_crs")
    if isinstance(towards, str) and schema.CRS.fullmatch(towards):
        line += f"; only trains calling at {places.station(towards.upper())}"
    return line + ("; switched off" if _off(train) else "")


def stops_of(bus: dict) -> dict[str, str]:
    """[bus] stops as {label: stop code}, only those shaped as setup saves them."""
    stops = bus.get("stops")
    if not isinstance(stops, dict):
        return {}
    return {label: code for label, code in stops.items()
            if isinstance(label, str) and schema.LABEL.fullmatch(label) and isinstance(code, str)
            and schema.ATCO.fullmatch(code)}


def stop_walks(bus: dict, labels: Sequence[str]) -> dict[str, int | None]:
    """The walk to each stop by label: [bus] walk_minutes, one number for
    every stop or a table by label (with "*" for any other)."""
    walks = bus.get("walk_minutes")
    if isinstance(walks, dict):
        every = _minutes(walks.get("*"))
        return {label: _minutes(walks[label]) if label in walks else every for label in labels}
    return dict.fromkeys(labels, _minutes(walks))


def _timetable_rows(folder: Path, bus: dict, stops: dict[str, str], now: datetime, zone: tzinfo) -> list[str]:
    """When the bus timetable was saved, with a warning only when it is due
    to be built again (timetable.due); and how the roads for the maps are
    getting on while they are being found."""
    e = html.escape
    path = folder / timetable.FILE_NAME
    try:
        saved = datetime.fromtimestamp(path.stat().st_mtime, zone).replace(tzinfo=None)
    except OSError:
        rows = [_row("Bus timetable", "Not built yet")]
    else:
        found = places.schedule(folder)
        operators = bus.get("operators")
        if not (isinstance(operators, list) and all(isinstance(o, str) for o in operators)):
            operators = None
        reason = "it can't be read" if found is None else timetable.due(found, stops, now, operators)
        words = e(f"Saved {time_ago(saved, now)}")
        if reason:
            due = f"Due to be built again: {reason}. It is built again by itself; if this stays, the log says why."
            words = f'{_pill("pill-warn", "triangle-alert", "Out of date")} {words}. {e(due)}'
        rows = [_row("Bus timetable", words)]
    progress = timetable.roads_progress(folder)
    if progress is not None:
        rows.append(_row("Bus map roads", f"Finding them ({progress[0]} of {progress[1]})"))
    return rows


def _tram_rows(metro: dict, folder: Path, now: datetime, zone: tzinfo) -> list[str]:
    """The tram stops with their names and walks, and when the tram timetable
    was saved, with a warning when it is due to be built again."""
    e = html.escape
    stops = {label: code for label, code in as_dict(metro.get("stops")).items()
             if isinstance(label, str) and isinstance(code, str)}
    names = places.tram_stop_names()
    walks = stop_walks(metro, list(stops))
    items = "".join(f"<li>{e(label)}: {e(names.get(code, code))}{_walk(walks[label])}</li>"
                    for label, code in stops.items())
    off = '<p class="hint">Switched off.</p>' if _off(metro) else ""
    rows = [_row("Tram stops", f'<ul class="stops">{items}</ul>{off}')]
    if _off(metro):
        return rows
    if schema.london_metro({"metro": metro}):
        return [*rows, _row("Times", "Live from TfL: no timetable to build.")]
    try:
        saved = datetime.fromtimestamp((folder / trams.FILE_NAME).stat().st_mtime, zone).replace(tzinfo=None)
    except OSError:
        return [*rows, _row("Tram timetable", "Not built yet. It is built within the hour.")]
    found = trams.load(folder)
    reason = "it can't be read" if found is None else trams.due(found, stops, now)
    words = e(f"Saved {time_ago(saved, now)}")
    if reason:
        due = f"Due to be built again: {reason}. It is built again by itself; if this stays, the log says why."
        words = f'{_pill("pill-warn", "triangle-alert", "Out of date")} {words}. {e(due)}'
    return [*rows, _row("Tram timetable", words)]


def render_place(data: dict, folder: Path, now: datetime, zone: tzinfo) -> str:
    """Your place in names, from local data only: the station and the one
    trains must call at, the stops with their names, the bus timetable's
    freshness, home and the weather."""
    e = html.escape
    train, bus, weather = (as_dict(data.get(name)) for name in ("train", "bus", "weather"))
    rows = []
    station = _station_line(train) if "train" in data else ""
    rows.append(_row("Station", e(station) if station else "Not set up"))
    stops = stops_of(bus) if "bus" in data else {}
    if stops:
        names = places.stop_names(folder)
        walks = stop_walks(bus, list(stops))
        items = "".join(f"<li>{e(label)}: {e(names.get(code, code))}{_walk(walks[label])}</li>"
                        for label, code in stops.items())
        off = '<p class="hint">Switched off.</p>' if _off(bus) else ""
        rows.append(_row("Bus stops", f'<ul class="stops">{items}</ul>{off}'))
        if not _off(bus) and schema.bus_source(bus) == "bods":
            rows.extend(_timetable_rows(folder, bus, stops, now, zone))
    else:
        rows.append(_row("Bus stops", "Not set up"))
    if "metro" in data:
        rows.extend(_tram_rows(as_dict(data.get("metro")), folder, now, zone))
    home = "lat" in weather and "lon" in weather
    rows.append(_row("Home", "Set (to about 1 km)" if home else "Not set"))
    if "weather" in data:
        rows.append(_row("Weather", "Off" if _off(weather) else "On"))
    return ('<section class="place card" aria-labelledby="place-title"><h2 id="place-title">Your place</h2>'
            f'<dl class="readonly">{"".join(rows)}</dl></section>')


def render_keys(states: Sequence[KeyState], keys_page: str) -> str:
    """One line for each key the views on use, headed and said as on the Keys
    page (web/keystate.py's key_states), so the two pages agree; never any
    part of a key."""
    e = html.escape
    rows = []
    for state in states:
        optional = state.state == "not_set" and state.form.optional
        rows.append(_row(state.form.feature, pill(state) + (" Optional." if optional else "")))
    return ('<section class="keys card" aria-labelledby="keys-title"><h2 id="keys-title">Keys</h2>'
            '<p class="hint lead">Keys are never shown here.</p>'
            f'<dl class="readonly">{"".join(rows)}</dl>'
            f'<p><a href="{e(keys_page)}">Change or test keys</a></p></section>')


def kiosk_address(host: str | None) -> str | None:
    """The wall tablet's address on the host the browser used (the Host
    header, already let in by the server), or None when that can't reach
    this device from another one: this computer's own name or address, or a
    header not shaped as one."""
    if not host or not _HOST.fullmatch(host.strip()):
        return None
    host = host.strip()
    name = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    if name.lower() == "localhost" or name.lower().endswith(".localhost"):
        return None
    try:
        if ipaddress.ip_address(name).is_loopback:
            return None
    except ValueError:
        pass
    return f"http://{host}/?kiosk"


def render_actions(data: dict, setup: str, dashboard: bool, host: str | None) -> str:
    """The ways to change your place with setup (all of it, or the stops or
    the station alone), to open the dashboard while it runs, and the wall
    tablet's address."""
    e = html.escape
    items = [f'<li><a href="{e(setup)}">Change home, station or stops</a></li>']
    if "bus" in data:
        items.append(f'<li><a href="{e(setup)}?step=stops">Change stops only</a></li>')
    if "train" in data:
        items.append(f'<li><a href="{e(setup)}?step=station">Change station only</a></li>')
    if dashboard:
        items.append('<li><a href="/">Open the dashboard</a></li>')
    out = ['<section class="actions card" aria-labelledby="actions-title"><h2 id="actions-title">Shortcuts</h2>',
           f'<ul class="shortcuts">{"".join(items)}</ul>',
           '<p class="hint">Setup goes step by step, with walk times and the bus companies; '
           "the shortcuts start it at that step with today's choices.</p>"]
    if dashboard:
        address = kiosk_address(host)
        out.append('<h3 id="kiosk-title">Wall tablet</h3>')
        if address:
            out.append(f'<p>Open <code>{e(address)}</code> on a tablet on your home network. It shows the next '
                       "departures in large type, dimmed in the quiet hours.</p>")
        else:
            out.append("<p>On a tablet on your home network, open this device's address on that network with "
                       "<code>/?kiosk</code> on the end. It shows the next departures in large type, dimmed in the "
                       "quiet hours. This page was opened on this computer itself, so its address here won't work "
                       "on the tablet.</p>")
    out.append("</section>")
    return "".join(out)
