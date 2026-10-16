"""The Performance page's figures (GET /api/performance, docs/dashboard-api.md).

How each regular route, and all the trains or all the buses together, ran
over the last 7 and 28 days (today included), by the measures the rail
industry reports: Right Time, T-3, T-15 and cancellations (history.measure()).
Buses get the same, with on time meaning under the bus late threshold.
Each departure's sheet gets its route's figures too (route()).

Worked out from the punctuality history each time it is asked for; nothing
is kept. A span with fewer than ENOUGH_DAYS days in it says so instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Protocol

from ..core.model import iso
from .history import measure

SPANS = (7, 28)
ENOUGH_DAYS = 3  # as the history's phrases: fewer days than this say nothing
MAX_ROUTES = 50  # per mode, the busiest; a page of tables is long enough
_PARTS = {"train": "trains", "bus": "buses"}

Outcome = tuple[str, str, str, int | None]  # (route, day, status, delay_min), as History.outcomes


class Outcomes(Protocol):
    """History or HistoryReader."""

    def outcomes(self, mode: str, since: date, route: str | None = None) -> list[Outcome]: ...


def route_parts(mode: str, route: str) -> tuple[str, str | None]:
    """(label, stop) for a route as the history names it (snapshot.train_route,
    snapshot.bus_route): "GLO>MAN|Manchester Piccadilly" is ("Manchester
    Piccadilly", None), and its rail replacement buses' "GLO>MAN bus|Manchester
    Piccadilly" is ("Manchester Piccadilly (replacement bus)", None);
    "A|341|Hyde Bus Station" is ("341 to Hyde Bus Station", "A"). Anything
    else is its own label."""
    if mode == "train":
        prefix, bar, destination = route.partition("|")
        if not (bar and destination):
            return route, None
        return (f"{destination} (replacement bus)" if prefix.endswith(" bus") else destination), None
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


class Performance:
    """The figures from `source`, as of `now()` (an aware local datetime),
    late from `late_minutes` by mode ("train", "bus")."""

    def __init__(self, source: Outcomes, now: Callable[[], datetime], late_minutes: Mapping[str, int]) -> None:
        self._source = source
        self._now = now
        self._late = dict(late_minutes)

    def _since(self, today: date) -> date:
        return today - timedelta(days=max(SPANS) - 1)

    def report(self) -> dict:
        """The body of GET /api/performance."""
        now = self._now()
        today = now.date()
        body: dict = {"generated_at": iso(now)}
        for mode, part in _PARTS.items():
            late = self._late[mode]
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
        return body

    def route(self, mode: str, route: str) -> dict:
        """One route's figures, for its departures' sheets."""
        today = self._now().date()
        late = self._late[mode]
        label, stop = route_parts(mode, route)
        outcomes = self._source.outcomes(mode, self._since(today), route)
        return {"label": label, "stop": stop, "late_minutes": late, "periods": periods(outcomes, today, late)}
