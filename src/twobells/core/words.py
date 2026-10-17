"""The status and leave words, in one place for the dashboard and both board
looks, so a departure reads the same wherever it is shown. They are the words
of UK station boards: "On time", "Exp 17:54", "Delayed", "Cancelled", "No report"."""

from __future__ import annotations

from .model import Departure, Status

ON_TIME = "On time"
# Late, with no expected time yet.
DELAYED = "Delayed"
CANCELLED = "Cancelled"
# A train with no live report.
NO_REPORT = "No report"
# A bus with no live position, on the dashboard.
TIMETABLE_ONLY = "Timetable only"
# The same, after the bus's time on the board, where there is less room: "07:35 sch".
SCHEDULED = "sch"
# A London train or tram, live from TfL, on e-paper, where its time is when it is
# expected and there is no booked time to be on time for. A screen counts down instead (due_in).
LIVE = "Live"
# A rail replacement bus, run in a train's place: in a train's platform column
# on the board, and before its destination where there is no such column.
BUS = "Bus"
# The same in full, on the dashboard.
REPLACEMENT_BUS = "Replacement bus"
# A ferry listed with the trains: likewise in the platform column, or before
# its destination.
FERRY = "Ferry"
# After a platform that isn't the one the train was given before: "Plat 2 (changed)".
CHANGED = "(changed)"


def platform(number: str, changed: bool = False) -> str:
    """A train's platform on the board: "Plat 1", or "Plat 2 (changed)"."""
    return f"Plat {number} {CHANGED}" if changed else f"Plat {number}"


def expected(time: str) -> str:
    """Late, with the time it is expected: "Exp 17:54"."""
    return f"Exp {time}"


def expected_spoken(time: str) -> str:
    """`expected` in full, for screen readers: "Expected 17:54"."""
    return f"Expected {time}"


def status(dep: Departure) -> str:
    """A departure's status in words. A bus with no live position comes out
    as NO_REPORT here; the dashboard and the board each say that their own way."""
    if dep.status is Status.CANCELLED:
        return CANCELLED
    if dep.status is Status.ON_TIME:
        return ON_TIME
    if dep.status is Status.LATE:
        return expected(dep.expected) if dep.expected else DELAYED
    return NO_REPORT


def due_in(minutes: int) -> str:
    """How soon a live departure is due, as a countdown sign says it: "3 min", or "Due" in its last minute."""
    return "Due" if minutes < 1 else f"{minutes} min"


# Before a clock time to set off; on its own, the plain look's heading over its bus leave times.
LEAVE_BY = "Leave by"


def leave_by(time: str) -> str:
    """When to set off, as a clock time: "Leave by 17:40"."""
    return f"{LEAVE_BY} {time}"


def leave_in(minutes: int) -> str:
    """When to set off, as a countdown: "Leave in 4 min", or "Leave now" in the last minute."""
    return "Leave now" if minutes <= 0 else f"Leave in {minutes} min"
