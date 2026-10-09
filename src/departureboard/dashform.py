"""The dashboard's everyday settings form: what it edits, and saving.

The board's form (settingsform.py) edits a board: its display, look and
checking windows, and keys for TransportAPI, which the dashboard never uses.
This one edits what the dashboard runs on, with the same field checks and
markup: the stations and stops, walk times, how many rows and how often to
check ([dashboard]), the weather, and the dashboard's keys.

Saving merges the fields into the dashboard's own overlay (see
dashconfig.overlay_target): nothing is written unless every field passes
its check and the dashboard's settings then load. Keys are write-only: the
page says whether one is set, never its value.
"""

from __future__ import annotations

import html
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .config import ConfigError
from .dashconfig import DashboardOptions
from .disruptions import KB_URL_PLACEHOLDER, kb_url_is_placeholder
from .holidays import DIVISIONS, ENGLAND_AND_WALES
from .overlay import OverlayError, Target, save_to
from .schedule import parse_window
from .settingsform import (
    _CONTROL,
    _KEY_VALUE,
    Field,
    Invalid,
    _choice,
    _crs,
    _directions,
    _number,
    _shown,
    _stops,
    _text,
    _walk_per_stop,
)

KEYS = (
    ("RDM_API_KEY", "Rail Data Marketplace key", "Trains: the consumer key for Live Departure Board."),
    ("BODS_API_KEY", "Bus Open Data Service key", "Buses: their timetable, live positions and disruptions."),
    ("RDM_DETAILS_API_KEY", "Rail Data Marketplace Service Details key",
     "Optional: shows where each of the next few trains is."),
    ("RDM_KB_API_KEY", "Rail Data Marketplace Knowledgebase Incidents key",
     "Optional: engineering works, with the Knowledgebase address under Trains."),
)
_MAX_URL = 300
_DEFAULTS = DashboardOptions()
QUIET_DEFAULT = ("Mon-Sun 01:00-05:00",)  # DashboardOptions.quiet_windows, as written


def _quiet_windows(raw: str) -> list[str]:
    """Windows one per line, like the board's checking times, but none is fine:
    then the dashboard checks all day and night."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    for line in lines:
        try:
            parse_window(line)
        except ValueError:
            raise Invalid(f"“{line[:40]}” isn't a window like Mon-Sun 01:00-05:00") from None
    return lines


def _https(raw: str) -> str:
    """An https address, or empty for none (stored as the placeholder, which
    keeps engineering works off)."""
    value = raw.strip()
    if not value:
        return KB_URL_PLACEHOLDER
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        parts = None
    if (parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or len(value) > _MAX_URL or _CONTROL.search(value) or any(c.isspace() for c in value)):
        raise Invalid("Enter an https address like https://example.com/path, or leave it empty")
    return value


def _on_off(raw: str) -> bool:
    return _choice(("true", "false"))(raw) == "true"


FIELDS = {f.name: f for f in (
    Field("title", None, "title", "Title", _text(60), hint="Shown at the top of the dashboard."),
    Field("bank_holidays", None, "bank_holidays", "Bank holidays", _on_off, "select",
          hint="Trains and buses run a Sunday service on bank holidays. Dates come from GOV.UK.",
          options=(("true", "Treat bank holidays like Sundays"), ("false", "Ignore bank holidays"))),
    Field("bank_holiday_division", None, "bank_holiday_division", "Bank holidays for", _choice(DIVISIONS), "select",
          hint="The part of the UK you live in; each has its own bank holidays.",
          options=(("england-and-wales", "England and Wales"), ("scotland", "Scotland"),
                   ("northern-ireland", "Northern Ireland"))),
    Field("quiet_windows", "dashboard", "quiet_windows", "When not to check", _quiet_windows, "area",
          hint="One per line, like Mon-Sun 01:00-05:00. The dashboard still checks while someone has it open. "
               "Empty checks all the time."),
    Field("history_days", "dashboard", "history_days", "Days of punctuality history to keep",
          _number(1, 3650), "number"),
    Field("train_crs", "train", "crs", "Station code", _crs(), hint="3 letters, like GLO for Glossop."),
    Field("train_filter_crs", "train", "filter_crs", "Only trains calling at", _crs(optional=True),
          hint="Optional station code, like MAN. Empty shows every train."),
    Field("train_walk_minutes", "train", "walk_minutes", "Walk to the station (minutes)",
          _number(0, 60, optional=True), "number", hint="Shows when to leave. Empty for none."),
    Field("train_rows", "dashboard", "train_rows", "Trains shown", _number(1, 20), "number"),
    Field("train_interval_seconds", "dashboard", "train_interval_seconds", "Check every (seconds)",
          _number(30, 3600), "number"),
    Field("late_minutes_train", "dashboard", "late_minutes_train", "Late means at least (minutes)",
          _number(1, 60), "number", hint="For the punctuality history."),
    Field("kb_incidents_url", "dashboard", "kb_incidents_url", "Knowledgebase Incidents address", _https,
          hint="For engineering works: the address on the Rail Data Marketplace product page. "
               "Empty leaves them off."),
    Field("bus_stops", "bus", "stops", "Stops", _stops, "area",
          hint="One per line, LABEL = ATCO code, like A = 1000DGHS0900. To find stops and their bus "
               "companies on a map, use setup."),
    Field("bus_directions", "bus", "directions", "Only buses heading to", _directions,
          hint="Optional, comma-separated, like Ashton, Hyde. Empty shows every bus."),
    Field("bus_walk_minutes", "bus", "walk_minutes", "Walk to each stop (minutes)", _walk_per_stop, "area",
          hint="One per line, LABEL = minutes, or one number for every stop. Empty for none."),
    Field("bus_rows", "dashboard", "bus_rows", "Buses shown", _number(1, 20), "number"),
    Field("bus_interval_seconds", "dashboard", "bus_interval_seconds", "Check every (seconds)",
          _number(15, 3600), "number"),
    Field("late_minutes_bus", "dashboard", "late_minutes_bus", "Late means at least (minutes)",
          _number(1, 60), "number", hint="For the punctuality history."),
    Field("weather_enabled", "weather", "enabled", "Weather", _on_off, "select",
          hint="The forecast and a rain warning, from Open-Meteo.", options=(("true", "On"), ("false", "Off"))),
    Field("weather_threshold_percent", "weather", "threshold_percent", "Warn of rain from (chance, %)",
          _number(1, 100), "number", hint="50 means warn when rain is more likely than not."),
    Field("weather_interval_minutes", "dashboard", "weather_interval_minutes", "Check every (minutes)",
          _number(10, 1440), "number"),
)}

# (legend, the table the group needs in the settings, its fields, what to say without it)
GROUPS = (
    ("Dashboard", None, ("title", "bank_holidays", "bank_holiday_division", "quiet_windows", "history_days"), ""),
    ("Trains", "train", ("train_crs", "train_filter_crs", "train_walk_minutes", "train_rows",
                         "train_interval_seconds", "late_minutes_train", "kb_incidents_url"),
     "Trains aren't set up. Use setup to pick a station, or add a [train] section to config.toml."),
    ("Buses", "bus", ("bus_stops", "bus_directions", "bus_walk_minutes", "bus_rows", "bus_interval_seconds",
                      "late_minutes_bus"),
     "Buses aren't set up. Use setup to pick stops and find their bus companies."),
    ("Weather", "weather", ("weather_enabled", "weather_threshold_percent", "weather_interval_minutes"),
     "No location set. Use setup, or add a [weather] section with lat and lon to config.toml."),
)


def _current(data: dict) -> dict[str, str]:
    """What each field shows for the settings in force, with the dashboard's defaults."""
    values = {}
    for f in FIELDS.values():
        source = data if f.section is None else data.get(f.section, {})
        value = source.get(f.key) if isinstance(source, dict) else None
        if value is None and f.section == "dashboard":
            value = list(QUIET_DEFAULT) if f.key == "quiet_windows" else getattr(_DEFAULTS, f.key)
        if f.name in ("bank_holidays", "weather_enabled"):
            value = "false" if value is False else "true"  # on unless turned off
        if f.name == "title" and value is None:
            value = "Departures"
        if f.name == "weather_threshold_percent" and value is None:
            value = 50
        if f.name == "bank_holiday_division" and value is None:
            value = ENGLAND_AND_WALES
        if f.name == "kb_incidents_url" and isinstance(value, str) and kb_url_is_placeholder(value):
            value = ""
        values[f.name] = _shown(value, f.kind)
    return values


class DashboardForm:
    """The form for a signed-in owner: drawn from the settings in force, and
    saved into `target`. `load_secrets` gives the environment with every key
    the dashboard would use, saved ones included."""

    def __init__(self, target: Target, load_secrets: Callable[[], Mapping[str, str]]) -> None:
        self._target = target
        self._load_secrets = load_secrets

    def _read(self) -> tuple[dict, str]:
        """The settings in force, and a problem with the saved settings file if
        it can't be read: then config.toml's alone."""
        base = self._target.base()
        try:
            return self._target.current(), ""
        except ConfigError as error:
            return base, str(error)

    def save(self, fields: dict[str, str]) -> dict[str, str]:
        """Save the fields; field name -> error, or empty when saved."""
        try:
            data, _ = self._read()
        except (OSError, ConfigError) as error:
            return {"_form": f"Not saved: the settings can't be read ({error})"}
        errors: dict[str, str] = {}
        changes: dict = {}
        for _, needs, names, _ in GROUPS:
            if needs is not None and needs not in data:
                continue  # not set up: setup or config.toml adds it
            for name in names:
                f = FIELDS[name]
                try:
                    value = f.check(fields.get(f.name, ""))
                except Invalid as error:
                    errors[f.name] = str(error)
                    continue
                target = changes if f.section is None else changes.setdefault(f.section, {})
                # Every key is written, empty ones too, so clearing a field clears
                # config.toml's value rather than letting it show through.
                target[f.key] = "" if value is None else value
        keys = {}
        for name, _, _ in KEYS:
            raw = fields.get(name, "").strip()
            if not raw:
                continue
            if not _KEY_VALUE.match(raw):
                errors[name] = "Paste the key exactly; it has only letters, digits and . _ ~ + / = -"
            else:
                keys[name] = raw
        if errors:
            return errors
        try:
            save_to(self._target, changes, keys, self._load_secrets())
        except OverlayError as error:
            return {"_form": str(error)}
        return {}

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str:
        errors = errors or {}
        e = html.escape
        try:
            data, broken = self._read()
        except (OSError, ConfigError) as error:
            data, broken = {}, str(error)
        shown = {**_current(data), **(values or {})}
        secrets = self._load_secrets()
        out = [
            '<header class="bar"><h1>Dashboard settings</h1>'
            '<form method="post" action="/settings/logout">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            '<button type="submit" class="quiet">Sign out</button></form></header>',
            '<nav aria-label="Settings pages"><p><a href="/">Back to the dashboard</a></p>'
            '<p><a href="/settings/setup/">Set up with a map: find your station, stops and bus companies</a></p>'
            "</nav>",
        ]
        if message:
            out.append(f'<p class="saved" role="status">{e(message)}</p>')
        if broken:
            out.append(
                '<div class="summary" role="alert"><h2>The saved settings can\'t be read</h2>'
                f"<p>{e(broken)}</p><p>The page shows config.toml's settings. Fix or remove the file to save "
                "changes here.</p></div>"
            )
        if errors:
            names = {name: f"{legend} – {FIELDS[name].label}" for legend, _, group, _ in GROUPS for name in group}
            names |= {name: f"Keys – {label}" for name, label, _ in KEYS}
            items = "".join(
                f'<li><a href="#{e(name)}">{e(names.get(name, name))}: {e(text)}</a></li>'
                if name != "_form" else f"<li>{e(text)}</li>"
                for name, text in errors.items()
            )
            out.append(
                '<div class="summary" role="alert" tabindex="-1" id="problems" autofocus>'
                f"<h2>Not saved: fix {len(errors)} {'thing' if len(errors) == 1 else 'things'}</h2>"
                f"<ul>{items}</ul></div>"
            )
        out.append('<form method="post" action="/settings" novalidate>')
        out.append(f'<input type="hidden" name="csrf" value="{e(csrf)}">')
        for legend, needs, names, missing in GROUPS:
            out.append(f"<fieldset><legend>{e(legend)}</legend>")
            if needs is not None and needs not in data:
                out.append(f'<p class="hint">{e(missing)}</p></fieldset>')
                continue
            for name in names:
                out.append(_field(FIELDS[name], shown.get(name, ""), errors.get(name)))
            out.append("</fieldset>")
        out.append("<fieldset><legend>Keys</legend>"
                   '<p class="hint">Keys are never shown. Paste one to replace it; leave a box empty to keep it.</p>')
        for name, label, hint in KEYS:
            status = "set" if str(secrets.get(name, "")).strip() else "not set"
            out.append(_key_field(name, label, f"{hint} Currently {status}.", errors.get(name)))
        out.append("</fieldset>")
        out.append('<div class="actions"><button type="submit">Save settings</button></div></form>')
        return "".join(out)


def _described(name: str, hint: str, error: str | None) -> tuple[str, str]:
    """A field's aria attributes, and its hint and error paragraphs."""
    e = html.escape
    ids, parts = [], []
    if hint:
        ids.append(f"{name}-hint")
        parts.append(f'<p class="hint" id="{name}-hint">{e(hint)}</p>')
    if error:
        ids.append(f"{name}-error")
        parts.append(f'<p class="error" id="{name}-error"><span class="tag">Error:</span> {e(error)}</p>')
    attrs = f' aria-describedby="{" ".join(ids)}"' if ids else ""
    if error:
        attrs += ' aria-invalid="true"'
    return attrs, "".join(parts)


def _field(f: Field, value: str, error: str | None) -> str:
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    label = f'<label for="{f.name}">{e(f.label)}</label>'
    if f.kind == "select":
        options = "".join(
            f'<option value="{e(v)}"{" selected" if v == value else ""}>{e(text)}</option>' for v, text in f.options
        )
        control = f'<select id="{f.name}" name="{f.name}"{attrs}>{options}</select>'
    elif f.kind == "area":
        rows = max(2, value.count("\n") + 2)
        control = (f'<textarea id="{f.name}" name="{f.name}" rows="{rows}" spellcheck="false"{attrs}>'
                   f"{e(value)}</textarea>")
    else:
        mode = ' inputmode="numeric"' if f.kind == "number" else ""
        control = f'<input id="{f.name}" name="{f.name}" type="text" value="{e(value)}"{mode}{attrs}>'
    return f'<div class="field{" has-error" if error else ""}">{label}{notes}{control}</div>'


def _key_field(name: str, label: str, hint: str, error: str | None) -> str:
    e = html.escape
    attrs, notes = _described(name, hint, error)
    return (
        f'<div class="field{" has-error" if error else ""}"><label for="{name}">{e(label)}</label>{notes}'
        # Plain text: password managers ignore autocomplete="off" on password
        # boxes and could fill in the settings password here.
        f'<input id="{name}" name="{name}" type="text" autocomplete="off" spellcheck="false"{attrs}></div>'
    )
