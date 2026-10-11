"""Where each shown train and bus is, for a screen's rows. E-paper never asks.

Buses: the bus source's live board (sources/bodsbus.py), as the dashboard's
rows say it ("3 stops away", "At your stop"). The board's own departures fetch
brings one; between those it is asked for again every BUS_EVERY, while the bus
windows are open.

Trains: Darwin's service details for the first few trains, asked for by the
board itself, during the train windows, with the dashboard's rules
(sources.darwin.TrainDetails: each train at most once a minute, six hours off
after a refused key). After any failure nothing more is asked for a minute,
as the screen ticks every second.

Everything here is kept in memory only, under state["strips"], which is never
saved (board/state.py): it changes every few seconds, and the SD card would
wear. A strip older than MAX_AGE is dropped rather than shown stale; a failure
gives no strip, never a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..core.errors import safe_message
from ..core.model import Strip, strip_key
from ..core.polling import PollPolicy
from ..sources.darwin import DETAILS_TTL, DetailsUnavailable, ServiceDetails, ServiceGone, TrainDetails, train_position
from .state import MEMORY_ONLY

log = logging.getLogger(__name__)

BUS_EVERY = timedelta(seconds=30)
MAX_AGE = timedelta(minutes=2)
# After a failed ask, how long before the trains are asked about again.
PAUSE = timedelta(seconds=DETAILS_TTL)

StripKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class Positions:
    """A screen's row strips. `details` asks where the trains are: None
    without a Service Details key, and then only buses have strips."""

    details: TrainDetails | None = None


class _Config(Protocol):
    @property
    def source(self) -> object: ...

    @property
    def policy(self) -> PollPolicy: ...


def _entry(state: dict, name: str) -> dict:
    return state.setdefault(MEMORY_ONLY, {}).setdefault(name, {})


def poll(positions: Positions, sources: Iterable[_Config], state: dict, now: datetime, fetched: set[str]) -> None:
    """Asks where the buses and trains are, if due. `fetched` names the
    sources whose departures were fetched this tick."""
    for config in sources:
        name = getattr(config.source, "name", "")
        if hasattr(config.source, "live_strips"):
            _poll_buses(config, state, now, name in fetched)
        if positions.details is not None and hasattr(config.source, "services"):
            _poll_trains(positions.details, config, state, now)


def _poll_buses(config: _Config, state: dict, now: datetime, fetched: bool) -> None:
    entry = _entry(state, "bus")
    last = entry.get("last_attempt")
    due = last is None or not timedelta(0) <= now - last < BUS_EVERY
    if not fetched and not (due and config.policy.is_active(now)):
        return
    entry["last_attempt"] = now
    try:
        # Just fetched: its live board is fresh, so BODS isn't asked again.
        found = config.source.live_strips(reuse=fetched)  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - no strip rather than an old one
        message = safe_message(error)
        if entry.get("last_error") != message:  # once, not every 30 s
            log.warning("bus positions failed: %s", message)
        entry.update(last_error=message, items={}, at=None)
        return
    entry.update(last_error=None, at=now, items={
        key: strip for key, strip in dict(found).items() if isinstance(strip, Strip)
    })


def _poll_trains(details: TrainDetails, config: _Config, state: dict, now: datetime) -> None:
    if not config.policy.is_active(now):
        return
    entry = _entry(state, "train")
    paused = entry.get("paused_until")
    if paused is not None and now < paused <= now + PAUSE:
        return
    # Only the trains still to leave, as the board shows: not ones a failed
    # fetch has left in the list.
    hhmm = now.strftime("%H:%M")
    shown = [s for s in config.source.services if not s.departure.is_gone(hhmm)]  # type: ignore[attr-defined]
    errors = [error for _, error in details.refresh(shown) if error is not None]
    for error in errors:
        if isinstance(error, DetailsUnavailable):
            log.warning("train positions off for 6 hours: %s", safe_message(error))
        elif not isinstance(error, ServiceGone):
            log.warning("train position skipped: %s", safe_message(error))
    # A train Darwin has forgotten stays on the board until its next fetch:
    # waiting a minute keeps it from being asked about every second.
    entry["paused_until"] = now + PAUSE if errors else None


def strips(positions: Positions, sources: Iterable[_Config], state: dict, now: datetime) -> dict[StripKey, Strip]:
    """The strips to draw now, by strip_key: none older than MAX_AGE."""
    found: dict[StripKey, Strip] = {}
    bus = state.get(MEMORY_ONLY, {}).get("bus", {})
    at = bus.get("at")
    if at is not None and timedelta(0) <= now - at <= MAX_AGE:
        found.update(bus.get("items", {}))
    if positions.details is None:
        return found
    known = positions.details.known(max_age=MAX_AGE.total_seconds())
    for config in sources:
        for service in getattr(config.source, "services", ()):
            strip = _train_strip(known.get(service.service_id or ""), now)
            if strip is not None:
                found[strip_key(service.departure)] = strip
    return found


def _train_strip(details: ServiceDetails | None, now: datetime) -> Strip | None:
    """Where the train is, in the dashboard row's strip and a few of its
    words; None if that isn't known or a row can't draw it."""
    if details is None:
        return None
    try:
        found = train_position(details, now)
        if found is None:
            return None
        # The short words: a row has room for about 20 characters.
        return Strip(found.short, len(found.strip_stops), found.yours, found.train_at)
    except Exception as error:  # noqa: BLE001 - a position is never worth the board
        log.debug("train position not drawn: %s", safe_message(error))
        return None
