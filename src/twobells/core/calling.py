"""The "Calling at" line of a UK station board: the stops a train calls at
after this station, those it no longer calls at, and how long it is.

"Calling at: Dinting, Broadbottom, Hattersley and Manchester Piccadilly.
This train is formed of 3 coaches."

A screen scrolls it under the first train, and the dashboard shows it in the
first train's row. The parts are here on their own too, for an announcement
to say them in its own words ("calling at Dinting and Glossop")."""

from __future__ import annotations

from collections.abc import Sequence

CALLING_AT = "Calling at:"


def stop_list(names: Sequence[str], last: str = "and") -> str:
    """Names as a list in words: "Dinting", "Dinting and Glossop", "Dinting,
    Broadbottom and Glossop"; `last` joins the last two ("or")."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} {last} {names[-1]}"


def calling_at(names: Sequence[str]) -> str | None:
    """"Calling at: Dinting and Manchester Piccadilly.", or "Calling at:
    Manchester Piccadilly only." for one stop; None for none."""
    if not names:
        return None
    if len(names) == 1:
        return f"{CALLING_AT} {names[0]} only."
    return f"{CALLING_AT} {stop_list(names)}."


def not_calling_at(names: Sequence[str]) -> str | None:
    """"Does not call at Hattersley or Godley.", for stops cancelled; None for none."""
    return f"Does not call at {stop_list(names, 'or')}." if names else None


def formed_of(coaches: int | None, replacement_bus: bool = False, ferry: bool = False) -> str | None:
    """"This train is formed of 3 coaches.", or for a rail replacement bus
    "This is a replacement bus." and for a ferry "This is a ferry."; None
    when the length isn't known."""
    if replacement_bus:
        return "This is a replacement bus."
    if ferry:
        return "This is a ferry."
    if coaches is None or coaches < 1:
        return None
    return f"This train is formed of {coaches} {'coach' if coaches == 1 else 'coaches'}."


def line(calling: Sequence[str], skipped: Sequence[str] = (), coaches: int | None = None,
         replacement_bus: bool = False, ferry: bool = False) -> str | None:
    """The whole line: where it calls, where it no longer does, and how long
    it is. None when it calls nowhere: a train cancelled, or one whose stops
    aren't known, has no line."""
    first = calling_at(calling)
    if first is None:
        return None
    parts = (first, not_calling_at(skipped), formed_of(coaches, replacement_bus, ferry))
    return " ".join(part for part in parts if part)
