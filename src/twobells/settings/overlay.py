"""Saving changes into the web page's settings overlay without losing the rest.

The settings pages (web/groupform.py, web/keyspage.py) and the setup wizard
each change only some settings, so all save through here: it reads the saved overlay, merges
the changes in as config.merge layers the overlay over config.toml
(top-level tables key by key, anything deeper replaced) and writes the
result back, keeping everything the caller doesn't manage. The wizard keeps
the look, rotation, text size, the dashboard's [dashboard] and so on; the
page keeps the wizard's [bus] source, operators and routes and [weather] lat
and lon.

Nothing is written unless the merged settings then load, and keys go to the
secrets overlay only, never into settings.toml; a save of keys alone leaves
settings.toml as it is. Only the settings a
Target allows are read from the overlay (schema.saved_keys),
so a save drops anything else found in it. to_toml writes every kind of
value settings carry, floats (the weather position) and route strips
(arrays of inline tables) included.

Saves take turns: each holds a lock on settings.lock in the overlay's
folder from reading the saved settings to writing them back, so the web
server's threads, the wizard and the CLI saving at once lose nothing.

Every install has one overlay, in its store (store.Store.target), which
the board and the dashboard share. A Target names the settings under it,
its folder, the loader the merged settings must pass and what the saved
settings may hold.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..board.config import settings_from
from ..core import files
from ..core.errors import ConfigError, scrub_secrets
from .config import OVERLAY_NAME, SECRETS_OVERLAY_NAME, merge, read_overlay_in
from .schema import KEY_VALUE, saved_keys

# A saved settings file that couldn't be read, kept beside it when the page replaces it.
BROKEN_NAME = OVERLAY_NAME + ".broken"
# Beside them, held by every save from reading the saved settings to writing them back.
LOCK_NAME = "settings.lock"
HEADER = ("# Saved from the settings site and setup. Layered over config.toml, which sets everything else:\n"
          "# anything here that the page and setup don't save is ignored.")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class OverlayError(Exception):
    """The changes weren't saved; the text says why and is safe to show."""


def _toml_string(value: str) -> str:
    """A TOML basic string. JSON's escapes are TOML's too, except that JSON
    writes characters beyond the BMP as surrogate pairs, which TOML rejects,
    and leaves DEL raw, which TOML forbids."""
    if any("\ud800" <= c <= "\udfff" for c in value):
        raise ValueError("Text has a broken character")
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml_string(key)


def _write(path: Path, text: str | bytes, mode: int) -> None:
    """Replace a file whole: written aside with `mode`, then renamed over it."""
    files.write_atomic(path, text, mode)


def _keep_broken(path: Path) -> None:
    """A copy of a settings file that can't be read, as settings.toml.broken
    beside it (for its owner only), before it is replaced. OSError if it
    can't be kept, so it isn't replaced either."""
    try:
        fd = os.open(path, os.O_RDONLY | files.NOFOLLOW)
    except FileNotFoundError:
        return  # gone since it was read: nothing to keep
    with os.fdopen(fd, "rb") as handle:
        content = handle.read()
    _write(path.with_name(BROKEN_NAME), content, 0o600)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"Can't write {type(value).__name__} to TOML")


def to_toml(data: dict, header: str = HEADER) -> str:
    """TOML for settings: in each table its values first, then its sub-tables.
    Lists of tables are written inline, as route strips are in config.toml."""
    lines = [header]

    def body(values: dict) -> None:
        lines.extend(f"{_toml_key(k)} = {_value(v)}" for k, v in values.items() if not isinstance(v, dict))

    def table(path: list[str], values: dict) -> None:
        lines.append("")
        lines.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        body(values)
        for k, v in values.items():
            if isinstance(v, dict):
                table([*path, k], v)

    body(data)
    for key, value in data.items():
        if isinstance(value, dict):
            table([key], value)
    return "\n".join(lines) + "\n"


def _board_loads(data: dict, env: Mapping[str, str]) -> None:
    settings_from(data, env)


@dataclass(frozen=True)
class Target:
    """An overlay to save into. `base` reads the settings it is layered over;
    `folder` is where its settings.toml and secrets.env live (None: nowhere
    yet, so nothing can be saved); `check` raises ConfigError (or KeyError,
    TypeError, ValueError) unless merged settings and an environment load;
    `allowed` is what the saved settings may hold (see schema.saved_keys)."""

    base: Callable[[], dict]
    folder: Callable[[dict], Path | None]
    check: Callable[[dict, Mapping[str, str]], object] = _board_loads
    allowed: Mapping[str | None, frozenset[str]] = field(default_factory=saved_keys)

    def current(self) -> dict:
        """The settings in force: the base with the saved overlay over it."""
        base = self.base()
        return merge(base, read_overlay_in(self.folder(base), self.allowed))


def board_target(config_path: Path | str) -> Target:
    """The overlay in the store of a config.toml path, checked as the board
    loads it: for callers and tests that still name config.toml alone."""
    from .store import Store

    return Store.open(config_path).target()


def save_to(target: Target, changes: dict, keys: Mapping[str, str], env: Mapping[str, str],
            replace: bool = False) -> dict:
    """Merge `changes` into the target's saved settings overlay and `keys` into
    its secrets overlay, if the settings then load with `env` (the
    environment and keys already saved) plus `keys`. Returns the overlay as
    written. OverlayError, with text that never holds a key, if not saved.
    With `replace`, `changes` take the saved overlay's place instead: for
    the settings site replacing one it can't read, which is kept as
    settings.toml.broken."""
    for name, value in keys.items():
        if not _ENV_NAME.fullmatch(name) or not isinstance(value, str) or not KEY_VALUE.fullmatch(value):
            raise OverlayError(f"{name if _ENV_NAME.fullmatch(name) else 'A key'} isn't a key the board can store")
    try:
        base = target.base()
        folder = target.folder(base)
    except (OSError, ConfigError) as error:
        reason = scrub_secrets(str(error), keys.values())
        raise OverlayError(f"Not saved: the settings can't be read ({reason})") from None
    if folder is None:
        raise OverlayError("config.toml needs a state_file before settings can be saved")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        lock = files.open_lock(folder / LOCK_NAME)
    except OSError as error:
        raise OverlayError(f"Not saved: the settings can't be written ({_reason(error)})") from None
    # Held from reading the saved settings to writing them back, so saves at
    # once (the web server's threads, the wizard, the CLI) take turns and
    # each keeps the others' changes. The system lets go if this process dies.
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _save_locked(target, base, folder, changes, keys, env, replace)
    except OSError as error:  # the lock's; _save_locked says what it couldn't do
        raise OverlayError(f"Not saved: the settings can't be written ({_reason(error)})") from None
    finally:
        os.close(lock)


def _reason(error: OSError) -> str:
    """Why a file couldn't be read or written, without its path or content."""
    return error.strerror or type(error).__name__


def _save_locked(target: Target, base: dict, folder: Path, changes: dict, keys: Mapping[str, str],
                 env: Mapping[str, str], replace: bool) -> dict:
    """save_to's work, with the folder's settings lock held."""
    settings_path, secrets_path = folder / OVERLAY_NAME, folder / SECRETS_OVERLAY_NAME
    try:
        saved = {} if replace else read_overlay_in(folder, target.allowed)
    except (OSError, ConfigError) as error:
        reason = scrub_secrets(str(error), keys.values())
        raise OverlayError(f"Not saved: the settings can't be read ({reason})") from None
    overlay = merge(saved, changes)
    try:
        target.check(merge(base, overlay), {**env, **keys})
    except (ConfigError, KeyError, TypeError, ValueError) as error:
        raise OverlayError(f"Not saved: {scrub_secrets(str(error), keys.values())}") from None
    try:
        text = to_toml(overlay)
        readable = tomllib.loads(text) == overlay
    except (ValueError, TypeError):
        readable = False
    if not readable:
        raise OverlayError("Not saved: some text can't be stored. Remove any unusual characters and try again.")
    try:
        secrets_text = _secrets_text(secrets_path, keys) if keys else ""
    except UnicodeDecodeError:
        raise OverlayError(f"Not saved: {SECRETS_OVERLAY_NAME} isn't text the board can read") from None
    except OSError as error:
        raise OverlayError(f"Not saved: {SECRETS_OVERLAY_NAME} can't be read ({_reason(error)})") from None
    if replace:
        try:
            _keep_broken(settings_path)
        except OSError as error:
            raise OverlayError(f"Not saved: the broken settings file can't be kept aside ({_reason(error)})") from None
    try:
        # Keys first, so settings that need them are never saved without them.
        if keys:
            _write(secrets_path, secrets_text, 0o600)
        if changes or replace:  # keys alone (the Keys page's) leave settings.toml as it is
            _write(settings_path, text, 0o640)
    except OSError as error:
        raise OverlayError(f"Not saved: the settings can't be written ({_reason(error)})") from None
    return overlay


def _secrets_text(path: Path, keys: Mapping[str, str]) -> str:
    """The secrets overlay with `keys` set, keeping every other NAME=value line."""
    saved: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        name, sep, value = line.partition("=")
        if sep and _ENV_NAME.fullmatch(name.strip()):
            saved[name.strip()] = value.strip()
    saved.update(keys)
    return "".join(f"{k}={v}\n" for k, v in saved.items())
