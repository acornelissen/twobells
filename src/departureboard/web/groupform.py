"""One settings form for any of the groups of settings in schema.FIELDS.

Each tab of the settings site (web/settingsite.py) is a GroupForm of one
group: General, Board or Dashboard. What the site puts around the form
(its heading, signing out, the tabs) comes from it.

Saved values go to settings.toml in the store (layered over config.toml)
and keys to the secrets.env beside it, merged into what is there
(overlay.save_to), so settings the form doesn't show, other groups'
included, survive. Nothing is written unless every field passes its own
check and the merged settings then load in every view that is on, so a
change for one view can't break the other. Keys are write-only: the form
shows whether one is set, never its value. A settings.toml that can't be
read is replaced on saving, keeping a copy as settings.toml.broken.

Given a way to test keys, the form has a test button for each of its keys
(TransportAPI's app ID and key are one test), after Save so that Enter in
a box still saves. A test takes the key typed in its box, else the saved
one, saves nothing, and is answered with the form drawn again: the result
in words by the key's box, its key boxes empty, never a key in the page.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..board.displays.display import DISPLAY_PANELS
from ..board.state import StateStore
from ..core.errors import safe_message
from ..settings import schema
from ..settings.config import ConfigError, merge, settings_from
from ..settings.overlay import BROKEN_NAME, OverlayError, save_to
from ..settings.schema import KEY_VALUE, Field, Invalid
from ..setup import keytest
from ..sources.disruptions import kb_url_is_placeholder
from ..sources.holidays import DIVISIONS, ENGLAND_AND_WALES, read_cache
from .status import render_status

if TYPE_CHECKING:
    from ..settings.installs import Install
    from ..settings.store import Store

# (name, typed value or None, typed app ID or None) -> its result; see SettingsSite.
KeyTest = Callable[[str, "str | None", "str | None"], keytest.KeyResult]
# Each key test's result: a glyph, hidden from screen readers, and the words that carry it.
RESULTS = {
    "works": ("✓", "Works"),
    "wrong_key": ("✗", "Wrong key"),
    "not_subscribed": ("✗", "Not subscribed"),
    "service_down": ("!", "Service not answering"),
    "missing": ("!", "No key"),
    "unknown": ("?", "Can't tell"),
    "not_tested": ("!", "Not tested"),
}
_MIN_SCRUBBED = 6  # as setupapi: shorter can't be told from ordinary words
_KEY_SHAPE = "Paste the key exactly; it has only letters, digits and . _ ~ + / = -"
LEGENDS: dict[str | None, str] = {None: "General", "train": "Trains", "bus": "Buses", "weather": "Weather"}
VIEWS_LEGEND = "Views"
_SECTIONS = (None, "train", "bus", "weather")
_MISSING = {
    "train": "Trains aren't set up. Use setup to pick a station, or add a [train] section to config.toml.",
    "bus": "Buses aren't set up. Use setup to pick stops and find their bus companies, "
           "or add a [bus] section to config.toml.",
    "weather": "No location set. Use setup to set home, or add a [weather] section with lat and lon "
               "to config.toml, for the rain warning and forecast.",
}


def neither_on(data: dict, fields: dict[str, str], noun: str) -> dict[str, str]:
    """An error on the first switch if the save would leave neither trains nor
    buses on, as the {noun} would then show nothing (setup refuses it too);
    else none. `data` is the settings in force: a section not in it isn't set up."""
    present = [section for section in ("train", "bus") if section in data]
    if present and all(fields.get(f"{section}_enabled") == "false" for section in present):
        return {f"{present[0]}_enabled": f"Trains and buses can't both be off, or the {noun} would show nothing. "
                                         "Turn one on."}
    return {}


def check_views(data: dict, env: Mapping[str, str], folder: Path, install: Install) -> None:
    """Raise (ConfigError) unless merged settings `data` load in every view
    that would be on (or, with none on, every view `install` offers), with
    the store in `folder`. What the forms and setup check before saving."""
    from ..dashboard.config import _build

    on = schema.views(data, install) or install.offered
    if "board" in on:
        settings_from(data, env)
    if "dashboard" in on:
        _build(data, env, folder)


@dataclass(frozen=True)
class Tested:
    """A key test's result, for the box of key `name`: a RESULTS state, and
    plain words with no key in them."""

    name: str
    state: str
    message: str


def edited(raw: str, shown: str) -> bool:
    """Whether a field was changed from what the page showed, ignoring the
    spaces, blank lines and line endings a browser may add."""
    def lines(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return lines(raw) != lines(shown)


def _shown(value, kind: str) -> str:
    """A config value as the form shows it: lists one per line in a text area,
    comma-separated in a one-line box."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ("\n" if kind == "area" else ", ").join(str(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k} = {v}" for k, v in value.items())
    return str(value)


def _default(f: Field, data: dict):
    """What a field shows when the settings don't say: what the loader uses then."""
    if f.name == "rotate":
        return 180 if data.get("rotate_180") else f.default  # the older spelling of rotate = 180
    if f.group == "board" and f.section == "bus":
        bus = data.get("bus")
        return schema.bus_defaults(bus.get("source") if isinstance(bus, dict) else None)[f.key]
    return f.default


class GroupForm:
    """The form for some groups of settings, for a signed-in owner: drawn
    from the settings in force, and saved into the store.

    `groups` are schema.GROUPS to show; `action` is where the form posts;
    `load_secrets` gives the environment with every key saved; `install`
    says which views can be switched on (a choice only when it offers more
    than one) and which are on by default; `noun` is what shows the
    departures ("board"), for the words when trains and buses are both off;
    `header` draws what goes before the form, given the form token;
    `legends` renames the fieldsets (None is the first); `clock` gives the
    local time as a naive datetime, by default the time now in the
    configured timezone. `test_key`, if given, tests a key and never
    raises; without it the form has no test buttons."""

    def __init__(self, store: Store, groups: Sequence[str], action: str,
                 load_secrets: Callable[[], Mapping[str, str]], *, install: Install, noun: str,
                 header: Callable[[str], str] = lambda csrf: "",
                 legends: Mapping[str | None, str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 test_key: KeyTest | None = None) -> None:
        unknown = set(groups) - set(schema.GROUPS)
        if unknown:
            raise ValueError(f"Unknown settings groups: {', '.join(sorted(unknown))}")
        self._store = store
        self._groups = tuple(groups)
        self._action = action
        self._load_secrets = load_secrets
        self._install = install
        self._noun = noun
        self._header = header
        self._legends = {**LEGENDS, **(legends or {})}
        self._clock = clock
        self._keys = schema.keys(*self._groups)
        self._test_key = test_key

    # Reading

    def _read(self) -> tuple[dict, str]:
        """The settings in force, and a problem with the saved settings file if
        it can't be read: then config.toml's alone, so saving can replace it.
        ConfigError (or OSError) if config.toml itself can't be read."""
        base = self._store.base()
        try:
            return merge(base, self._store.saved()), ""
        except ConfigError as error:
            return base, str(error)

    def _fields(self, data: dict, theme: str) -> list[Field]:
        display = data.get("display", schema.BOARD_DEFAULTS["display"])
        return [f for f in schema.fields(str(display), theme) if f.group in self._groups]

    def _choosing_views(self) -> bool:
        return "general" in self._groups and len(self._install.offered) > 1

    def _views_on(self, data: dict) -> tuple[str, ...]:
        try:
            return schema.views(data, self._install)
        except ConfigError:
            return self._install.default_views  # saving says what is wrong

    def _current(self, data: dict) -> dict[str, str]:
        """What each field shows for the settings in force, with the loaders' defaults."""
        values = {}
        on = self._views_on(data)
        for f in self._fields(data, str(data.get("theme", ""))):
            if f.kind == "check":
                values[f.name] = "on" if f.name.removeprefix("view_") in on else ""
                continue
            source = data if f.section is None else data.get(f.section, {})
            value = source.get(f.key) if isinstance(source, dict) else None
            if value is None:
                value = _default(f, data)
            if f.check is schema.on_off:
                value = "false" if value is False else "true"  # on unless turned off
            if f.name == "kb_incidents_url" and isinstance(value, str) and kb_url_is_placeholder(value):
                value = ""
            values[f.name] = _shown(value, f.kind)
        return values

    # Saving

    def _loads(self, data: dict, env: Mapping[str, str]) -> None:
        check_views(data, env, self._store.folder, self._install)

    def save(self, fields: dict[str, str]) -> dict[str, str]:
        """Save the fields; field name -> error, or empty when saved."""
        try:
            data, broken = self._read()
        except (OSError, ConfigError) as error:
            return {"_form": f"Not saved: the settings can't be read ({error})"}
        form = self._fields(data, fields.get("theme", ""))
        # A section switched off keeps what is saved for it, for switching it back on:
        # a field left as shown is left alone, unchecked (it may be the template's
        # placeholder), and one changed is checked and saved, as with it on.
        off = {f.needs for f in form if f.key == "enabled" and fields.get(f.name) == "false"}
        shown = self._current(data)
        errors: dict[str, str] = {}
        changes: dict = {}
        for f in form:
            if f.kind == "check":
                continue  # the views, below
            if f.needs is not None and f.needs not in data:
                continue  # not set up: setup or config.toml adds it
            if f.needs in off and f.key != "enabled" and not edited(fields.get(f.name, ""), shown[f.name]):
                continue
            try:
                value = f.check(fields.get(f.name, ""))
            except Invalid as error:
                errors[f.name] = str(error)
                continue
            target = changes if f.section is None else changes.setdefault(f.section, {})
            # Every key is written, empty ones too, so clearing a field clears
            # config.toml's value rather than letting it show through.
            target[f.key] = "" if value is None else value
        if self._choosing_views():
            errors |= self._views(fields, changes)
        if "general" in self._groups:
            errors |= neither_on(data, fields, self._noun)
        keys = {}
        for name, _, _ in self._keys:
            raw = fields.get(name, "").strip()
            if not raw:
                continue
            if not KEY_VALUE.match(raw):
                errors[name] = _KEY_SHAPE
            else:
                keys[name] = raw
        if errors:
            return errors
        # Merged into what is saved, so settings this form doesn't show (setup's
        # bus operators and routes, the weather position, the other view's)
        # survive; a broken file can't be merged into, so it is replaced.
        try:
            save_to(self._store.target(self._loads), changes, keys, self._load_secrets(), replace=bool(broken))
        except OverlayError as error:
            return {"_form": str(error)}
        return {}

    def _views(self, fields: dict[str, str], changes: dict) -> dict[str, str]:
        """[app] views from the checkboxes into `changes`; an error unless at least one is ticked."""
        offered = self._install.offered
        chosen = []
        for view in offered:
            try:
                if schema.ticked(fields.get(f"view_{view}", "")):
                    chosen.append(view)
            except Invalid as error:
                return {f"view_{view}": str(error)}
        if not chosen:
            return {f"view_{offered[0]}": "Pick at least one view, or nothing shows your departures"}
        changes["app"] = {"views": chosen}
        return {}

    # Testing a key

    def key_tests(self) -> tuple[str, ...]:
        """The keys with a test button, by the name it sends: each of the
        form's keys keytest knows, TransportAPI's app ID tested with its app key."""
        if self._test_key is None:
            return ()
        return tuple(name for name, _, _ in self._keys if name in keytest.KEY_NAMES and name != "TAPI_APP_ID")

    def test_key(self, fields: dict[str, str]) -> Tested | None:
        """Test the key the button pressed names (fields["test"]): the one
        typed in its box, else the saved one. None if the form has no such
        button. Nothing is saved."""
        name = fields.get("test", "")
        if name not in self.key_tests():
            return None
        boxes = ("TAPI_APP_ID", name) if name == "TAPI_APP_KEY" else (name,)
        typed = {box: fields.get(box, "").strip() for box in boxes}
        if any(value and not KEY_VALUE.match(value) for value in typed.values()):
            return Tested(name, "not_tested", _KEY_SHAPE)
        result = self._test_key(name, typed[name] or None, typed.get("TAPI_APP_ID") or None)
        # Belt and braces: whatever the test said, no key, typed or saved, is shown.
        env = self._load_secrets()
        secrets = [*typed.values(), *(str(env.get(key, "")).strip() for key in keytest.KEY_NAMES)]
        message = result.message
        for secret in sorted((s for s in secrets if len(s) >= _MIN_SCRUBBED), key=len, reverse=True):
            message = message.replace(secret, "…")
        return Tested(name, result.state if result.state in RESULTS else "unknown", safe_message(message))

    # Drawing

    def _now(self, data: dict) -> datetime:
        if self._clock is not None:
            return self._clock()
        try:
            return datetime.now(ZoneInfo(data.get("timezone", "Europe/London"))).replace(tzinfo=None)
        except (KeyError, TypeError, ValueError):  # a bad zone: the form says so on saving
            return datetime.now()

    def _status(self, data: dict) -> str:
        """The board's status panel, from the state file it saves. Read only:
        the board alone writes it."""
        try:
            state = StateStore(self._store.state_file).load()
        except OSError:
            state = None
        division = data.get("bank_holiday_division", ENGLAND_AND_WALES)
        if division not in DIVISIONS:  # the form says so on saving
            division = ENGLAND_AND_WALES
        return render_status(data, state, self._now(data), read_cache(self._store.folder, division))

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None, tested: Tested | None = None) -> str:
        errors = errors or {}
        e = html.escape
        unreadable = ""
        try:
            data, broken = self._read()
        except (OSError, ConfigError) as error:
            data, broken, unreadable = {}, "", str(error)
        shown = {**self._current(data), **(values or {})}
        fields = self._fields(data, shown.get("theme", ""))
        secrets = self._load_secrets()
        out = [self._header(csrf)]
        if message:
            out.append(f'<p class="saved" role="status">{e(message)}</p>')
        if unreadable:
            out.append(
                '<div class="summary" role="alert"><h2>The settings can\'t be read</h2>'
                f"<p>{e(unreadable)}</p><p>Fix config.toml to save changes here.</p></div>"
            )
        if broken:
            out.append(
                '<div class="summary" role="alert"><h2>The saved settings file is broken</h2>'
                f"<p>{e(broken)}</p>"
                "<p>The page shows config.toml's settings. Saving replaces the broken file, "
                f"keeping a copy of it as {BROKEN_NAME}.</p></div>"
            )
        if errors:
            out.append(self._summary(fields, errors))
        if "board" in self._groups:
            out.append(self._status(data))
        out.append(f'<form method="post" action="{e(self._action)}" novalidate>')
        out.append(f'<input type="hidden" name="csrf" value="{e(csrf)}">')
        for section in _SECTIONS:
            out.append(self._fieldset(section, fields, data, shown, errors))
            if section is None and self._choosing_views():
                out.append(self._views_fieldset(fields, shown, errors))
        out.append("<fieldset><legend>Keys</legend>"
                   '<p class="hint">Keys are never shown. Paste one to replace it; leave a box empty to keep it.</p>')
        for name, label, hint in self._keys:
            status = "set" if str(secrets.get(name, "")).strip() else "not set"
            result_id, result = "", ""
            if tested is not None and name == tested.name:
                result_id, result = f"{name}-test", _result(tested)
            elif tested is not None and (name, tested.name) == ("TAPI_APP_ID", "TAPI_APP_KEY"):
                result_id = "TAPI_APP_KEY-test"  # tested together: the one result, by the app key's box
            out.append(_key_field(name, label, f"{hint} Currently {status}.", errors.get(name), result_id, result))
        out.append("</fieldset>")
        # Save first: the first button is the one Enter in a box presses.
        out.append('<div class="actions"><button type="submit">Save settings</button></div>')
        out.append(self._key_test_buttons())
        out.append("</form>")
        return "".join(out)

    def _key_test_buttons(self) -> str:
        names = self.key_tests()
        if not names:
            return ""
        e = html.escape
        labels = {name: label for name, label, _ in self._keys}
        out = ['<fieldset class="key-tests"><legend>Test a key</legend>'
               '<p class="hint" id="key-tests-hint">Tests the key typed in its box above, or the saved one if the '
               "box is empty, with one small request to its service. Nothing is saved, and the key boxes are "
               "emptied: paste a key again to save it.</p>"]
        for name in names:
            described, cost = "key-tests-hint", ""
            text = f"Test the {labels[name]}"
            if name in keytest.COSTS_A_LOOKUP:
                text = "Test the TransportAPI app ID and key"
                described += f" {name}-cost"
                cost = (f'<p class="hint" id="{name}-cost">Uses 1 of today\'s 30 TransportAPI lookups, the same '
                        "ones bus times use.</p>")
            out.append(f'<div class="key-test">{cost}<button type="submit" name="test" value="{e(name)}" '
                       f'class="quiet" aria-describedby="{described}">{e(text)}</button></div>')
        out.append("</fieldset>")
        return "".join(out)

    def _summary(self, fields: list[Field], errors: dict[str, str]) -> str:
        e = html.escape
        names = {f.name: f"{VIEWS_LEGEND if f.kind == 'check' else self._legends[f.needs]} – {f.label}"
                 for f in fields}
        names |= {name: f"Keys – {label}" for name, label, _ in self._keys}
        items = "".join(
            f'<li><a href="#{e(name)}">{e(names.get(name, name))}: {e(text)}</a></li>'
            if name != "_form" else f"<li>{e(text)}</li>"
            for name, text in errors.items()
        )
        return (
            '<div class="summary" role="alert" tabindex="-1" id="problems" autofocus>'
            f"<h2>Not saved: fix {len(errors)} {'thing' if len(errors) == 1 else 'things'}</h2>"
            f"<ul>{items}</ul></div>"
        )

    def _fieldset(self, section: str | None, fields: list[Field], data: dict, shown: dict[str, str],
                  errors: dict[str, str]) -> str:
        e = html.escape
        here = [f for f in fields if f.needs == section and f.kind != "check"]
        if not here:
            return ""
        out = [f"<fieldset><legend>{e(self._legends[section])}</legend>"]
        if section is None and "board" in self._groups:
            display = str(data.get("display", schema.BOARD_DEFAULTS["display"]))
            panel = DISPLAY_PANELS.get(display)
            size = f" ({panel.width}×{panel.height})" if panel is not None else ""
            out.append(
                f'<p class="hint">Display: <strong>{e(display)}</strong>{size}. Changing the display '
                "means switching services, so it's set in config.toml.</p>"
            )
        if section is not None and section not in data:
            out.append(f'<p class="hint">{e(_MISSING[section])}</p></fieldset>')
            return "".join(out)
        if section == "weather" and isinstance(data["weather"], dict):
            where = data["weather"]
            out.append(
                f'<p class="hint">Location: <strong>{e(str(where.get("lat", "")))}, '
                f'{e(str(where.get("lon", "")))}</strong>, rounded to about 1 km before it is sent to '
                "Open-Meteo. Setup sets it from home, or set it in config.toml.</p>"
            )
        if section == "bus" and "dashboard" in self._groups and isinstance(data["bus"], dict) \
                and data["bus"].get("source") != "bods":
            from ..dashboard.config import BUS_NOTE

            out.append(f'<p class="hint">{e(BUS_NOTE)}</p>')
        for f in here:
            out.append(_field(f, shown.get(f.name, ""), errors.get(f.name)))
        out.append("</fieldset>")
        return "".join(out)

    def _views_fieldset(self, fields: list[Field], shown: dict[str, str], errors: dict[str, str]) -> str:
        boxes = [f for f in fields if f.kind == "check" and f.name.removeprefix("view_") in self._install.offered]
        out = [f"<fieldset><legend>{VIEWS_LEGEND}</legend>"
               '<p class="hint">Where your departures show. At least one is on.</p>']
        for f in boxes:
            out.append(_checkbox(f, shown.get(f.name, ""), errors.get(f.name)))
        out.append("</fieldset>")
        return "".join(out)


def _described(name: str, hint: str, error: str | None, also: str = "") -> tuple[str, str]:
    """A field's aria attributes, and its hint and error paragraphs; `also`
    is the id of another paragraph about it, like a key test's result."""
    e = html.escape
    ids, parts = [], []
    if hint:
        ids.append(f"{name}-hint")
        parts.append(f'<p class="hint" id="{name}-hint">{e(hint)}</p>')
    if error:
        ids.append(f"{name}-error")
        parts.append(f'<p class="error" id="{name}-error"><span class="tag">Error:</span> {e(error)}</p>')
    if also:
        ids.append(also)
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


def _checkbox(f: Field, value: str, error: str | None) -> str:
    """A checkbox, its label beside it and its hint and error below."""
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    checked = " checked" if value == "on" else ""
    return (
        f'<div class="field check{" has-error" if error else ""}">'
        f'<input id="{f.name}" name="{f.name}" type="checkbox" value="on"{checked}{attrs}>'
        f'<label for="{f.name}">{e(f.label)}</label>{notes}</div>'
    )


def _key_field(name: str, label: str, hint: str, error: str | None, result_id: str = "", result: str = "") -> str:
    """A key's box, never with its value; `result` (HTML, with id `result_id`) goes right after it."""
    e = html.escape
    attrs, notes = _described(name, hint, error, result_id)
    return (
        f'<div class="field{" has-error" if error else ""}"><label for="{name}">{e(label)}</label>{notes}'
        # Plain text: password managers ignore autocomplete="off" on password
        # boxes and could fill in the settings password here.
        f'<input id="{name}" name="{name}" type="text" autocomplete="off" spellcheck="false"{attrs}>{result}</div>'
    )


def _result(tested: Tested) -> str:
    """A key test's result, in words, focused when the page opens so it is read out."""
    glyph, words = RESULTS[tested.state]
    message = html.escape(tested.message, quote=False)
    return (
        f'<p class="key-result key-result-{tested.state}" id="{tested.name}-test" role="status" tabindex="-1" '
        f'autofocus><span aria-hidden="true">{glyph}</span> <strong>{words}:</strong> {message}</p>'
    )
