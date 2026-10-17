"""The Delay Repay watch: when trains reached the filter station, so the
Performance page can list those that got there 15 or more, or 30 or more,
minutes late, the thresholds train companies pay Delay Repay from.

It needs a filter station and a Service Details key (RDM_DETAILS_API_KEY):
once a train has left your station, only its service details say when it
got there. (The arrival boards are another RDM product, which the Live
Departure Board key can't ask.) Without either, it isn't built, nothing is
asked, and the Performance page says why it is off.

Each train poll (Collector.poll_trains, so inside the dashboard's own hours:
never in a quiet window nobody is using it in) hands its board to
ArrivalWatch.poll:
  1. Each train on the board that calls at the filter station is recorded
     with its booked time there and Darwin's estimate (History.watch), from
     the rich board's calling point. That costs nothing extra.
  2. A train that has left the board is followed until it gets there, only
     while it is due there within FOLLOW_AHEAD or overdue, by asking for its
     service details: at most once a minute per train. One Darwin no longer
     knows (ServiceGone) isn't asked about again.
  3. History.settle decides each arrival, after the asking: Darwin's
     reported time, or else its last estimate once that has passed. A train
     Darwin has answered for waits for Darwin's time, until Darwin no longer
     knows it or asking about it fails (History.let_go); history.py has the
     rules.
A key that is refused turns following off for six hours; anything else
that fails is tried again a minute later, and given to be logged once
while it repeats.

Rail replacement buses and ferries are left out: Darwin's times for them
are rough, and the train company's Delay Repay rules for them vary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..core.model import Status
from ..sources.darwin import _CLOCK, CallingPoint, DetailsSource, DetailsUnavailable, ServiceGone, TrainBoard
from .history import Following, History, Report, Watched
from .snapshot import clock_at, train_route

BANDS = (30, 15)  # minutes late at the filter station, most first
LEAST = BANDS[-1]
OFF_FOR = 6 * 3600.0  # seconds without following after a refused key
ASK_EVERY = timedelta(minutes=1)  # the least time between asks about one train


def band(minutes_late: int) -> int | None:
    """The Delay Repay band a train that got there `minutes_late` late is
    in, 30 or 15, or None under 15 minutes."""
    return next((least for least in BANDS if minutes_late >= least), None)


def _clock_or_word(value: str | None, booked: datetime, now: datetime) -> datetime | None:
    """A time Darwin gave as HH:MM, the one nearest `now`, when it was
    given, or "On time" (`booked`); None for anything else ("Delayed",
    "No report", nothing). Nearest `now`, not `booked`, as an estimate or
    an arrival is near when it is given: in the hour the clocks go back,
    which comes twice, an arrival an hour late is told from one on time."""
    if value == "On time":
        return booked
    if value is not None and _CLOCK.fullmatch(value):
        return clock_at(value, now)
    return None


def _report(booked: datetime, now: datetime, expected: str | None, actual: str | None, cancelled: bool) -> Report:
    if cancelled or "Cancelled" in (expected, actual):
        return Report(None, None, True)
    return Report(_clock_or_word(expected, booked, now), _clock_or_word(actual, booked, now), False)


def watched(board: TrainBoard, fetched: datetime, route_prefix: str, station: str) -> list[Watched]:
    """The trains on `board` that call at `station`, from their calling
    points there. `fetched` is when the board came, aware, in the board's
    zone, as the times are."""
    found = []
    for service in board.services:
        dep = service.departure
        if not dep.is_train:
            continue
        point = next((p for p in service.calling_points if p.crs == station), None)
        if point is None:
            continue
        leaves = clock_at(dep.scheduled, fetched)
        booked = clock_at(point.scheduled, leaves)
        report = _report(booked, fetched, point.expected, point.actual,
                         point.cancelled or dep.status is Status.CANCELLED)
        found.append(Watched(
            route=train_route(route_prefix, dep.destination),
            leaves=leaves,
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


def _said(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _point_there(points: tuple[CallingPoint, ...], station: str) -> CallingPoint | None:
    return next((p for p in points if p.crs == station), None)


class ArrivalWatch:
    """Records the trains that call at `station` (the filter station) in
    `history`, and follows them there with `details`."""

    def __init__(self, history: History, station: str, route_prefix: str, details: DetailsSource, *,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._history = history
        self._station = station
        self._prefix = route_prefix
        self._details = details
        self._monotonic = monotonic
        self._off_until: float | None = None
        # By Darwin service ID, of the trains being followed: when each was
        # last asked about (UTC), and those Darwin no longer knows.
        self._asked: dict[str, datetime] = {}
        self._gone: set[str] = set()
        # The failures given the last time anything was asked, each given once while it repeats.
        self._failing: set[str] = set()

    def poll(self, board: TrainBoard, now: datetime) -> list[Exception]:
        """Take in a train poll's board at `now` (aware, in the board's zone),
        and follow the trains that are due. The errors from following, which
        doesn't stop for them; anything the history raises is raised."""
        rows = watched(board, now, self._prefix, self._station)
        self._history.watch(rows, now)
        here = {(row.route, row.leaves.astimezone(UTC)) for row in rows}
        due = [train for train in self._history.following(now) if (train.route, train.leaves) not in here]
        errors = self._follow(due, now) if due else []
        self._history.settle(now)  # after asking: Darwin's time, not the estimate, where it has one
        return errors

    def _follow(self, due: list[Following], now: datetime) -> list[Exception]:
        """Ask about each train due, for its journey, at most once every
        ASK_EVERY; a train Darwin no longer knows, or whose ask fails, is let
        go, to be settled from its estimate, and one Darwin no longer knows
        isn't asked about again. The failures not given last time."""
        ids = {train.identity for train in due if train.identity}
        self._asked = {key: asked for key, asked in self._asked.items() if key in ids}
        self._gone &= ids
        if self._off_until is not None and self._monotonic() < self._off_until:
            return []
        asking = [(train, train.identity) for train in due
                  if train.identity and train.identity not in self._gone and self._may_ask(train.identity, now)]
        if not asking:
            return []
        errors: list[Exception] = []
        for train, identity in asking:
            self._asked[identity] = now.astimezone(UTC)
            try:
                found = self._details.details(identity)
            except DetailsUnavailable as error:
                self._off_until = self._monotonic() + OFF_FOR
                errors.append(error)
                self._history.let_go(train)
                break
            except ServiceGone:
                self._gone.add(identity)
                self._history.let_go(train)
                continue
            except Exception as error:  # noqa: BLE001 - this train is tried again in a minute
                errors.append(error)
                self._history.let_go(train)
                continue
            point = _point_there(found.subsequent, self._station)
            if point is None:  # no longer calls there, as far as Darwin says
                self._history.let_go(train)
                continue
            booked = train.booked.astimezone(now.tzinfo)  # its HH:MM are the board's
            report = _report(booked, now, point.expected, point.actual, point.cancelled)
            self._history.report(train, report, now)
        return self._news(errors)

    def _may_ask(self, identity: str, now: datetime) -> bool:
        """Whether ASK_EVERY has passed since the train was last asked about,
        or the computer's clock has been set back since."""
        asked = self._asked.get(identity)
        return asked is None or not timedelta(0) <= now.astimezone(UTC) - asked < ASK_EVERY

    def _news(self, errors: list[Exception]) -> list[Exception]:
        """`errors` less those given the last time anything was asked: a
        failure is given once while it repeats, and again after it stops."""
        said = {_said(error) for error in errors}
        news = [error for error in errors if _said(error) not in self._failing]
        self._failing = said
        return news
