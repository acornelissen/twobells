"""JSON state persisted between timer runs."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from ..core import files
from ..core.bounded import finite
from ..core.model import Departure, Status
from ..sources.bods import Vehicle

log = logging.getLogger(__name__)

# What the board keeps in memory beside the departures and never saves: where
# each shown train and bus is (board/positions.py). It changes every few
# seconds, so writing it would wear the SD card, and it is useless once old.
MEMORY_ONLY = "strips"


# What a screen's speaker says of a train beyond its row (Departure). Saved
# only when there is something to say, so an e-paper board's state, which
# never has it, is as it always was, and a version from before reads it.
SPOKEN = ("calling_stops", "skipped_stops", "coaches", "reason")
# Those saved as JSON lists, which read back as tuples.
_STOP_LISTS = ("calling_stops", "skipped_stops")


def departure_to_dict(dep: Departure) -> dict:
    data = asdict(dep)
    data["status"] = dep.status.value
    for name in SPOKEN:
        if not data[name]:
            del data[name]
    return data


def departure_from_dict(data: dict) -> Departure:
    """A saved departure. Raises TypeError or ValueError if it isn't one,
    such as a time that isn't HH:MM (Departure checks its own fields).
    One saved before the speaker's fields were has none."""
    if not isinstance(data, dict):
        raise TypeError("a saved departure is not an object")
    lists = {name: tuple(data[name]) for name in _STOP_LISTS if isinstance(data.get(name), list)}
    return Departure(**{**data, **lists, "status": Status(data["status"])})


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


def vehicle_from_dict(data: dict) -> Vehicle:
    """A saved bus position. Raises TypeError or ValueError if it isn't one,
    such as a position off the map or not a finite number (JSON reads 10**400
    as an int too big for a float). Vehicle doesn't check its own fields, as
    parse_siri_vm checks what it reads, so they are checked here."""
    if not isinstance(data, dict):
        raise TypeError("a saved bus position is not an object")
    lat, lon, bearing = finite(data.get("lat")), finite(data.get("lon")), finite(data.get("bearing"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("position is not on the map")
    if bearing is None and data.get("bearing") is not None:
        raise ValueError("bearing is not a number")
    numbers = {"lat": lat, "lon": lon, "bearing": bearing}
    vehicle = Vehicle(**{**data, **numbers, "origin_aimed": from_iso(data.get("origin_aimed"))})
    for field in fields(vehicle):
        if field.name not in (*numbers, "origin_aimed") and not isinstance(getattr(vehicle, field.name), str):
            raise TypeError(f"{field.name} is not text")
    return vehicle


def vehicles_from_dicts(items: list) -> list[Vehicle]:
    """Saved bus positions, skipping any that don't read back (hand-edited, or from another version)."""
    if not isinstance(items, list):
        log.warning("Ignoring saved bus positions that aren't a list: %r", type(items).__name__)
        return []
    vehicles = []
    for item in items:
        try:
            vehicles.append(vehicle_from_dict(item))
        except (TypeError, ValueError) as error:
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
