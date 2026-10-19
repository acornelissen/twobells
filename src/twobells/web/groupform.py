"""One settings form for any of the settings pages in schema.PAGES.

Each page of the settings site (web/settingsite.py) but the overview is a
GroupForm of one page: Trains and buses, Board, Dashboard or General. The
Keys page is web/keyspage.py's.
What the site puts around the form (its header, the tabs, the footer)
comes from it.

A page's fields are drawn in sections, one per heading the fields give
(schema.Field.heading), each a fieldset whose legend holds the section's
h2, with its advanced fields in a closed Advanced disclosure, opened when
one of them has an error. A field shows only while the view that uses it
is on, and while what it needs is set up; a section with nothing set up
says so, with a link to setup. A view's modes list shows only while there
are two modes or more to choose from.

Saved values go to settings.toml in the store (layered over config.toml),
merged into what is there (overlay.save_to), so settings the form doesn't
show, other pages' included, survive. Nothing is written unless every
field passes its own check and the merged settings then load in every view
that is on, so a change for one view can't break the other. A
settings.toml that can't be read is replaced on saving, keeping a copy as
settings.toml.broken. A save says what it changed in words, for the page
drawn after it.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..board.config import settings_from
from ..board.names import DISPLAY_PANELS
from ..board.themes import THEMES
from ..core.errors import ConfigError
from ..dashboard.config import BUS_NOTE, _build
from ..settings import modes, schema, show
from ..settings.config import board_modes, merge
from ..settings.overlay import BROKEN_NAME, OverlayError, save_to
from ..settings.schema import Field, Invalid
from . import places, windowrows
from .icons import icon

if TYPE_CHECKING:
    from ..settings.installs import Install
    from ..settings.store import Store

# Each section's one-line description, by (page, heading).
NOTES: dict[tuple[str, str], str] = {
    ("trains_buses", "Title"): "What the board and the dashboard are called.",
    ("trains_buses", "Trains"): "Departures from your station.",
    ("trains_buses", "Buses"): "Departures from your stops.",
    ("trains_buses", "Trams"): "Metrolink departures from your tram stops, from TfGM's timetable; or London's "
                               "Underground, DLR, trams and river buses, live from TfL.",
    ("trains_buses", "Weather"): "A rain warning beside the departures, and the dashboard's forecast.",
    ("board", "Display"): "How the board looks.",
    ("board", "Modes of transport"): "Which departures the board shows, each in a section of its own.",
    ("board", "What to show"): "What the board shows beside the departures.",
    ("dashboard", "What to show"): "What the dashboard shows beside the departures.",
    ("board", "Trains"): "How many trains the board shows, and when it checks for them.",
    ("board", "Buses"): "How many buses the board shows, and when it checks for them.",
    ("board", "Trams"): "How many trams the board shows, and when it checks the timetable for them.",
    ("dashboard", "Departures"): "Which departures the dashboard lists, how many, and how often it checks.",
    ("dashboard", "Quiet hours"): "When the dashboard rests, overnight say.",
    ("general", "Views"): "Where your departures show. At least one is on.",
    ("general", "Long text"): "What happens to text too long for its line, on the dashboard and a board's screen.",
    ("general", "Dates and times"): "Bank holidays, and the time zone.",
}
# What a section whose fields all need something not set up says: the words,
# the link to setup's, and the words after the link.
_MISSING = {
    "train": ("Trains aren't set up.", "Pick a station with setup", ", or add a [train] section to config.toml."),
    "bus": ("Buses aren't set up.", "Pick stops with setup",
            ", which finds their bus companies too, or add a [bus] section to config.toml."),
    "metro": ("Trams aren't set up.", "Pick tram stops with setup", ", or add a [metro] section to config.toml."),
    "weather": ("No location is set for the rain warning and forecast.", "Set home with setup",
                ", or add a [weather] section with lat and lon to config.toml."),
}
# The fields a section not set up still shows, so it can be set up here: tram
# stops by their codes, for those who want trams nowhere near home. With the
# words that then end the section's "not set up" line.
_SETS_UP = {"metro_stops": ", or type their codes below."}
# How a unit is said by a screen reader, where its sign isn't a word.
_SPOKEN = {"%": "per cent"}
_KEYBOARDS = {
    "code": ' autocapitalize="characters" autocorrect="off" spellcheck="false"',
    "plain": ' autocapitalize="none" autocorrect="off" spellcheck="false"',
}


def neither_on(data: dict, fields: dict[str, str], noun: str) -> dict[str, str]:
    """An error on the first switch if the save would leave none of the
    trains, buses and trams set up on, as the {noun} would then show
    nothing (setup refuses it too); else none. `data` is the settings in
    force: a section not in it isn't set up."""
    present = [section for section in modes.KEYS if section in data]
    if present and all(fields.get(f"{section}_enabled") == "false" for section in present):
        nouns = [modes.mode(section).noun for section in present]
        if len(nouns) == 1:
            said, fix = f"{nouns[0]} can't be off", "Turn them on."
        else:
            both = "both" if len(nouns) == 2 else "all"
            said, fix = f"{', '.join(nouns[:-1])} and {nouns[-1]} can't {both} be off", "Turn one on."
        return {f"{present[0]}_enabled": f"{said[:1].upper()}{said[1:]}, or the {noun} would show nothing. {fix}"}
    return {}


def check_views(data: dict, env: Mapping[str, str], folder: Path, install: Install) -> None:
    """Raise (ConfigError) unless merged settings `data` load in every view
    that would be on (or, with none on, every view `install` offers), with
    the store in `folder`. What the forms and setup check before saving."""
    on = schema.views(data, install) or install.offered
    if "board" in on:
        settings_from(data, env)
    if "dashboard" in on:
        _build(data, env, folder)


def edited(raw: str, shown: str) -> bool:
    """Whether a field was changed from what the page showed, ignoring the
    spaces, blank lines and line endings a browser may add."""
    def lines(text: str) -> list[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]
    return lines(raw) != lines(shown)


def section_id(heading: str) -> str:
    """A section's heading's id: never a field's name, which are ids too."""
    return "section-" + "-".join(heading.lower().split())


def _shown(value, kind: str) -> str:
    """A config value as the form shows it: lists one per line in a text area
    or as time windows, comma-separated in a one-line box."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ("\n" if kind in ("area", "windows") else ", ").join(str(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k} = {v}" for k, v in value.items())
    return str(value)


def _walk_text(values: Mapping[str, str]) -> str:
    """Walks by stop label as one text, "A = 5" a line, to tell whether they changed."""
    return "\n".join(f"{label} = {value.strip()}" for label, value in values.items() if value.strip())


def _default(f: Field, data: dict):
    """What a field shows when the settings don't say: what the loader uses then."""
    if f.name == "rotate":
        return 180 if data.get("rotate_180") else f.default  # the older spelling of rotate = 180
    if f.group == "board" and f.section == "bus":
        bus = data.get("bus")
        return schema.bus_defaults(schema.bus_source(bus))[f.key]
    return f.default


def _words(f: Field, value: str) -> str:
    """A field's value in words, for saying what a save changed."""
    if f.kind in ("radios", "select"):
        return dict(f.options).get(value, value) or "none"
    if f.kind == "check":
        return "on" if value == "on" else "off"
    if not value:
        return "none"
    if f.unit == "%":
        return f"{value}%"
    return f"{value} {f.unit}" if f.unit else value


class GroupForm:
    """The form for some settings pages (usually one), for a signed-in
    owner: drawn from the settings in force, and saved into the store.

    `pages` are schema.PAGES to show (not the overview); `action` is where
    the form posts; `load_secrets` gives the environment with every key
    saved; `install` says which views can be switched on (a choice only
    when it offers more than one) and which are on by default; `noun` is
    what shows the departures ("board"), for the words when trains and
    buses are both off; `header` draws what goes before the form, given the
    form token, and `footer` what goes after it; `applies` says when saved
    changes take effect, in the save bar; `setup` is the setup wizard's
    address, for the sections with nothing set up; `overview` is the
    Overview's, which the Board page points to for how the board is doing."""

    def __init__(self, store: Store, pages: Sequence[str], action: str,
                 load_secrets: Callable[[], Mapping[str, str]], *, install: Install, noun: str,
                 header: Callable[[str], str] = lambda csrf: "", footer: Callable[[], str] = lambda: "",
                 applies: str = "", setup: str = "", overview: str = "") -> None:
        # Not the overview, nor the keys page: it is a keyspage.KeysPage.
        unknown = set(pages) - {page for page in schema.PAGES[1:] if page != "keys"}
        if unknown:
            raise ValueError(f"Unknown settings pages: {', '.join(sorted(unknown))}")
        self._store = store
        self._pages = tuple(pages)
        self._action = action
        self._load_secrets = load_secrets
        self._install = install
        self._noun = noun
        self._header = header
        self._footer = footer
        self._applies = applies
        self._setup = setup
        self._overview = overview

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

    def _fields(self, data: dict, theme: str, text_size: object = None) -> list[Field]:
        """The fields on the form: those of its pages, while the view that uses
        each is on. The TransportAPI allowance only while TransportAPI is used.
        The board's room for modes goes by `theme` and `text_size` (as sent,
        or as saved when either is empty or no choice)."""
        display = data.get("display", schema.BOARD_DEFAULTS["display"])
        on = self._views_on(data)
        theme = theme if theme in THEMES else str(data.get("theme", schema.BOARD_DEFAULTS["theme"]))
        size = _text_size(text_size)
        if size is None:
            size = _text_size(data.get("text_size", schema.BOARD_DEFAULTS["text_size"]))
        try:
            chosen = len(modes.listed(data, "board") or ())
        except ConfigError:
            chosen = 0  # saving says what is wrong
        found = []
        for f in schema.fields(str(display), theme, size, chosen):
            if f.page not in self._pages or (f.group != "general" and f.group not in on):
                continue
            if f.kind == "check" and (not self._choosing_views() or f.name.removeprefix("view_")
                                      not in self._install.offered):
                continue
            if f.name == "bus_daily_budget" and not self._transportapi(data):
                continue
            if f.kind == "modes" and len(modes.set_up(data)) < 2:
                continue  # nothing to choose between
            found.append(f)
        return found

    def _transportapi(self, data: dict) -> bool:
        """Whether the board's buses may come from TransportAPI: its source,
        or standing in for the timetable with its keys saved."""
        if schema.bus_source(data.get("bus")) == "transportapi":
            return True
        return bool(str(self._load_secrets().get("TAPI_APP_KEY", "")).strip())

    def _choosing_views(self) -> bool:
        return "general" in self._pages and len(self._install.offered) > 1

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
            if f.name == "bank_holidays":
                values[f.name] = schema.holiday_choice(data)
                continue
            if f.kind == "per_stop":
                walks = self._walks_shown(f, data)
                values |= walks
                values[f.name] = _walk_text({box[len(f.name) + 1:]: value for box, value in walks.items()})
                continue
            if f.kind == "show":
                values |= _show_shown(f, data)
                continue
            if f.kind == "modes":
                values |= _modes_shown(f, data)
                continue
            source = data if f.section is None else data.get(f.section, {})
            value = source.get(f.key) if isinstance(source, dict) else None
            if value is None:
                value = _default(f, data)
            if f.check is schema.on_off:
                value = "false" if value is False else "true"  # on unless turned off
            values[f.name] = _shown(value, f.kind)
        return values

    # Saving

    def _loads(self, data: dict, env: Mapping[str, str]) -> None:
        check_views(data, env, self._store.folder, self._install)

    def save(self, fields: dict[str, str], changed: list[str] | None = None) -> dict[str, str]:
        """Save the fields; field name -> error, or empty when saved. Once
        saved, what changed is added to `changed` in words, if given."""
        try:
            data, broken = self._read()
        except (OSError, ConfigError) as error:
            return {"_form": f"Not saved: the settings can't be read ({error})"}
        form = self._fields(data, fields.get("theme", ""), fields.get("text_size"))
        # A section switched off keeps what is saved for it, for switching it back on:
        # a field left as shown is left alone, unchecked (it may be the template's
        # placeholder), and one changed is checked and saved, as with it on.
        off = {f.needs for f in form if f.key == "enabled" and fields.get(f.name) == "false"}
        shown = self._current(data)
        # Time windows come as rows: made into one window a line, as a text area sent them, and each
        # row's mistakes, which count as a change.
        fields = dict(fields)
        rows: dict[str, dict[str, str]] = {}
        for f in form:
            if f.kind == "windows":
                fields[f.name], rows[f.name] = windowrows.read(f.name, fields, shown.get(f.name, ""))
        errors: dict[str, str] = {}
        changes: dict = {}
        for f in form:
            if f.kind == "check":
                continue  # the views, below
            if f.needs is not None and f.needs not in data and (f.name not in _SETS_UP
                                                                 or not fields.get(f.name, "").strip()):
                continue  # not set up: setup or config.toml adds it, or typing in a field that does
            if f.kind == "per_stop":
                errors |= self._save_walks(f, data, fields, shown, f.needs in off, changes)
                continue
            if f.kind == "show":
                errors |= _save_show(f, data, fields, shown, changes)
                continue
            if f.kind == "modes":
                errors |= _save_modes(f, data, fields, shown, changes)
                continue
            if (f.needs in off and f.key != "enabled" and not rows.get(f.name)
                    and not edited(fields.get(f.name, ""), shown[f.name])):
                continue
            if rows.get(f.name):
                errors |= rows[f.name]
                continue
            try:
                value = f.check(fields.get(f.name, ""))
            except Invalid as error:
                errors[f.name] = str(error)
                continue
            target = changes if f.section is None else changes.setdefault(f.section, {})
            if f.name == "bank_holidays":
                target.update(schema.holiday_settings(str(value)))
                continue
            # Every key is written, empty ones too, so clearing a field clears
            # config.toml's value rather than letting it show through.
            target[f.key] = "" if value is None else value
        if self._choosing_views():
            errors |= self._views(fields, changes)
        if "trains_buses" in self._pages:
            errors |= neither_on(data, fields, self._noun)
        if errors:
            return errors
        # Merged into what is saved, so settings this form doesn't show (setup's
        # bus operators and routes, the weather position, the other view's)
        # survive; a broken file can't be merged into, so it is replaced.
        try:
            save_to(self._store.target(self._loads), changes, {}, self._load_secrets(), replace=bool(broken))
        except OverlayError as error:
            return {"_form": str(error)}
        if changed is not None:
            changed.extend(self._changed(form, shown))
        return {}

    def _changed(self, form: list[Field], before: dict[str, str]) -> list[str]:
        """What a save changed, in words: each field's label, and its old and
        new values unless it is free text."""
        try:
            after = self._current(self._read()[0])
        except (OSError, ConfigError):
            return []
        said = []
        for f in form:
            if f.kind == "modes" and f.name not in after:
                continue  # a mode switched off leaves nothing to choose between: the list itself is as it was
            old, new = before.get(f.name, ""), after.get(f.name, "")
            same = not edited(old, new) if f.kind in ("text", "area", "windows", "per_stop") else old == new
            if same:
                continue
            if f.kind in ("text", "area", "windows", "per_stop", "show", "modes"):
                said.append(f.label)
            else:
                said.append(f"{f.label}, {_words(f, old)} to {_words(f, new)}")
        return said

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

    # Walks to each stop

    def _stop_labels(self, data: dict, fields: Mapping[str, str] | None = None, section: str = "bus") -> list[str]:
        """The stops' labels, the bus stops' or the tram stops' (`section`): as
        typed in their stop codes when that is right, else as saved."""
        typed = (fields or {}).get(f"{section}_stops")
        if typed is not None:
            try:
                return list((schema.tram_stops if section == "metro" else schema.stops)(typed))
            except Invalid:
                pass  # its own error says so; the walks go by the saved stops
        table = data.get(section)
        stops = table.get("stops") if isinstance(table, dict) else None
        if not isinstance(stops, dict):
            return []
        return [label for label in stops if isinstance(label, str) and schema.LABEL.fullmatch(label)]

    def _walk_boxes(self, f: Field, data: dict) -> list[tuple[str, str]]:
        """Each stop's walk box: its name (the field's, then the label) and its
        label in words, like "Henry Street (A)", from the bus timetable when it
        has the stop, or a tram stop's from the bundled list."""
        section = str(f.section)
        table = data.get(section)
        stops = table.get("stops") if isinstance(table, dict) else None
        stops = stops if isinstance(stops, dict) else {}
        if not stops:
            names = {}
        elif section == "metro":
            names = places.tram_stop_names()
        else:
            names = places.stop_names(self._store.folder)
        boxes = []
        for label in self._stop_labels(data, section=section):
            name = names.get(str(stops.get(label)))
            boxes.append((f"{f.name}_{label}", f"{name} ({label})" if name else f"Stop {label}"))
        return boxes

    def _walks_shown(self, f: Field, data: dict) -> dict[str, str]:
        """What each stop's walk box shows: its own walk, else the one for
        every stop ("*", or a single number)."""
        table = data.get(str(f.section))
        walks = table.get(f.key) if isinstance(table, dict) else None
        shown = {}
        for label in self._stop_labels(data, section=str(f.section)):
            value = walks.get(label, walks.get("*")) if isinstance(walks, dict) else walks
            shown[f"{f.name}_{label}"] = _shown(value, "text")
        return shown

    def _save_walks(self, f: Field, data: dict, fields: Mapping[str, str], shown: Mapping[str, str], off: bool,
                    changes: dict) -> dict[str, str]:
        """The walk boxes into `changes` as a table by stop label (with any "*"
        kept), as setup saves them; or each box's error. A section switched off
        keeps its walks unless one was changed. Nothing for no stops."""
        labels = self._stop_labels(data, fields, str(f.section))
        if not labels:
            return {}
        typed = {label: fields.get(f"{f.name}_{label}", "") for label in labels}
        if off and not edited(_walk_text(typed), shown.get(f.name, "")):
            return {}
        section = data.get(str(f.section))
        saved = section.get(f.key) if isinstance(section, dict) else None
        table = {"*": saved["*"]} if isinstance(saved, dict) and "*" in saved else {}
        errors = {}
        check = schema.number(0, 60, optional=True)
        for label, raw in typed.items():
            try:
                minutes = check(raw)
            except Invalid as error:
                errors[f"{f.name}_{label}"] = str(error)
                continue
            if minutes is not None:
                table[label] = minutes
        if not errors:
            changes.setdefault(f.section, {})[f.key] = table
        return errors

    def _walks(self, f: Field, data: dict, shown: Mapping[str, str], errors: Mapping[str, str]) -> str:
        """The walk boxes, one a stop, in a group of their own under the field's label."""
        boxes = self._walk_boxes(f, data)
        if not boxes:
            return ""
        attrs, notes = _described(f.name, f.hint, None)
        inside = "".join(_field(replace(f, name=box, label=words, kind="number"), shown.get(box, ""),
                                errors.get(box), "") for box, words in boxes)
        return (f'<fieldset class="field walks"{attrs}><legend>{html.escape(f.label)}</legend>{notes}{inside}'
                "</fieldset>")

    # Drawing

    def _sections(self, fields: list[Field], data: dict) -> list[tuple[str, str, list[Field]]]:
        """The form's sections in order: each one's page and heading, and its fields."""
        found: dict[tuple[str, str], list[Field]] = {}
        for f in fields:
            found.setdefault((f.page, f.heading), []).append(f)
        return [(page, heading, here) for (page, heading), here in found.items()]

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None) -> str:
        errors = errors or {}
        e = html.escape
        unreadable = ""
        try:
            data, broken = self._read()
        except (OSError, ConfigError) as error:
            data, broken, unreadable = {}, "", str(error)
        shown = {**self._current(data), **(values or {})}
        fields = self._fields(data, shown.get("theme", ""), shown.get("text_size"))
        unsaved: set[str] = set()
        if values is not None:  # drawn again as sent: an unticked box isn't sent, so it stays unticked
            shown |= {f.name: values.get(f.name, "") for f in fields if f.kind == "check"}
            shown |= {box: values.get(box, "") for f in fields if f.kind == "show"
                      for box in (*_leave_boxes(f, data), *_show_boxes(f, data))}
            shown |= {box: values.get(box, "") for f in fields if f.kind == "modes" for box in _mode_boxes(f, data)}
            unsaved = self._unsaved(fields, data, values)
        sections = self._sections(fields, data)
        out = [self._header(csrf)]
        if errors:
            out.append(self._summary(fields, data, errors))
        if message:
            out.append(f'<p class="notice-ok" role="status" tabindex="-1" autofocus>{icon("check")} '
                       f"{e(message)}</p>")
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
        if "board" in self._pages and self._overview:  # each feed and the last redraw are the Overview's
            out.append(f'<p class="lead"><a href="{e(self._overview)}">See how the board is doing</a> on the '
                       "Overview.</p>")
        if len(sections) > 3:
            links = "".join(f'<li><a href="#{section_id(heading)}">{e(heading)}</a></li>'
                            for _, heading, _ in sections)
            out.append(f'<nav class="index" aria-label="On this page"><ul>{links}</ul></nav>')
        out.append(f'<form method="post" action="{e(self._action)}" novalidate class="settings-form">')
        out.append(f'<input type="hidden" name="csrf" value="{e(csrf)}">')
        for page, heading, here in sections:
            out.append(self._section(page, heading, here, data, shown, errors, unsaved))
        # Save first: the first button is the one Enter in a box presses.
        out.append('<div class="savebar">')
        if self._applies:
            out.append(f'<p class="applies" id="applies">{e(self._applies)}</p>'
                       '<button type="submit" class="primary" aria-describedby="applies">Save changes</button>')
        else:
            out.append('<button type="submit" class="primary">Save changes</button>')
        out.append("</div>")
        out.append("</form>")
        out.append(self._footer())
        return "".join(out)

    def _section(self, page: str, heading: str, fields: list[Field], data: dict, shown: dict[str, str],
                 errors: dict[str, str], unsaved: set[str]) -> str:
        """One section: a fieldset with the heading in its legend, a pill when
        it is switched off, its description, then its fields, the advanced
        ones in a disclosure; or, with nothing set up, how to set it up."""
        e = html.escape
        needs = {f.needs for f in fields}
        single = next(iter(needs)) if len(needs) == 1 else None
        missing = single in _MISSING and single not in data
        here = [f for f in fields if f.needs is None or f.needs in data]
        if not here and fields and not missing:  # a mixed section with none of it set up
            single = next(f.needs for f in fields if f.needs in _MISSING)
            missing = True
        pill = ""
        if missing:
            pill = f' <span class="pill pill-neutral">{icon("minus")} Not set up</span>'
        elif single in _MISSING and self._off(str(single), fields, data, shown):
            pill = f' <span class="pill pill-neutral">{icon("minus")} Off</span>'
        out = [f'<fieldset class="card"><legend><h2 id="{section_id(heading)}">{e(heading)}</h2>{pill}</legend>']
        note = NOTES.get((page, heading), "")
        if note:
            out.append(f'<p class="hint lead">{e(note)}</p>')
        if missing:
            before, link, after = _MISSING[str(single)]
            typed = [f for f in fields if f.name in _SETS_UP]
            if typed:
                after = _SETS_UP[typed[0].name]
            where = f'<a href="{e(self._setup)}">{e(link)}</a>' if self._setup else e(link)
            out.append(f'<p class="empty">{e(before)} {where}{e(after)}</p>')
            out.extend(self._draw(f, data, shown, errors, unsaved) for f in typed)
            out.append("</fieldset>")
            return "".join(out)
        common = [f for f in here if f.tier == "common"]
        advanced = [f for f in here if f.tier == "advanced"]
        for f in common:
            out.append(self._draw(f, data, shown, errors, unsaved))
            if f.name == "bus_enabled":
                out.append(self._stops(data))
        out.append(self._readonly(page, heading, data))
        if advanced:
            opened = " open" if any(f.name in errors for f in advanced) else ""
            out.append(f'<details class="advanced"{opened}><summary>Advanced</summary>')
            out.extend(self._draw(f, data, shown, errors, unsaved) for f in advanced)
            out.append("</details>")
        out.append("</fieldset>")
        return "".join(out)

    @staticmethod
    def _off(needs: str, fields: list[Field], data: dict, shown: dict[str, str]) -> bool:
        """Whether section `needs` is switched off: as its switch shows, if it
        is in `fields`, else as the settings say."""
        switch = f"{needs}_enabled"
        if any(f.name == switch for f in fields):
            return shown.get(switch) == "false"
        section = data.get(needs)
        return isinstance(section, dict) and section.get("enabled") is False

    def _draw(self, f: Field, data: dict, shown: dict[str, str], errors: dict[str, str], unsaved: set[str]) -> str:
        """A field with what it shows and its errors: time windows as rows,
        with each row's, and the walks as a box for each stop, with each box's.
        One in `unsaved` says so on its first tag, for settings.js."""
        if f.kind == "windows":
            drawn = windowrows.render(f, shown, errors)
        elif f.kind == "per_stop":
            drawn = self._walks(f, data, shown, errors)
        elif f.kind == "show":
            drawn = _show_list(f, data, shown, errors.get(f.name))
        elif f.kind == "modes":
            drawn = _modes_list(f, data, shown, errors.get(f.name))
        else:
            drawn = self._field(f, data, shown.get(f.name, ""), errors.get(f.name))
        if f.name in unsaved and drawn:
            drawn = drawn.replace(">", " data-unsaved>", 1)
        return drawn

    def _unsaved(self, fields: list[Field], data: dict, values: Mapping[str, str]) -> set[str]:
        """The fields whose `values` (a refused save's) differ from the
        settings in force: drawn again as typed, they are still not saved."""
        current = self._current(data)
        found = set()
        for f in fields:
            was = current.get(f.name, "")
            if f.kind == "windows":
                text, mistakes = windowrows.read(f.name, values, was)
                differs = bool(mistakes) or edited(text, was)
            elif f.kind == "per_stop":
                differs = any(edited(values.get(box, ""), current.get(box, "")) for box, _ in self._walk_boxes(f, data))
            elif f.kind == "show":
                boxes = (*_leave_boxes(f, data), *_show_boxes(f, data))
                differs = bool(values.get(_preset_name(f))) or any(
                    values.get(box, "") != current.get(box, "") for box in boxes)
            elif f.kind == "modes":
                differs = any(values.get(box, "") != current.get(box, "") for box in _mode_boxes(f, data))
            elif f.kind in ("text", "number", "area"):
                differs = edited(values.get(f.name, ""), was)
            else:
                differs = values.get(f.name, "") != was
            if differs:
                found.add(f.name)
        return found

    def _field(self, f: Field, data: dict, value: str, error: str | None) -> str:
        if f.kind == "check":
            return _checkbox(f, value, error)
        if f.kind == "radios":
            return _radios(f, value, error)
        hint = f.hint
        crs = data.get("train", {}).get("crs") if isinstance(data.get("train"), dict) else None
        if f.name == "train_crs" and isinstance(crs, str) and schema.CRS.fullmatch(crs):
            hint = f"{hint} Currently {places.station(crs.upper())}."
        return _field(f, value, error, hint)

    def _stops(self, data: dict) -> str:
        """The bus stops, read only: setup changes them, or Advanced by hand."""
        bus = data.get("bus")
        stops = bus.get("stops") if isinstance(bus, dict) else None
        if not isinstance(stops, dict) or not stops:
            return ""
        e = html.escape
        names = places.stop_names(self._store.folder)
        listed = "".join(f"<li>{e(str(label))}: {e(names[code])} ({e(str(code))})</li>" if code in names
                         else f"<li>{e(str(label))}: {e(str(code))}</li>" for label, code in stops.items())
        change = f'<a href="{e(self._setup)}?step=stops">Change them with setup</a>' if self._setup else ""
        return (f'<dl class="readonly"><div><dt>Stops</dt><dd><ul class="stops">{listed}</ul>{change}</dd></div>'
                "</dl>")

    def _readonly(self, page: str, heading: str, data: dict) -> str:
        """What a section shows but can't change: where it is set instead."""
        e = html.escape
        if (page, heading) == ("board", "Display"):
            display = str(data.get("display", schema.BOARD_DEFAULTS["display"]))
            panel = DISPLAY_PANELS.get(display)
            size = f" ({panel.width}×{panel.height})" if panel is not None else ""
            return (f'<dl class="readonly"><div><dt>Display</dt><dd>{e(display)}{size}</dd></div></dl>'
                    '<p class="hint">Changing the display means switching services, so it is set in '
                    "config.toml.</p>")
        if (page, heading) == ("trains_buses", "Weather") and isinstance(data.get("weather"), dict):
            where = data["weather"]
            place = f"{where.get('lat', '')}, {where.get('lon', '')}"
            return (f'<dl class="readonly"><div><dt>Location</dt><dd>{e(place)}</dd></div></dl>'
                    '<p class="hint">Rounded to about 1 km before it is sent to Open-Meteo. Setup sets it from '
                    "home, or set it in config.toml.</p>")
        if (page, heading) == ("dashboard", "Departures") and isinstance(data.get("bus"), dict) \
                and schema.bus_source(data["bus"]) != "bods":
            return f'<p class="hint">{e(BUS_NOTE)}</p>'
        return ""

    def _summary(self, fields: list[Field], data: dict, errors: dict[str, str]) -> str:
        """The error summary: each problem named by section and field, linked
        to the field (a choice's first option)."""
        e = html.escape
        names = {f.name: f.label if f.heading == f.label else f"{f.heading} – {f.label}" for f in fields}
        anchors = {}
        for f in fields:
            for name in errors:
                if f.kind == "windows" and (name == f.name or name.startswith(f.name + "-")):
                    anchors[name], names[name] = windowrows.anchor(f.name, name), windowrows.said(f, name)
            if f.kind == "per_stop":
                names |= {box: f"{f.heading} – {f.label}, {words}" for box, words in self._walk_boxes(f, data)}
            if f.kind == "show" and f.name in errors:
                anchors[f.name] = [*_leave_boxes(f, data), *_show_boxes(f, data)][0]
            if f.kind == "modes" and f.name in errors and _mode_boxes(f, data):
                anchors[f.name] = _mode_boxes(f, data)[0]
        items = "".join(
            f'<li><a href="#{e(anchors.get(name, name))}">{e(names.get(name, name))}: {e(text)}</a></li>'
            if name != "_form" else f"<li>{e(text)}</li>"
            for name, text in errors.items()
        )
        return (
            '<div class="summary" role="alert" tabindex="-1" id="problems" autofocus>'
            f"<h2>Not saved: fix {len(errors)} {'thing' if len(errors) == 1 else 'things'}</h2>"
            f"<ul>{items}</ul></div>"
        )


def _described(name: str, hint: str, error: str | None, also: str = "") -> tuple[str, str]:
    """A field's aria attributes, and its hint and error paragraphs; `also`
    is the id of another paragraph about it, like a key test's result
    (web/keyspage.py)."""
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


# A view's show list (settings/show.py): a tickbox for each item, named
# "<field>-<item>", and the presets as a choice, "<field>_preset", that
# saving applies in place of the ticks. settings.js swaps the choice for
# buttons that tick the boxes at once.

_NO_PRESET = "None: as ticked below"
_PRESET_HINT = "Saving with a preset picked shows just what it includes, whatever is ticked."
_SCREENS_ONLY = "Screens only."


def _preset_name(f: Field) -> str:
    return f"{f.name}_preset"


# When to leave, in a show list: a tickbox for each mode set up, saved as
# [view] leave_modes (settings/modes.py). On the dashboard they stand for
# Next to catch's one tickbox, which is on while any is ticked; the board
# has no such item, so its group comes first. Every preset ticks them all
# where it has the item (the board's has no item: always).
_LEAVE_ITEM = {"board": None, "dashboard": "next_to_catch"}
_BOARD_LEAVE = ("When to leave",
                "“Leave by” and “Leave in”, for the modes ticked. Untick them all for none.")
_DASHBOARD_LEAVE = ("From the modes ticked. With none ticked, or nothing to catch, the time and the weather "
                    "take its place.")


def _leave_boxes(f: Field, data: dict) -> list[str]:
    """The names of a show list's leave tickboxes, one a mode set up, in order."""
    return [f"{f.name}-leave-{key}" for key in modes.set_up(data)]


def _leave_item(f: Field, data: dict) -> str | None:
    """The show list's item its leave tickboxes stand for, while there are any."""
    return _LEAVE_ITEM[f.group] if _leave_boxes(f, data) else None


def _show_boxes(f: Field, data: dict) -> list[str]:
    """The names of a show list's item tickboxes, in order: not the one its
    leave tickboxes stand for."""
    replaced = _leave_item(f, data)
    return [f"{f.name}-{key}" for key in show.keys(f.group) if key != replaced]


def _show_shown(f: Field, data: dict) -> dict[str, str]:
    """What a show list shows for settings `data`: each item's box ticked
    ("on") or not, each leave box ticked while the view gives that mode
    leave times, no preset picked, and the list and the leave modes
    themselves as their keys separated by spaces, to tell whether they
    changed. A list that doesn't load shows the view's default; saving says
    what is wrong."""
    try:
        on = show.shown(data, f.group)
    except ConfigError:
        on = show.default(f.group, data)
    try:
        leave = modes.leave(data, f.group)
    except ConfigError:
        leave = modes.set_up(data)
    item = _LEAVE_ITEM[f.group]
    if item is not None and item not in on:
        leave = ()  # Next to catch left out: no mode ticked
    values = {f"{f.name}-{key}": "on" if key in on else "" for key in show.keys(f.group)}
    values |= {f"{f.name}-leave-{key}": "on" if key in leave else "" for key in modes.set_up(data)}
    values[f.name] = " ".join(show.ordered(f.group, on))
    values[f"{f.name}-leave"] = " ".join(leave)
    values[_preset_name(f)] = ""
    return values


def _save_show(f: Field, data: dict, fields: Mapping[str, str], shown: Mapping[str, str],
               changes: dict) -> dict[str, str]:
    """A show list into `changes`, from its preset if one was picked, else
    from its ticks; and its leave modes; or its error. A list left as shown
    isn't written, so a view without one keeps showing whatever it shows by
    default; nor are leave modes left as shown."""
    names = tuple(name for name, _ in show.PRESETS)
    boxes = _leave_boxes(f, data)
    item = _leave_item(f, data)
    ready = modes.set_up(data)
    try:
        picked = fields.get(_preset_name(f), "")
        if picked:
            keys: tuple[str, ...] | list[str] = show.preset(f.group, schema.choice(names)(picked))
            leave = list(ready) if item is None or item in keys else []
        else:
            leave = [key for key in ready if schema.ticked(fields.get(f"{f.name}-leave-{key}", ""))]
            keys = [key for key in show.keys(f.group)
                    if (bool(leave) if key == item else schema.ticked(fields.get(f"{f.name}-{key}", "")))]
        value = show.check(f.group)(" ".join(keys))  # f.check, typed
    except Invalid as error:
        return {f.name: str(error)}
    if " ".join(value) != shown.get(f.name):
        changes.setdefault(f.section, {})[f.key] = value
    if boxes and " ".join(leave) != shown.get(f"{f.name}-leave"):
        changes.setdefault(f.section, {})["leave_modes"] = leave
    return {}


def _leave_group(f: Field, data: dict, shown: Mapping[str, str], label: str, hint: str, presets: str) -> str:
    """The leave tickboxes: a group of their own in the show list, a box a
    mode set up, each saying which presets tick it (data-presets)."""
    e = html.escape
    hint_id = f"{f.name}-leave-hint"
    boxes = "".join(
        _checkbox(replace(f, name=name, label=modes.mode(key).label, kind="check", hint=""), shown.get(name, ""),
                  None, f' data-presets="{e(presets)}"')
        for key, name in zip(modes.set_up(data), _leave_boxes(f, data), strict=True))
    return (f'<fieldset class="field leave-modes" aria-describedby="{hint_id}"><legend>{e(label)}</legend>'
            f'<p class="hint" id="{hint_id}">{e(hint)}</p><div class="options inline">{boxes}</div></fieldset>')


def _show_list(f: Field, data: dict, shown: Mapping[str, str], error: str | None) -> str:
    """A show list: a group of its own, under the field's label, with the
    presets first, then a tickbox for each item with what it is, and the
    leave tickboxes (_LEAVE_ITEM). Each box says which presets include it
    (data-presets), for settings.js."""
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    attrs = attrs.replace(' aria-invalid="true"', "")  # a fieldset takes none: the error says it of the group
    hidden = ' class="vh"' if f.label == f.heading else ""
    presets = replace(f, name=_preset_name(f), label="Fill in from a preset", kind="radios", hint=_PRESET_HINT,
                      options=(("", _NO_PRESET), *show.PRESETS))
    boxes = []
    replaced = _leave_item(f, data)
    if _leave_boxes(f, data) and replaced is None:
        boxes.append(_leave_group(f, data, shown, *_BOARD_LEAVE, " ".join(name for name, _ in show.PRESETS)))
    for item in show.items(f.group):
        hint = f"{item.hint} {_SCREENS_ONLY}" if item.screen else item.hint
        if item.key == replaced:
            boxes.append(_leave_group(f, data, shown, item.label, f"{hint} {_DASHBOARD_LEAVE}", " ".join(item.presets)))
            continue
        box = replace(f, name=f"{f.name}-{item.key}", label=item.label, kind="check", hint=hint)
        boxes.append(_checkbox(box, shown.get(box.name, ""), None, f' data-presets="{e(" ".join(item.presets))}"'))
    choice = _radios(presets, shown.get(presets.name, ""), None).replace("field radios", "field radios show-presets", 1)
    return (f'<fieldset class="field show-list{" has-error" if error else ""}"{attrs}>'
            f"<legend{hidden}>{e(f.label)}</legend>{notes}{choice}"
            f'<div class="show-items">{"".join(boxes)}</div></fieldset>')


# A view's modes list (settings/modes.py): a tickbox for each mode set up,
# named "<field>-<mode>", in modes.MODES' order.


def _text_size(value: object) -> int | None:
    """A text size as saved or sent, if it is one of the choices; else None."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 9 <= value <= 16:
        return None
    return value


def _mode_boxes(f: Field, data: dict) -> list[str]:
    """The names of a modes list's tickboxes, one a mode set up, in order."""
    return [f"{f.name}-{key}" for key in modes.set_up(data)]


def _modes_shown(f: Field, data: dict) -> dict[str, str]:
    """What a modes list shows for settings `data`: each mode set up ticked
    ("on") while the view shows it, and the list itself as its keys
    separated by spaces, to tell whether it changed. A list that doesn't
    load shows every mode set up; saving says what is wrong."""
    try:
        on = board_modes(data) if f.group == "board" else modes.shown(data, f.group)
    except ConfigError:
        on = modes.set_up(data)
    values = {f"{f.name}-{key}": "on" if key in on else "" for key in modes.set_up(data)}
    values[f.name] = " ".join(key for key in modes.set_up(data) if key in on)
    return values


def _save_modes(f: Field, data: dict, fields: Mapping[str, str], shown: Mapping[str, str],
                changes: dict) -> dict[str, str]:
    """A modes list into `changes`, from its ticks; or its error. A list
    left as shown isn't written, so a view without one keeps showing every
    mode set up, a mode set up later included; one the view has is checked
    as it stands, as the look or text size sent may leave less room."""
    try:
        ticked = [key for key in modes.set_up(data) if schema.ticked(fields.get(f"{f.name}-{key}", ""))]
        try:
            kept = modes.listed(data, f.group)
        except ConfigError:
            kept = None
        changed = " ".join(ticked) != shown.get(f.name)
        # The ticks, in modes.MODES' order as set_up gives them; or the list kept.
        value = f.check(" ".join(ticked if changed or kept is None else kept))
    except Invalid as error:
        return {f.name: str(error)}
    if changed:
        changes.setdefault(f.section, {})[f.key] = value
    return {}


def _modes_list(f: Field, data: dict, shown: Mapping[str, str], error: str | None) -> str:
    """A modes list: a group of its own, under the field's label, with a
    tickbox for each mode set up."""
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    attrs = attrs.replace(' aria-invalid="true"', "")  # a fieldset takes none: the error says it of the group
    hidden = ' class="vh"' if f.label == f.heading else ""
    boxes = []
    for key in modes.set_up(data):
        box = replace(f, name=f"{f.name}-{key}", label=modes.mode(key).label, kind="check", hint="")
        boxes.append(_checkbox(box, shown.get(box.name, ""), None))
    return (f'<fieldset class="field modes-list{" has-error" if error else ""}"{attrs}>'
            f"<legend{hidden}>{e(f.label)}</legend>{notes}"
            f'<div class="options inline">{"".join(boxes)}</div></fieldset>')


def _label(f: Field) -> str:
    """A field's label, its unit said too for screen readers (the suffix shown after the box is hidden from them).
    A label its section's heading already shows, like Title's, is said but not shown again."""
    unit = f'<span class="vh"> ({html.escape(_SPOKEN.get(f.unit, f.unit))})</span>' if f.unit else ""
    hidden = ' class="vh"' if f.label == f.heading else ""
    return f'<label for="{f.name}"{hidden}>{html.escape(f.label)}{unit}</label>'


def _field(f: Field, value: str, error: str | None, hint: str | None = None) -> str:
    e = html.escape
    attrs, notes = _described(f.name, f.hint if hint is None else hint, error)
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
        extra = ' inputmode="numeric" autocomplete="off"' if f.kind == "number" else ""
        extra += f' class="w-{f.width}"' if f.width else ""
        extra += _KEYBOARDS.get(f.keyboard, "")
        suggestions = ""
        if f.options:
            extra += f' list="{f.name}-suggestions"'
            listed = "".join(f'<option value="{e(v)}"></option>' for v, _ in f.options)
            suggestions = f'<datalist id="{f.name}-suggestions">{listed}</datalist>'
        control = f'<input id="{f.name}" name="{f.name}" type="text" value="{e(value)}"{extra}{attrs}>{suggestions}'
        if f.unit:
            control = f'<div class="with-unit">{control}<span class="unit" aria-hidden="true">{e(f.unit)}</span></div>'
    return f'<div class="field{" has-error" if error else ""}">{_label(f)}{notes}{control}</div>'


def _radios(f: Field, value: str, error: str | None) -> str:
    """A short choice: a radio button each, in a fieldset of its own whose
    legend is the field's label. The first button's id is the field's name,
    for the links to it; the hint and error describe the group. Two choices,
    or a few short ones, sit side by side."""
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    # The error says it of the group: a fieldset takes no aria-invalid.
    attrs = attrs.replace(' aria-invalid="true"', "")
    options = []
    for index, (v, text) in enumerate(f.options):
        box = f.name if index == 0 else f"{f.name}-{index}"
        checked = " checked" if v == value else ""
        options.append(f'<div class="option"><input type="radio" id="{box}" name="{f.name}" value="{e(v)}"{checked}>'
                       f'<label for="{box}">{e(text)}</label></div>')
    inline = " inline" if len(f.options) <= 2 or all(len(text) <= 4 for _, text in f.options) else ""
    return (f'<fieldset class="field radios{" has-error" if error else ""}"{attrs}><legend>{e(f.label)}</legend>'
            f'{notes}<div class="options{inline}">{"".join(options)}</div></fieldset>')


def _checkbox(f: Field, value: str, error: str | None, extra: str = "") -> str:
    """A checkbox, its label beside it and its hint and error below.
    `extra` is more attributes for the box's field."""
    e = html.escape
    attrs, notes = _described(f.name, f.hint, error)
    checked = " checked" if value == "on" else ""
    return (
        f'<div class="field check{" has-error" if error else ""}"{extra}>'
        f'<input id="{f.name}" name="{f.name}" type="checkbox" value="on"{checked}{attrs}>'
        f'<label for="{f.name}">{e(f.label)}</label>{notes}</div>'
    )
