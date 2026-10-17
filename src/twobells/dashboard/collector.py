"""The dashboard's pollers: fetch each feed on its own interval and publish it to the Hub.

One thread per job (trains, buses, weather, upkeep, and a heartbeat that
keeps the snapshot's clock fresh and says whether a quiet window is on). A job that fails keeps the last good data
on show, marked stale with a short error that never carries a key, and tries
again later and later (up to five minutes apart, or its own interval if that
is longer: a failing job never runs more often than a working one) until it
works.

In the quiet windows (01:00-05:00 by default) nothing is fetched unless a
browser has used the dashboard in the last ten minutes.

With a Service Details key, each train poll also asks where the first few
trains are (see _refresh_details); a refused key turns that off for a while.
With a filter station, a Service Details key and a history, each train
poll also feeds the Delay Repay watch (repay.ArrivalWatch), which follows
trains that have left until they reach the filter station; it asks Darwin
only from the train poller, so never in a quiet window, and without all
three there is no watch.

Disruptions (see sources/disruptions.py) are polled on their own threads: bus
situations every 10 minutes, rail incidents every 30. Which are ours
depends on the bus timetable and the train board, so when either changes
the disruptions are filtered again; that state is shared, under its lock.

A feed the dashboard's show list ([dashboard] show, settings/show.py)
leaves nothing to show from isn't fetched at all (see _fetched_for): the
weather, the disruptions, where the trains are and the Delay Repay watch.
The collector runs only while the dashboard view is on, and starts afresh
when the settings change, so it never fetches for an item no view that is
on shows. The departures are always fetched, and the punctuality history
records from them whatever the list says.

Threads: each feed's state below is only touched by its own poller's
thread, except the disruptions' (see _disruptions_lock) and the buses' when
the timetable is set aside (see set_timetable_aside). The bus poller and the upkeep thread each have their own
BodsBusSource (see DashboardSettings.bus_upkeep), as one remembers the
timetable it last read; they share only the timetable file, which is
replaced whole, and the poller reads it again when it changes.

The bus timetable is built by upkeep only with build_timetable (Docker and
a desktop). Without it (the Pi, where its timer and the setup search build
it) upkeep never downloads one, and the poller reads what they save. Either
way a build that finds another running in the store is skipped
(timetable.build) and tried again next hour.
"""

from __future__ import annotations

import logging
import random as _random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.errors import safe_message
from ..core.model import mark_platform_changes
from ..sources import holidays as _holidays
from ..sources.darwin import DetailsUnavailable, ServiceGone, TrainBoard, TrainDetails
from ..sources.disruptions import BusArea, RailArea, bus_disruptions, disruptions_part, train_disruptions
from ..sources.lastbus import LastBus
from ..timetable.build import TimetableBusy
from ..timetable.routesetup import DEFAULT_LIMITS, DocumentLimits
from .area import area_view, timetable_changes
from .config import DashboardSettings
from .history import History, day_type, no_show_phrase, phrase
from .repay import ArrivalWatch
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

log = logging.getLogger(__name__)

MAX_BACKOFF = 300.0  # seconds between tries of a failing job, at most, unless its interval is longer
QUIET_CHECK = 5.0  # seconds between looks at the clock in a quiet window
CLIENT_GRACE = 600.0  # a browser seen this recently keeps the quiet windows busy
UPKEEP_SECONDS = 3600.0
HEARTBEAT_SECONDS = 60.0
HEALTHY_AGE = 180.0  # /healthz wants a snapshot rebuilt this recently
BUS_DISRUPTIONS_SECONDS = 600.0
TRAIN_INCIDENTS_SECONDS = 1800.0
_JITTER = 0.1  # intervals vary by this fraction either way, so jobs drift apart
# Doublings of the interval a failing job's wait counts at most: far past any
# backoff cap, and short of a float's range, which days of failures would pass.
_MOST_DOUBLINGS = 32


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
            doubled = self._interval * 2 ** min(self.failures, _MOST_DOUBLINGS)
            delay = max(self._interval, min(doubled, self._max_backoff))
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


def _with_platform_changes(before: TrainBoard | None, fetched: datetime | None, board: TrainBoard,
                           now: datetime) -> TrainBoard:
    """`board` with each train whose platform isn't the one the last board
    (`before`, fetched at `fetched`) gave it marked so (mark_platform_changes)."""
    earlier = []
    if before is not None and fetched is not None:
        earlier = [s.departure for s in before.services if not s.departure.has_left(now, fetched)]
    marked = mark_platform_changes(earlier, [s.departure for s in board.services])
    services = [s if s.departure is dep else replace(s, departure=dep)
                for s, dep in zip(board.services, marked, strict=True)]
    return replace(board, services=services)


# The dashboard's items each feed it can leave out is fetched for: with
# none of them shown, it isn't fetched. Service Details is asked where the
# trains on the board are for the rows and Around here, and where trains
# that have left are for the Delay Repay list on the Performance page.
WEATHER_FOR = frozenset({"weather"})
DISRUPTIONS_FOR = frozenset({"disruptions"})
POSITIONS_FOR = frozenset({"where", "area_map"})
ARRIVALS_FOR = frozenset({"performance"})


def _fetched_for(settings: DashboardSettings) -> DashboardSettings:
    """`settings` without the feeds the dashboard shows nothing from, as if
    they weren't set up; the Delay Repay watch is decided apart (Collector).
    The copy holds no lock: the caller's settings keep it."""
    show = settings.options.show
    return replace(
        settings,
        lock=None,
        weather=settings.weather if show & WEATHER_FOR else None,
        bus_disruptions=settings.bus_disruptions if show & DISRUPTIONS_FOR else None,
        train_incidents=settings.train_incidents if show & DISRUPTIONS_FOR else None,
        train_details=settings.train_details if show & POSITIONS_FOR else None,
    )


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
        build_timetable: bool = True,
        timetable_limits: DocumentLimits = DEFAULT_LIMITS,
    ) -> None:
        # Only the feeds the dashboard shows something from.
        self._settings = _fetched_for(settings)
        self._options = settings.options
        self._hub = hub
        self._history = history
        self._tz = ZoneInfo(settings.timezone)
        self._clock = clock or (lambda: datetime.now(self._tz))
        self._fetch_holidays = fetch_holidays
        self._random = random
        self._monotonic = monotonic
        self._holidays = (_holidays.read_cache(settings.state_dir, settings.bank_holiday_division)
                          if settings.bank_holidays else frozenset())
        crs, filter_crs = settings.train_crs, settings.train_filter
        self._route_prefix = f"{crs}>{filter_crs}" if filter_crs else crs
        self._trains, self._buses, self._weather = _Feed(), _Feed(), _Feed()
        # How asking where the trains and the buses are is going, for the settings' Overview.
        self._train_positions, self._bus_positions = _Feed(), _Feed()
        # Where the first few trains are: their service details, asked for at
        # most once a minute each (sources.darwin.TrainDetails has the rules).
        details = self._settings.train_details
        self._details = TrainDetails(details, monotonic) if details else None
        # When trains reached the filter station, for the Performance page's Delay Repay list.
        self._arrival_watch = None
        if (history is not None and settings.train is not None and filter_crs and settings.train_details
                and settings.options.show & ARRIVALS_FOR):
            self._arrival_watch = ArrivalWatch(history, filter_crs, self._route_prefix, settings.train_details,
                                               monotonic=monotonic)
        self._build_timetable = build_timetable
        # The most of each timetable file upkeep reads and parses: the install's (settings/installs.py).
        self._timetable_limits = timetable_limits
        # Has upkeep tried to build the bus timetable yet; without build_timetable it never will.
        self._timetable_tried = not build_timetable
        self._building = False  # is it building one now, with none usable
        # Set aside (set_timetable_aside): the bus poller holds none of the
        # timetable and doesn't read it. The lock is held through each bus
        # poll and while setting it aside, so what a poll found is let go too.
        self._timetable_aside = False
        self._bus_lock = threading.Lock()
        # Disruptions: the feeds, and what counts as ours, shared between the
        # disruption, bus and train pollers under this lock.
        self._disruptions_lock = threading.Lock()
        self._situations, self._incidents = _Feed(), _Feed()
        self._bus_area: BusArea | None = None
        # The last buses today, as the bus poller last found them; the bus poller's alone.
        self._last_buses: list[LastBus] = []
        self._area_schedule = None  # the schedule _bus_area came from; the bus poller's alone
        self._changes_for: tuple | None = None  # (schedule, day) the timetable changes were worked out for
        self._rail_area: RailArea | None = None
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

    def _bus_no_shows(self, route: str, scheduled: str, day: date) -> str | None:
        """"No tracker on 3 of the last 10 weekdays" for a bus, or None."""
        if self._history is None:
            return None
        kind = day_type(day, self._holidays)
        try:
            without, days = self._history.no_shows("bus", route, scheduled, kind, holidays=self._holidays)
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard history unreadable: %s", safe_message(error))
            return None
        return no_show_phrase(without, days, kind)

    def _usual_platform(self, route: str, scheduled: str) -> str | None:
        """The platform a train usually leaves from, from the history, or None."""
        if self._history is None:
            return None
        try:
            return self._history.usual_platform("train", route, scheduled)
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard history unreadable: %s", safe_message(error))
            return None

    def _publish_trains(self) -> None:
        settings = self._settings
        details = self._details.known() if self._details is not None else {}
        part, lookups = trains_part(
            self._trains.data, self._trains.fetched, self._trains.status(), settings.train_crs,
            settings.train_filter, settings.walk, self._route_prefix, self._phrases,
            details=details, now=self._clock(),
            positions=self._train_positions.status() if settings.train_details is not None else None,
            platforms=self._usual_platform,
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
                                   self._settings.bus_stops, self._settings.walk, self._phrases,
                                   self._bus_no_shows, self._bus_positions.status(),
                                   self._last_buses if board is not None else ())
        self._hub.publish("buses", part, lookups)

    def _publish_bus_note(self) -> None:
        """No buses, and why (DashboardSettings.bus_note): the board's come
        from TransportAPI, which the dashboard doesn't use. Said as poll_buses
        says a timetable it can't use: a buses part with no rows and a note."""
        timetable = timetable_block("missing", None, self._tz)
        part, lookups = buses_part(None, self._clock(), status_block(note=self._settings.bus_note), timetable,
                                   {}, self._settings.walk)
        self._hub.publish("buses", part, lookups)

    def _publish_weather(self) -> None:
        part = weather_part(self._weather.data, self._clock(), self._weather.status(), self._settings.rain_threshold)
        self._hub.publish("weather", part)

    def _has_disruptions(self) -> bool:
        settings = self._settings
        return settings.bus_disruptions is not None or settings.train_incidents is not None

    def _publish_disruptions(self) -> None:
        """Filter the disruptions again and publish them; call with
        _disruptions_lock held."""
        settings = self._settings
        if not self._has_disruptions():
            return
        now = self._clock()
        bus, train, bus_status, train_status = [], [], None, None
        if settings.bus_disruptions is not None:
            bus_status = self._situations.status()
            if self._bus_area is None:
                bus_status["note"] = "Waiting for the bus timetable"
            bus = bus_disruptions(self._situations.data or [], self._bus_area, now)
        if settings.train_incidents is not None:
            train_status = self._incidents.status()
            if self._rail_area is None:
                train_status["note"] = "Waiting for the train board"
            train = train_disruptions(self._incidents.data or [], self._rail_area, now)
        self._hub.publish("disruptions", disruptions_part(bus, bus_status, train, train_status, self._tz))

    def _publish_all(self) -> None:
        if self._settings.train is not None:
            self._publish_trains()
        if self._settings.bus is not None:
            self._publish_buses()
        elif self._settings.bus_note is not None:
            self._publish_bus_note()
        if self._settings.weather is not None:
            self._publish_weather()
        with self._disruptions_lock:
            self._publish_disruptions()

    def _observe(self, mode: str, rows: list, now: datetime) -> None:
        if self._history is None:
            return
        try:
            self._history.observe(mode, rows, now)
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard history not saved: %s", safe_message(error))

    # Jobs

    def poll_trains(self) -> None:
        source = self._settings.train
        if source is None:  # pollers() only polls the feeds there are
            return
        now = self._clock()
        try:
            board = source.fetch_board()
        except Exception as error:
            self._trains.failed(error)
            self._publish_trains()
            raise
        board = _with_platform_changes(self._trains.data, self._trains.fetched, board, now)
        self._observe("train", train_observations(board, now, self._route_prefix), now)
        self._trains.worked(board, now)
        self._publish_trains()
        if self._settings.train_details is not None:
            # After the board is out, so slow details never hold it up.
            self._refresh_details(board)
            self._publish_trains()
        self._watch_arrivals(board, now)
        self._note_stations(board)

    def _watch_arrivals(self, board: TrainBoard, now: datetime) -> None:
        """Feed the Delay Repay watch; its trouble never holds up the departures."""
        if self._arrival_watch is None:
            return
        try:
            errors = self._arrival_watch.poll(board, now)
        except Exception as error:  # noqa: BLE001 - the departures matter more than the history
            log.warning("dashboard arrivals not saved: %s", safe_message(error))
            return
        for error in errors:
            log.warning("dashboard arrival not followed: %s", safe_message(error))

    def _note_stations(self, board: TrainBoard) -> None:
        """Our train operators and stations, for the rail incidents: this
        station, the filter station, and every station our trains call at.
        An empty board (late at night) says nothing, so the last is kept."""
        if self._settings.train_incidents is None:
            return
        operators = frozenset(s.operator_code for s in board.services if s.operator_code)
        if not operators:
            return
        stations = {self._settings.train_crs: board.location or ""}
        if self._settings.train_filter:
            stations.setdefault(self._settings.train_filter, "")
        points = [p for s in board.services for p in s.calling_points]
        known = self._details.known() if self._details is not None else {}
        points += [p for found in known.values() for p in found.previous]
        for point in points:
            if point.crs:
                stations[point.crs] = point.name
        found = RailArea(operators, stations, self._settings.train_crs)
        if found != self._rail_area:
            with self._disruptions_lock:
                self._rail_area = found
                self._publish_disruptions()

    def _refresh_details(self, board: TrainBoard) -> None:
        """Service details for the first few trains (TrainDetails has the
        rules), with how asking went for the settings' Overview: a refused
        key turns details off for six hours; any other failure skips that
        train until the next poll, keeping what was known."""
        if self._details is None:  # poll_trains only asks when there is a client
            return
        for _, error in self._details.refresh(board.services):
            if error is None:
                self._train_positions.worked(None, self._clock())
            elif isinstance(error, DetailsUnavailable):
                self._train_positions.failed(error)
                self._train_positions.note = "Not asked again for 6 hours"
                log.warning("dashboard train positions off for 6 hours: %s", safe_message(error))
            elif not isinstance(error, ServiceGone):
                self._train_positions.failed(error)
                log.warning("dashboard train position skipped: %s", safe_message(error))

    def set_timetable_aside(self, aside: bool) -> None:
        """While `aside`, the bus poller lets go of the timetable and doesn't
        read it again, showing it as being built; after, it reads it again at
        once. The Pi's fetch service sets it aside while a setup search runs
        beside it, which builds a timetable: the two are never held at once
        (fetch/service.py). A bus poll under way is waited for."""
        with self._bus_lock:
            self._timetable_aside = aside
            if aside:
                self._let_go_of_timetable()
        if not aside and "buses" in self._pollers:
            self._pollers["buses"].wake()

    def _let_go_of_timetable(self) -> None:
        """Hold nothing of the timetable, showing it as being built; with _bus_lock held."""
        source = self._settings.bus
        if source is not None:
            source.let_go()
        self._buses.data, self._buses.error = None, None
        self._buses.note = "Building the bus timetable; this takes a few minutes"
        self._area_schedule = None
        self._publish_buses("building")
        self._hub.put("area", None)
        if self._changes_for is not None:
            self._changes_for = None
            self._hub.publish("timetable_changes", None)

    def poll_buses(self) -> None:
        if self._settings.bus is None:  # pollers() only polls the feeds there are
            return
        with self._bus_lock:
            if self._timetable_aside:
                self._publish_buses("building")
                return
            self._poll_buses()

    def _poll_buses(self) -> None:
        source = self._settings.bus
        if source is None:
            return
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
            self._hub.put("area", None)
            if self._changes_for is not None:
                self._changes_for = None
                self._hub.publish("timetable_changes", None)
            return
        try:
            board = source.fetch_live()
        except Exception as error:
            self._buses.failed(error)
            self._publish_buses()
            raise
        if board.vehicles_ok:  # timetabled times alone say nothing about punctuality
            self._observe("bus", bus_observations(board, self._tz, now), now)
            self._bus_positions.worked(None, now)
        else:
            self._bus_positions.error = board.note or "Live positions failed"
        self._buses.worked(board, now, board.note)
        self._last_buses = self._find_last_buses(source, now)
        self._publish_buses()
        self._hub.put("area", area_view(board, now))
        self._note_schedule(board)
        self._publish_changes(board.schedule, now.date())

    @staticmethod
    def _find_last_buses(source, now: datetime) -> list[LastBus]:
        """The last buses today, from the timetable; none if that fails,
        since the departures matter more."""
        try:
            return source.last_buses(now)
        except Exception as error:  # noqa: BLE001 - never worth the bus rows
            log.warning("bus: last buses not worked out: %s", safe_message(error))
            return []

    def _publish_changes(self, schedule, day) -> None:
        """The timetable changes coming up, worked out again only when the
        timetable or the day changes (it reads every journey for each day)."""
        if self._changes_for is not None and self._changes_for[0] is schedule and self._changes_for[1] == day:
            return
        self._changes_for = (schedule, day)
        self._hub.publish("timetable_changes", timetable_changes(schedule, day))

    def _note_schedule(self, board) -> None:
        """Our bus lines and stops, for the bus disruptions, whenever the
        timetable is read again."""
        if self._settings.bus_disruptions is None or board.schedule is self._area_schedule:
            return
        self._area_schedule = board.schedule
        with self._disruptions_lock:
            self._bus_area = BusArea.from_schedule(board.schedule)
            self._publish_disruptions()

    def poll_weather(self) -> None:
        source = self._settings.weather
        if source is None:  # pollers() only polls the feeds there are
            return
        now = self._clock()
        try:
            conditions = source.fetch_conditions()
        except Exception as error:
            self._weather.failed(error)
            self._publish_weather()
            raise
        self._weather.worked(conditions, now)
        self._publish_weather()

    def _poll_disruptions(self, feed: _Feed, source) -> None:
        now = self._clock()
        try:
            found = source.fetch()
        except Exception as error:
            with self._disruptions_lock:
                feed.failed(error)
                self._publish_disruptions()
            raise
        with self._disruptions_lock:
            feed.worked(found, now)
            self._publish_disruptions()

    def poll_bus_disruptions(self) -> None:
        self._poll_disruptions(self._situations, self._settings.bus_disruptions)

    def poll_train_incidents(self) -> None:
        self._poll_disruptions(self._incidents, self._settings.train_incidents)

    def upkeep(self) -> None:
        """Hourly: bank holidays (refreshed weekly), the bus timetable (rebuilt
        when due, with build_timetable), and the history's sweep and purge."""
        now = self._clock()
        local = now.replace(tzinfo=None)
        errors: list[Exception] = []
        try:
            found = self._fetch_holidays(self._settings.state_dir, local,
                                         division=self._settings.bank_holiday_division)
            if self._settings.bank_holidays:
                self._holidays = frozenset(found)
        except Exception as error:  # noqa: BLE001 - bank_holidays keeps the last list
            errors.append(error)
        upkeep_bus = self._settings.bus_upkeep if self._build_timetable else None
        if upkeep_bus is not None:
            self._building = upkeep_bus.unusable() is not None
            try:
                upkeep_bus.refresh_timetable(limits=self._timetable_limits)
                self._timetable_tried = True
            except TimetableBusy:
                # Building elsewhere: shown as building until it is saved, or tried here next hour.
                self._timetable_tried = False
                log.info("dashboard upkeep: another bus timetable build is running; trying again next hour")
            except Exception as error:  # noqa: BLE001 - the saved timetable is kept
                errors.append(error)
                self._timetable_tried = True
            finally:
                self._building = False
            if "buses" in self._pollers:
                self._pollers["buses"].wake()  # show the new timetable, or why there is none
        if self._history is not None:
            try:
                self._history.sweep(now)
                self._history.purge(now, self._options.history_days)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        if errors:
            raise errors[0]

    # Running

    def in_quiet_window(self) -> bool:
        """Whether one of the quiet windows is on now, bank holidays counted."""
        local = self._clock().replace(tzinfo=None)
        return any(window.contains(local, self._holidays) for window in self._options.quiet_windows)

    def quiet(self) -> bool:
        """In a quiet window with no browser seen lately: don't fetch."""
        return self.in_quiet_window() and not self._hub.active(CLIENT_GRACE)

    def heartbeat(self) -> None:
        """Every minute: the snapshot again with the time now, and whether a
        quiet window is on, for the wall-tablet page to dim in."""
        self._hub.refresh(quiet=self.in_quiet_window())

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
        if self._settings.bus_disruptions is not None:
            add("bus-disruptions", self.poll_bus_disruptions, BUS_DISRUPTIONS_SECONDS)
        if self._settings.train_incidents is not None:
            add("train-incidents", self.poll_train_incidents, TRAIN_INCIDENTS_SECONDS)
        add("upkeep", self.upkeep, UPKEEP_SECONDS, quiet=False)
        add("heartbeat", self.heartbeat, HEARTBEAT_SECONDS, quiet=False)
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
