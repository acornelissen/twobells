"""Names for the codes in the settings, from local data only: the bundled
station list, and the bus timetable in the store for the stops. The
settings site never connects out (a Pi's can't), so these are all it has.

The timetable can be megabytes, and the pages that name stops are drawn
on every visit, so what they need of it (when it was built, what for, and
the names of your stops) is kept, until the file changes. It is read
without its journeys, which a Pi's small web server hasn't the memory to
hold.
"""

from __future__ import annotations

import threading
from functools import cache
from pathlib import Path

from ..timetable import build as timetable
from ..timetable.model import Schedule

# The longest name shown, as the wizard's (setup/api.py).
NAME_LENGTH = 80

_lock = threading.Lock()
_kept: tuple[tuple[str, int, int], Schedule | None] | None = None


@cache
def _stations() -> dict[str, str]:
    """Each station's name by its code, from the bundled list."""
    from ..setup.lookups import bundled_stations

    return {code: name for name, code, _, _ in bundled_stations()}


def station_name(code: str) -> str | None:
    """The name of the station with `code` (upper case), or None if the bundled list hasn't it."""
    try:
        return _stations().get(code)
    except (OSError, ValueError):  # the list is part of the package; the pages can do without
        return None


def station(code: str) -> str:
    """A station code with its name, like "Glossop (GLO)", or the code alone."""
    name = station_name(code)
    return f"{name} ({code})" if name else code


def schedule(folder: Path | str) -> Schedule | None:
    """The store's bus timetable, as much as the pages need: when and what it
    was built for, and the stops' positions (their names), without its
    journeys and roads. None if there is none or it can't be read. Read
    again only when the file changes."""
    global _kept
    path = Path(folder) / timetable.FILE_NAME
    try:
        found = path.stat()
    except OSError:
        return None
    key = (str(path), found.st_mtime_ns, found.st_size)
    with _lock:
        if _kept is not None and _kept[0] == key:
            return _kept[1]
    slim = timetable.load(folder, journeys=False)  # read a journey at a time, none kept
    with _lock:
        _kept = (key, slim)
    return slim


def stop_names(folder: Path | str) -> dict[str, str]:
    """{ATCO code: name} of the stops in the bus timetable; empty without one."""
    found = schedule(folder)
    if found is None:
        return {}
    return {atco: str(place.name)[:NAME_LENGTH] for atco, place in found.positions.items() if place.name}
