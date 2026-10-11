"""JSON state persisted between timer runs."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..core import files
from ..core.model import Departure, Status
from ..sources.bods import Vehicle

log = logging.getLogger(__name__)

# What the board keeps in memory beside the departures and never saves: where
# each shown train and bus is (board/positions.py). It changes every few
# seconds, so writing it would wear the SD card, and it is useless once old.
MEMORY_ONLY = "strips"


def departure_to_dict(dep: Departure) -> dict:
    data = asdict(dep)
    data["status"] = dep.status.value
    return data


def departure_from_dict(data: dict) -> Departure:
    """A saved departure. Raises TypeError or ValueError if it isn't one,
    such as a time that isn't HH:MM (Departure checks its own fields)."""
    return Departure(**{**data, "status": Status(data["status"])})


def departures_from_dicts(items: list) -> list[Departure]:
    """Saved departures, skipping any that don't read back (hand-edited, or from another version)."""
    if not isinstance(items, list):
        log.warning("Ignoring saved departures that aren't a list: %r", type(items).__name__)
        return []
    departures = []
    for item in items:
        try:
            departures.append(departure_from_dict(item))
        except (TypeError, ValueError, KeyError) as error:
            log.warning("Skipping a saved departure that doesn't read back: %r (%s)", item, error)
    return departures


def to_iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def vehicle_to_dict(vehicle: Vehicle) -> dict:
    data = asdict(vehicle)
    data["origin_aimed"] = to_iso(vehicle.origin_aimed)  # a datetime, which JSON can't hold
    return data


def vehicles_from_dicts(items: list) -> list[Vehicle]:
    """Saved bus positions, skipping any that don't read back (hand-edited, or from another version)."""
    vehicles = []
    for item in items:
        try:
            vehicles.append(Vehicle(**{**item, "origin_aimed": from_iso(item.get("origin_aimed"))}))
        except (TypeError, ValueError, AttributeError) as error:
            log.warning("Skipping a saved bus position that doesn't read back: %r (%s)", item, error)
    return vehicles


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> dict:
        """The saved state, or {} if there is none or it can't be read: a bad file
        must not stop the board, which refills it on the next fetch."""
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as error:  # bad JSON or bad UTF-8
            log.warning("Ignoring unreadable state file %s: %s", self._path, error)
            return {}
        if not isinstance(state, dict):
            log.warning("Ignoring state file %s: not a JSON object", self._path)
            return {}
        state.pop(MEMORY_ONLY, None)  # never saved; one in the file was put there by hand
        for key in ("sources", "budgets", "last_render", "vehicles"):
            if key in state and not isinstance(state[key], dict):
                log.warning("Ignoring malformed %r in state file %s", key, self._path)
                del state[key]
        # Each source's entry is read with .get() on every run.
        for name, entry in list(state.get("sources", {}).items()):
            if not isinstance(entry, dict):
                log.warning("Ignoring malformed source %r in state file %s", name, self._path)
                del state["sources"][name]
        return state

    def save(self, state: dict) -> None:
        kept = {key: value for key, value in state.items() if key != MEMORY_ONLY}
        files.write_atomic(self._path, json.dumps(kept, indent=2), 0o640)
