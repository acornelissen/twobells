"""Each key's state, for the Keys page and the Overview.

The keys are in forms, one per key, but for TransportAPI's app ID and key,
which work as a pair and share one (named TAPI_APP_KEY, the name its test
and its result go by). A form's state, from the keys saved and the last test
kept in the store (settings/keyresults.py), is one of:

- not_set: no key saved, and nothing on needs it;
- needed: no key saved, and something on needs it ("Needed for trains");
- not_tested: saved, with no test that said either way since;
- works: the last test worked;
- refused: the last test was refused.

key_states(store, install) gives every form's state for the views on, read
from local files only: it never connects, and never holds a key.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..settings import keyresults, schema
from ..settings.config import ConfigError
from .icons import icon
from .overview import needed_for

if TYPE_CHECKING:
    from ..settings.installs import Install
    from ..settings.store import Store


@dataclass(frozen=True)
class KeyForm:
    """One form on the Keys page: what it unlocks, and its key boxes."""

    name: str  # its test's name, and its result's
    feature: str  # what the key unlocks, as the form's heading
    unlocks: str  # a line on what that is
    boxes: tuple[str, ...]  # the keys it saves, each a box


FORMS = (
    KeyForm("RDM_API_KEY", "Trains", "Live departures from your station.", ("RDM_API_KEY",)),
    KeyForm("BODS_API_KEY", "Buses", "The bus timetable, where each bus is, and bus disruptions.",
            ("BODS_API_KEY",)),
    KeyForm("TAPI_APP_KEY", "Buses from TransportAPI",
            "The board's bus times while there is no bus timetable, or when the board's buses come from it.",
            ("TAPI_APP_ID", "TAPI_APP_KEY")),
    KeyForm("RDM_DETAILS_API_KEY", "Where each train is",
            "Optional: where each of the next few trains is, on the dashboard.", ("RDM_DETAILS_API_KEY",)),
    KeyForm("RDM_KB_API_KEY", "Engineering works",
            "Optional: planned engineering works on your trains' lines, on the dashboard.", ("RDM_KB_API_KEY",)),
)
STATES = ("not_set", "needed", "not_tested", "works", "refused")
# Each state's pill: its colour and icon, which only repeat its words.
_PILLS = {
    "not_set": ("neutral", "minus"),
    "needed": ("warn", "triangle-alert"),
    "not_tested": ("neutral", "info"),
    "works": ("ok", "check"),
    "refused": ("bad", "x"),
}


@dataclass(frozen=True)
class KeyState:
    """A form's key's state: `state` (STATES), what it is `needed_for` while
    needed, and when the last test was, in the settings' time zone."""

    form: KeyForm
    state: str
    needed_for: str = ""
    tested: datetime | None = None

    @property
    def name(self) -> str:
        return self.form.name

    @property
    def words(self) -> str:
        """The state in words, like "Works, tested 10 Oct 10:42"."""
        when = f"{self.tested.day} {self.tested:%b %H:%M}" if self.tested is not None else ""
        return {
            "not_set": "Not set",
            "needed": f"Needed for {self.needed_for}",
            "not_tested": "Saved, not tested",
            "works": f"Works, tested {when}",
            "refused": f"Refused, tested {when}",
        }[self.state]


def pill(state: KeyState) -> str:
    """The state as a pill: its words, with an icon and colour repeating them."""
    tone, shape = _PILLS[state.state]
    return f'<span class="pill pill-{tone}">{icon(shape)} {html.escape(state.words)}</span>'


def forms_for(views: tuple[str, ...]) -> tuple[KeyForm, ...]:
    """The forms of the keys the views on use."""
    names = {name for name, _, _ in schema.keys("general", *views)}
    return tuple(form for form in FORMS if set(form.boxes) <= names)


def _zone(data: Mapping) -> ZoneInfo:
    try:
        return ZoneInfo(str(data.get("timezone", "Europe/London")))
    except (KeyError, ValueError):  # a bad zone: the settings say so on saving
        return ZoneInfo("Europe/London")


def states(forms: tuple[KeyForm, ...], data: Mapping, env: Mapping[str, str], views: tuple[str, ...],
           results: Mapping[str, keyresults.LastTest]) -> tuple[KeyState, ...]:
    """Each form's state, from the settings in force, the keys saved and the results kept."""
    out = []
    for form in forms:
        if not all(str(env.get(box, "")).strip() for box in form.boxes):
            needed = needed_for(form.name, dict(data), views)
            out.append(KeyState(form, "needed" if needed else "not_set", needed))
            continue
        last = results.get(form.name)
        if last is None:
            out.append(KeyState(form, "not_tested"))
            continue
        tested = datetime.fromtimestamp(last.time, _zone(data)).replace(tzinfo=None)
        out.append(KeyState(form, last.state, tested=tested))
    return tuple(out)


def key_states(store: Store, install: Install) -> tuple[KeyState, ...]:
    """Each key's state, for the views on in `store`: for the Overview. From
    local files only; the settings in force are config.toml's alone if the
    saved ones can't be read, and none if neither can."""
    try:
        data = store.data()
    except (OSError, ConfigError):
        try:
            data = store.base()
        except (OSError, ConfigError):
            data = {}
    try:
        views = schema.views(data, install)
    except ConfigError:
        views = install.default_views
    try:
        env = store.env()
    except (OSError, ValueError):  # a secrets file that isn't text
        env = {}
    return states(forms_for(views), data, env, views, keyresults.load(store.folder))
