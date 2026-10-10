"""The board's bus source the way bustimes.org does it: the BODS timetable for
your stops (built by `departureboard timetable`), adjusted by live BODS bus
positions (see livebus).

Until there is a usable timetable (none yet, your stops changed, or it has
run out), TransportAPI fills in if its keys are set. Live positions failing
only costs the delays: the timetabled times are still shown.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import cast

from ..core.errors import safe_message
from ..core.model import Departure
from ..core.net import get_bytes
from ..timetable import build as timetable
from ..timetable.model import Journey, Schedule
from . import holidays, livebus
from .bods import DEFAULT_BASE_URL, BodsVehicleSource, HttpGetBytes, Vehicle
from .livebus import LiveDeparture, LiveJourney

log = logging.getLogger(__name__)

# The timetable is rebuilt weekly; one this old means rebuilding has failed for
# weeks, and operators may have changed times since.
MAX_AGE = timedelta(days=28)
# Live positions are asked for in a box around the stops the schedule places
# buses by, this much (in degrees, about a kilometre) wider on each side. They
# include the way into stands near your stops, so inbound buses are seen.
_MARGIN = 0.01


def unusable(schedule: Schedule | None, stops: Mapping[str, str], now: datetime) -> str | None:
    """Why the schedule can't be used for departures, or None if it can.
    `now` is board-local time without a zone, as the schedule's `built` is."""
    if schedule is None:
        return "there is no bus timetable yet"
    if dict(schedule.stops) != dict(stops):
        return "the bus timetable is for other stops"
    if schedule.valid_until and now.date() > schedule.valid_until:
        return "the bus timetable has run out"
    if now - schedule.built > MAX_AGE:
        return "the bus timetable is over four weeks old"
    return None


def _box(schedule: Schedule) -> tuple[float, float, float, float] | None:
    """(west, south, east, north) around the positioned stops, or None if there are none."""
    positions = schedule.positions.values()
    if not positions:
        return None
    lats = [p.lat for p in positions]
    lons = [p.lon for p in positions]
    return (min(lons) - _MARGIN, min(lats) - _MARGIN, max(lons) + _MARGIN, max(lats) + _MARGIN)


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
        self._clock = clock or (lambda: datetime.now(tz))
        self._loaded: tuple[tuple[int, int] | None, Schedule | None] = (None, None)
        # How the last fetch fell short, for the settings page; None if it didn't.
        self.note: str | None = None

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

    def refresh_timetable(self, force: bool = False) -> Schedule | None:
        """Build the timetable again if it is due (see timetable.due) or `force`d,
        from the operators' BODS timetables. Slow: run it apart from the board.
        TimetableBusy, building nothing, if another build is running."""
        if not self._operators:
            raise ValueError("No operators to read timetables for")
        now = self._clock().replace(tzinfo=None)
        schedule = timetable.build(self._folder, self._stops, self._operators, now, self._api_key, force=force)
        self._loaded = (None, None)  # read it again next time
        return schedule

    def unusable(self) -> str | None:
        """Why the timetable can't be used right now, or None."""
        return unusable(self._schedule(), self._stops, self._clock().replace(tzinfo=None))

    def idle_note(self) -> str | None:
        """Why buses aren't being checked while the fallback waits for its own
        windows, for the settings page; None when that isn't the reason."""
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
        if reason is not None:
            if self._fallback is None:
                raise RuntimeError(f"{reason}; run departureboard timetable")
            log.warning("bus: %s, using TransportAPI", reason)
            self.note = f"From TransportAPI: {reason}"
            return self._fallback.fetch()
        # unusable() gives a reason when there is no timetable, so there is one here.
        board = self._board(cast(Schedule, schedule), now, self._rows)
        self.note = board.note
        return [live.departure for live in board.departures]

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
    """TransXChange bank holiday names for yesterday and today, from the cached
    gov.uk list. Timetables say for themselves what runs on each."""
    events = holidays.read_events(folder)
    return {day: holidays.holiday_names(day, events) for day in (today - timedelta(days=1), today)}
