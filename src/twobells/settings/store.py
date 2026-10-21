"""The store: the one folder an install keeps everything it saves in.

The board and the dashboard share it. It holds the settings the settings
pages and setup save (settings.toml), the keys saved there (secrets.env),
the settings password (web.json), its sign-ins (sessions.json, form.key;
all three 0600), the keys' last tests (keytests.json, 0600; never a key)
and everything kept between runs: the
board's state, the bus and tram timetables, bank holidays and the dashboard's
history. find_folder says which folder it is, by one rule for every
command, so whichever view saves a setting, the other reads it.

Settings are layered: the loaders' defaults, then config.toml if there is
one, then settings.toml, of which only the keys the pages and setup save are
read (schema.saved_keys), whichever view saved them. Keys: the environment,
then the --secrets file, then the store's secrets.env.

Root may not write a store that belongs to another user (check_owner):
the commands that write to it say whom to run as instead.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..core.errors import ConfigError
from . import schema
from .config import OVERLAY_NAME, SECRETS_OVERLAY_NAME, load_env_file, merge, read_overlay_in
from .installs import DEFAULT, install_named
from .schema import saved_keys

if TYPE_CHECKING:
    from .overlay import Target

log = logging.getLogger(__name__)

FOLDER_NAME = "twobells"
STATE_NAME = "state.json"
PASSWORD_NAME = "web.json"  # noqa: S105 - the file the password hash is kept in
SESSIONS_NAME = "sessions.json"  # hashes of the settings' session tokens (web/auth.py, Sessions)
FORM_KEY_NAME = "form.key"  # the key the settings' sign-in forms are signed with (web/auth.py, FormTokens)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _home() -> Path:
    return Path.home()


def platform_base(platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                  home: Path | None = None) -> Path:
    """Where this platform keeps apps' state: Application Support on a Mac,
    else $XDG_STATE_HOME (only if absolute, as its spec says) or ~/.local/state."""
    environ = os.environ if environ is None else environ
    home = _home() if home is None else home
    if platform == "darwin":
        return home / "Library" / "Application Support"
    xdg = environ.get("XDG_STATE_HOME", "")
    return Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "state"


def platform_folder(platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                    home: Path | None = None) -> Path:
    """A desktop's store when nothing else says: twobells in the platform's folder for state."""
    return platform_base(platform, environ, home) / FOLDER_NAME


def find_folder(*, given: Path | str | None = None, config_data: Callable[[], dict],
                install: str = DEFAULT, environ: Mapping[str, str] | None = None,
                platform: str = sys.platform, home: Path | None = None) -> Path:
    """The store's folder; the first of these that applies:
    1. `given` (--state-dir);
    2. $STATE_DIRECTORY, the first path in it, as systemd sets it;
    3. the folder of config.toml's state_file (relative: to the working folder);
    4. config.toml's [dashboard] state_dir;
    5. the install's default.
    `config_data` reads config.toml, only if rules 1 and 2 don't apply.
    ConfigError if config.toml can't be read or says it wrongly."""
    environ = os.environ if environ is None else environ
    if given:
        return _absolute(Path(given).expanduser())
    systemd = environ.get("STATE_DIRECTORY", "").split(":")[0]
    if systemd and Path(systemd).is_absolute():
        return Path(systemd)
    data = config_data()
    state_file = data.get("state_file")
    if state_file is not None and not isinstance(state_file, str):
        raise ConfigError(f"state_file must be a path in quotes, not {state_file!r}")
    if state_file:
        return _absolute(Path(state_file).parent)
    section = data.get("dashboard")
    if isinstance(section, dict) and "state_dir" in section:
        named = section["state_dir"]
        if not isinstance(named, str) or not named.strip():
            raise ConfigError(f"[dashboard] state_dir must be a folder path in quotes, not {named!r}")
        return _absolute(Path(named).expanduser())
    default = install_named(install).default_folder
    return default if default is not None else platform_folder(platform, environ, home)


def make_folder(folder: Path) -> None:
    """Create the folder readable by its owner only. An existing one is left
    as it is (a Docker volume may belong to the container's user, and
    systemd makes a Pi's)."""
    if folder.exists():
        if not folder.is_dir():
            raise ConfigError(f"{folder} is not a folder")
        return
    try:
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)  # whatever the umask
    except OSError as error:
        raise ConfigError(f"Can't create the state folder {folder}: {error.strerror or error}") from None


class WrongUser(Exception):
    """Root, on a store that belongs to another user: the command is to be
    run as that user. The text is the one line to say."""


def _owner(folder: Path) -> int:
    return os.stat(folder).st_uid


def check_owner(folder: Path, command: str) -> None:
    """Raise WrongUser, naming the owner to run `command` as, when this
    process runs as root and the store's folder belongs to another user (on
    a Pi, the services'): a file root wrote there would be root's alone, and
    the services would fail on it with "Permission denied". Not root, no
    folder yet, or root's own: nothing to say. Privileges are never dropped."""
    geteuid = getattr(os, "geteuid", None)  # not on Windows
    if geteuid is None or geteuid() != 0:
        return
    try:
        uid = _owner(folder)
    except OSError:
        return  # no folder yet: whoever makes it owns it
    if uid == 0:
        return
    import pwd
    import shlex

    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = shlex.quote(f"#{uid}")  # sudo -u takes a user's number as #uid
    raise WrongUser(f"Run this as the service's user: sudo -u {name} {command}")


def read_base(path: Path | str) -> dict:
    """config.toml alone; {} if there is none. ConfigError if it can't be
    read: a folder (as Docker makes for a missing file it is asked to
    mount), unreadable, or not TOML."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except IsADirectoryError:
        raise ConfigError(f"{path} is a folder, not a config.toml") from None
    except OSError as error:
        raise ConfigError(f"Can't read {path}: {error.strerror or error}") from None
    except UnicodeDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error


def _missing(path: Path) -> bool:
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False  # there, but can't be read: read_base says so
    return False


def _state_name(data: dict, folder: Path) -> str:
    """The state file's name: config.toml's, if its state_file is in the store."""
    state_file = data.get("state_file")
    if isinstance(state_file, str) and state_file and _absolute(Path(state_file).parent) == folder:
        name = Path(state_file).name
        if name not in ("", ".", ".."):
            return name
    return STATE_NAME


@dataclass(frozen=True)
class Store:
    """One install's store, found once (see find_folder), and how settings
    and keys are layered over config.toml and the environment."""

    folder: Path
    config_path: Path
    secrets_path: Path | None = None
    state_name: str = STATE_NAME

    @classmethod
    def open(cls, config_path: Path | str, secrets_path: Path | str | None = None, *,
             state_dir: Path | str | None = None, install: str = DEFAULT,
             environ: Mapping[str, str] | None = None, platform: str = sys.platform,
             home: Path | None = None) -> Store:
        """The store for config.toml at `config_path`, which need not be
        there. ConfigError if config.toml is needed to find the folder and
        can't be read."""
        config_path = Path(config_path)
        folder = find_folder(given=state_dir, config_data=lambda: read_base(config_path), install=install,
                             environ=environ, platform=platform, home=home)
        try:
            data = read_base(config_path)
        except ConfigError:
            data = {}  # said when the settings are read
        if _missing(config_path):
            log.info("There is no %s; using the settings saved in %s", config_path, folder)
        return cls(folder, config_path, Path(secrets_path) if secrets_path is not None else None,
                   _state_name(data, folder))

    @classmethod
    def of(cls, store_or_path: Store | Path | str) -> Store:
        """A store as given, or the one for a config.toml path (for callers
        and tests that still name config.toml alone)."""
        return store_or_path if isinstance(store_or_path, Store) else cls.open(store_or_path)

    @property
    def state_file(self) -> Path:
        return self.folder / self.state_name

    @property
    def settings_file(self) -> Path:
        return self.folder / OVERLAY_NAME

    @property
    def secrets_file(self) -> Path:
        return self.folder / SECRETS_OVERLAY_NAME

    @property
    def password_file(self) -> Path:
        return self.folder / PASSWORD_NAME

    @property
    def sessions_file(self) -> Path:
        return self.folder / SESSIONS_NAME

    @property
    def form_key_file(self) -> Path:
        return self.folder / FORM_KEY_NAME

    def base(self) -> dict:
        """config.toml alone; {} if there is none."""
        return read_base(self.config_path)

    def saved(self) -> dict:
        """The settings saved in settings.toml, only schema.saved_keys; {} if none."""
        if self.folder.exists() and not self.folder.is_dir():
            raise ConfigError(f"{self.folder} is not a folder")
        try:
            return read_overlay_in(self.folder, saved_keys())
        except OSError as error:
            raise ConfigError(f"Can't read {self.settings_file}: {error.strerror or error}") from None

    def placed(self, data: dict) -> dict:
        """`data` with its state_file in the store, whatever it said."""
        return {**data, "state_file": str(self.state_file)}

    def data(self) -> dict:
        """The settings in force: config.toml with the saved settings over it."""
        return self.placed(merge(self.base(), self.saved()))

    def zone(self) -> ZoneInfo:
        """The settings' time zone (schema.zone), the default if they can't be
        read, so broken settings can't stop a page or a reset code saying a time."""
        try:
            return schema.zone(self.data())
        except (OSError, ConfigError):
            return schema.zone({})

    def env(self, environ: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment, then the --secrets file, then the keys saved in the store."""
        env = dict(os.environ if environ is None else environ)
        if self.secrets_path is not None:
            env.update(load_env_file(self.secrets_path))
        env.update(load_env_file(self.secrets_file))
        return env

    def target(self, check: Callable[[dict, Mapping[str, str]], object]) -> Target:
        """Where the settings pages and setup save: the store, over config.toml.
        `check` raises unless merged settings load (overlay.Target)."""
        from .overlay import Target

        return Target(lambda: self.placed(self.base()), lambda base: self.folder, check, allowed=saved_keys())

    def watched(self) -> list[Path]:
        """Every file settings and keys come from, to notice a change."""
        paths = [self.config_path]
        if self.secrets_path is not None:
            paths.append(self.secrets_path)
        return [*paths, self.settings_file, self.secrets_file]

    def make(self) -> None:
        """Create the folder, for its owner only, unless it is there."""
        make_folder(self.folder)
