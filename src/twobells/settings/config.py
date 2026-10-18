"""Reads config.toml (settings) and the environment (secrets), and checks what
every view shares; board/config.py loads them into a Board.

Settings saved from the web page live in settings.toml in the store (see
settings/store.py) and are layered over config.toml, so the service never writes to
/etc and comments in a hand-edited config.toml survive. Only the settings
the pages and setup save are read from it (schema.saved_keys); anything else, like
the addresses the keys are sent to, is set in config.toml. Keys saved there
go in a secrets.env beside it, layered over the main one.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import ConfigError
from ..core.polling import parse_window
from ..sources.holidays import DIVISIONS, ENGLAND_AND_WALES
from . import modes as mode_list
from .schema import (
    BOARD_DEFAULTS,
    LONDON_STOP,
    board_room,
    bus_source,
    refused,
    room_other,
    saved_keys,
    views,
)

if TYPE_CHECKING:
    from .installs import Install
    from .store import Store

log = logging.getLogger(__name__)


OVERLAY_NAME = "settings.toml"
SECRETS_OVERLAY_NAME = "secrets.env"


def state_dir(data: dict) -> Path | None:
    """The folder of already-read settings' state_file, if they name one."""
    state_file = data.get("state_file")
    if state_file is not None and not isinstance(state_file, str):
        raise ConfigError(f"state_file must be a path in quotes, not {state_file!r}")
    return Path(state_file).parent if state_file else None


def merge(base: dict, over: dict) -> dict:
    """Top-level tables like [bus] merge key by key, so keys the page doesn't
    manage (like bus.routes) survive; anything else in `over`, tables below the
    top level included, replaces what's in `base`, so the page can remove a stop."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def read_base(path: Path | str) -> dict:
    """config.toml alone, without the web page's settings."""
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error


_PLAIN_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _key_name(key: str) -> str:
    """A key as a log line names it: as it is if plain, else quoted and cut short."""
    return key if _PLAIN_KEY.fullmatch(key) else repr(key[:64])


def saved_only(saved: dict, allowed: Mapping[str | None, frozenset[str]], where: Path) -> dict:
    """`saved` with only the keys in `allowed`, less any value schema.refused
    refuses. Each other is logged by its name, never its value."""
    kept: dict = {}
    for key, value in saved.items():
        ignored = []
        if key in allowed and isinstance(value, dict):
            table = {}
            for k, v in value.items():
                why = "only config.toml can set it" if k not in allowed[key] else refused(key, k, v)
                if why is None:
                    table[k] = v
                else:
                    ignored.append((f"[{_key_name(key)}] {_key_name(k)}", why))
            if table or not value:  # a table left empty only by this is dropped, as if never there
                kept[key] = table
        elif key in allowed[None] and refused(None, key, value) is None:
            kept[key] = value
        else:
            name = f"[{_key_name(key)}]" if isinstance(value, dict) else _key_name(key)
            ignored = [(name, refused(None, key, value) or "only config.toml can set it")]
        for name, why in ignored:
            log.warning("%s: %s is ignored; %s", where, name, why)
    return kept


def read_overlay_in(folder: Path | None, allowed: Mapping[str | None, frozenset[str]] | None = None) -> dict:
    """The saved settings in `folder`'s settings.toml, only the keys in
    `allowed` (by default schema.saved_keys); empty if there are none."""
    if folder is None:
        return {}
    if allowed is None:
        allowed = saved_keys()
    overlay = folder / OVERLAY_NAME
    try:
        saved = tomllib.loads(overlay.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{overlay}: {error}") from error
    return saved_only(saved, allowed, overlay)


def load_env_file(path: Path | str) -> dict[str, str]:
    """NAME=value lines from a secrets file; empty if it can't be read.
    ConfigError, never holding the file's content, if it isn't UTF-8 text."""
    try:
        return parse_env(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    except UnicodeDecodeError:
        raise ConfigError(f"{path} isn't UTF-8 text; save it again as plain NAME=value lines") from None


def parse_env(text: str) -> dict[str, str]:
    """NAME=value lines from a secrets file's text."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


# What examples/config.example.toml has where your station and stops go.
PLACEHOLDER = "CHANGE_ME"


def _has_placeholder(value) -> bool:
    if isinstance(value, str):
        return value.strip() == PLACEHOLDER
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    return False


def needs_setup(store: Store | Path | str) -> bool:
    """Whether the install is still to be set up: config.toml and the saved
    settings together switch on no mode (none there, a blank one, or each
    switched off), or they still have one of the
    template's CHANGE_ME placeholders outside a section switched off. A
    config.toml that can't be read or parsed is not a first run but a
    fault, for the loaders to report."""
    from .store import Store

    try:
        data = Store.of(store).data()
    except (OSError, ConfigError):
        return False
    return still_to_set_up(data)


def still_to_set_up(data: dict) -> bool:
    """needs_setup for settings `data` already read, config.toml and the
    saved settings together. A section's enabled that isn't true or false
    is a fault, for the loaders to report, not a first run."""
    try:
        data = switched_on(data)
    except ConfigError:
        return False
    if not any(mode.key in data for mode in mode_list.MODES if mode.ready):
        return True
    return _has_placeholder(data)


# Hand edits can put anything anywhere; these turn a wrong type into a ConfigError
# naming the setting, rather than a crash now or on some later tick.
_MOST = 100_000  # far above any sensible value, far below what timedelta overflows on


def _table(parent: dict, key: str, where: str) -> dict:
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table, not {value!r}")
    return value


def _whole(section: dict, key: str, default: int, where: str = "", least: int = 1) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= _MOST:
        raise ConfigError(f"{where}{key} must be a whole number from {least} to {_MOST}, not {value!r}")
    return value


def _text(section: dict, key: str, default: str, where: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}{key} must be text in quotes, not {value!r}")
    return value


def _flag(section: dict, key: str, default: bool, where: str = "") -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}{key} must be true or false, not {value!r}")
    return value


# Sections that enabled = false switches off. The settings site and setup do
# that, rather than remove one, as a saved overlay can't remove what
# config.toml has; and what's in it is kept for switching it back on.
SWITCHABLE = (*mode_list.KEYS, "weather")


def is_enabled(data: dict, name: str) -> bool:
    """Whether the section `name` isn't switched off: on unless it says
    enabled = false. ConfigError if enabled isn't true or false."""
    section = data.get(name)
    if not isinstance(section, dict):
        return True  # absent, or not a table, which its loader reports
    return _flag(section, "enabled", True, f"[{name}] ")


def metrolink_on(data: dict) -> bool:
    """Whether [metro] is on with Metrolink's stops, whose times come from
    TfGM's timetable, which must be built; London's come live from TfL.
    ConfigError if enabled isn't true or false."""
    metro = data.get("metro")
    if not isinstance(metro, dict) or not is_enabled(data, "metro"):
        return False
    stops = metro.get("stops")
    codes = stops.values() if isinstance(stops, dict) else ()
    return not any(isinstance(code, str) and LONDON_STOP.fullmatch(code) for code in codes)


def needs_timetable(data: dict, mode: str, install: Install | None = None) -> bool:
    """Whether `mode` ("bus" or "metro") has a timetable to build: its times
    come from one (buses from BODS, Metrolink's trams from TfGM's) and a view
    that is on shows it ([app] views, or `install`'s default ones): the
    board as many modes as its display has room for (board_modes). The bus
    and tram timetables both go by this. ConfigError for settings that say
    neither, as their loaders do."""
    if mode == "metro":
        timetabled = metrolink_on(data)
    else:
        bus = data.get("bus")
        timetabled = isinstance(bus, dict) and is_enabled(data, "bus") and bus_source(bus) == "bods"
    return timetabled and any(mode in _shown_by(data, view) for view in views(data, install))


def _shown_by(data: dict, view: str) -> tuple[str, ...]:
    """The modes `view` shows: the board as many as its display has room
    for, or, where its settings don't load, those its list names (its
    loader says what is wrong); the dashboard all it names."""
    if view == "board":
        try:
            return board_modes(data)
        except ConfigError:
            pass
    return mode_list.shown(data, view)


def switched_on(data: dict) -> dict:
    """`data` without the sections switched off, so every loader ignores a
    section with enabled = false as if it weren't there."""
    return {key: value for key, value in data.items() if key not in SWITCHABLE or is_enabled(data, key)}


def _texts(section: dict, key: str, where: str, default: tuple[str, ...] = ()) -> list[str]:
    value = section.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f'{where}{key} must be a list of text, like ["a", "b"], not {value!r}')
    return value


def _windows(section: dict, key: str, default: tuple[str, ...], where: str) -> tuple:
    try:
        return tuple(parse_window(w) for w in _texts(section, key, where, default))
    except ValueError as error:
        raise ConfigError(str(error)) from error


def holiday_division(data: dict) -> str:
    """Which of gov.uk's bank holiday lists applies: England and Wales unless set."""
    division = data.get("bank_holiday_division", ENGLAND_AND_WALES)
    if division not in DIVISIONS:
        choices = ", ".join(f'"{d}"' for d in DIVISIONS)
        raise ConfigError(f"bank_holiday_division must be one of {choices}, not {division!r}")
    return division


def board_modes(data: dict) -> tuple[str, ...]:
    """The modes the board shows by settings `data`, as many as its display
    has room for in its look and text size (schema.board_room). ConfigError
    for a [board] modes that isn't a list of modes, or names more than that,
    or a text size that isn't one; the board's loader checks the look and
    the display."""
    display = _text(data, "display", BOARD_DEFAULTS["display"])
    look = _text(data, "theme", BOARD_DEFAULTS["theme"])
    text_size = _text_size(data)
    return mode_list.shown(data, "board", *board_room(display, look, text_size),
                           other=room_other(display, look, text_size))


def _text_size(data: dict) -> int:
    """The plain look's text size, checked: a whole number from 9 to 16."""
    text_size = data.get("text_size", BOARD_DEFAULTS["text_size"])
    if isinstance(text_size, bool) or not isinstance(text_size, int) or not 9 <= text_size <= 16:
        raise ConfigError(f"text_size must be a whole number from 9 to 16, not {text_size!r}")
    return text_size
