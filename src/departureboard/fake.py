"""Deterministic fake sources and a simulated clock, for the emulator and demos.

Statuses are drawn from a seeded RNG keyed on the service and a six-minute
time bucket, so they evolve over a simulated morning but repeat exactly for
the same seed.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from .model import Departure, Status

_BUCKET_MINUTES = 6


class SimClock:
    def __init__(self, start: datetime, step: timedelta = timedelta(minutes=1)) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> datetime:
        return self._now

    def advance(self) -> None:
        self._now += self._step


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _upcoming(now: datetime, headway: int, offset: int, count: int) -> list[datetime]:
    """Next `count` times at `offset` past the hour, every `headway` minutes."""
    hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    times: list[datetime] = []
    moment = hour + timedelta(minutes=offset)
    while len(times) < count:
        if moment >= now:
            times.append(moment)
        moment += timedelta(minutes=headway)
    return times


def _rng(seed: int, key: str, now: datetime) -> random.Random:
    bucket = (now.hour * 60 + now.minute) // _BUCKET_MINUTES
    return random.Random(f"{seed}|{key}|{now.date()}|{bucket}")


class _FakeSource:
    def __init__(self, clock, seed: int = 1, rows: int = 3, failing_when=None) -> None:
        self._clock = clock
        self._seed = seed
        self._rows = rows
        self._failing_when = failing_when
        self.failing = False

    def fetch(self) -> list[Departure]:
        now = self._clock()
        if self.failing or (self._failing_when and self._failing_when(now)):
            raise OSError(f"fake {self.name} source is failing")
        return self._departures(now)


class FakeTrainSource(_FakeSource):
    name = "train"

    def _departures(self, now: datetime) -> list[Departure]:
        departures = []
        for scheduled in _upcoming(now, headway=30, offset=12, count=self._rows):
            std = _hhmm(scheduled)
            roll = _rng(self._seed, std, now).random()
            if roll < 0.08:
                status, expected = Status.CANCELLED, None
            elif roll < 0.14:
                status, expected = Status.LATE, None
            elif roll < 0.38:
                late = _rng(self._seed, std + "late", now).randint(2, 14)
                status, expected = Status.LATE, _hhmm(scheduled + timedelta(minutes=late))
            else:
                status, expected = Status.ON_TIME, None
            departures.append(
                Departure("train", "Northern", "Manchester Piccadilly", std, expected, status, "1")
            )
        return departures


_BUS_ROUTES = (
    # line, destination, stop, headway, offset
    ("341", "Hyde", "A", 20, 5),
    ("237", "Ashton", "A", 30, 15),
    ("61", "Buxton", "B", 60, 35),
    ("393", "Hadfield", "B", 30, 8),
)


class FakeBusSource(_FakeSource):
    name = "bus"

    def __init__(self, clock, seed: int = 1, rows: int = 2, failing_when=None) -> None:
        super().__init__(clock, seed, rows, failing_when)

    def _departures(self, now: datetime) -> list[Departure]:
        departures = []
        for line, destination, stop, headway, offset in _BUS_ROUTES:
            for scheduled in _upcoming(now, headway, offset, count=self._rows):
                aimed = _hhmm(scheduled)
                roll = _rng(self._seed, line + aimed, now).random()
                if roll < 0.3:
                    status, expected = Status.UNKNOWN, None
                elif roll < 0.55:
                    late = _rng(self._seed, line + aimed + "late", now).randint(1, 8)
                    status, expected = Status.LATE, _hhmm(scheduled + timedelta(minutes=late))
                else:
                    status, expected = Status.ON_TIME, None
                departures.append(
                    Departure("bus", line, destination, aimed, expected, status, stop=stop)
                )
        departures.sort(key=lambda d: d.departs_at)
        return departures[: self._rows]
