"""The Performance page's figures (GET /api/performance, docs/dashboard-api.md).

How each regular route, and all the trains or all the buses together, ran
over the last 7 and 28 days (today included), by the measures the rail
industry reports: Right Time, T-3, T-15 and cancellations (history.measure()).
Buses get the same, with on time meaning under the bus late threshold.
Each departure's sheet gets its route's figures too (route()). And the
Delay Repay watch: the trains that reached the filter station 15 or more
minutes late over the last 28 days (repay.py), or why it is off: no
trains, no filter station, or no Service Details key to follow them with.

Worked out from the punctuality history each time it is asked for; nothing
is kept. A span with fewer than ENOUGH_DAYS days in it says so instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import NamedTuple, Protocol

from ..core.model import iso
from .history import measure
from .repay import LEAST, band

SPANS = (7, 28)
ENOUGH_DAYS = 3  # as the history's phrases: fewer days than this say nothing
MAX_ROUTES = 50  # per mode, the busiest; a page of tables is long enough
MAX_LISTED = 100  # Delay Repay trains, the latest
_PARTS = {"train": "trains", "bus": "buses"}

Outcome = tuple[str, str, str, int | None]  # (route, day, status, delay_min), as History.outcomes


class Outcomes(Protocol):
    """History or HistoryReader."""

    def outcomes(self, mode: str, since: date, route: str | None = None) -> list[Outcome]: ...

    def arrivals(self, station: str, since: date) -> list[dict]: ...


def route_parts(mode: str, route: str) -> tuple[str, str | None]:
    """(label, stop) for a route as the history names it (snapshot.train_route,
    snapshot.bus_route): "GLO>MAN|Manchester Piccadilly" is ("Manchester
    Piccadilly", None), its rail replacement buses' "GLO>MAN bus|Manchester
    Piccadilly" is ("Manchester Piccadilly (replacement bus)", None), and a
    ferry's "PMH ferry|Ryde Pier Head" is ("Ryde Pier Head (ferry)", None);
    "A|341|Hyde Bus Station" is ("341 to Hyde Bus Station", "A"). Anything
    else is its own label."""
    if mode == "train":
        prefix, bar, destination = route.partition("|")
        if not (bar and destination):
            return route, None
        if prefix.endswith(" bus"):
            return f"{destination} (replacement bus)", None
        return (f"{destination} (ferry)" if prefix.endswith(" ferry") else destination), None
    parts = route.split("|", 2)
    if len(parts) == 3 and parts[1] and parts[2]:
        stop, line, destination = parts
        return f"{line} to {destination}", stop or None
    return route, None


def periods(outcomes: list[Outcome], today: date, late_minutes: int) -> list[dict]:
    """The measures of `outcomes` over each of SPANS days to `today`."""
    found = []
    for span in SPANS:
        since = (today - timedelta(days=span - 1)).isoformat()
        within = [(day, status, delay) for _, day, status, delay in outcomes if since <= day <= today.isoformat()]
        measures = measure(within, late_minutes)
        found.append({"span": span, "since": since, "enough": measures.days >= ENOUGH_DAYS, **measures._asdict()})
    return found


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def delay_repay(arrivals: list[dict]) -> list[dict]:
    """The trains of History.arrivals that got there LEAST or more minutes
    late, newest first, at most MAX_LISTED, each with its band."""
    found = []
    for arrival in arrivals:
        if len(found) >= MAX_LISTED:
            break
        booked, arrived = datetime.fromisoformat(arrival["booked"]), datetime.fromisoformat(arrival["arrived"])
        late = int((arrived - booked) / timedelta(minutes=1))
        if late < LEAST:
            continue
        found.append({
            "day": arrival["day"], "scheduled": arrival["scheduled"], "destination": arrival["destination"],
            "operator": arrival["operator"], "booked": _hhmm(booked), "arrived": _hhmm(arrived),
            "minutes_late": late, "band": band(late), "reported": arrival["reported"],
        })
    return found


class Setup(NamedTuple):
    """What the figures depend on in the settings: late from `late_minutes`
    by mode ("train", "bus"); for the Delay Repay list, `trains`, whether
    there are any; `station`, the filter station; `details`, whether there is
    a Service Details key, which it needs."""

    late_minutes: Mapping[str, int]
    station: str | None = None
    trains: bool = False
    details: bool = False


class Performance:
    """The figures from `source`, as of `now()` (an aware local datetime),
    with the settings `setup()` gives. It is asked on every request, so a
    save on a Pi, which doesn't start the web server again, takes effect at
    once."""

    def __init__(self, source: Outcomes, now: Callable[[], datetime], setup: Callable[[], Setup]) -> None:
        self._source = source
        self._now = now
        self._setup = setup

    def _since(self, today: date) -> date:
        return today - timedelta(days=max(SPANS) - 1)

    def report(self) -> dict:
        """The body of GET /api/performance."""
        now = self._now()
        today = now.date()
        setup = self._setup()
        body: dict = {"generated_at": iso(now)}
        for mode, part in _PARTS.items():
            late = setup.late_minutes[mode]
            outcomes = self._source.outcomes(mode, self._since(today))
            by_route: dict[str, list[Outcome]] = {}
            for outcome in outcomes:
                by_route.setdefault(outcome[0], []).append(outcome)
            busiest = sorted(by_route.items(), key=lambda item: (-len(item[1]), item[0]))[:MAX_ROUTES]
            routes = []
            for route, found in busiest:
                label, stop = route_parts(mode, route)
                routes.append({"label": label, "stop": stop, "periods": periods(found, today, late)})
            routes.sort(key=lambda row: (row["label"].casefold(), row["stop"] or ""))
            body[part] = {"late_minutes": late, "all": {"periods": periods(outcomes, today, late)}, "routes": routes}
        body["delay_repay"] = self._delay_repay(today, setup)
        return body

    def _delay_repay(self, today: date, setup: Setup) -> dict:
        since = self._since(today)
        found: dict = {"station": setup.station, "station_name": None, "since": since.isoformat(),
                       "least_minutes": LEAST, "off": None, "trains": []}
        station = setup.station
        if not setup.trains:
            found["off"] = "no_trains"
        elif station is None:
            found["off"] = "no_filter"
        elif not setup.details:
            found["off"] = "no_details_key"
        if found["off"] is not None or station is None:
            return found
        arrivals = self._source.arrivals(station, since)
        if arrivals:
            found["station_name"] = arrivals[0]["station_name"]
        found["trains"] = delay_repay(arrivals)
        return found

    def route(self, mode: str, route: str) -> dict:
        """One route's figures, for its departures' sheets."""
        today = self._now().date()
        late = self._setup().late_minutes[mode]
        label, stop = route_parts(mode, route)
        outcomes = self._source.outcomes(mode, self._since(today), route)
        return {"label": label, "stop": stop, "late_minutes": late, "periods": periods(outcomes, today, late)}
