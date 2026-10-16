"""The Delay Repay watch: when trains reached the filter station, so the
Performance page can list those that got there 15 or more, or 30 or more,
minutes late, the thresholds train companies pay Delay Repay from.

Each train poll (Collector.poll_trains, so inside the dashboard's own hours:
never in a quiet window nobody is using it in) hands its board to
ArrivalWatch.poll:
  1. Each train on the board that calls at the filter station is recorded
     with its booked time there and Darwin's estimate (History.watch), from
     the rich board's calling point. That costs nothing extra.
  2. A train that has left the board is followed until it gets there, only
     while it is due there within FOLLOW_AHEAD or overdue: with a Service
     Details key, by asking for that train's details (one ask per train);
     otherwise by one ask for the filter station's arrival board of trains
     from your station (sources/arrivalboard.py), whichever trains are due.
  3. History.settle decides each arrival: Darwin's reported time, or else
     its last estimate once that has passed (history.py has the rules).
A key that is refused turns following off for six hours; anything else
that fails is tried again on the next poll. With no filter station there
is nothing to watch, and nothing is asked.

Rail replacement buses are left out: Darwin's times for them are rough, and
the train company's Delay Repay rules for a bus vary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from ..core.model import Status
from ..sources.arrivalboard import Arrival, ArrivalBoard, ArrivalsRefused
from ..sources.darwin import _CLOCK, CallingPoint, DetailsSource, DetailsUnavailable, TrainBoard
from .history import Following, History, Report, Watched
from .snapshot import clock_at, train_route

BANDS = (30, 15)  # minutes late at the filter station, most first
LEAST = BANDS[-1]
OFF_FOR = 6 * 3600.0  # seconds without following after a refused key


def band(minutes_late: int) -> int | None:
    """The Delay Repay band a train that got there `minutes_late` late is
    in, 30 or 15, or None under 15 minutes."""
    return next((least for least in BANDS if minutes_late >= least), None)


def _clock_or_word(value: str | None, booked: datetime) -> datetime | None:
    """A time Darwin gave as HH:MM, or "On time" (the booked time), near
    `booked`; None for anything else ("Delayed", "No report", nothing)."""
    if value == "On time":
        return booked
    if value is not None and _CLOCK.fullmatch(value):
        return clock_at(value, booked)
    return None


def _report(booked: datetime, expected: str | None, actual: str | None, cancelled: bool) -> Report:
    if cancelled or "Cancelled" in (expected, actual):
        return Report(None, None, True)
    return Report(_clock_or_word(expected, booked), _clock_or_word(actual, booked), False)


def watched(board: TrainBoard, fetched: datetime, route_prefix: str, station: str) -> list[Watched]:
    """The trains on `board` that call at `station`, from their calling
    points there (naive local times; `fetched` is when the board came)."""
    found = []
    for service in board.services:
        dep = service.departure
        if dep.replacement_bus:
            continue
        point = next((p for p in service.calling_points if p.crs == station), None)
        if point is None:
            continue
        leaves = clock_at(dep.scheduled, fetched)
        booked = clock_at(point.scheduled, leaves)
        report = _report(booked, point.expected, point.actual,
                         point.cancelled or dep.status is Status.CANCELLED)
        found.append(Watched(
            route=train_route(route_prefix, dep.destination),
            scheduled=dep.scheduled,
            day=leaves.date(),
            station=station,
            station_name=point.name,
            identity=service.service_id,
            operator=dep.service,
            destination=dep.destination,
            booked=booked,
            expected=report.expected,
            actual=report.actual,
            cancelled=report.cancelled,
        ))
    return found


def _point_there(points: tuple[CallingPoint, ...], station: str) -> CallingPoint | None:
    return next((p for p in points if p.crs == station), None)


def _on_board(train: Following, arrivals: list[Arrival]) -> Arrival | None:
    """The train on the arrival board: by its service ID, else by when it left your station."""
    if train.identity:
        found = next((a for a in arrivals if a.service_id == train.identity), None)
        if found is not None:
            return found
    return next((a for a in arrivals if a.departs == train.scheduled), None)


class ArrivalWatch:
    """Records and follows the trains that call at `station` (the filter
    station) from `crs` (yours), in `history`."""

    def __init__(self, history: History, crs: str, station: str, route_prefix: str, *,
                 details: DetailsSource | None = None, arrivals: ArrivalBoard | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._history = history
        self._crs = crs
        self._station = station
        self._prefix = route_prefix
        self._details = details
        self._arrivals = arrivals
        self._monotonic = monotonic
        self._off_until: float | None = None

    def poll(self, board: TrainBoard, now: datetime) -> list[Exception]:
        """Take in a train poll's board at `now` (aware or naive local), and
        follow the trains that are due. The errors from following, which
        doesn't stop for them; anything the history raises is raised."""
        local = now.replace(tzinfo=None)
        rows = watched(board, local, self._prefix, self._station)
        self._history.watch(rows, local)
        self._history.settle(local)
        here = {(row.day, row.route, row.scheduled) for row in rows}
        due = [train for train in self._history.following(local)
               if (train.day, train.route, train.scheduled) not in here]
        errors = self._follow(due, local) if due else []
        self._history.settle(local)
        return errors

    def _off(self) -> bool:
        return self._off_until is not None and self._monotonic() < self._off_until

    def _refused(self, error: Exception) -> list[Exception]:
        self._off_until = self._monotonic() + OFF_FOR
        return [error]

    def _follow(self, due: list[Following], now: datetime) -> list[Exception]:
        if self._off():
            return []
        if self._details is not None:
            return self._by_details(self._details, due, now)
        if self._arrivals is not None:
            return self._by_arrivals(self._arrivals, due, now)
        return []

    def _by_details(self, client: DetailsSource, due: list[Following], now: datetime) -> list[Exception]:
        """One ask per train due, for its journey; a train Darwin no longer
        knows is left to History.settle."""
        errors: list[Exception] = []
        for train in due:
            if not train.identity:
                continue
            try:
                found = client.details(train.identity)
            except DetailsUnavailable as error:
                return errors + self._refused(error)
            except Exception as error:  # noqa: BLE001 - this train is tried again next poll
                errors.append(error)
                continue
            point = _point_there(found.subsequent, self._station)
            if point is not None:
                report = _report(train.booked, point.expected, point.actual, point.cancelled)
                self._history.report(train, report, now)
        return errors

    def _by_arrivals(self, source: ArrivalBoard, due: list[Following], now: datetime) -> list[Exception]:
        """One ask for the arrival board, for every train due; one no longer
        on it is left to History.settle."""
        try:
            board = source.arrivals(self._station, self._crs)
        except ArrivalsRefused as error:
            return self._refused(error)
        except Exception as error:  # noqa: BLE001 - asked again next poll
            return [error]
        for train in due:
            found = _on_board(train, board)
            if found is not None:
                self._history.report(train, _report(train.booked, found.eta, found.ata, found.cancelled), now)
        return []
