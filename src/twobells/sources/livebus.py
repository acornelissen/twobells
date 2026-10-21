"""Live bus departures: the timetable for your stops, adjusted by where the buses are.

Works the way bustimes.org does. Each live BODS vehicle is matched to the
timetabled journey it says it is running, placed on the road between that
journey's stops, and its delay is when it was there against when the timetable
has it there. A bus that is late at that point is shown that late at your stop;
one that is early is shown on time, since buses wait at timing points.

Following a bus in. Most departures from a stop where journeys start can't
be live until their bus turns round there: until then it is on the journey
before. Where the timetable gives blocks (Journey.block), the journey before
a departure in its block, ending where the departure starts, brings in its
bus. If that bus is placed (matched to that journey, or sending the same
block as its BlockRef), the departure is live too, marked inbound ("Bus on
its way, due in 6 min"): expected when that bus gets in plus TURNAROUND,
never before its time; once in, on time, or, that gone, any minute. Only
a journey whose first stop is yours is followed in, and the same bus goes
with it to its later stops of yours until it is placed on the journey:
expected at each the timetable's run time from the first stop after it
is expected to leave there, so never before its time either. Its words
count down to the bus getting to your stop. A bus still in INBOUND_WINDOW
after the time at the first stop, still on the journey before, has laid
over or gone out of service: it is let go at every stop, and the departure
goes as one with no bus seen would. The block is a hint, never trusted blindly: the
journey before must be the same operator's, on the same service day, the
one just before it in the block, and end at the departure's first stop, or
within NEAR_M of it, up to INBOUND_WINDOW before it leaves. If that bus
isn't seen, the departure stays timetable only.

Without a block to follow (none given, or one that fails those checks), the
bus is guessed by line, as bustimes.org does without blocks. Each bus placed
on a journey goes to the earliest departure it can make: of the same line
and operator, from your stop where that journey ends (or within NEAR_M of
it), on the same service day, leaving up to INBOUND_WINDOW after the
journey is due in, with no bus of its own and no block to follow. A bus
whose block goes on to another journey is that journey's, and a departure
first in its block has its bus come out of the depot: neither is guessed. A
departure with exactly one bus coming to it this way is live, marked
inbound, the same as one followed by its block, with the same words: the
rider sees no difference, only Inbound.followed tells them apart. With two,
or two departures leaving at once, it is too much of a guess and none is
made. The timetable keeps journeys that end near your stop without calling
at it (inbound-only ones), and every stop of those that turn round there,
for both.

Everything here is pure: the caller passes the time, the timetable and the
vehicles, so nothing reads the clock or the network.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from itertools import pairwise

from ..core.model import Departure, Status, Strip
from ..timetable.model import NEAR_M, Call, Journey, Schedule, StopPosition, metres
from ..timetable.roads import shape_key
from ..timetable.route import MAX_HEADING_OFF, MAX_OFF_ROUTE_M, MAX_OVERSHOOT_M, where
from .bods import Vehicle, recorded_time

RunsOn = Callable[[Journey, date], bool]

# A timetabled departure stays this long after its time, in case the bus is just late.
GRACE = timedelta(minutes=1)
# Times show without a date, and model.minutes_between reads them as within 12 hours.
HORIZON = timedelta(hours=12)
# A bus this far (in stops) past your stop has left; nearer, it may still be at the pole.
PASSED_MARGIN = 0.1
# A bus coming in arrives at most this long before the departure it works.
INBOUND_WINDOW = timedelta(minutes=30)
# The least time a bus followed in takes to turn round: a late one leaves this long after it gets in.
TURNAROUND = timedelta(minutes=2)
_DAY = 24 * 60


def service_minute(when: datetime, service_day: date, tz: tzinfo) -> int:
    """Minutes after midnight of service_day, by the local clock, of an aware time."""
    local = when.astimezone(tz)
    return (local.date() - service_day).days * _DAY + local.hour * 60 + local.minute


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def match(vehicle: Vehicle, journeys: Iterable[Journey], service_day: date, tz: tzinfo) -> Journey | None:
    """The journey a vehicle is running, out of those running on service_day.

    Matched on line, first stop and timetabled time from it, with the operator
    only breaking ties: Bee Network timetables and live feeds can give one
    operator different codes. Without those, on the ticket machine's journey
    code and operator. None when nothing or more than one journey fits, as a
    wrong match is worse than none.
    """
    candidates = [j for j in journeys if j.calls and _same(j.line, vehicle.line)]
    if vehicle.origin_ref and vehicle.origin_aimed is not None:
        minute = service_minute(vehicle.origin_aimed, service_day, tz)
        found = [j for j in candidates if j.origin.atco == vehicle.origin_ref and j.origin.minute == minute]
        if len(found) > 1:
            found = [j for j in found if _same(j.operator, vehicle.operator)]
    elif vehicle.journey_ref:
        found = [
            j for j in candidates if _same(j.operator, vehicle.operator) and j.journey_code == vehicle.journey_ref
        ]
    else:
        return None
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Progress:
    """Where a matched bus is on its journey and how it compares to the timetable."""

    at: float  # along the journey's calls: 2.5 is halfway from the third call to the fourth
    delay: float  # seconds behind the timetable at that point; negative is early


def _minute_at(calls: tuple[Call, ...], at: float) -> float:
    """Timetabled minute at a point along the calls, assuming steady progress between stops."""
    i = min(int(at), len(calls) - 1)
    if i == len(calls) - 1:
        return float(calls[i].minute)
    return calls[i].minute + (at - i) * (calls[i + 1].minute - calls[i].minute)


# Placing a bus. Each pair of consecutive calls whose stops have positions is a
# leg: the road the timetable saved between the two stops (timetable/roads.py),
# or a straight line where it saved none. A bus is on a leg when it is near
# enough to the road and heading along it, as route.position_on has it for a
# strip. Of the legs it is on, the nearest wins; where others are about as near
# (a road the journey takes twice, or both ways), the one nearest the
# timetable does. Nearest first, so a bus on a parallel road, or late by just
# the time to a road further on, isn't placed there.

# A saved road is the road itself, to within a few metres, so a bus this far
# from it is somewhere else; the rest allows for a poor GPS fix, as on
# bustimes.org. A straight line is close only to a straight road, so it keeps
# route.MAX_OFF_ROUTE_M: a bus on a bending road with no saved road is left
# unplaced rather than guessed at.
ROAD_TOLERANCE_M = 300
# A leg this much further from the bus than the nearest is as likely as it.
TIE_M = 50
_M_PER_DEG = 111_320
_NO_ROADS: Mapping[str, Sequence[tuple[float, float]]] = {}


def _on_road(
    vehicle: Vehicle, road: list[tuple[float, float]], tolerance: float, first: bool, last: bool
) -> tuple[float, float] | None:
    """How far a bus is from a road ((lat, lon) points in order), in metres,
    and how far along the road the nearest point is, from 0 to 1 by distance.
    None if it is further than `tolerance`, or heading more than
    MAX_HEADING_OFF from the road's direction there. Before the start of the
    `first` road of a stretch, or past the end of the `last`, it has not got
    there yet or has gone, so only MAX_OVERSHOOT_M counts there."""
    # A flat grid in metres with the bus at (0, 0): fine over a few kilometres.
    across = _M_PER_DEG * math.cos(math.radians(vehicle.lat))
    grid = [((lon - vehicle.lon) * across, (lat - vehicle.lat) * _M_PER_DEG) for lat, lon in road]
    # The same point twice has no direction to check a heading against.
    pieces = [(a, b) for a, b in pairwise(grid) if a != b]
    final = len(pieces) - 1
    best = None  # (metres off, metres along, the road's bearing there)
    length = 0.0
    for i, ((ax, ay), (bx, by)) in enumerate(pieces):
        dx, dy = bx - ax, by - ay
        piece = math.hypot(dx, dy)
        raw = -(ax * dx + ay * dy) / (piece * piece)
        t = max(0.0, min(1.0, raw))
        off = math.hypot(ax + t * dx, ay + t * dy)
        overshoot = (first and i == 0 and raw < 0) or (last and i == final and raw > 1)
        if off <= (MAX_OVERSHOOT_M if overshoot else tolerance) and (best is None or off < best[0]):
            best = (off, length + t * piece, math.degrees(math.atan2(dx, dy)) % 360)
        length += piece
    if best is None:
        return None
    off, along, bearing = best
    if vehicle.bearing is not None and abs((vehicle.bearing - bearing + 180) % 360 - 180) > MAX_HEADING_OFF:
        return None
    return off, along / length


def _fits(
    vehicle: Vehicle,
    calls: tuple[Call, ...],
    positions: Mapping[str, StopPosition],
    shapes: Mapping[str, Sequence[tuple[float, float]]],
) -> list[tuple[float, float]]:
    """Each leg of the calls the bus is on: how far it is from the road, in
    metres, and where it is along the calls (2.5 is halfway along the third leg)."""
    fits = []
    for k, (a, b) in enumerate(pairwise(calls)):
        if a.atco not in positions or b.atco not in positions:
            continue
        saved = shapes.get(shape_key((a.atco, b.atco)), ())
        start, end = positions[a.atco], positions[b.atco]
        road = [(start.lat, start.lon), *saved, (end.lat, end.lon)]
        first = k == 0 or calls[k - 1].atco not in positions
        last = k + 2 == len(calls) or calls[k + 2].atco not in positions
        found = _on_road(vehicle, road, ROAD_TOLERANCE_M if saved else MAX_OFF_ROUTE_M, first, last)
        if found is not None:
            fits.append((found[0], k + found[1]))
    return fits


def progress(
    vehicle: Vehicle,
    journey: Journey,
    positions: Mapping[str, StopPosition],
    service_day: date,
    tz: tzinfo,
    shapes: Mapping[str, Sequence[tuple[float, float]]] = _NO_ROADS,
) -> Progress | None:
    """Place a bus on its journey and say how late it is there.

    Only the legs of the journey between stops with positions can place it,
    along `shapes`, the timetable's saved roads (see above). None if it is on
    none of them, or its report time can't be read.
    """
    recorded = recorded_time(vehicle)
    if recorded is None:
        return None
    fits = _fits(vehicle, journey.calls, positions, shapes)
    if not fits:
        return None
    seen = recorded.astimezone(tz).replace(tzinfo=None)
    midnight = datetime.combine(service_day, time())
    nearest = min(off for off, _ in fits)
    best = None
    for off, at in fits:
        if off > nearest + TIE_M:
            continue
        due = midnight + timedelta(minutes=_minute_at(journey.calls, at))
        delay = (seen - due).total_seconds()
        if at <= 0 and delay < 0:
            delay = 0.0  # waiting at the first stop: it won't leave early
        if best is None or abs(delay) < abs(best.delay):
            best = Progress(at, delay)
    return best


def heading_to(destination: str, directions: Iterable[str] | None) -> bool:
    """Whether a bus to `destination` goes one of the board's `directions`
    (any, without them): the rows and the last buses keep only those."""
    if not directions:
        return True
    return any(d.lower() in destination.lower() for d in directions)


def _late_minutes(delay: float) -> int:
    """Whole minutes late, rounding halves up; early counts as on time."""
    return math.floor(max(delay, 0.0) / 60 + 0.5)


def _live(
    vehicles: Iterable[Vehicle],
    running: dict[date, list[Journey]],
    positions: Mapping[str, StopPosition],
    tz: tzinfo,
    shapes: Mapping[str, Sequence[tuple[float, float]]] = _NO_ROADS,
) -> dict[tuple[date, int], tuple[Progress, Vehicle]]:
    """Where the bus is on each journey a vehicle could be matched to and placed
    on (along `shapes`, the saved roads), and the bus itself, keyed by
    (service day, id of the journey)."""
    live: dict[tuple[date, int], tuple[datetime, Progress, Vehicle]] = {}
    for vehicle in vehicles:
        hits = [(day, j) for day, journeys in running.items() if (j := match(vehicle, journeys, day, tz))]
        if len(hits) != 1:
            continue  # unmatched, or could be yesterday's journey or today's
        day, journey = hits[0]
        placed = progress(vehicle, journey, positions, day, tz, shapes)
        recorded = recorded_time(vehicle)  # readable whenever progress places the bus
        if placed is None or recorded is None:
            continue
        key = (day, id(journey))
        if key in live and live[key][0] >= recorded:
            continue  # two buses on one journey: keep the latest report
        live[key] = (recorded, placed, vehicle)
    return {key: (placed, vehicle) for key, (_, placed, vehicle) in live.items()}


@dataclass(frozen=True)
class LiveJourney:
    """A timetabled journey with a live bus placed on it, whatever stop it
    ends at. Journeys that end at your stops (or inbound-only ones, at a
    stand near them) never give a departure row, but their buses often go on
    to work the next journey from there (see Inbound)."""

    journey: Journey
    service_day: date
    progress: Progress
    vehicle: Vehicle


@dataclass(frozen=True)
class Inbound:
    """The bus coming in to work a journey that starts at your stop, placed
    on the journey before (see the module's notes). Its times are at the
    end of the journey before, the departure's first stop."""

    bus: LiveJourney
    # By the block: the same bus. False for a guess by line. Either makes the
    # departure live, with the same words.
    followed: bool

    @property
    def due(self) -> datetime:
        """When the bus gets to the end of its journey, by the timetable (board-local, no zone)."""
        return datetime.combine(self.bus.service_day, time()) + timedelta(minutes=self.bus.journey.calls[-1].minute)

    @property
    def late(self) -> int:
        """How late the bus is now, in whole minutes; early counts as on time."""
        return _late_minutes(self.bus.progress.delay)

    @property
    def arrives(self) -> datetime:
        """When the bus is expected at the end of its journey: due, as late as it is now."""
        return self.due + timedelta(minutes=self.late)

    @property
    def is_in(self) -> bool:
        """Whether the bus has got to the end of its journey: once there, it
        loses no more time, however long it waits."""
        return self.bus.progress.at >= len(self.bus.journey.calls) - 1 - PASSED_MARGIN


@dataclass(frozen=True)
class LiveDeparture:
    """A board row with what it was worked out from: the journey, the stop on
    it, and the bus running it if one was matched and placed, or the bus
    coming in to work it."""

    departure: Departure
    journey: Journey
    service_day: date
    call_index: int  # your stop's place in journey.calls
    leaves: datetime  # timetabled, board-local without a zone
    expected: datetime  # leaves plus any lateness, board-local without a zone
    progress: Progress | None  # None when no bus was placed on the journey
    vehicle: Vehicle | None  # the bus behind progress
    # The bus coming in to work it, for a journey starting at your stop with
    # no bus placed on it, at that stop and its later ones (see the module's notes).
    inbound: Inbound | None = None

    @property
    def live(self) -> bool:
        """Whether its bus is tracked: placed on its journey, or followed in on the one before."""
        return self.progress is not None or self.inbound is not None

    @property
    def observed(self) -> bool:
        """Whether its lateness was seen: its bus placed on its journey, or on
        the way in on the one before. Not for a bus only waiting in, whose
        lateness is just the time going by."""
        if self.progress is not None:
            return True
        return self.inbound is not None and not self.inbound.is_in

    @property
    def tracked(self) -> Vehicle | None:
        """The bus tracked for it, if it is live: its own, or the one followed in."""
        if self.progress is not None or self.inbound is None:
            return self.vehicle
        return self.inbound.bus.vehicle

    @property
    def key(self) -> str:
        """Names this departure the same way every fetch, even after the
        timetable is read again: service day, operator, line, first stop and
        time, your stop, its time (a loop can call there twice) and its label."""
        origin = self.journey.origin
        call = self.journey.calls[self.call_index]
        return "|".join(
            (
                self.service_day.isoformat(),
                self.journey.operator,
                self.journey.line,
                origin.atco,
                str(origin.minute),
                call.atco,
                str(call.minute),
                self.departure.stop or "",
            )
        )


def departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[Departure]:
    """The next `rows` buses from your stops, soonest first. See live_departures."""
    return [live.departure for live in live_departures(schedule, now, vehicles, runs_on, rows, directions)]


def live_departures(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> list[LiveDeparture]:
    """The next `rows` buses from your stops, soonest first. See live_board."""
    return live_board(schedule, now, vehicles, runs_on, rows, directions)[0]


def live_board(
    schedule: Schedule,
    now: datetime,
    vehicles: Iterable[Vehicle],
    runs_on: RunsOn,
    rows: int,
    directions: Iterable[str] | None = None,
) -> tuple[list[LiveDeparture], list[LiveJourney]]:
    """The next `rows` buses from your stops, soonest first, each with its
    journey and the bus running it or coming in to work it; and every
    journey (today's or yesterday's) a live bus was matched to and placed
    on, including ones that end at your stops and inbound-only ones, which
    give no departure row. `directions` filters only the rows.

    `now` must be timezone-aware in the board's zone, e.g.
    datetime.now(ZoneInfo("Europe/London")); its zone is used to read BODS times.
    Journeys come from today's timetable, yesterday's (for buses still out
    after midnight) and tomorrow's (for its first buses, before midnight), as
    runs_on says; tomorrow's are timetabled only. A bus placed before your stop is shown
    with its delay and stays on the board however late; one that has passed
    your stop is dropped. A departure whose bus is followed in (see the
    module's notes) is shown as late as that bus makes it, until a minute
    after then, or, once in, until INBOUND_WINDOW after its journey's time
    at the first stop. Without
    either, the timetabled time is shown as UNKNOWN until a minute after it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    tz = now.tzinfo
    local_now = now.replace(tzinfo=None)
    today = local_now.date()
    days = (today - timedelta(days=1), today, today + timedelta(days=1))
    running = {day: [j for j in schedule.journeys if j.calls and runs_on(j, day)] for day in days}
    vehicles = list(vehicles)
    # Buses are matched to yesterday's and today's journeys only: none is out on
    # tomorrow's yet, and a journey code that fits tomorrow too would make
    # today's match ambiguous.
    live = _live(vehicles, {day: running[day] for day in days[:2]}, schedule.positions, tz, schedule.shapes)
    by_id = {id(j): j for js in running.values() for j in js}
    placed = {(day, i): LiveJourney(by_id[i], day, found, bus) for (day, i), (found, bus) in live.items()}
    used = {id(bus) for _, bus in live.values()}
    follow = _Follow(
        {day: running[day] for day in days[:2]}, placed, [v for v in vehicles if id(v) not in used], schedule, tz
    )
    labels: dict[str, list[str]] = {}
    for label, atco in schedule.stops.items():
        labels.setdefault(atco, []).append(label)

    found = []
    for day, journeys in running.items():
        midnight = datetime.combine(day, time())
        for journey in journeys:
            if journey.inbound_only or not heading_to(journey.destination, directions):
                continue  # inbound-only journeys are there to find buses coming in, never as rows
            progress, vehicle = live.get((day, id(journey)), (None, None))
            starts = midnight + timedelta(minutes=journey.origin.minute)
            inbound = None
            if progress is None and journey.origin.atco in labels:
                inbound = follow.bus_in(journey, day)
                if inbound is not None and inbound.is_in and local_now > starts + INBOUND_WINDOW:
                    inbound = None  # still in long after its time: laid over or out of service
            for k, call in enumerate(journey.calls[:-1]):  # the last call is where it terminates
                leaves = midnight + timedelta(minutes=call.minute)
                if progress is not None and progress.at > k + PASSED_MARGIN:
                    continue  # the bus has been past this stop
                if progress is None and _expected(leaves, None, inbound, local_now, starts) < local_now - GRACE:
                    continue
                if leaves > local_now + HORIZON:
                    continue
                for label in labels.get(call.atco, ()):
                    found.append(
                        _departure(journey, day, k, leaves, progress, vehicle, label, inbound, local_now, starts)
                    )
    found.sort(key=lambda live: (live.expected, live.leaves, live.departure.stop, live.journey.line))
    return found[:rows], list(placed.values())


def _blocks(journeys: Iterable[Journey]) -> dict[tuple[str, str], list[Journey]]:
    """A service day's journeys that give a block, by operator and block,
    in the order they start."""
    found: dict[tuple[str, str], list[Journey]] = {}
    for journey in journeys:
        if journey.block:
            found.setdefault((journey.operator.casefold(), journey.block.casefold()), []).append(journey)
    for block in found.values():
        block.sort(key=lambda journey: journey.origin.minute)
    return found


def _ends_at(journey: Journey, stop: str, positions: Mapping[str, StopPosition]) -> bool:
    """Whether a journey ends at `stop`, or within NEAR_M of it."""
    end = journey.calls[-1].atco
    if end == stop:
        return True
    a, b = positions.get(end), positions.get(stop)
    return a is not None and b is not None and metres(a, b) <= NEAR_M


def _comes_in(before: Journey, journey: Journey, positions: Mapping[str, StopPosition]) -> bool:
    """Whether the bus on `before` can work `journey`, both on one service
    day: it ends where `journey` starts, or within NEAR_M, up to
    INBOUND_WINDOW before it leaves."""
    gap = timedelta(minutes=journey.origin.minute - before.calls[-1].minute)
    return _ends_at(before, journey.origin.atco, positions) and timedelta(0) <= gap <= INBOUND_WINDOW


class _Follow:
    """Finds the bus coming in to work a departure from its journey's first
    stop (see the module's notes): by the block, or else by a guess."""

    def __init__(
        self,
        running: Mapping[date, Sequence[Journey]],
        placed: Mapping[tuple[date, int], LiveJourney],
        spare: list[Vehicle],
        schedule: Schedule,
        tz: tzinfo,
    ) -> None:
        # The journeys running on each service day buses are placed on, and their blocks.
        self._blocks = {day: _blocks(journeys) for day, journeys in running.items()}
        self._placed = placed  # each journey a bus was matched to and placed on, by (service day, id)
        self._spare = spare  # the buses that weren't
        self._schedule = schedule
        self._tz = tz
        self._guessed = self._guesses(running)  # by (service day, id of the departure's journey)

    def _block(self, journey: Journey, day: date) -> list[Journey]:
        """The journeys of `journey`'s block on `day`, in the order they start; none without a block."""
        if not journey.block:
            return []
        return self._blocks.get(day, {}).get((journey.operator.casefold(), journey.block.casefold()), [])

    def journey_before(self, journey: Journey, day: date) -> Journey | None:
        """The journey just before `journey` in its block on `day`, if its bus
        can work `journey` next (_comes_in); None without a block, or with one
        that says otherwise."""
        block = self._block(journey, day)
        earlier = [other for other in block if other.origin.minute < journey.origin.minute]
        if not earlier:
            return None  # the first in its block
        before = earlier[-1]
        starts = (before.origin.minute, journey.origin.minute)
        if sum(other.origin.minute in starts for other in block) != 2:
            return None  # two journeys starting at once: not one bus
        return before if _comes_in(before, journey, self._schedule.positions) else None

    def bus_in(self, journey: Journey, day: date) -> Inbound | None:
        """The bus coming in to work `journey`, if it is seen: followed by
        its block, or without one to follow, guessed by line."""
        before = self.journey_before(journey, day)
        if before is None:
            return self._guessed.get((day, id(journey)))
        bus = self._placed.get((day, id(before))) or self._by_block_ref(before, day)
        return Inbound(bus, followed=True) if bus is not None else None

    def _by_block_ref(self, before: Journey, day: date) -> LiveJourney | None:
        """A bus matched to no journey, sending `before`'s block as its
        BlockRef, placed on `before`: if exactly one is. One placed more
        than INBOUND_WINDOW early is on an earlier journey on the same road."""
        found = []
        for vehicle in self._spare:
            if not (vehicle.block and _same(vehicle.block, before.block) and _same(vehicle.operator, before.operator)):
                continue
            placed = progress(vehicle, before, self._schedule.positions, day, self._tz, self._schedule.shapes)
            if placed is not None and placed.delay >= -INBOUND_WINDOW.total_seconds():
                found.append(LiveJourney(before, day, placed, vehicle))
        return found[0] if len(found) == 1 else None

    def _guesses(self, running: Mapping[date, Sequence[Journey]]) -> dict[tuple[date, int], Inbound]:
        """The bus guessed to be coming in to work each departure from your
        stop with no bus of its own and no block to follow, out of `running`:
        each placed bus goes to the earliest it can make, and a departure two
        buses go to gets neither (see the module's notes)."""
        yours = set(self._schedule.stops.values())
        going: dict[tuple[date, int], list[Inbound]] = {}
        for bus in self._placed.values():
            day = bus.service_day
            if any(other.origin.minute > bus.journey.origin.minute for other in self._block(bus.journey, day)):
                continue  # its block says which journey its bus works next
            makes = [journey for journey in running.get(day, ()) if self._may_work(bus, journey, day, yours)]
            if not makes:
                continue
            first = min(journey.origin.minute for journey in makes)
            earliest = [journey for journey in makes if journey.origin.minute == first]
            if len(earliest) == 1:  # two leaving at once: either could be its
                going.setdefault((day, id(earliest[0])), []).append(Inbound(bus, followed=False))
        return {key: found[0] for key, found in going.items() if len(found) == 1}

    def _may_work(self, bus: LiveJourney, journey: Journey, day: date, yours: Set[str]) -> bool:
        """Whether the bus placed on `bus.journey` could, by line, work
        `journey` next, a departure from your stop on the same service day."""
        return (
            journey is not bus.journey
            and not journey.inbound_only
            and journey.origin.atco in yours
            and _same(journey.line, bus.journey.line)
            and _same(journey.operator, bus.journey.operator)
            and (day, id(journey)) not in self._placed
            and _comes_in(bus.journey, journey, self._schedule.positions)
            and self.journey_before(journey, day) is None
            and not self._first_in_block(journey, day)
        )

    def _first_in_block(self, journey: Journey, day: date) -> bool:
        """Whether `journey` is the first of its block on `day`: its bus
        comes out of the depot, not off the journey before."""
        block = self._block(journey, day)
        return bool(block) and all(other.origin.minute >= journey.origin.minute for other in block)


def _expected(
    leaves: datetime, placed: Progress | None, inbound: Inbound | None, now: datetime, starts: datetime
) -> datetime:
    """When a departure is expected: as late as its bus is, or, followed in,
    TURNAROUND after its bus gets in; never before its time. A bus already
    in is no later, however long it has waited: it leaves on time, or, that
    gone, any minute `now` (board-local). `starts` is when the journey
    leaves its first stop: a bus followed in is expected at a later stop the
    timetable's run time from there after it is expected to leave."""
    if placed is not None:
        return leaves + timedelta(minutes=_late_minutes(placed.delay))
    if inbound is None:
        return leaves
    run = leaves - starts
    if inbound.is_in:
        return max(starts, now.replace(second=0, microsecond=0)) + run
    return max(starts, inbound.arrives + TURNAROUND) + run


def _departure(
    journey: Journey,
    day: date,
    k: int,
    leaves: datetime,
    placed: Progress | None,
    vehicle: Vehicle | None,
    label: str,
    inbound: Inbound | None,
    now: datetime,
    starts: datetime,
) -> LiveDeparture:
    """A board row for the k-th call of a journey, and what it came from, at
    `now` (board-local); `starts` is when the journey leaves its first stop."""
    expected = _expected(leaves, placed, inbound, now, starts)
    if placed is None and inbound is None:
        status = Status.UNKNOWN
    else:
        status = Status.LATE if expected > leaves else Status.ON_TIME
    departure = Departure(
        mode="bus",
        service=journey.line,
        destination=journey.destination,
        scheduled=leaves.strftime("%H:%M"),
        expected=expected.strftime("%H:%M") if status is Status.LATE else None,
        status=status,
        stop=label,
    )
    return LiveDeparture(departure, journey, day, k, leaves, expected, placed, vehicle, inbound)


@dataclass(frozen=True)
class Approach:
    """The road up to your stop for one departure, for a small route strip."""

    stops: tuple[StopPosition, ...]  # in calling order
    yours: int  # your stop's index in stops
    bus_at: float | None  # stops along from the first; None if not on the strip yet


def approach(live: LiveDeparture, positions: dict[str, StopPosition], before: int = 6) -> Approach | None:
    """Your stop, the positioned stops the bus calls at before it (at most
    `before` of them) and the next stop after it if positioned, with where the
    bus is along them.

    Calls without a position are left out, so the strip joins the stops either
    side of them; a bus among them is placed in proportion to the calls it has
    passed. bus_at is None without a placed bus or when the bus has not reached
    the first stop. Past the last stop it counts on in calls, so more than
    len(stops) - 1 means it has gone beyond the strip. None if your stop has no
    position.
    """
    calls = live.journey.calls
    yours = live.call_index
    if not 0 <= yours < len(calls) or calls[yours].atco not in positions:
        return None
    earlier = [i for i in range(yours) if calls[i].atco in positions]
    picked = [*earlier[max(len(earlier) - before, 0):], yours]
    if yours + 1 < len(calls) and calls[yours + 1].atco in positions:
        picked.append(yours + 1)
    stops = tuple(positions[calls[i].atco] for i in picked)
    bus_at = _along(picked, live.progress.at) if live.progress is not None else None
    return Approach(stops, picked.index(yours), bus_at)


def _along(picked: list[int], at: float) -> float | None:
    """A point along a journey's calls as stops along the picked calls."""
    if at < picked[0]:
        return None
    for n, (a, b) in enumerate(pairwise(picked)):
        if at <= b:
            return n + (at - a) / (b - a)
    return len(picked) - 1 + (at - picked[-1])


def inbound_words(live: LiveDeparture, now: datetime, short: bool = False) -> str:
    """The bus followed in for a departure, in words, "due" counting down to
    it getting to your stop. At the stop its journey starts: "Bus on its
    way, due in 6 min" at the end of the journey before, then "Bus in,
    waiting to leave". At a later stop: "Bus on its way, due in 9 min", when
    the departure is expected there, then, in at the first stop, "Bus
    waiting to start, due in 4 min". `short` for a board's row, which has
    room for twelve letters ("3 stops away") and more on a wider screen:
    "On its way, 6 min", "Bus in, waiting", "Waiting to start". `now` is board-local without a zone, as the
    departure's times are."""
    inbound = live.inbound
    if inbound is None:
        return "No live position"
    first = live.call_index == 0
    if inbound.is_in and first:
        return "Bus in, waiting" if short else "Bus in, waiting to leave"
    minutes = int(((inbound.arrives if first else live.expected) - now).total_seconds() // 60)
    if inbound.is_in:
        if short:
            return "Waiting to start"
        return f"Bus waiting to start, due in {minutes} min" if minutes >= 1 else "Bus waiting to start, due now"
    if short:
        return f"On its way, {minutes} min" if minutes >= 1 else "On its way, due"
    return f"Bus on its way, due in {minutes} min" if minutes >= 1 else "Bus on its way, due now"


def where_words(live: LiveDeparture, found: Approach | None, now: datetime) -> str:
    """Where the bus is, in words for a row and screen readers, as the route
    strip says it ("3 stops away", "At your stop"), or where the bus followed
    in is (inbound_words). `found` is its approach; `now` is board-local
    without a zone."""
    if live.progress is None:
        return inbound_words(live, now)
    if found is None:
        return "Live position known"
    names = tuple(stop.name for stop in found.stops)
    return where(names[: found.yours + 1], found.bus_at, False)


def row_strip(live: LiveDeparture, positions: dict[str, StopPosition], now: datetime) -> Strip | None:
    """Where the bus is, for a screen's row: the dashboard row's words and
    strip, or for a bus followed in, inbound_words short, with no bus on the
    strip yet. None without a placed or followed bus or your stop's position,
    and for anything a row can't draw (Strip refuses it).
    `now` is board-local without a zone."""
    found = approach(live, positions)
    if not live.live or found is None:
        return None
    if live.progress is None:
        if live.inbound is None:
            return None
        try:
            return Strip(inbound_words(live, now, short=True), len(found.stops), found.yours, None)
        except ValueError:
            return None
    last = len(found.stops) - 1
    at = found.bus_at
    if at is not None:
        if at > last + PASSED_MARGIN:
            return None  # past your stop and off the strip: its row has gone
        at = round(min(at, last), 3)  # at your stop, perhaps just past the pole
    words = where_words(live, found, now)
    if at is None:
        # Not on the strip yet: "Not yet past The Junction" is too long for a
        # row with room for "3 stops away", so count the calls to your stop instead.
        away = live.call_index - math.floor(live.progress.at + 0.02)
        words = "1 stop away" if away == 1 else f"{away} stops away"
    try:
        return Strip(words, len(found.stops), found.yours, at)
    except ValueError:
        return None
