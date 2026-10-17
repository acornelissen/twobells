"""The dashboard's snapshot, handed from the fetch service to the web server on a Pi.

Why. On a Pi the web server (twobells-web.service) reaches only the
home network: its kernel IP filter denies everything else. The dashboard's
feeds are on the internet, so its pollers (collector.Collector) run in the
fetch service instead, which goes out and listens on nothing. The two share
the spool folder in memory (setup/spool/), and the snapshot goes from one
to the other as a file there:

  snapshots/dashboard.json

written whole by SnapshotWriter (made new in tmp/, 0600, then renamed) when
the snapshot changes, and every HEARTBEAT_S besides so its `made` stays
fresh. It holds exactly:

  {"v": 1, "run": <16 hex, new for each writer>, "made": <unix seconds>,
   "version": <the hub's version>, "snapshot": {...as the hub sends it},
   "lookups": {<row id>: {...}}, "previous": {<row id>: {...}},
   "area": {...} or null, "healthy": true or false}

Docker and a desktop have no such filter: their server reads the pollers'
Hub directly, and none of this is used.

Trust. SnapshotMirror, in the web server, treats the file as untrusted, as
the spool's other files are: it must be a plain file of ours, with one
link, opened without following a link, within MAX_SNAPSHOT_BYTES, strict
JSON no deeper than MAX_DEPTH, with exactly the fields above, `v` 1, ids of
the right form and `made` not in the future. A file that fails keeps the
last good snapshot on show, and the log names what was wrong by field,
never by value. What the browsers get is encoded here again, as the hub
encodes it: ASCII JSON on one line, safe in an event stream.

Versions. The mirror counts its own: it goes up whenever the file's run or
version changes, so a fetch service started again (a new run, its hub's
versions from 1 again) never repeats a version a browser has seen.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.walk import WalkTimes
from ..dashboard.collector import HEALTHY_AGE
from ..dashboard.snapshot import PARTS, Hub, _encode
from ..settings import modes as mode_list
from ..settings import show as show_items
from ..setup.spool.checks import CLOCK_SKEW_S, Refused, check_dict, check_int, check_num, check_obj, check_str, parse
from ..setup.spool.folder import Spool
from ..setup.spool.runner import ACTIVE_EVERY_S, Poster

log = logging.getLogger(__name__)

VERSION = 1
NAME = "dashboard"  # snapshots/dashboard.json
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 32
MAX_LOOKUPS = 1000
HEARTBEAT_S = 30.0  # written at least this often, so `made` says the fetch service is still there
STALE_S = 2 * HEARTBEAT_S  # older than this, the fetch service may have stopped: ask it to start
READ_EVERY_S = 0.5  # how often the mirror looks at the file

_FIELDS = ("v", "run", "made", "version", "snapshot", "lookups", "previous", "area", "healthy")
# The trams came later: an older fetch service, mid-upgrade, sends no "metro".
_LATER_PARTS = ("metro",)
_SNAPSHOT_FIELDS = ("version", "generated_at", "title", "walk", *(p for p in PARTS if p not in _LATER_PARTS))
_OPTIONAL_FLAGS = ("quiet", "row_strips", "scrolling")
# What the page shows ([dashboard] show): a list of the dashboard's items.
_SHOW = "show"
# The modes Next to catch picks from ([dashboard] leave_modes): a list of modes.
_LEAVE = "leave_modes"
_HISTORY_FIELDS = ("mode", "route", "scheduled", "label", "day_type", "phrase")
_HEX16 = re.compile(r"[0-9a-f]{16}")


def snapshot_path(folder: Path | str) -> Path:
    return Spool(folder).snapshots / f"{NAME}.json"


def remove(folder: Path | str) -> None:
    """Delete the snapshot: the dashboard view is off, so none is kept."""
    snapshot_path(folder).unlink(missing_ok=True)


def _lookup(value, name: str) -> None:
    """A row's lookup: its bus map (sent as it is) and its history key,
    which the web server asks the history with (snapshot.bus_row, train_row)."""
    entry = check_obj(value, (), ("bus", "history"))
    if "bus" in entry and not isinstance(entry["bus"], dict):
        raise Refused(f"{name} has a bus map that isn't an object")
    if "history" in entry:
        history = check_obj(entry["history"], _HISTORY_FIELDS)
        for field in _HISTORY_FIELDS:
            if not isinstance(history[field], str) and not (field == "phrase" and history[field] is None):
                raise Refused(f"{name} has a history {field} that isn't text")


def check(data, now: float) -> dict:
    """The snapshot file's contents, checked; Refused, naming the field, if they don't fit."""
    top = check_obj(data, _FIELDS)
    if type(top["v"]) is not int or top["v"] != VERSION:
        raise Refused("v isn't this version")
    check_str(top["run"], "run", 16, 16)
    if not _HEX16.fullmatch(top["run"]):
        raise Refused("run isn't in the right form")
    if check_num(top["made"], "made") > now + CLOCK_SKEW_S:
        raise Refused("made is in the future")
    check_int(top["version"], "version", 0, 2**53)
    # An older fetch service, mid-upgrade, sends no `quiet`, `row_strips`, `scrolling`, `show` or `leave_modes`.
    snapshot = check_obj(top["snapshot"], _SNAPSHOT_FIELDS, (*_OPTIONAL_FLAGS, _SHOW, _LEAVE, *_LATER_PARTS))
    for name in _OPTIONAL_FLAGS:
        if name in snapshot and not isinstance(snapshot[name], bool):
            raise Refused(f"{name} isn't true or false")
    if _SHOW in snapshot:
        try:
            show_items.checked("dashboard", snapshot[_SHOW], "")
        except ValueError:
            raise Refused("show isn't a list of what the dashboard shows") from None
    if _LEAVE in snapshot:
        try:
            mode_list.leave_checked(snapshot[_LEAVE], "")
        except ValueError:
            raise Refused("leave_modes isn't a list of modes") from None
    for name in ("lookups", "previous"):
        for key, value in check_dict(top[name], name, MAX_LOOKUPS).items():
            if not _HEX16.fullmatch(key):
                raise Refused(f"{name} has a row id in the wrong form")
            _lookup(value, name)
    if top["area"] is not None and not isinstance(top["area"], dict):
        raise Refused("area isn't an object")
    if not isinstance(top["healthy"], bool):
        raise Refused("healthy isn't true or false")
    return top


class SnapshotWriter:
    """Writes the hub's snapshot to the spool, in the fetch service: when
    its version changes and every `heartbeat` seconds, on a thread of its
    own until stopped or the hub is closed. `healthy` says whether the
    pollers are, for the web server's /healthz."""

    def __init__(self, hub: Hub, spool: Spool, healthy: Callable[[], bool], *, heartbeat: float = HEARTBEAT_S,
                 clock: Callable[[], float] = time.time, cap: int = MAX_SNAPSHOT_BYTES) -> None:
        self.run_id = secrets.token_hex(8)  # new for each writer, so a mirror sees each as new
        self._hub = hub
        self._spool = spool
        self._healthy = healthy
        self._heartbeat = heartbeat
        self._clock = clock
        self._cap = cap
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refused = False

    def write(self) -> int:
        """Write the snapshot now, unless it can't be (too big, say: then the
        last one stays, and goes stale). The hub's version it had."""
        exported = self._hub.export()
        try:
            healthy = bool(self._healthy())
        except Exception as error:  # noqa: BLE001 - said as unhealthy
            log.warning("Can't tell whether the dashboard's pollers are healthy: %s", type(error).__name__)
            healthy = False
        data = {"v": VERSION, "run": self.run_id, "made": self._clock(), "version": exported["version"],
                "snapshot": exported["snapshot"], "lookups": exported["lookups"],
                "previous": exported["previous"], "area": exported["area"], "healthy": healthy}
        try:
            self._spool.write(self._spool.snapshots, NAME, data, self._cap)
        except (OSError, ValueError) as error:
            if not self._refused:  # once, not every heartbeat
                log.warning("The dashboard's snapshot can't be handed over (%s); the last one stays",
                            "too big" if isinstance(error, ValueError) else type(error).__name__)
            self._refused = True
            return exported["version"]
        if self._refused:
            log.info("The dashboard's snapshot is handed over again")
        self._refused = False
        return exported["version"]

    def run(self) -> None:
        while not self._stop.is_set() and not self._hub.closed:
            self._hub.wait(self.write(), self._heartbeat)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="snapshot-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop writing. Close the hub first, which wakes the thread at once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


class SnapshotMirror:
    """The fetch service's snapshot, read from the spool, for the web server
    on a Pi: a snapshot.SnapshotSource, as the Hub is elsewhere. Until the
    first good file it shows an empty snapshot with `title`, `walk`,
    `row_strips`, `scrolling`, `show` and `leave`.

    `poster` tells the fetch service a browser is using the dashboard
    (touch), which also starts it if it has stopped; and while `wanted()`
    (the fetch service would run the dashboard: its view is on and its
    settings load) and the file is missing or stale, the mirror asks it to
    start, as often as the poster allows."""

    def __init__(self, folder: Path | str, title: str, walk: WalkTimes, poster: Poster | None, *,
                 wanted: Callable[[], bool] = lambda: True, timezone: str | None = None,
                 row_strips: bool = True, show: Iterable[str] | None = None, scrolling: bool = True,
                 leave: Iterable[str] | None = None, every: float = READ_EVERY_S,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._path = snapshot_path(folder)
        self._poster = poster
        self._wanted = wanted
        self._every = every
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        placeholder = Hub(title, walk, tz=ZoneInfo(timezone) if timezone else None, row_strips=row_strips,
                          show=show, scrolling=scrolling, leave=leave)
        self._version, self._body = placeholder.current()
        self._lookups: dict[str, dict] = {}
        self._previous: dict[str, dict] = {}
        self._area: dict | None = None
        self._shown: tuple[str, int] | None = None  # the (run, version) on show
        self._made: float | None = None
        self._healthy = False
        self._stamp: tuple | None = None
        self._touched: float | None = None
        self._woken: float | None = None
        self._closed = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # Reading the file

    def check(self) -> bool:
        """Look at the file once, and take it if it changed and fits. Whether it was taken."""
        try:
            info = os.lstat(self._path)
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        if stamp is None:
            return False  # gone: the last one stays, going stale
        try:
            body = Spool.read(self._path, MAX_SNAPSHOT_BYTES, delete=False)
            if body is None:
                return False
            found = check(parse(body, MAX_DEPTH), self._clock())
        except Refused as why:
            log.warning("Refused the fetch service's snapshot: %s; the last one stays", why)
            return False
        self._take(found)
        return True

    def _take(self, found: dict) -> None:
        shown = (found["run"], found["version"])
        snapshot = found["snapshot"]
        with self._lock:
            new = shown != self._shown
            if new:
                self._version += 1
            snapshot["version"] = self._version
            self._body = _encode(snapshot).encode("ascii")
            self._lookups, self._previous, self._area = found["lookups"], found["previous"], found["area"]
            self._shown, self._made, self._healthy = shown, float(found["made"]), found["healthy"]
            if new:
                self._changed.notify_all()

    def wake_if_stale(self) -> None:
        """While the dashboard is wanted and the snapshot is missing or stale,
        ask the fetch service to start; at most once every ACTIVE_EVERY_S, so
        the settings are read no more often than that."""
        if self._poster is None:
            return
        now = self._monotonic()
        if self._woken is not None and now - self._woken < ACTIVE_EVERY_S:
            return
        if self.age() <= STALE_S:
            return
        self._woken = now
        try:
            wanted = self._wanted()
        except Exception as error:  # noqa: BLE001 - asked again next time
            log.warning("Can't tell whether the dashboard is on: %s", type(error).__name__)
            return
        if wanted:
            self._poster.active()

    def _run(self) -> None:
        while not self._stopping.wait(self._every):
            self.check()
            self.wake_if_stale()

    def start(self) -> None:
        """Read the file now, then keep looking on a thread of its own until closed."""
        self.check()
        self.wake_if_stale()
        self._thread = threading.Thread(target=self._run, name="snapshot-mirror", daemon=True)
        self._thread.start()

    # snapshot.SnapshotSource

    def current(self) -> tuple[int, bytes]:
        with self._lock:
            return self._version, self._body

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def wait(self, after_version: int, timeout: float) -> int:
        with self._lock:
            self._changed.wait_for(lambda: self._version != after_version or self._closed, timeout)
            return self._version

    def lookup(self, row_id: str) -> dict | None:
        with self._lock:
            return self._lookups.get(row_id) or self._previous.get(row_id)

    def get(self, name: str) -> dict | None:
        with self._lock:
            return self._area if name == "area" else None

    def touch(self) -> None:
        """A browser is using the dashboard: noted here, and said to the fetch service (at most once a minute)."""
        with self._lock:
            self._touched = self._monotonic()
        if self._poster is not None:
            self._poster.active()

    def active(self, within: float) -> bool:
        with self._lock:
            return self._touched is not None and self._monotonic() - self._touched <= within

    def age(self) -> float:
        """Seconds since the fetch service made the snapshot on show; infinite before the first."""
        with self._lock:
            return math.inf if self._made is None else self._clock() - self._made

    def healthy(self) -> bool:
        """For /healthz: a fresh snapshot, from pollers the fetch service says are healthy."""
        with self._lock:
            healthy = self._healthy
        return healthy and self.age() < HEALTHY_AGE

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._changed.notify_all()
        self._stopping.set()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(5)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed
