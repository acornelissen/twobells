"""Settings for the dashboard view (`twobells serve`).

The dashboard reads the board's config.toml, plus an optional [dashboard]
table, and keeps everything it writes (the bus timetable, bank holidays,
punctuality history) in the install's store (settings/store.py), which it shares
with the board. It uses only the free feeds: Darwin, the BODS timetable
with live positions, and Open-Meteo. TransportAPI keys are dropped so no
fallback is ever built, and buses from TransportAPI aren't shown: the
dashboard says so (BUS_NOTE) rather than refusing to start.
Darwin's Service Details, which places each train on its route, is used
only when its own key, RDM_DETAILS_API_KEY, is set; with a filter station
it also follows trains there for the Delay Repay watch (repay.py). Bus disruptions come
from BODS SIRI-SX with the buses' own key; rail engineering works from the
National Rail Knowledgebase only when RDM_KB_API_KEY is set, at the address
in [dashboard] kb_incidents_url.

One dashboard per store: a lock held for the life of the process stops a
second one sharing (and fighting over) the same files.

Settings saved from the dashboard's settings site (and its setup wizard) go
in the store too, never beside config.toml, which is read-only in Docker:
the one settings.toml and secrets.env the board's page saves into. The
dashboard watches every file its settings come from and, when one changes
and the result still loads, starts afresh to use them (see _SettingsWatch in cli/serve.py).
"""

from __future__ import annotations

import copy
import fcntl
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from ..core import files
from ..core.polling import Window, parse_window
from ..core.walk import WalkTimes
from ..settings import show as show_items
from ..settings.config import ConfigError, _flag, _table, _texts, _whole, settings_from, switched_on
from ..settings.schema import bus_source
from ..sources.bodsbus import BodsBusSource, BusBoard
from ..sources.darwin import DarwinSource, ServiceDetails, ServiceDetailsClient, TrainBoard
from ..sources.disruptions import KB_URL_PLACEHOLDER, BusDisruptionSource, TrainIncidentSource, kb_url_is_placeholder
from ..sources.holidays import ENGLAND_AND_WALES
from ..sources.lastbus import LastBus
from ..sources.weather import Conditions, OpenMeteoSource
from ..timetable.routesetup import DEFAULT_LIMITS, DocumentLimits

if TYPE_CHECKING:
    from ..settings.overlay import Target
    from ..settings.store import Store

log = logging.getLogger(__name__)

LOCK_FILE = "dashboard.lock"

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
    "row_strips",
    "show",
}
_MAX_ROWS = 20
# Why there are no buses on the dashboard when the board's come from TransportAPI.
BUS_NOTE = ('The dashboard shows buses only from the free BODS timetable. Use setup to find your stops\' '
            'bus companies, or take source = "transportapi" out of [bus] in config.toml and add their operators.')
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
    # Each row's small drawing of its stops, with the train or bus on it.
    # The page leaves them out when this is off, and always on a wall tablet.
    row_strips: bool = True
    # What the page shows of what it can leave out ([dashboard] show,
    # settings/show.py): everything unless the settings say otherwise.
    show: frozenset[str] = field(default_factory=lambda: show_items.default("dashboard"))


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


def allowed_hosts(data: dict) -> frozenset[str]:
    """[dashboard] allowed_hosts alone, checked: for the settings, which
    answer to them even when the rest of [dashboard] doesn't load."""
    section = _table(data, "dashboard", "[dashboard]")
    return frozenset(_host(h) for h in _texts(section, "allowed_hosts", _WHERE))


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
        row_strips=_flag(section, "row_strips", True, _WHERE),
        show=show_items.shown(data, "dashboard"),
    )


class TrainSource(Protocol):
    def fetch_board(self) -> TrainBoard: ...


class DetailsSource(Protocol):
    def details(self, service_id: str) -> ServiceDetails: ...


class BusSource(Protocol):
    def unusable(self) -> str | None: ...

    def fetch_live(self, rows: int | None = None) -> BusBoard: ...

    def last_buses(self, now: datetime | None = None) -> list[LastBus]: ...

    def let_go(self) -> None: ...

    # What it returns isn't used.
    def refresh_timetable(self, force: bool = False, limits: DocumentLimits = DEFAULT_LIMITS) -> object: ...


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
    # Why there are no buses, when [bus] is set up but can't be shown here.
    bus_note: str | None = None
    # A second bus source for rebuilding the timetable from the upkeep thread:
    # BodsBusSource remembers the timetable it last read, so each thread gets
    # its own and they only share the file, which is replaced whole.
    bus_upkeep: BusSource | None = None
    bus_stops: dict[str, str] = field(default_factory=dict)
    weather: ConditionsSource | None = None
    # BODS SIRI-SX, with the buses; None without buses.
    bus_disruptions: DisruptionFeed | None = None
    # The Knowledgebase's incidents; None without RDM_KB_API_KEY or its address.
    train_incidents: DisruptionFeed | None = None
    rain_threshold: int = 50
    bank_holidays: bool = True
    bank_holiday_division: str = ENGLAND_AND_WALES
    # Whether the page scrolls the first train's calling-at line in the looks
    # that do (the scrolling setting, which the board's screen follows too).
    scrolling: bool = True
    lock: IO | None = None

    def close(self) -> None:
        """Let go of the state folder's lock."""
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def _lock(folder: Path) -> IO:
    """An exclusive lock on the folder, held until the file is closed."""
    try:
        fd = files.open_lock(folder / LOCK_FILE)
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


def dashboard_target(store: Store) -> Target:
    """Where the dashboard's settings site and setup wizard save: the store,
    over config.toml, and only what the dashboard would load."""
    return store.target(lambda data, env: _build(data, env, store.folder))


class FileWatch:
    """Notices when any of some files is changed, added or removed."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = list(paths)
        self._stamp = self._stamps()

    def _stamps(self) -> list:
        stamps = []
        for path in self._paths:
            try:
                info = path.stat()
                stamps.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except OSError:
                stamps.append(None)
        return stamps

    def changed(self) -> bool:
        """Whether anything changed since the last call (or since made)."""
        stamp = self._stamps()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        return True


def load_dashboard(store: Store | Path | str, env: Mapping[str, str],
                   state_dir_override: Path | str | None = None, lock: bool = True) -> DashboardSettings:
    """The dashboard's settings from the store (config.toml with the saved
    settings over it) and `env`, the environment with every saved key. Takes
    the store's lock; call close() on the result to let it go. With
    lock=False it only checks that the settings load: no folder is made and
    no lock taken.

    Given a config.toml path rather than a store (as tests still do), it opens
    that path's store, in `state_dir_override` if given, and layers the keys
    saved there over `env`."""
    from ..settings.store import Store

    if not isinstance(store, Store):
        store = Store.open(store, state_dir=state_dir_override)
        env = store.env(env)
    settings = _build(store.data(), env, store.folder)
    if lock:
        store.make()
        settings.lock = _lock(store.folder)
    return settings


def _build(data: dict, env: Mapping[str, str], folder: Path) -> DashboardSettings:
    """The settings from already-read, already-layered data, without the lock."""
    data = switched_on(copy.deepcopy(data))  # [train] or [bus] with enabled = false is ignored, as the board does
    options = dashboard_options(data)

    if "train" in data:
        _table(data, "train", "[train]")["rows"] = options.train_rows
    bus_note = None
    if "bus" in data:
        bus = _table(data, "bus", "[bus]")
        if bus_source(bus) == "bods":
            bus["rows"] = options.bus_rows
        else:
            del data["bus"]  # the board's TransportAPI buses: none here, and a note saying why
            bus_note = BUS_NOTE
    # In the store, whatever the data says: the bus timetable and holidays live there.
    data["state_file"] = str(folder / "state.json")
    free_env = {key: value for key, value in env.items() if not key.startswith("TAPI_")}
    settings = settings_from(data, free_env)

    train = bus = None
    for config in settings.board.sources:
        if isinstance(config.source, DarwinSource):
            train = config.source
            train.with_details = True  # GetDepBoardWithDetails: calling points and reasons
        elif isinstance(config.source, BodsBusSource):  # TransportAPI buses were dropped above
            bus = config.source
    train_section = data.get("train", {})
    details_key = _header_key(env, "RDM_DETAILS_API_KEY")
    weather = settings.board.weather
    kb_key = _header_key(env, "RDM_KB_API_KEY")
    incidents = None
    if train is not None and kb_key:
        if kb_url_is_placeholder(options.kb_incidents_url):
            # Setup advice: the log and the settings site say it, not the household's page.
            log.info("Engineering works are off: set [dashboard] kb_incidents_url from the RDM product page")
        else:
            incidents = TrainIncidentSource(kb_key, options.kb_incidents_url)
    bus_key = env.get("BODS_API_KEY", "").strip()
    train_filter = (str(train_section.get("filter_crs") or "").strip().upper() or None) if train else None

    return DashboardSettings(
        title=settings.board.title,
        timezone=settings.timezone,
        state_dir=folder,
        walk=settings.board.walk,
        options=options,
        train=train,
        train_crs=str(train_section.get("crs", "")).strip().upper() if train else "",
        train_filter=train_filter,
        train_details=ServiceDetailsClient(details_key) if train is not None and details_key else None,
        bus=bus,
        bus_note=bus_note,
        bus_upkeep=copy.copy(bus) if bus is not None else None,
        bus_stops=dict(data["bus"]["stops"]) if bus is not None else {},
        # The board's forecast is Open-Meteo's, which gives the conditions now too.
        weather=weather.source if weather is not None and isinstance(weather.source, OpenMeteoSource) else None,
        bus_disruptions=BusDisruptionSource(bus_key) if bus is not None and bus_key else None,
        train_incidents=incidents,
        rain_threshold=weather.threshold if weather is not None else 50,
        bank_holidays=_flag(data, "bank_holidays", True),
        bank_holiday_division=settings.bank_holiday_division,
        scrolling=settings.board.scrolling,
    )
