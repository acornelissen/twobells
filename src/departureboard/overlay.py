"""Saving changes into the web page's settings overlay without losing the rest.

The settings page (settingsform.py) and the setup wizard each change only
some settings, so both save through here: it reads the saved overlay, merges
the changes in as config.merge layers the overlay over config.toml
(top-level tables key by key, anything deeper replaced) and writes the
result back, keeping everything the caller doesn't manage. The wizard keeps
the display, the look, rotation, text size, [dashboard] and so on; the page
keeps the wizard's [bus] source, operators and routes and [weather] lat and
lon.

Nothing is written unless the merged settings then load, and keys go to the
secrets overlay only, never into settings.toml. Settings carry floats (the
weather position) and route strips (arrays of inline tables), which the
settings page's own TOML writer doesn't, so this module has its own.

The board's overlay sits beside its state file. Another host, like the
dashboard, names its own with a Target: the settings under it, its folder,
and the loader the merged settings must pass.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .config import (
    OVERLAY_NAME,
    SECRETS_OVERLAY_NAME,
    ConfigError,
    merge,
    read_base,
    read_overlay_in,
    settings_from,
    state_dir,
)
from .settingsform import _KEY_VALUE, _toml_key, _toml_string, _write

HEADER = "# Saved from the settings page and setup. Layered over config.toml; edit there or here."
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class OverlayError(Exception):
    """The changes weren't saved; the text says why and is safe to show."""


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
                table(path + [k], v)

    body(data)
    for key, value in data.items():
        if isinstance(value, dict):
            table([key], value)
    return "\n".join(lines) + "\n"


def overlay_paths(base: dict) -> tuple[Path, Path]:
    folder = state_dir(base)
    if folder is None:
        raise OverlayError("config.toml needs a state_file before settings can be saved")
    return folder / OVERLAY_NAME, folder / SECRETS_OVERLAY_NAME


def _board_loads(data: dict, env: Mapping[str, str]) -> None:
    settings_from(data, env)


@dataclass(frozen=True)
class Target:
    """An overlay to save into. `base` reads the settings it is layered over;
    `folder` is where its settings.toml and secrets.env live (None: nowhere
    yet, so nothing can be saved); `check` raises ConfigError (or KeyError,
    TypeError, ValueError) unless merged settings and an environment load."""

    base: Callable[[], dict]
    folder: Callable[[dict], Path | None]
    check: Callable[[dict, Mapping[str, str]], object] = _board_loads

    def current(self) -> dict:
        """The settings in force: the base with the saved overlay over it."""
        base = self.base()
        return merge(base, read_overlay_in(self.folder(base)))


def board_target(config_path: Path | str) -> Target:
    """The board's overlay, beside the state file config.toml names."""
    return Target(lambda: read_base(config_path), state_dir)


def _scrub(text: str, secrets: Mapping[str, str]) -> str:
    for value in secrets.values():
        if value:
            text = text.replace(value, "…")
    return text


def save_changes(config_path: Path | str, changes: dict, keys: Mapping[str, str],
                 env: Mapping[str, str], replace: bool = False) -> dict:
    """Merge `changes` into the board's saved settings overlay and `keys` into
    its secrets overlay; see save_to."""
    return save_to(board_target(config_path), changes, keys, env, replace=replace)


def save_to(target: Target, changes: dict, keys: Mapping[str, str], env: Mapping[str, str],
            replace: bool = False) -> dict:
    """Merge `changes` into the target's saved settings overlay and `keys` into
    its secrets overlay, if the settings then load with `env` (the
    environment and keys already saved) plus `keys`. Returns the overlay as
    written. OverlayError, with text that never holds a key, if not saved.
    With `replace`, `changes` take the saved overlay's place instead: for
    the settings page replacing one it can't read."""
    for name, value in keys.items():
        if not _ENV_NAME.match(name) or not isinstance(value, str) or not _KEY_VALUE.match(value):
            raise OverlayError(f"{name if _ENV_NAME.match(name) else 'A key'} isn't a key the board can store")
    try:
        base = target.base()
        folder = target.folder(base)
        if folder is None:
            raise OverlayError("config.toml needs a state_file before settings can be saved")
        settings_path, secrets_path = folder / OVERLAY_NAME, folder / SECRETS_OVERLAY_NAME
        saved = {} if replace else read_overlay_in(folder)
    except (OSError, ConfigError) as error:
        raise OverlayError(f"Not saved: the settings can't be read ({_scrub(str(error), keys)})") from None
    overlay = merge(saved, changes)
    try:
        target.check(merge(base, overlay), {**env, **keys})
    except (ConfigError, KeyError, TypeError, ValueError) as error:
        raise OverlayError(f"Not saved: {_scrub(str(error), keys)}") from None
    try:
        text = to_toml(overlay)
        readable = tomllib.loads(text) == overlay
    except (ValueError, TypeError):
        readable = False
    if not readable:
        raise OverlayError("Not saved: some text can't be stored. Remove any unusual characters and try again.")
    # Keys first, so settings that need them are never saved without them.
    if keys:
        _write(secrets_path, _secrets_text(secrets_path, keys), 0o600)
    _write(settings_path, text, 0o640)
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
        if sep and _ENV_NAME.match(name.strip()):
            saved[name.strip()] = value.strip()
    saved.update(keys)
    return "".join(f"{k}={v}\n" for k, v in saved.items())
