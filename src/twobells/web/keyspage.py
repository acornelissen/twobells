"""The settings' Keys page: one small form per key, each saved and tested on its own.

Each key the views on use has a form of its own (web/keystate.py's FORMS;
TransportAPI's app ID and key share one), headed by what it unlocks, with a
pill saying its state: not set, needed for something on, saved but not
tested, works or refused (with when it was tested). The engineering works
address sits in its key's form.

A form has three buttons, Save key first so that Enter in a box saves:
- Save key (do=save) writes that form's key (and the address) alone,
  merged into what is saved, so every other key and setting stays as it is.
- Save and test (do=save_test) saves, then tests the key just saved: no
  need to paste it again. With nothing to save, it tests the saved key.
- Test saved key (do=test), shown while a key is saved, tests it as saved.
The page's stylesheet shows Save and test while a box has something typed
in it and Test saved key while they are empty; without it, both show.

Keys are write-only: a box is never filled, and no part of a key is in any
page, error or log line. A key replaced forgets its last test. A test's
result is kept as works or refused, with its time (settings/keyresults.py),
never the key or the test's words; the words are shown once, on the page
after the test. The site answers each post with a redirect (formpages,
FormPages.key_form), so reloading the page can't post a key again.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.errors import ConfigError, safe_message
from ..settings import keyresults, schema
from ..settings.config import merge
from ..settings.overlay import OverlayError, save_to
from ..settings.schema import KEY_VALUE, Invalid
from ..setup import keytest
from ..sources.disruptions import kb_url_is_placeholder
from . import keystate
from .groupform import _described, _field, check_views, edited, section_id
from .icons import icon
from .keystate import KeyForm, KeyState

if TYPE_CHECKING:
    from ..settings.installs import Install
    from ..settings.store import Store

# (name, typed value or None, typed app ID or None) -> its result; see SettingsSite.
KeyTest = Callable[[str, "str | None", "str | None"], keytest.KeyResult]
# Each key test's result: an icon, hidden from screen readers, and the words that carry it.
RESULTS = {
    "works": ("check", "Works"),
    "wrong_key": ("x", "Wrong key"),
    "not_subscribed": ("x", "Not subscribed"),
    "service_down": ("triangle-alert", "Service not answering"),
    "missing": ("triangle-alert", "No key"),
    "unknown": ("info", "Can't tell"),
    "not_tested": ("triangle-alert", "Not tested"),
}
ADDRESS = "kb_incidents_url"  # the engineering works address, in its key's form
_ADDRESS_FIELD = next(f for f in schema.FIELDS if f.name == ADDRESS)
_MIN_SCRUBBED = 6  # as setupapi: shorter can't be told from ordinary words
_KEY_SHAPE = "Paste the key exactly; it has only letters, digits and . _ ~ + / = -"
_NOTHING = "Nothing to save: paste a key first."


@dataclass(frozen=True)
class Tested:
    """A key test's result, for form `name`: a RESULTS state, and plain
    words with no key in them."""

    name: str
    state: str
    message: str


def _address_shown(data: Mapping) -> str:
    """The engineering works address as its box shows it: empty for none or the placeholder."""
    section = data.get("dashboard")
    value = section.get(ADDRESS) if isinstance(section, dict) else None
    return value if isinstance(value, str) and not kb_url_is_placeholder(value) else ""


class KeysPage:
    """The Keys page, for a signed-in owner: drawn from the keys saved and
    their last tests, and each form saved and tested on its own.

    `action` is where its forms post; `load_secrets` gives the environment
    with every key saved; `install` says which views are on by default;
    `header` draws what goes before the page, given the form token, and
    `footer` what goes after it; `applies` says when saved changes take
    effect. `test_key`, if given, tests a key and never raises; without it
    there are no test buttons. `clock` gives the time a test is kept with."""

    def __init__(self, store: Store, action: str, load_secrets: Callable[[], Mapping[str, str]], *,
                 install: Install, header: Callable[[str], str] = lambda csrf: "",
                 footer: Callable[[], str] = lambda: "", applies: str = "", test_key: KeyTest | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._action = action
        self._load_secrets = load_secrets
        self._install = install
        self._header = header
        self._footer = footer
        self._applies = applies
        self._test_key = test_key
        self._clock = clock

    # Reading

    def _read(self) -> tuple[dict, str]:
        """The settings in force, and a problem with the saved settings file if
        it can't be read: then config.toml's alone. ConfigError (or OSError)
        if config.toml itself can't be read."""
        base = self._store.base()
        try:
            return merge(base, self._store.saved()), ""
        except ConfigError as error:
            return base, str(error)

    def _views(self, data: dict) -> tuple[str, ...]:
        try:
            return schema.views(data, self._install)
        except ConfigError:
            return self._install.default_views

    def _forms(self, data: dict) -> tuple[KeyForm, ...]:
        return keystate.forms_for(self._views(data), data)

    def _form(self, name: str) -> KeyForm | None:
        """The form called `name`, while it is on the page."""
        try:
            data, _ = self._read()
        except (OSError, ConfigError):
            data = {}
        return next((form for form in self._forms(data) if form.name == name), None)

    def _address(self, form: KeyForm, data: dict) -> bool:
        """Whether `form` has the engineering works address: its key's, while trains are set up."""
        return form.name == "RDM_KB_API_KEY" and "train" in data

    def knows(self, name: str) -> bool:
        """Whether `name` is one of the page's forms."""
        return self._form(name) is not None

    # Saving

    def save(self, fields: dict[str, str], changed: list[str] | None = None) -> dict[str, str]:
        """Save one form (fields["key"]): its keys pasted, and its address.
        Field name -> error, or empty when saved (or when there was nothing
        to save before testing a saved key). Once saved, what changed is
        added to `changed` in words: a key by its label only."""
        try:
            data, _ = self._read()
        except (OSError, ConfigError) as error:
            return {"_form": f"Not saved: the settings can't be read ({error})"}
        form = next((f for f in self._forms(data) if f.name == fields.get("key")), None)
        if form is None:
            return {"_form": "That isn't one of the keys on this page."}
        errors: dict[str, str] = {}
        keys = {}
        for box in form.boxes:
            raw = fields.get(box, "").strip()
            if raw and not KEY_VALUE.fullmatch(raw):
                errors[box] = _KEY_SHAPE
            elif raw:
                keys[box] = raw
        changes: dict = {}
        if self._address(form, data):
            before, raw = _address_shown(data), fields.get(ADDRESS, "")
            if edited(raw, before):
                try:
                    value = str(_ADDRESS_FIELD.check(raw))
                except Invalid as error:
                    errors[ADDRESS] = str(error)
                else:
                    if (value if not kb_url_is_placeholder(value) else "") != before:
                        changes = {"dashboard": {"kb_incidents_url": value}}
        if errors:
            return errors
        env = self._load_secrets()
        if not keys and not changes:
            saved = all(str(env.get(box, "")).strip() for box in form.boxes)
            if self._address(form, data) or (fields.get("do") == "save_test" and saved):
                return {}
            return {form.boxes[0]: _NOTHING}
        try:
            save_to(self._store.target(self._check(data, env)), changes, keys, env)
        except OverlayError as error:
            return {"_form": str(error)}
        keyresults.forget(self._store.folder, [*keys, *(["RDM_KB_API_KEY"] if changes else [])])
        if changed is not None:
            labels = {name: label for name, label, _ in schema.keys(*schema.GROUPS)}
            changed.extend(labels[box] for box in keys)
            if changes:
                changed.append(_ADDRESS_FIELD.label)
        return {}

    def _check(self, data: dict, env: Mapping[str, str]) -> Callable[[dict, Mapping[str, str]], None]:
        """What a save must pass: the settings load in every view on after it,
        if they did before it. A key can only help settings that didn't load
        (another key missing, say), and the address has passed its own check,
        so the first key on a new install can be saved before the second."""
        folder, install = self._store.folder, self._install
        try:
            check_views(self._store.placed(data), env, folder, install)
        except (ConfigError, KeyError, TypeError, ValueError):
            return lambda merged, keys: None
        return lambda merged, keys: check_views(merged, keys, folder, install)

    # Testing

    def test_key(self, fields: dict[str, str]) -> Tested | None:
        """Test form fields["key"]'s key as saved, and keep what the test
        found if it says either way. None if there is no such form or no
        way to test. Whatever the test says, no key, typed or saved, is shown."""
        form = self._form(fields.get("key", ""))
        if form is None or self._test_key is None:
            return None
        result = self._test_key(form.name, None, None)
        env = self._load_secrets()
        secrets = [*(fields.get(box, "").strip() for box in form.boxes),
                   *(str(env.get(key, "")).strip() for key in keytest.KEY_NAMES)]
        message = result.message
        for secret in sorted((s for s in secrets if len(s) >= _MIN_SCRUBBED), key=len, reverse=True):
            message = message.replace(secret, "…")
        state = result.state if result.state in RESULTS else "unknown"
        found = keyresults.FOUND.get(state)
        if found is not None:
            keyresults.record(self._store.folder, form.name, found, self._clock())
        return Tested(form.name, state, safe_message(message))

    # Drawing

    def render(self, csrf: str, message: str = "", errors: dict[str, str] | None = None,
               values: dict[str, str] | None = None, tested: Tested | None = None) -> str:
        """The page: `message` says what a save changed, `errors` and `values`
        are a refused form's, `tested` the result of a test just run."""
        errors, values = errors or {}, values or {}
        e = html.escape
        unreadable = ""
        try:
            data, _ = self._read()
        except (OSError, ConfigError) as error:
            data, unreadable = {}, str(error)
        env = self._load_secrets()
        forms = self._forms(data)
        states = keystate.states(forms, data, env, self._views(data), keyresults.load(self._store.folder))
        out = [self._header(csrf)]
        if errors:
            out.append(self._summary(forms, errors, values))
        if message:
            focus = "" if tested is not None else ' tabindex="-1" autofocus'
            out.append(f'<p class="notice-ok" role="status"{focus}>{icon("check")} {e(message)}</p>')
        if unreadable:
            out.append('<div class="summary" role="alert"><h2>The settings can\'t be read</h2>'
                       f"<p>{e(unreadable)}</p><p>Fix config.toml to save keys here.</p></div>")
        out.append('<p class="lead">Keys are never shown, not even here. Paste a key to replace the one saved; '
                   "each key is saved on its own.</p>")
        if self._applies:
            out.append(f'<p class="hint lead">{e(self._applies)}</p>')
        posted = values.get("key")
        for form, state in zip(forms, states, strict=True):
            mine = posted == form.name
            result = tested if tested is not None and tested.name == form.name else None
            out.append(self._key_form(csrf, form, state, data, env, errors if mine else {},
                                      values if mine else {}, result, saved=bool(message)))
        out.append(self._footer())
        return "".join(out)

    def _key_form(self, csrf: str, form: KeyForm, state: KeyState, data: dict, env: Mapping[str, str],
                  errors: dict[str, str], values: dict[str, str], tested: Tested | None, saved: bool) -> str:
        """One key's form: a card headed by what it unlocks, with its state,
        its boxes, any address, then its buttons."""
        e = html.escape
        labels = {name: (label, hint) for name, label, hint in schema.keys(*schema.GROUPS)}
        has_key = state.state not in ("not_set", "needed")
        result_id = f"{form.name}-test" if tested is not None else ""
        out = [
            f'<form method="post" action="{e(self._action)}" novalidate class="keyform">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}">'
            f'<input type="hidden" name="key" value="{e(form.name)}">'
            f'<fieldset class="card"><legend><h2 id="{section_id(form.feature)}">{e(form.feature)}</h2> '
            f"{keystate.pill(state)}</legend>"
            f'<p class="hint lead">{e(form.unlocks)}</p>'
        ]
        for box in form.boxes:
            label, hint = labels[box]
            hint = _under(form.feature, hint)
            last = box == form.boxes[-1]
            there = bool(str(env.get(box, "")).strip())
            out.append(_key_box(box, label, hint, errors.get(box), there, result_id,
                                _result(tested, saved) if tested is not None and last else ""))
        address = self._address(form, data)
        if address:
            shown = values[ADDRESS] if ADDRESS in values else _address_shown(data)
            out.append(_field(_ADDRESS_FIELD, shown, errors.get(ADDRESS)))
        described = ""
        if self._test_key is not None and form.name == "TAPI_APP_KEY":
            described = f' aria-describedby="{form.name}-cost"'
            out.append(f'<p class="hint" id="{form.name}-cost">Testing uses 1 of today\'s 30 TransportAPI '
                       "lookups, the same ones bus times use.</p>")
        # Save first: the first button is the one Enter in a box presses.
        out.append('<div class="key-actions"><button type="submit" name="do" value="save" class="primary">'
                   "Save key</button>")
        if self._test_key is not None:
            typed = ' class="if-typed"' if has_key and not address else ""
            out.append(f'<button type="submit" name="do" value="save_test"{typed}{described}>Save and test</button>')
            if has_key:
                out.append(f'<button type="submit" name="do" value="test" class="if-empty"{described}>'
                           "Test saved key</button>")
        out.append("</div></fieldset></form>")
        return "".join(out)

    def _summary(self, forms: tuple[KeyForm, ...], errors: dict[str, str], values: dict[str, str]) -> str:
        """The error summary: each problem named by its form and box, linked
        to the box; and, as key boxes are never filled again, that the key
        pasted wasn't saved."""
        e = html.escape
        names = {name: label for name, label, _ in schema.keys(*schema.GROUPS)}
        names = {box: f"{form.feature} – {names[box]}" for form in forms for box in form.boxes}
        names[ADDRESS] = f"Engineering works – {_ADDRESS_FIELD.label}"
        items = "".join(
            f'<li><a href="#{e(name)}">{e(names.get(name, name))}: {e(text)}</a></li>'
            if name != "_form" else f"<li>{e(text)}</li>"
            for name, text in errors.items()
        )
        pasted = ""
        if any(values.get(box, "").strip() for form in forms for box in form.boxes):
            pasted = "<p>The key you pasted wasn't saved. Paste it again once the rest is right.</p>"
        return (
            '<div class="summary" role="alert" tabindex="-1" id="problems" autofocus>'
            f"<h2>Not saved: fix {len(errors)} {'thing' if len(errors) == 1 else 'things'}</h2>"
            f"<ul>{items}</ul>{pasted}</div>"
        )


def _under(heading: str, hint: str) -> str:
    """A key's hint under its form's heading: without the heading it starts
    with for the wizard, which has no such heading ("Trains: the key from…")."""
    rest = hint.removeprefix(f"{heading}: ")
    return rest if rest == hint else rest[:1].upper() + rest[1:]


def _key_box(name: str, label: str, hint: str, error: str | None, there: bool, result_id: str = "",
             result: str = "") -> str:
    """A key's box, never with its value; `result` (HTML, with id `result_id`) goes right after it."""
    e = html.escape
    attrs, notes = _described(name, hint, error, result_id)
    placeholder = "Paste a new key to replace the saved one" if there else "Paste the key"
    return (
        f'<div class="field{" has-error" if error else ""}"><label for="{name}">{e(label)}</label>{notes}'
        # Plain text: password managers ignore autocomplete="off" on password
        # boxes and could fill in the settings password here, so they are told
        # to leave it alone in their own words too. The placeholder tells the
        # stylesheet whether anything is typed (:placeholder-shown).
        f'<input id="{name}" name="{name}" type="text" class="key-box" autocomplete="off" spellcheck="false" '
        f'autocapitalize="none" autocorrect="off" data-1p-ignore data-lpignore="true" '
        f'placeholder="{placeholder}"{attrs}>{result}</div>'
    )


def _result(tested: Tested, saved: bool) -> str:
    """A key test's result, in words, focused when the page opens so it is
    read out; after a save, saying so first."""
    shape, words = RESULTS[tested.state]
    message = html.escape(tested.message, quote=False)
    first = "Saved. " if saved else ""
    return (
        f'<p class="key-result key-result-{tested.state}" id="{tested.name}-test" role="status" tabindex="-1" '
        f"autofocus>{icon(shape)} {first}<strong>{words}:</strong> {message}</p>"
    )
