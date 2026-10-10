"""Bringing the old dashboard's folder into the store, once.

Before the store, the dashboard kept its own folder ([dashboard] state_dir,
or departureboard-dashboard in Application Support or ~/.local/state) with
its own settings, keys and password. On a desktop where that folder isn't
the store, `serve` brings them across when it starts:

- a store with none of settings.toml, secrets.env and web.json takes the
  old folder's;
- a store with any of them keeps its own (the board's) and takes only what
  can't conflict: the [dashboard] table and the dashboard's own keys, if it
  has none;
- the punctuality history is copied if the store has none.

Files are copied, never moved, so the old folder stays as it was, and a
file there that is a link (or not a plain file) is refused. migrated.json
records where from and what was brought (file and key names, never
values); with it there, this never runs again. A Pi and Docker never had
a dashboard folder apart from the store, so there is nothing to do there.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import sqlite3
import stat
import sys
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..core import files
from .config import OVERLAY_NAME, SECRETS_OVERLAY_NAME, ConfigError, load_env_file, merge, parse_env, saved_only
from .schema import KEY_VALUE, saved_keys
from .store import OLD_FOLDER_NAME, PASSWORD_NAME, Store, platform_base

log = logging.getLogger(__name__)

RECORD = "migrated.json"
LOCK = "migrate.lock"
HISTORY = "history.sqlite3"
# What makes a folder a store, and each one's mode when copied.
STORE_FILES = ((OVERLAY_NAME, 0o640), (SECRETS_OVERLAY_NAME, 0o600), (PASSWORD_NAME, 0o600))
# Keys only the dashboard uses, so a store keeping its own can take them.
DASHBOARD_KEYS = ("RDM_DETAILS_API_KEY", "RDM_KB_API_KEY")
# Far above any settings, keys or password file.
_MOST = 1 << 20


class Refused(Exception):
    """A file in the old folder that isn't brought across; the text says why."""


def legacy_folder(store: Store, install: str, config_data: dict, *, platform: str = sys.platform,
                  environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path | None:
    """The old dashboard's folder, if there is one apart from the store: the
    [dashboard] state_dir config.toml names, or else the platform's
    departureboard-dashboard. Only a desktop had one."""
    if install != "desktop":
        return None
    section = config_data.get("dashboard")
    named = section.get("state_dir") if isinstance(section, dict) else None
    if isinstance(named, str) and named.strip():
        folder = Path(named).expanduser()
    else:
        folder = platform_base(platform, environ, home) / OLD_FOLDER_NAME
    folder = Path(os.path.abspath(folder))
    if not folder.is_dir() or (store.folder.exists() and os.path.samefile(folder, store.folder)):
        return None
    return folder


def adopt_legacy(store: Store, install: str = "desktop", config_data: dict | None = None, *,
                 platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                 home: Path | None = None) -> list[str]:
    """Bring the old dashboard's folder into the store, if there is one and
    it hasn't been yet; what was brought (file, table and key names). It
    never stops a start: a problem is logged and the rest carries on."""
    try:
        data = store.base() if config_data is None else config_data
        old = legacy_folder(store, install, data, platform=platform, environ=environ, home=home)
        if old is None or os.path.lexists(store.folder / RECORD):
            return []
        store.make()
        with _locked(store.folder / LOCK):
            if os.path.lexists(store.folder / RECORD):
                return []  # another process brought it while this one waited
            imported = _import(old, store)
            _record(store.folder / RECORD, old, imported)
    except (OSError, RuntimeError, ConfigError) as error:  # RuntimeError: no home folder
        log.warning("Couldn't bring the dashboard's old folder into %s: %s", store.folder, error)
        return []
    if imported:
        log.info("Brought %s from %s into %s", ", ".join(imported), old, store.folder)
    return imported


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    fd = files.open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _has_store(folder: Path) -> bool:
    return any(os.path.lexists(folder / name) for name, _ in STORE_FILES)


def _import(old: Path, store: Store) -> list[str]:
    imported: list[str] = []
    if not _has_store(store.folder):
        for name, mode in STORE_FILES:
            content = _read_plain(old / name)
            if content is not None and _create(store.folder / name, content, mode):
                imported.append(name)
    elif _has_store(old):
        log.info("Kept the board's settings and password in %s; the dashboard's in %s are no longer used",
                 store.folder, old)
        imported += _dashboard_table(old, store)
        imported += _dashboard_keys(old, store)
    imported += _history(old / HISTORY, store.folder / HISTORY)
    return imported


def _read_plain(path: Path) -> bytes | None:
    """A plain file's content, never through a link; None if it isn't there
    or is refused (and logged)."""
    try:
        return _read_or_refuse(path)
    except Refused as why:
        log.warning("Not bringing %s: %s", path, why)
        return None


def _read_or_refuse(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Refused("it is a link") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Refused("it isn't a plain file")
        content = handle.read(_MOST + 1)
    if len(content) > _MOST:
        raise Refused("it is far too big")
    return content


def _create(path: Path, content: bytes, mode: int) -> bool:
    """Write a new file with `mode`; False if one is already there."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError:
        return False
    try:
        os.fchmod(fd, mode)  # whatever the umask
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _replace(path: Path, text: str, mode: int) -> None:
    """Replace a store file whole: written aside, then renamed over it."""
    from .overlay import _write

    _write(path, text, mode)


def _dashboard_table(old: Path, store: Store) -> list[str]:
    """The old folder's [dashboard] table, if the store's settings have none."""
    content = _read_plain(old / OVERLAY_NAME)
    if content is None:
        return []
    try:
        saved = saved_only(tomllib.loads(content.decode("utf-8")), saved_keys(), old / OVERLAY_NAME)
        current = store.saved()
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ConfigError) as error:
        log.warning("Not bringing the dashboard's [dashboard] settings: %s", error)
        return []
    table = saved.get("dashboard")
    if not table or "dashboard" in current:
        return []
    from .overlay import to_toml

    merged = merge(current, {"dashboard": table})
    text = to_toml(merged)
    if tomllib.loads(text) != merged:
        log.warning("Not bringing the dashboard's [dashboard] settings: they can't be written back as they are")
        return []
    _replace(store.settings_file, text, 0o640)
    return ["[dashboard]"]


def _dashboard_keys(old: Path, store: Store) -> list[str]:
    """The dashboard's own keys from the old folder, each only if the store has none."""
    from .overlay import _secrets_text

    content = _read_plain(old / SECRETS_OVERLAY_NAME)
    if content is None:
        return []
    found = parse_env(content.decode("utf-8", errors="replace"))
    have = load_env_file(store.secrets_file)
    keys = {name: found[name] for name in DASHBOARD_KEYS
            if found.get(name) and KEY_VALUE.match(found[name]) and not have.get(name)}
    if keys:
        _replace(store.secrets_file, _secrets_text(store.secrets_file, keys), 0o600)
    return list(keys)


def _history(source: Path, target: Path) -> list[str]:
    """A copy of the punctuality history, through SQLite's backup so it is
    whole, if the store has none."""
    if os.path.lexists(target):
        return []
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode):
        log.warning("Not bringing %s: it isn't a plain file", source)
        return []
    # Made first, so the copy is the owner's alone from the start.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    os.chmod(target, 0o600)
    try:
        reading = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
        try:
            writing = sqlite3.connect(target)
            try:
                reading.backup(writing)
            finally:
                writing.close()
        finally:
            reading.close()
    except sqlite3.Error as error:
        target.unlink(missing_ok=True)
        log.warning("Not bringing %s: %s", source, error)
        return []
    return [HISTORY]


def _record(path: Path, old: Path, imported: list[str]) -> None:
    record = {"v": 1, "at": datetime.now(UTC).isoformat(timespec="seconds"), "from": str(old),
              "imported": imported}
    _create(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"), 0o600)
