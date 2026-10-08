"""JSON state persisted between timer runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .model import Departure, Status


def departure_to_dict(dep: Departure) -> dict:
    data = asdict(dep)
    data["status"] = dep.status.value
    return data


def departure_from_dict(data: dict) -> Departure:
    return Departure(**{**data, "status": Status(data["status"])})


def to_iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> dict:
        try:
            return json.loads(self._path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, state: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, self._path)
