"""Settings for the web dashboard (`departureboard dashboard`).

The dashboard reads the board's config.toml, plus an optional [dashboard]
table, but keeps everything it writes (the bus timetable, bank holidays,
punctuality history) in its own state folder, never the board's. It uses only
the free feeds: Darwin, the BODS timetable with live positions, and
Open-Meteo. TransportAPI keys are dropped so no fallback is ever built.
Darwin's Service Details, which places each train on its route, is used
only when its own key, RDM_DETAILS_API_KEY, is set. Bus disruptions come
from BODS SIRI-SX with the buses' own key; rail engineering works from the
National Rail Knowledgebase only when RDM_KB_API_KEY is set, at the address
in [dashboard] kb_incidents_url.

One dashboard per state folder: a lock held for the life of the process stops
a second one sharing (and fighting over) the same files.
"""

from __future__ import annotations

import copy
import fcntl
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Mapping, Protocol
from urllib.parse import urlsplit

from .bodsbus import BusBoard
from .config import ConfigError, _flag, _table, _texts, _whole, read_config, settings_from
from .config import state_dir as board_state_dir
from .darwin import DarwinSource, ServiceDetails, ServiceDetailsClient, TrainBoard
from .disruptions import KB_URL_PLACEHOLDER, BusDisruptionSource, TrainIncidentSource, kb_url_is_placeholder
from .schedule import Window, parse_window
from .walk import WalkTimes
from .weather import Conditions

FOLDER_NAME = "departureboard-dashboard"
STATE_FILE = "dashboard-state.json"
LOCK_FILE = "dashboard.lock"
BOARD_STATE = "state.json"

_WHERE = "[dashboard] "
_KEYS = {
    "state_dir",
    "train_rows",
    "bus_rows",
    "train_interval_seconds",
    "bus_interval_seconds",
    "weather_interval_minutes",
    "quiet_windows",
    "late_minutes_train",
    "late_minutes_bus",
    "history_days",
    "allowed_hosts",
    "kb_incidents_url",
}
_MAX_ROWS = 20
_MAX_LATE = 60
_MAX_HISTORY_DAYS = 3650
# A host name: dot-separated labels of letters, digits and inner hyphens.
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class DashboardOptions:
    """The [dashboard] table, checked, with defaults filled in."""

    state_dir: str | None = None
    train_rows: int = 10
    bus_rows: int = 10
    train_interval_seconds: int = 60
    bus_interval_seconds: int = 30
    weather_interval_minutes: int = 30
    quiet_windows: tuple[Window, ...] = (parse_window("Mon-Sun 01:00-05:00"),)
    late_minutes_train: int = 3
    late_minutes_bus: int = 2
    history_days: int = 90
    allowed_hosts: frozenset[str] = frozenset()
    # The Knowledgebase Incidents address from the RDM product page; until it
    # is set, rail engineering works are off even with RDM_KB_API_KEY.
    kb_incidents_url: str = KB_URL_PLACEHOLDER


def _ranged(section: dict, key: str, default: int, low: int, high: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{_WHERE}{key} must be a whole number from {low} to {high}, not {value!r}")
    return value


def _host(name: str) -> str:
    """An exact host name for allowed_hosts, lower-cased; ConfigError if it isn't one."""
    host = name.strip().lower()
    labels = host.split(".")
    if not host or len(host) > 253 or not all(_LABEL.fullmatch(label) for label in labels):
        raise ConfigError(
            f"{_WHERE}allowed_hosts: {name!r} is not a host name like departures.example.com "
            "(no wildcards, ports or schemes)"
        )
    return host


def _feed_url(section: dict, key: str, default: str) -> str:
    """An https address for a feed that will be sent a key; ConfigError otherwise."""
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{_WHERE}{key} must be an https address in quotes, not {value!r}")
    url = value.strip()
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        parts = None
    if (parts is None or parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or any(c.isspace() or not c.isprintable() for c in url)):
        raise ConfigError(f"{_WHERE}{key} must be an https address like https://example.com/path, not {value!r}")
    return url


def dashboard_options(data: dict) -> DashboardOptions:
    """The [dashboard] table of already-read config data, checked."""
    section = _table(data, "dashboard", "[dashboard]")
    unknown = sorted(set(section) - _KEYS)
    if unknown:
        raise ConfigError(f"{_WHERE}has unknown settings: {', '.join(unknown)}")
    state_dir = section.get("state_dir")
    if state_dir is not None and (not isinstance(state_dir, str) or not state_dir.strip()):
        raise ConfigError(f"{_WHERE}state_dir must be a folder path in quotes, not {state_dir!r}")
    windows = DashboardOptions.quiet_windows
    if "quiet_windows" in section:
        try:
            windows = tuple(parse_window(w) for w in _texts(section, "quiet_windows", _WHERE))
        except ValueError as error:
            raise ConfigError(f"{_WHERE}quiet_windows: {error}") from None
    return DashboardOptions(
        state_dir=state_dir,
        train_rows=_ranged(section, "train_rows", 10, 1, _MAX_ROWS),
        bus_rows=_ranged(section, "bus_rows", 10, 1, _MAX_ROWS),
        train_interval_seconds=_whole(section, "train_interval_seconds", 60, _WHERE, least=30),
        bus_interval_seconds=_whole(section, "bus_interval_seconds", 30, _WHERE, least=15),
        weather_interval_minutes=_whole(section, "weather_interval_minutes", 30, _WHERE, least=10),
        quiet_windows=windows,
        late_minutes_train=_ranged(section, "late_minutes_train", 3, 1, _MAX_LATE),
        late_minutes_bus=_ranged(section, "late_minutes_bus", 2, 1, _MAX_LATE),
        history_days=_ranged(section, "history_days", 90, 1, _MAX_HISTORY_DAYS),
        allowed_hosts=frozenset(_host(h) for h in _texts(section, "allowed_hosts", _WHERE)),
        kb_incidents_url=_feed_url(section, "kb_incidents_url", KB_URL_PLACEHOLDER),
    )


def default_state_dir(platform: str = sys.platform, env: Mapping[str, str] | None = None,
                      home: Path | None = None) -> Path:
    """Where the dashboard keeps its files when nothing says: Application
    Support on a Mac, else $XDG_STATE_HOME or ~/.local/state."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if platform == "darwin":
        return home / "Library" / "Application Support" / FOLDER_NAME
    xdg = env.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "state"
    return base / FOLDER_NAME


class TrainSource(Protocol):
    def fetch_board(self) -> TrainBoard: ...


class DetailsSource(Protocol):
    def details(self, service_id: str) -> ServiceDetails: ...


class BusSource(Protocol):
    def unusable(self) -> str | None: ...

    def fetch_live(self, rows: int | None = None) -> BusBoard: ...

    def refresh_timetable(self, force: bool = False): ...


class ConditionsSource(Protocol):
    def fetch_conditions(self) -> Conditions: ...


class DisruptionFeed(Protocol):
    def fetch(self) -> list: ...


@dataclass
class DashboardSettings:
    """Everything the dashboard runs on. Sources are None when not configured."""

    title: str
    timezone: str
    state_dir: Path
    walk: WalkTimes
    options: DashboardOptions
    train: TrainSource | None = None
    train_crs: str = ""
    train_filter: str | None = None
    # Service details for the trains' positions; None without RDM_DETAILS_API_KEY.
    train_details: DetailsSource | None = None
    bus: BusSource | None = None
    # A second bus source for rebuilding the timetable from the upkeep thread:
    # BodsBusSource remembers the timetable it last read, so each thread gets
    # its own and they only share the file, which is replaced whole.
    bus_upkeep: BusSource | None = None
    bus_stops: dict[str, str] = field(default_factory=dict)
    weather: ConditionsSource | None = None
    # BODS SIRI-SX, with the buses; None without buses.
    bus_disruptions: DisruptionFeed | None = None
    # The Knowledgebase's incidents; None without RDM_KB_API_KEY or its address,
    # with train_incidents_note saying which when the key is there.
    train_incidents: DisruptionFeed | None = None
    train_incidents_note: str | None = None
    rain_threshold: int = 50
    bank_holidays: bool = True
    lock: IO | None = None

    def close(self) -> None:
        """Let go of the state folder's lock."""
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def _same_folder(a: Path, b: Path) -> bool:
    return a.expanduser().resolve() == b.expanduser().resolve()


def _check_folder(folder: Path, data: dict) -> None:
    """Refuse the board's own state folder: the dashboard would overwrite its
    timetable and holidays with ones built for other rows and stops."""
    board = board_state_dir(data)
    # Only an absolute state_file names a folder on this machine; a relative one
    # is relative to wherever the board runs (in a container, that's not here).
    if board is not None and board.is_absolute() and _same_folder(folder, board):
        raise ConfigError(f"{folder} is the board's state folder; give the dashboard its own --state-dir")
    if (folder / BOARD_STATE).exists():
        raise ConfigError(f"{folder} holds a board's {BOARD_STATE}; give the dashboard its own --state-dir")


def _make_folder(folder: Path) -> None:
    """Create the state folder readable by its owner only. An existing one is
    left as it is (a Docker volume may belong to the container's user)."""
    if folder.exists():
        if not folder.is_dir():
            raise ConfigError(f"{folder} is not a folder")
        return
    try:
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)  # whatever the umask
    except OSError as error:
        raise ConfigError(f"Can't create the state folder {folder}: {error.strerror or error}") from None


def _lock(folder: Path) -> IO:
    """An exclusive lock on the folder, held until the file is closed."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(folder / LOCK_FILE, flags, 0o600)
    except OSError as error:
        raise ConfigError(f"Can't open {folder / LOCK_FILE}: {error.strerror or error}") from None
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ConfigError(f"Another dashboard is already running with the state folder {folder}") from None
    return handle


def _header_key(env: Mapping[str, str], name: str) -> str:
    """An optional key from the environment, "" if unset. It goes in a header:
    anything that could split one is refused, without repeating the key."""
    key = env.get(name, "").strip()
    if key and (not key.isprintable() or any(c.isspace() for c in key)):
        raise ConfigError(f"{name} has spaces or control characters in it")
    return key


def load_dashboard(config_path: Path | str, env: Mapping[str, str],
                   state_dir_override: Path | str | None = None) -> DashboardSettings:
    """The dashboard's settings from config.toml (with the settings page's
    overlay), the environment and secrets in `env`, and an optional state
    folder that wins over [dashboard] state_dir. Takes the folder's lock;
    call close() on the result to let it go."""
    data = copy.deepcopy(read_config(config_path))
    options = dashboard_options(data)
    folder = Path(state_dir_override or options.state_dir or default_state_dir(env=env)).expanduser()
    _check_folder(folder, data)

    if "train" in data:
        _table(data, "train", "[train]")["rows"] = options.train_rows
    if "bus" in data:
        bus = _table(data, "bus", "[bus]")
        if bus.get("source") != "bods":
            raise ConfigError('The dashboard needs [bus] source = "bods" (the free BODS timetable and positions)')
        bus["rows"] = options.bus_rows
    # Only the dashboard's own folder: the bus timetable and holidays live beside this.
    data["state_file"] = str(folder / STATE_FILE)
    free_env = {key: value for key, value in env.items() if not key.startswith("TAPI_")}
    settings = settings_from(data, free_env)

    train = bus = None
    for config in settings.board.sources:
        if isinstance(config.source, DarwinSource):
            train = config.source
            train.with_details = True  # GetDepBoardWithDetails: calling points and reasons
        elif config.source.name == "bus":
            bus = config.source
    train_section = data.get("train", {})
    details_key = _header_key(env, "RDM_DETAILS_API_KEY")
    weather = settings.board.weather
    kb_key = _header_key(env, "RDM_KB_API_KEY")
    incidents = incidents_note = None
    if train is not None and kb_key:
        if kb_url_is_placeholder(options.kb_incidents_url):
            incidents_note = "Engineering works are off: set [dashboard] kb_incidents_url from the RDM product page"
        else:
            incidents = TrainIncidentSource(kb_key, options.kb_incidents_url)
    bus_key = env.get("BODS_API_KEY", "").strip()

    _make_folder(folder)
    lock = _lock(folder)
    return DashboardSettings(
        title=settings.board.title,
        timezone=settings.timezone,
        state_dir=folder,
        walk=settings.board.walk,
        options=options,
        train=train,
        train_crs=str(train_section.get("crs", "")).strip().upper() if train else "",
        train_filter=(str(train_section.get("filter_crs") or "").strip().upper() or None) if train else None,
        train_details=ServiceDetailsClient(details_key) if train is not None and details_key else None,
        bus=bus,
        bus_upkeep=copy.copy(bus) if bus is not None else None,
        bus_stops=dict(data["bus"]["stops"]) if bus is not None else {},
        weather=weather.source if weather is not None else None,
        bus_disruptions=BusDisruptionSource(bus_key) if bus is not None and bus_key else None,
        train_incidents=incidents,
        train_incidents_note=incidents_note,
        rain_threshold=weather.threshold if weather is not None else 50,
        bank_holidays=_flag(data, "bank_holidays", True),
        lock=lock,
    )
