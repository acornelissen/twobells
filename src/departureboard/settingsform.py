"""The settings form: what it edits, how each field is checked, and saving.

Saved values go to settings.toml beside the state file (layered over
config.toml) and keys to the secrets.env beside it. Nothing is written unless
every field passes its own check and the whole configuration then loads.
Keys are write-only: the page shows whether one is set, never its value.
"""

from __future__ import annotations

import html
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import (
    ConfigError,
    OVERLAY_NAME,
    SECRETS_OVERLAY_NAME,
    merge,
    read_base,
    read_overlay,
    settings_from,
    state_dir,
)
from .display import DISPLAY_PANELS
from .schedule import parse_window
from .themes import THEMES

KEYS = (
    ("RDM_API_KEY", "Rail Data Marketplace key", "Trains: the consumer key for Live Departure Board."),
    ("TAPI_APP_ID", "TransportAPI app ID", "Buses."),
    ("TAPI_APP_KEY", "TransportAPI app key", "Buses."),
    ("BODS_API_KEY", "Bus Open Data Service key", "Only for the bus position page on a screen."),
)
_KEY_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")
_CRS = re.compile(r"^[A-Za-z]{3}$")
_LABEL = re.compile(r"^[A-Za-z0-9]{1,3}$")
_ATCO = re.compile(r"^[A-Za-z0-9]{6,12}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Invalid(ValueError):
    pass


def _text(limit: int) -> Callable[[str], str]:
    def check(raw: str) -> str:
        value = raw.strip()
        if not value:
            raise Invalid("Enter some text")
        if len(value) > limit or _CONTROL.search(value):
            raise Invalid(f"Use up to {limit} ordinary characters")
        return value
    return check


def _number(low: int, high: int, optional: bool = False) -> Callable[[str], int | None]:
    def check(raw: str) -> int | None:
        value = raw.strip()
        if not value and optional:
            return None
        if not value.isdigit() or not low <= int(value) <= high:
            raise Invalid(f"Enter a whole number from {low} to {high}")
        return int(value)
    return check


def _choice(options: tuple[str, ...]) -> Callable[[str], str]:
    def check(raw: str) -> str:
        if raw not in options:
            raise Invalid("Pick one of the options")
        return raw
    return check


def _crs(optional: bool = False) -> Callable[[str], str | None]:
    def check(raw: str) -> str | None:
        value = raw.strip().upper()
        if not value and optional:
            return None
        if not _CRS.match(value):
            raise Invalid("Enter a 3-letter station code, like GLO")
        return value
    return check


def _windows(raw: str) -> list[str]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise Invalid("Add at least one window, like Mon-Fri 06:30-09:30")
    for line in lines:
        try:
            parse_window(line)
        except ValueError:
            raise Invalid(f"“{line[:40]}” isn't a window like Mon-Fri 06:30-09:30") from None
    return lines


def _pairs(raw: str, value_check: Callable[[str], object], what: str) -> dict[str, object]:
    pairs = {}
    for line in (line.strip() for line in raw.splitlines()):
        if not line:
            continue
        label, sep, value = (part.strip() for part in line.partition("="))
        if not sep or not _LABEL.match(label):
            raise Invalid(f"Write each line as LABEL = {what}, with a label of 1 to 3 letters or digits")
        pairs[label] = value_check(value)
    return pairs


def _atco(raw: str) -> str:
    if not _ATCO.match(raw):
        raise Invalid("A stop code is 6 to 12 letters and digits, like 1000DGHS0900")
    return raw


def _stops(raw: str) -> dict[str, object]:
    stops = _pairs(raw, _atco, "ATCO code")
    if not stops:
        raise Invalid("Add at least one stop, like A = 1000DGHS0900")
    return stops


def _walk_per_stop(raw: str) -> dict[str, object] | int:
    if not raw.strip():
        return {}
    if raw.strip().isdigit():
        return _number(0, 60)(raw)
    return _pairs(raw, lambda v: _number(0, 60)(v), "minutes")


def _directions(raw: str) -> list[str]:
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    for part in parts:
        if len(part) > 40 or _CONTROL.search(part):
            raise Invalid("Keep each direction under 40 characters")
    return parts


@dataclass(frozen=True)
class Field:
    name: str  # form field name and element id
    section: str | None  # None, "train" or "bus"
    key: str
    label: str
    check: Callable[[str], object]
    kind: str = "text"  # text, number, select, area
    hint: str = ""
    options: tuple[tuple[str, str], ...] = ()


def _fields(display: str) -> list[Field]:
    rotations = ("0", "180") if DISPLAY_PANELS[display].epaper else ("0", "90", "180", "270")
    return [
        Field("title", None, "title", "Title", _text(60), hint="Shown on the plain look's header."),
        Field("theme", None, "theme", "Look", _choice(tuple(THEMES)), "select",
              options=(("sign", "Station board (amber dot matrix)"), ("plain", "Plain (black on white)"))),
        Field("accent", None, "accent", "Accent colour", _choice(("", "red", "yellow")), "select",
              hint="Only reinforces meaning; the words always carry it.",
              options=(("", "The look's own"), ("yellow", "Yellow / amber"), ("red", "Red"))),
        Field("rotate", None, "rotate", "Rotation", lambda raw: int(_choice(rotations)(raw)), "select",
              hint="Degrees clockwise, to suit how the display is mounted.",
              options=tuple((r, f"{r}°") for r in rotations)),
        Field("min_redraw_minutes", None, "min_redraw_minutes", "Least minutes between redraws",
              _number(1, 60), "number", hint="E-paper only. Refreshes are slow and wear the panel."),
        Field("train_crs", "train", "crs", "Station code", _crs(), hint="3 letters, like GLO for Glossop."),
        Field("train_filter_crs", "train", "filter_crs", "Only trains calling at", _crs(optional=True),
              hint="Optional station code, like MAN. Empty shows every train."),
        Field("train_rows", "train", "rows", "Trains shown", _number(1, 6), "number"),
        Field("train_interval_minutes", "train", "interval_minutes", "Check every (minutes)", _number(1, 60), "number"),
        Field("train_stale_after_minutes", "train", "stale_after_minutes", "Say “not updating” after (minutes)",
              _number(1, 240), "number"),
        Field("train_windows", "train", "windows", "When to check", _windows, "area",
              hint="One per line, like Mon-Fri 06:30-09:30."),
        Field("train_walk_minutes", "train", "walk_minutes", "Walk to the station (minutes)",
              _number(0, 60, optional=True), "number", hint="Shows when to leave. Empty for none."),
        Field("bus_stops", "bus", "stops", "Stops", _stops, "area",
              hint="One per line, LABEL = ATCO code, like A = 1000DGHS0900. Find codes on bustimes.org."),
        Field("bus_directions", "bus", "directions", "Only buses heading to", _directions,
              hint="Optional, comma-separated, like Ashton, Hyde. Empty shows every bus."),
        Field("bus_rows", "bus", "rows", "Buses shown", _number(1, 6), "number"),
        Field("bus_interval_minutes", "bus", "interval_minutes", "Check every (minutes)", _number(1, 120), "number",
              hint="Each check costs one TransportAPI hit per stop."),
        Field("bus_stale_after_minutes", "bus", "stale_after_minutes", "Say “not updating” after (minutes)",
              _number(1, 480), "number"),
        Field("bus_daily_budget", "bus", "daily_budget", "Most hits a day", _number(1, 1000), "number",
              hint="The free TransportAPI plan allows 30."),
        Field("bus_windows", "bus", "windows", "When to check", _windows, "area",
              hint="One per line, like Mon-Fri 07:00-08:30."),
        Field("bus_walk_minutes", "bus", "walk_minutes", "Walk to each stop (minutes)", _walk_per_stop, "area",
              hint="One per line, LABEL = minutes, or one number for every stop. Empty for none."),
    ]


def _shown(value, kind: str) -> str:
    """A config value as the form shows it: lists one per line in a text area,
    comma-separated in a one-line box."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ("\n" if kind == "area" else ", ").join(str(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k} = {v}" for k, v in value.items())
    return str(value)


def _toml_string(value: str) -> str:
    """A TOML basic string. JSON's escapes are TOML's too, except that JSON
    writes characters beyond the BMP as surrogate pairs, which TOML rejects,
    and leaves DEL raw, which TOML forbids."""
    if any("\ud800" <= c <= "\udfff" for c in value):
        raise ValueError("Text has a broken character")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(key: str) -> str:
    return key if re.match(r"^[A-Za-z0-9_-]+$", key) else _toml_string(key)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"Can't write {type(value).__name__} to TOML")


def to_toml(data: dict) -> str:
    """TOML for the form's settings: top-level values, then tables and their sub-tables."""
    lines = ["# Saved from the settings page. Layered over config.toml; edit there or here."]
    lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in data.items() if not isinstance(v, dict)]

    def table(path: list[str], body: dict) -> None:
        lines.append("")
        lines.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in body.items() if not isinstance(v, dict))
        for k, v in body.items():
            if isinstance(v, dict):
                table(path + [k], v)

    for key, value in data.items():
        if isinstance(value, dict):
            table([key], value)
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class SettingsForm:
    def __init__(self, config_path: Path | str, load_secrets: Callable[[], dict[str, str]]) -> None:
        self._config_path = Path(config_path)
        self._load_secrets = load_secrets

    def _paths(self, data: dict) -> tuple[Path, Path]:
        folder = state_dir(data)
        if folder is None:
            raise ConfigError("config.toml needs a state_file before settings can be saved")
        return folder / OVERLAY_NAME, folder / SECRETS_OVERLAY_NAME

    def _read(self) -> tuple[dict, str]:
        """The settings in force, and a problem with the saved settings file if
        it can't be read: then config.toml's alone, so the page can replace it."""
        base = read_base(self._config_path)
        try:
            return merge(base, read_overlay(base)), ""
        except ConfigError as error:
            return base, str(error)

    def _current(self, data: dict) -> dict[str, str]:
        values = {}
        for f in _fields(data.get("display", "inky")):
            source = data if f.section is None else data.get(f.section, {})
            value = source.get(f.key)
            if f.name == "rotate" and value is None:
                value = 180 if data.get("rotate_180") else 0
            values[f.name] = _shown(value, f.kind)
        return values

    def save(self, fields: dict[str, str]) -> dict[str, str]:
        data, _ = self._read()
        display = data.get("display", "inky")
        errors: dict[str, str] = {}
        overlay: dict = {}
        for f in _fields(display):
            if f.section and f.section not in data:
                continue  # trains or buses aren't set up in config.toml
            try:
                value = f.check(fields.get(f.name, ""))
            except Invalid as error:
                errors[f.name] = str(error)
                continue
            target = overlay if f.section is None else overlay.setdefault(f.section, {})
            # Every key is written, empty ones too, so clearing a field clears
            # config.toml's value rather than letting it show through.
            target[f.key] = "" if value is None else value
        keys = {}
        for name, label, _ in KEYS:
            raw = fields.get(name, "").strip()
            if not raw:
                continue
            if not _KEY_VALUE.match(raw):
                errors[name] = "Paste the key exactly; it has only letters, digits and . _ ~ + / = -"
            else:
                keys[name] = raw
        if errors:
            return errors
        env = {**self._load_secrets(), **keys}
        try:
            settings_from(merge(data, overlay), env)
        except (ConfigError, KeyError, TypeError, ValueError) as error:
            return {"_form": f"Not saved: {error}"}
        try:
            text = to_toml(overlay)
            readable = tomllib.loads(text) == overlay
        except (ValueError, TypeError):
            readable = False
        if not readable:
            return {"_form": "Not saved: some text can't be stored. Remove any unusual characters and try again."}
        settings_path, secrets_path = self._paths(data)
        _write(settings_path, text, 0o640)
        if keys:
            saved = {}
            if secrets_path.exists():
                for line in secrets_path.read_text().splitlines():
                    name, sep, value = line.partition("=")
                    if sep and name.strip() in {k for k, _, _ in KEYS}:
                        saved[name.strip()] = value.strip()
            saved.update(keys)
            _write(secrets_path, "".join(f"{k}={v}\n" for k, v in saved.items()), 0o600)
        return {}

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str:
        errors = errors or {}
        data, broken = self._read()
        display = data.get("display", "inky")
        current = self._current(data)
        shown = {**current, **(values or {})}
        secrets = self._load_secrets()
        e = html.escape
        out = []
        out.append(
            '<header class="bar"><h1>Board settings</h1>'
            '<form method="post" action="/logout">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            '<button type="submit" class="quiet">Sign out</button></form></header>'
        )
        if message:
            out.append(f'<p class="saved" role="status">{e(message)}</p>')
        if broken:
            out.append(
                '<div class="summary" role="alert"><h2>The saved settings file is broken</h2>'
                f"<p>{e(broken)}</p><p>The page shows config.toml's settings. Saving replaces the broken file.</p></div>"
            )
        if errors:
            items = "".join(
                f'<li><a href="#{e(name)}">{e(text)}</a></li>' if name != "_form" else f"<li>{e(text)}</li>"
                for name, text in errors.items()
            )
            out.append(
                '<div class="summary" role="alert" tabindex="-1" id="problems">'
                f"<h2>Not saved: fix {len(errors)} {'thing' if len(errors) == 1 else 'things'}</h2><ul>{items}</ul></div>"
            )
        out.append('<form method="post" action="/" novalidate>')
        out.append(f'<input type="hidden" name="csrf" value="{e(csrf)}">')
        groups = (
            (None, "Board", ""),
            ("train", "Trains", "Trains aren't set up. Add a [train] section to config.toml."),
            ("bus", "Buses", "Buses aren't set up. Add a [bus] section to config.toml."),
        )
        panel = DISPLAY_PANELS[display]
        for section, legend, missing in groups:
            out.append(f"<fieldset><legend>{legend}</legend>")
            if section is None:
                out.append(
                    '<p class="hint">Display: '
                    f"<strong>{e(display)}</strong> ({panel.width}×{panel.height}). Changing the display "
                    "means switching services, so it's set in config.toml.</p>"
                )
            if section and section not in data:
                out.append(f'<p class="hint">{e(missing)}</p></fieldset>')
                continue
            for f in _fields(display):
                if f.section != section:
                    continue
                out.append(self._field(f, shown.get(f.name, ""), errors.get(f.name)))
            out.append("</fieldset>")
        out.append("<fieldset><legend>Keys</legend>"
                   '<p class="hint">Keys are never shown. Paste one to replace it; leave a box empty to keep it.</p>')
        for name, label, hint in KEYS:
            status = "set" if secrets.get(name) else "not set"
            out.append(self._key_field(name, label, f"{hint} Currently {status}.", errors.get(name)))
        out.append("</fieldset>")
        out.append('<div class="actions"><button type="submit">Save settings</button></div></form>')
        return "".join(out)

    @staticmethod
    def _described(name: str, hint: str, error: str | None) -> tuple[str, str]:
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

    def _field(self, f: Field, value: str, error: str | None) -> str:
        e = html.escape
        attrs, notes = self._described(f.name, f.hint, error)
        label = f'<label for="{f.name}">{e(f.label)}</label>'
        if f.kind == "select":
            options = "".join(
                f'<option value="{e(v)}"{" selected" if v == value else ""}>{e(text)}</option>' for v, text in f.options
            )
            control = f'<select id="{f.name}" name="{f.name}"{attrs}>{options}</select>'
        elif f.kind == "area":
            rows = max(2, value.count("\n") + 2)
            control = f'<textarea id="{f.name}" name="{f.name}" rows="{rows}" spellcheck="false"{attrs}>{e(value)}</textarea>'
        else:
            mode = ' inputmode="numeric"' if f.kind == "number" else ""
            control = f'<input id="{f.name}" name="{f.name}" type="text" value="{e(value)}"{mode}{attrs}>'
        return f'<div class="field{" has-error" if error else ""}">{label}{notes}{control}</div>'

    def _key_field(self, name: str, label: str, hint: str, error: str | None) -> str:
        e = html.escape
        attrs, notes = self._described(name, hint, error)
        return (
            f'<div class="field{" has-error" if error else ""}"><label for="{name}">{e(label)}</label>{notes}'
            f'<input id="{name}" name="{name}" type="password" autocomplete="off" spellcheck="false"{attrs}></div>'
        )
