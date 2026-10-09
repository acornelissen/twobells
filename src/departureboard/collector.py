"""The dashboard's pollers: fetch each feed on its own interval and publish it to the Hub.

One thread per job (trains, buses, weather, upkeep, and a heartbeat that
keeps the snapshot's clock fresh). A job that fails keeps the last good data
on show, marked stale with a short error that never carries a key, and tries
again later and later (up to five minutes apart) until it works.

In the quiet windows (01:00-05:00 by default) nothing is fetched unless a
browser has used the dashboard in the last ten minutes.

With a Service Details key, each train poll also asks where the first few
trains are (see _refresh_details); a refused key turns that off for a while.

Threads: each feed's state below is only touched by its own poller's
thread. The bus poller and the upkeep thread each have their own
BodsBusSource (see DashboardSettings.bus_upkeep), as one remembers the
timetable it last read; they share only the timetable file, which is
replaced whole, and the poller reads it again when it changes.
"""

from __future__ import annotations

import logging
import random as _random
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import holidays as _holidays
from .dashconfig import DashboardSettings
from .darwin import DetailsUnavailable, ServiceDetails, ServiceGone, TrainBoard
from .history import History, day_type, phrase
from .model import Status
from .snapshot import (
    Hub,
    bus_observations,
    buses_part,
    status_block,
    timetable_block,
    train_observations,
    trains_part,
    weather_part,
)
from .status import safe_message

log = logging.getLogger(__name__)

MAX_BACKOFF = 300.0  # seconds between tries of a failing job, at most
QUIET_CHECK = 5.0  # seconds between looks at the clock in a quiet window
CLIENT_GRACE = 600.0  # a browser seen this recently keeps the quiet windows busy
UPKEEP_SECONDS = 3600.0
HEARTBEAT_SECONDS = 60.0
HEALTHY_AGE = 180.0  # /healthz wants a snapshot rebuilt this recently
DETAILS_TRAINS = 3  # trains placed on their route each poll: the first ones not cancelled
DETAILS_TTL = 60.0  # seconds before a train's details are asked for again
DETAILS_OFF = 6 * 3600.0  # seconds without details after the key is refused
_JITTER = 0.1  # intervals vary by this fraction either way, so jobs drift apart


class Poller:
    """Runs `job` every `interval` seconds on its own thread until stopped."""

    def __init__(
        self,
        name: str,
        job: Callable[[], Any],
        interval: float,
        *,
        quiet: Callable[[], bool] | None = None,
        random: Callable[[], float] = _random.random,
        max_backoff: float = MAX_BACKOFF,
        quiet_check: float = QUIET_CHECK,
    ) -> None:
        self.name = name
        self._job = job
        self._interval = interval
        self._quiet = quiet or (lambda: False)
        self._random = random
        self._max_backoff = max_backoff
        self._quiet_check = quiet_check
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.failures = 0

    def step(self) -> float:
        """Run the job once, unless it is quiet; the seconds to wait before the next."""
        if self._quiet():
            return self._quiet_check
        try:
            self._job()
        except Exception as error:  # noqa: BLE001 - logged and retried; the job keeps its last data
            self.failures += 1
            log.warning("dashboard %s failed (%d in a row): %s", self.name, self.failures, safe_message(error))
            delay = min(self._interval * 2**self.failures, self._max_backoff)
        else:
            self.failures = 0
            delay = self._interval
        return delay * (1 - _JITTER + 2 * _JITTER * self._random())

    def run(self) -> None:
        while not self._stop.is_set():
            delay = self.step()
            self._wake.wait(delay)
            self._wake.clear()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name=f"dashboard-{self.name}", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Run the job now rather than at the end of the wait."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.alive

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


@dataclass
class _Feed:
    """The last good data from one feed, and how the last try went."""

    data: Any = None
    fetched: datetime | None = None
    last_success: datetime | None = None
    error: str | None = None
    note: str | None = None

    def status(self) -> dict:
        return status_block(self.last_success, self.error is not None, self.error, self.note)

    def worked(self, data: Any, now: datetime, note: str | None = None) -> None:
        self.data, self.fetched, self.last_success, self.error, self.note = data, now, now, None, note

    def failed(self, error: BaseException) -> None:
        self.error = safe_message(error)


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


class Collector:
    """Fetches the feeds the settings name and publishes them to the hub."""

    def __init__(
        self,
        settings: DashboardSettings,
        hub: Hub,
        history: History | None,
        *,
        clock: Callable[[], datetime] | None = None,
        fetch_holidays: Callable[..., frozenset[date]] = _holidays.bank_holidays,
        random: Callable[[], float] = _random.random,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._options = settings.options
        self._hub = hub
        self._history = history
        self._tz = ZoneInfo(settings.timezone)
        self._clock = clock or (lambda: datetime.now(self._tz))
        self._fetch_holidays = fetch_holidays
        self._random = random
        self._monotonic = monotonic
        self._holidays = _holidays.read_cache(settings.state_dir) if settings.bank_holidays else frozenset()
        crs, filter_crs = settings.train_crs, settings.train_filter
        self._route_prefix = f"{crs}>{filter_crs}" if filter_crs else crs
        self._trains, self._buses, self._weather = _Feed(), _Feed(), _Feed()
        # Service details by Darwin service ID, with when they came (monotonic).
        self._details: dict[str, tuple[float, ServiceDetails]] = {}
        self._details_off_until: float | None = None
        self._timetable_tried = False  # has upkeep tried to build the bus timetable yet
        self._building = False  # is it building one now, with none usable
        self._pollers: dict[str, Poller] = {}
        self._publish_all()

    # Publishing

    def _phrases(self, mode: str, route: str, scheduled: str, day: date) -> tuple[str, str | None]:
        kind = day_type(day, self._holidays)
        if self._history is None:
            return kind, None
        late = self._options.late_minutes_train if mode == "train" else self._options.late_minutes_bus
        try:
            stats = self._history.stats(mode, route, scheduled, kind, late_minutes=late, holidays=self._holidays)
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard history unreadable: %s", safe_message(error))
            return kind, None
        return kind, phrase(stats, kind)

    def _publish_trains(self) -> None:
        settings = self._settings
        details = {service_id: found for service_id, (_, found) in self._details.items()}
        part, lookups = trains_part(
            self._trains.data, self._trains.fetched, self._trains.status(), settings.train_crs,
            settings.train_filter, settings.walk, self._route_prefix, self._phrases,
            details=details, now=self._clock(),
        )
        self._hub.publish("trains", part, lookups)

    def _timetable_state(self) -> str:
        return "building" if self._building or not self._timetable_tried else "missing"

    def _publish_buses(self, timetable_state: str | None = None) -> None:
        board = self._buses.data
        if board is None:
            timetable = timetable_block(timetable_state or self._timetable_state(), None, self._tz)
        else:
            timetable = timetable_block("ready", board.schedule, self._tz)
        part, lookups = buses_part(board, self._clock(), self._buses.status(), timetable,
                                   self._settings.bus_stops, self._settings.walk, self._phrases)
        self._hub.publish("buses", part, lookups)

    def _publish_weather(self) -> None:
        part = weather_part(self._weather.data, self._clock(), self._weather.status(), self._settings.rain_threshold)
        self._hub.publish("weather", part)

    def _publish_all(self) -> None:
        if self._settings.train is not None:
            self._publish_trains()
        if self._settings.bus is not None:
            self._publish_buses()
        if self._settings.weather is not None:
            self._publish_weather()

    def _observe(self, mode: str, rows: list, now: datetime) -> None:
        if self._history is None:
            return
        try:
            self._history.observe(mode, rows, now.replace(tzinfo=None))
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard history not saved: %s", safe_message(error))

    # Jobs

    def poll_trains(self) -> None:
        now = self._clock()
        try:
            board = self._settings.train.fetch_board()
        except Exception as error:
            self._trains.failed(error)
            self._publish_trains()
            raise
        self._observe("train", train_observations(board, now, self._route_prefix), now)
        self._trains.worked(board, now)
        self._publish_trains()
        if self._settings.train_details is not None:
            # After the board is out, so slow details never hold it up.
            self._refresh_details(board)
            self._publish_trains()

    def _refresh_details(self, board: TrainBoard) -> None:
        """Service details for the first few trains that aren't cancelled,
        each asked for at most once a minute. Details of trains that have
        left the board, or been cancelled, are forgotten. A refused key turns
        details off for six hours; any other failure skips that train until
        the next poll, keeping what was known."""
        running = {s.service_id for s in board.services
                   if s.service_id and s.departure.status is not Status.CANCELLED}
        self._details = {sid: entry for sid, entry in self._details.items() if sid in running}
        if self._details_off_until is not None and self._monotonic() < self._details_off_until:
            return
        wanted = [s.service_id for s in board.services if s.service_id in running][:DETAILS_TRAINS]
        for service_id in wanted:
            entry = self._details.get(service_id)
            if entry is not None and self._monotonic() - entry[0] < DETAILS_TTL:
                continue
            try:
                found = self._settings.train_details.details(service_id)
            except DetailsUnavailable as error:
                self._details_off_until = self._monotonic() + DETAILS_OFF
                self._details = {}
                log.warning("dashboard train positions off for 6 hours: %s", safe_message(error))
                return
            except ServiceGone:
                self._details.pop(service_id, None)
            except Exception as error:  # noqa: BLE001 - the board matters more than one train's position
                log.warning("dashboard train position skipped: %s", safe_message(error))
            else:
                self._details[service_id] = (self._monotonic(), found)

    def poll_buses(self) -> None:
        source = self._settings.bus
        now = self._clock()
        reason = source.unusable()
        if reason is not None:
            # Nothing to show until upkeep builds a timetable; not an error to back off from.
            state = self._timetable_state()
            self._buses.data, self._buses.error = None, None
            self._buses.note = (
                "Building the bus timetable; this takes a few minutes" if state == "building" else _sentence(reason)
            )
            self._publish_buses(state)
            return
        try:
            board = source.fetch_live()
        except Exception as error:
            self._buses.failed(error)
            self._publish_buses()
            raise
        if board.vehicles_ok:  # timetabled times alone say nothing about punctuality
            self._observe("bus", bus_observations(board), now)
        self._buses.worked(board, now, board.note)
        self._publish_buses()

    def poll_weather(self) -> None:
        now = self._clock()
        try:
            conditions = self._settings.weather.fetch_conditions()
        except Exception as error:
            self._weather.failed(error)
            self._publish_weather()
            raise
        self._weather.worked(conditions, now)
        self._publish_weather()

    def upkeep(self) -> None:
        """Hourly: bank holidays (refreshed weekly), the bus timetable (rebuilt
        when due), and the history's sweep and purge."""
        local = self._clock().replace(tzinfo=None)
        errors: list[Exception] = []
        try:
            found = self._fetch_holidays(self._settings.state_dir, local)
            if self._settings.bank_holidays:
                self._holidays = frozenset(found)
        except Exception as error:  # noqa: BLE001 - bank_holidays keeps the last list
            errors.append(error)
        upkeep_bus = self._settings.bus_upkeep
        if upkeep_bus is not None:
            self._building = upkeep_bus.unusable() is not None
            try:
                upkeep_bus.refresh_timetable()
            except Exception as error:  # noqa: BLE001 - the saved timetable is kept
                errors.append(error)
            finally:
                self._building = False
                self._timetable_tried = True
            if "buses" in self._pollers:
                self._pollers["buses"].wake()  # show the new timetable, or why there is none
        if self._history is not None:
            try:
                self._history.sweep(local)
                self._history.purge(local, self._options.history_days)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            raise errors[0]

    # Running

    def quiet(self) -> bool:
        """In a quiet window with no browser seen lately: don't fetch."""
        local = self._clock().replace(tzinfo=None)
        if not any(window.contains(local, self._holidays) for window in self._options.quiet_windows):
            return False
        return not self._hub.active(CLIENT_GRACE)

    def pollers(self) -> dict[str, Poller]:
        """The pollers for the configured feeds, not yet started."""
        options = self._options
        found = {}

        def add(name: str, job: Callable[[], Any], interval: float, quiet: bool = True) -> None:
            found[name] = Poller(name, job, interval, quiet=self.quiet if quiet else None, random=self._random)

        if self._settings.train is not None:
            add("trains", self.poll_trains, options.train_interval_seconds)
        if self._settings.bus is not None:
            add("buses", self.poll_buses, options.bus_interval_seconds)
        if self._settings.weather is not None:
            add("weather", self.poll_weather, options.weather_interval_minutes * 60)
        add("upkeep", self.upkeep, UPKEEP_SECONDS, quiet=False)
        add("heartbeat", self._hub.refresh, HEARTBEAT_SECONDS, quiet=False)
        return found

    def start(self) -> None:
        self._pollers = self.pollers()
        for poller in self._pollers.values():
            poller.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Stop every poller; True if all their threads ended within `timeout`
        seconds (one still fetching ends with the process, as threads are daemons)."""
        for poller in self._pollers.values():
            poller.stop()
        deadline = time.monotonic() + timeout
        return all(poller.join(max(0.0, deadline - time.monotonic())) for poller in self._pollers.values())

    def healthy(self) -> bool:
        """All pollers running and the snapshot rebuilt in the last three minutes."""
        pollers = list(self._pollers.values())
        return bool(pollers) and all(p.alive for p in pollers) and self._hub.age() < HEALTHY_AGE
