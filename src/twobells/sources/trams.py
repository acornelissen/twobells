"""Manchester Metrolink trams from your tram stops, from the timetable alone.

The timetable is TfGM's GTFS, kept for your stops by timetable/trams.py;
reading it costs nothing, so the board and the dashboard ask it every
minute. Its times are booked times: TfGM's live tram data is closed to new
keys, so every departure is Status.UNKNOWN, with no expected time, and the
views mark it as the timetable's ("sch" on the board, "sched." on the
dashboard), as they do a bus with no live position. A replacement bus TfGM
lists with the trams is a Departure with replacement_bus set.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, tzinfo
from pathlib import Path

from ..core.model import Departure, Status
from ..timetable import trams as timetable
from ..timetable.trams import TramCall, TramTimetable


def departure(leaves: datetime, call: TramCall) -> Departure:
    return Departure(mode="metro", service=call.line, destination=call.destination,
                     scheduled=leaves.strftime("%H:%M"), expected=None, status=Status.UNKNOWN, stop=call.stop,
                     replacement_bus=call.bus)


class TramTimetableSource:
    name = "metro"
    # What the settings site says of the last fetch beside its time; nothing here.
    note: str | None = None

    def __init__(self, folder: Path | str, stops: Mapping[str, str], zone: tzinfo, rows: int = 3,
                 directions: list[str] | None = None, clock: Callable[[], datetime] | None = None) -> None:
        self._folder = Path(folder)
        self._stops = dict(stops)
        self._zone = zone
        self._rows = rows
        self._directions = directions
        self._clock = clock or (lambda: datetime.now(zone))
        self._loaded: tuple[tuple[int, int] | None, TramTimetable | None] = (None, None)
        # The board's notices come from a source's messages; the timetable has none.
        self.messages: list[str] = []

    @property
    def stops(self) -> dict[str, str]:
        return dict(self._stops)

    def _timetable(self) -> TramTimetable | None:
        """The saved timetable, read again only when the file changes."""
        try:
            info = (self._folder / timetable.FILE_NAME).stat()
            stamp: tuple[int, int] | None = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if stamp != self._loaded[0]:
            self._loaded = (stamp, timetable.load(self._folder) if stamp else None)
        return self._loaded[1]

    def built(self) -> datetime | None:
        """When the saved timetable was built, in the board's zone; None without one."""
        found = self._timetable()
        return found.built.replace(tzinfo=self._zone) if found is not None else None

    def unusable(self) -> str | None:
        """Why the timetable can't be used right now, or None."""
        return timetable.unusable(self._timetable(), self._stops, self._clock().replace(tzinfo=None))

    def refresh_timetable(self, force: bool = False) -> TramTimetable | None:
        """Build the timetable again if it is due or `force`d (timetable/trams.py).
        Slow: run it apart from the board."""
        self._loaded = (None, None)
        return timetable.refresh(self._folder, self._stops, self._clock().replace(tzinfo=None), force=force)

    def leaving(self, rows: int | None = None) -> list[tuple[datetime, TramCall]]:
        """The next `rows` (the board's, if not given) trams, each with when it
        leaves, board-local. RuntimeError saying why if there is no usable timetable."""
        now = self._clock()
        found = self._timetable()
        reason = timetable.unusable(found, self._stops, now.replace(tzinfo=None))
        if reason is not None or found is None:
            raise RuntimeError(f"{reason}; run twobells timetable")
        local = now.replace(tzinfo=None)
        return [(leaves.replace(tzinfo=now.tzinfo), call)
                for leaves, call in found.leaving(local, self._rows if rows is None else rows, self._directions)]

    def fetch(self) -> list[Departure]:
        return [departure(leaves, call) for leaves, call in self.leaving()]
