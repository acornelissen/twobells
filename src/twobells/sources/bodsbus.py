"""The board's bus source the way bustimes.org does it: the BODS timetable for
your stops (built by `twobells timetable`), adjusted by live BODS bus
positions (see livebus).

Until there is a usable timetable (none yet, your stops changed, or not
rebuilt for four weeks), TransportAPI fills in if its keys are set. One
service in it ending doesn't make it unusable: the rest still run, and it
is built again (timetable.due). Live positions failing
only costs the delays: the timetabled times are still shown.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import cast

from ..core.errors import safe_message
from ..core.model import Departure, Strip, strip_key
from ..core.net import get_bytes
from ..timetable import build as timetable
from ..timetable.model import Journey, Schedule, StopPosition
from ..timetable.routesetup import DEFAULT_LIMITS, DocumentLimits
from . import holidays, lastbus, livebus
from .bods import DEFAULT_BASE_URL, BodsVehicleSource, HttpGetBytes, Vehicle
from .livebus import LiveDeparture, LiveJourney

log = logging.getLogger(__name__)

# The timetable is rebuilt weekly; one this old means rebuilding has failed for
# weeks, and operators may have changed times since.
MAX_AGE = timedelta(days=28)
# Live positions are asked for in a box around the stops the schedule places
# buses by, this much (in degrees, about a kilometre) wider on each side. They
# include the way into stands near your stops, and the whole of each journey
# whose bus turns round at your stop (timetable/build.py), so inbound buses are
# seen all the way in.
_MARGIN = 0.01
# A box bigger than this, in square kilometres, is drawn round the stops of
# the 20 calls before yours only (all but Schedule.turning_only), as before
# those turning journeys were placed whole: BODS sends every bus in the box,
# its trackers' last positions too, and over a city that comes to more than
# an answer may be (bods.MAX_RESPONSE_BYTES), losing every check's live
# positions. In October 2026 a box of Glossop's (16 by 28 km, 450 km²) over
# Manchester answered with 1.1 MB, and its own with 0.2 MB.
_MAX_BOX_KM2 = 500
_KM_PER_DEGREE = 111.32


def unusable(schedule: Schedule | None, stops: Mapping[str, str], now: datetime) -> str | None:
    """Why the schedule can't be used for departures, or None if it can.
    `now` is board-local time without a zone, as the schedule's `built` is."""
    if schedule is None:
        return "there is no bus timetable yet"
    if dict(schedule.stops) != dict(stops):
        return "the bus timetable is for other stops"
    if now - schedule.built > MAX_AGE:
        return "the bus timetable is over four weeks old"
    return None


def _box(schedule: Schedule) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) around the positioned stops, or round those
    of the 20 calls before yours if that is too big; None if there are none."""
    box = _around(schedule.positions.values())
    if box is None or _km2(box) <= _MAX_BOX_KM2:
        return box
    return _around([position for atco, position in schedule.positions.items() if atco not in schedule.turning_only])


def _around(positions: Collection[StopPosition]) -> tuple[float, float, float, float] | None:
    if not positions:
        return None
    lats = [p.lat for p in positions]
    lons = [p.lon for p in positions]
    return (min(lons) - _MARGIN, min(lats) - _MARGIN, max(lons) + _MARGIN, max(lats) + _MARGIN)


def _km2(box: tuple[float, float, float, float]) -> float:
    west, south, east, north = box
    across = (east - west) * _KM_PER_DEGREE * math.cos(math.radians((south + north) / 2))
    return across * (north - south) * _KM_PER_DEGREE


@dataclass(frozen=True)
class BusBoard:
    """Bus departures with what they came from, for the dashboard."""

    departures: list[LiveDeparture]
    schedule: Schedule
    vehicles_ok: bool  # False when live positions failed and only timetabled times show
    note: str | None  # why it fell short, as BodsBusSource.note says it; None if it didn't
    # Every journey a live bus was placed on, rows or not (see livebus.live_board).
    journeys: list[LiveJourney] = field(default_factory=list)
    # Every live bus on your lines around your stops, placed or not, for the area map.
    vehicles: list[Vehicle] = field(default_factory=list)


class BodsBusSource:
    """Departures from your stops by timetable and live positions, with an
    optional fallback (TransportAPI) for when there is no usable timetable."""

    name = "bus"

    def __init__(
        self,
        folder: Path | str,
        stops: Mapping[str, str],
        api_key: str,
        tz: tzinfo,
        operators: list[str] | None = None,
        rows: int = 2,
        directions: list[str] | None = None,
        fallback=None,
        base_url: str = DEFAULT_BASE_URL,
        http_get: HttpGetBytes = get_bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not stops:
            raise ValueError("At least one stop is required")
        self._folder = Path(folder)
        self._stops = dict(stops)
        self._api_key = api_key
        self._operators = list(operators or ())
        self._rows = rows
        self._directions = directions
        self._fallback = fallback
        self._base_url = base_url
        self._http_get = http_get
        self._tz = tz
        self._clock = clock or (lambda: datetime.now(tz))
        self._loaded: tuple[tuple[int, int] | None, Schedule | None] = (None, None)
        # How the last fetch fell short, for the settings site; None if it didn't.
        self.note: str | None = None
        # The last fetch's live board, so a screen's row strips can use it
        # rather than ask again; None after a failure or the fallback.
        self.board: BusBoard | None = None
        # The last buses' day groups for yesterday and today (lastbus.day_groups),
        # with the timetable file and the day they were worked out for: once a
        # day, not on every look. They hold none of the timetable.
        self._last_days: tuple[tuple[int, int] | None, date, dict[date, lastbus.DayGroups]] | None = None

    def _schedule(self) -> Schedule | None:
        """The saved timetable, read again only when the file changes (the board
        may run for days while the timetable is rebuilt beside it)."""
        try:
            info = (self._folder / timetable.FILE_NAME).stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if stamp != self._loaded[0]:
            self._loaded = (stamp, timetable.load(self._folder) if stamp else None)
        return self._loaded[1]

    def let_go(self) -> None:
        """Hold nothing of the timetable until it is asked for again: neither
        what was read nor the last live board, which holds it too."""
        self._loaded = (None, None)
        self.board = None

    def refresh_timetable(self, force: bool = False, limits: DocumentLimits = DEFAULT_LIMITS) -> Schedule | None:
        """Build the timetable again if it is due (see timetable.due) or `force`d,
        from the operators' BODS timetables, reading as much of each as
        `limits` (the install's) allow. Slow: run it apart from the board.
        TimetableBusy, building nothing, if another build is running."""
        if not self._operators:
            raise ValueError("No operators to read timetables for")
        now = self._clock().replace(tzinfo=None)
        self._loaded = (None, None)  # not held beside the build; read again next time
        schedule = timetable.build(self._folder, self._stops, self._operators, now, self._api_key, force=force,
                                   limits=limits)
        self._loaded = (None, None)  # read it again next time
        return schedule

    def unusable(self) -> str | None:
        """Why the timetable can't be used right now, or None."""
        return unusable(self._schedule(), self._stops, self._clock().replace(tzinfo=None))

    def idle_note(self) -> str | None:
        """Why buses aren't being checked while the fallback waits for its own
        windows, for the settings site; None when that isn't the reason."""
        reason = self.unusable() if self._fallback is not None else None
        if reason is None:
            return None
        return f"{reason[0].upper()}{reason[1:]}; TransportAPI stands in only at its own checking times"

    @property
    def falling_back(self) -> bool:
        """True while departures come from the fallback instead of the timetable."""
        return self._fallback is not None and self.unusable() is not None

    @property
    def cost(self) -> int:
        """Hits of the fallback's daily allowance the next fetch will use."""
        return self._fallback.cost if self._fallback is not None and self.falling_back else 0

    def _vehicles(self, schedule: Schedule) -> tuple[list[Vehicle], str | None]:
        """Live buses around the timetable's stops, and a note if asking failed."""
        box = _box(schedule)
        if box is None:
            return [], None
        lines = {journey.line for journey in schedule.journeys}
        source = BodsVehicleSource(
            self._api_key, box, lines, self._base_url, self._http_get, now=self._clock
        )
        try:
            return source.fetch(), None
        except Exception as error:  # noqa: BLE001 - without positions, timetabled times still show
            log.warning("bus: live positions failed, showing timetabled times: %s", safe_message(error))
            return [], f"Timetabled times only; live positions failed: {safe_message(error)}"

    def _board(self, schedule: Schedule, now: datetime, rows: int) -> BusBoard:
        names = _holiday_names(self._folder, now.date())

        def runs_on(journey: Journey, day: date) -> bool:
            return timetable.runs_on(journey.calendar, day, names)

        vehicles, note = self._vehicles(schedule)
        found, placed = livebus.live_board(schedule, now, vehicles, runs_on, rows, self._directions)
        return BusBoard(found, schedule, note is None, note, placed, list(vehicles))

    def fetch(self) -> list[Departure]:
        now = self._clock()
        schedule = self._schedule()
        reason = unusable(schedule, self._stops, now.replace(tzinfo=None))
        self.note = None
        self.board = None
        if reason is not None:
            if self._fallback is None:
                raise RuntimeError(f"{reason}; run twobells timetable")
            log.warning("bus: %s, using TransportAPI", reason)
            self.note = f"From TransportAPI: {reason}"
            return self._fallback.fetch()
        # unusable() gives a reason when there is no timetable, so there is one here.
        board = self._board(cast(Schedule, schedule), now, self._rows)
        self.note = board.note
        self.board = board
        return [live.departure for live in board.departures]

    def live_strips(self, reuse: bool = False) -> dict[tuple[str, str, str, str], Strip]:
        """Where each row's bus is, for a screen, by core.model.strip_key: the
        dashboard row's words and strip. With `reuse`, from the last fetch's
        live board (none after the fallback) instead of asking BODS again.
        Buses without a live position have none; a bus followed in has
        words, and no bus on its strip yet. Raises as fetch_live does."""
        board = self.board if reuse else self.fetch_live()
        if board is None:
            return {}
        positions = board.schedule.positions
        now = self._clock().astimezone(self._tz).replace(tzinfo=None)
        found = {}
        for live in board.departures:
            strip = livebus.row_strip(live, positions, now)
            if strip is not None:
                found[strip_key(live.departure)] = strip
        return found

    def stop_names(self) -> dict[str, str]:
        """Your stops' names by label, from the timetable, as NaPTAN gives
        them: for a wide screen to call each row's stop by (the board cleans
        them, and tells apart stops that share one). A stop the timetable has
        no position for has none, and there are none while it can't be used:
        those rows show their labels, as they do from TransportAPI."""
        schedule = self._schedule()
        if schedule is None or unusable(schedule, self._stops, self._clock().replace(tzinfo=None)) is not None:
            return {}
        positions = schedule.positions
        return {label: positions[atco].name for label, atco in schedule.stops.items() if atco in positions}

    def last_buses(self, now: datetime | None = None) -> list[lastbus.LastBus]:
        """Each line and destination's last bus today from your stops, as
        lastbus.last_buses gives them, at `now` (aware, or board-local
        without a zone as the board keeps time; the clock if not given). Only
        the timetable's: none while it can't be used, as from TransportAPI.
        The day's groups are worked out once a day, or when the timetable
        changes, so a screen can ask every second."""
        now = now or self._clock()
        local = now.astimezone(self._tz).replace(tzinfo=None) if now.tzinfo else now
        schedule = self._schedule()
        if schedule is None or unusable(schedule, self._stops, local) is not None:
            return []
        today = local.date()
        cached = self._last_days
        if cached is None or cached[0] != self._loaded[0] or cached[1] != today:
            names = _holiday_names(self._folder, today)
            days = (today - timedelta(days=1), today)
            groups = {day: lastbus.day_groups(schedule, day, names, self._directions) for day in days}
            self._last_days = cached = (self._loaded[0], today, groups)
        return lastbus.last_buses(cached[2], local)

    def fetch_live(self, rows: int | None = None) -> BusBoard:
        """The next `rows` buses (the board's number if not given) with their
        journeys and live buses, from the timetable only: there is no fallback,
        and this leaves `note` to fetch. Raises RuntimeError saying why if the
        timetable can't be used."""
        now = self._clock()
        schedule = self._schedule()
        reason = unusable(schedule, self._stops, now.replace(tzinfo=None))
        if reason is not None:
            raise RuntimeError(reason)
        # As in fetch: no reason means there is a timetable.
        return self._board(cast(Schedule, schedule), now, self._rows if rows is None else rows)


def _holiday_names(folder: Path, today: date) -> dict[date, frozenset[str]]:
    """TransXChange bank holiday names for yesterday, today and tomorrow (the
    days livebus looks at), from the cached gov.uk list. Timetables say for
    themselves what runs on each."""
    events = holidays.read_events(folder)
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    return {day: holidays.holiday_names(day, events) for day in days}
