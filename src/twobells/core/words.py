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


# Before a clock time to set off; on its own, the plain look's heading over its bus leave times.
LEAVE_BY = "Leave by"


def leave_by(time: str) -> str:
    """When to set off, as a clock time: "Leave by 17:40"."""
    return f"{LEAVE_BY} {time}"


def leave_in(minutes: int) -> str:
    """When to set off, as a countdown: "Leave in 4 min", or "Leave now" in the last minute."""
    return "Leave now" if minutes <= 0 else f"Leave in {minutes} min"
