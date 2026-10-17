"""Reading what the board saves, for the Overview's board feeds (web/overview.py).

Only the state file the board saves (never written here) and config data
are read, so it is quick and needs no network. Hand edits and older
versions can leave anything in either, so every value is checked as it is
read.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..core.polling import PollPolicy, parse_window

_SOURCES = (
    # (state entry, config section, heading, minutes before "not updating")
    ("train", "train", "Trains", 6),
    ("bus", "bus", "Buses", 30),
    ("metro", "metro", "Trams", 10),
)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _ago(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 24 * 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} h {rest} min ago" if rest else f"{hours} h ago"
    days = minutes // (24 * 60)
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def _when(moment: datetime, now: datetime) -> str:
    """Like '07:58, 3 min ago', with the day too if it wasn't today, and the year if not this one."""
    if moment.date() == now.date():
        clock = f"{moment:%H:%M}"
    elif moment.year == now.year:
        clock = f"{moment:%a} {moment.day} {moment:%b %H:%M}"
    else:
        clock = f"{moment:%a} {moment.day} {moment:%b %Y %H:%M}"
    if moment > now:
        return clock  # the clock went back since; "ago" would be nonsense
    return f"{clock}, {_ago(now - moment)}"


def _policy(section: dict, holidays: frozenset[date]) -> PollPolicy | None:
    windows = section.get("windows")
    if not isinstance(windows, list):
        return None
    try:
        return PollPolicy(tuple(parse_window(w) for w in windows), timedelta(minutes=1), holidays)
    except (TypeError, ValueError):
        return None


def _switched_off(data: dict, name: str) -> bool:
    """Whether the section says enabled = false; anything else, saving reports."""
    return _dict(data.get(name)).get("enabled") is False


def _positive(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
