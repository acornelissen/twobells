"""Station announcements: what a UK station's public address says about a
train, in its classic words.

"The next train to depart from platform 1 will be the 08:12 Northern
service to Manchester Piccadilly. Calling at Dinting and Manchester
Piccadilly. This train is formed of 3 coaches."

"We are sorry to announce that the 08:12 Northern service to Manchester
Piccadilly has been cancelled. This is due to a shortage of train crew."

The dashboard speaks them in the browser, and the Pi's speaker can say the
same words. Everything comes in as plain values (a Train), so nothing here
knows Darwin; the stops and the length are said with core/calling.py's parts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .calling import formed_of, not_calling_at, stop_list

# The kinds of announcement, in the order they are said for one train.
PLATFORM = "platform"
CANCELLED = "cancelled"
DELAYED = "delayed"
NEXT = "next"  # the next train to depart
DEPARTS = "departs"  # a later one

# Darwin's reasons are whole sentences: "This train has been cancelled
# because of a shortage of train crew", "This train has been delayed by a
# broken down train". The cause is what a station says after "This is due to".
_CAUSE = re.compile(
    r"This (?:train|service) has been (?:partially )?(?:cancelled|delayed)"
    r" (?:because of|due to|owing to|by) (?P<cause>.+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Train:
    """What an announcement says of a train: its booked time (HH:MM), where
    it goes, the operator's name ("" when not known), Darwin's route words
    ("via Guide Bridge"), its platform, whether it is a rail replacement bus
    or a ferry listed with the trains, the stops it calls at after this
    station, those it no longer calls at, and its number of coaches."""

    time: str
    destination: str
    operator: str = ""
    via: str | None = None
    platform: str | None = None
    replacement_bus: bool = False
    ferry: bool = False
    calling: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    coaches: int | None = None


def _is_train(train: Train) -> bool:
    """A train proper: a replacement bus or a ferry has no platform or coaches."""
    return not (train.replacement_bus or train.ferry)


def service(train: Train, late: bool = False, kind: bool = True) -> str:
    """"the 08:12 Northern service to Manchester Piccadilly via Guide Bridge",
    "the delayed 08:12 ...", "the 08:12 Northern replacement bus service to
    ...", "the 07:12 Wightlink ferry service to ..."; without an operator,
    "the 08:12 service to ...". With `kind` False a replacement bus or ferry
    is a plain "service", where the sentence says which it is."""
    noun = "service"
    if kind and train.replacement_bus:
        noun = "replacement bus service"
    elif kind and train.ferry:
        noun = "ferry service"
    words = ["the", "delayed" if late else "", train.time, train.operator, noun,
             f"to {train.destination}", train.via or ""]
    return " ".join(word for word in words if word)


def _platform(train: Train) -> str | None:
    # A replacement bus or a ferry leaves from outside, never from a platform.
    return train.platform if _is_train(train) else None


def _sentence(text: str) -> str:
    return text[0].upper() + text[1:]


def reason_sentence(reason: str | None) -> str | None:
    """A cancellation or delay reason as a station says it. Darwin's own
    sentence, "This train has been cancelled because of a shortage of train
    crew", becomes "This is due to a shortage of train crew."; any other
    reason is said as Darwin gives it, as a sentence of its own. None for none."""
    if reason is None or not reason.strip():
        return None
    text = reason.strip().rstrip(".").strip()
    if not text:
        return None
    found = _CAUSE.fullmatch(text)
    if found:
        return f"This is due to {found['cause']}."
    return f"{_sentence(text)}."


def _calling(train: Train, say_ferry: bool = True) -> list[str]:
    """"Calling at Dinting and Glossop.", the stops no longer called at, then
    the train's length, or that it is a replacement bus or (`say_ferry`) a
    ferry, which have no coaches to count."""
    said = []
    if train.calling:
        stops = stop_list(train.calling)
        said.append(f"Calling at {stops} only." if len(train.calling) == 1 else f"Calling at {stops}.")
        said.append(not_calling_at(train.skipped))
    said.append(formed_of(train.coaches if _is_train(train) else None, train.replacement_bus,
                          train.ferry and say_ferry))
    return [part for part in said if part]


def next_train(train: Train, late: bool = False) -> str:
    """"The next train to depart from platform 1 will be the 08:12 Northern
    service to Manchester Piccadilly. Calling at ... This train is formed of
    3 coaches." Without a platform, "The next train to depart will be ...".
    A replacement bus is "The next service to depart will be ... This is a
    replacement bus.", and a ferry "The next ferry to depart will be ..."."""
    noun = "service" if train.replacement_bus else "ferry" if train.ferry else "train"
    platform = _platform(train)
    where = f" from platform {platform}" if platform else ""
    first = f"The next {noun} to depart{where} will be {service(train, late, kind=False)}."
    return " ".join([first, *_calling(train, say_ferry=False)])


def departs(train: Train, late: bool = False) -> str:
    """A train after the next: "The 08:42 Northern service to Manchester
    Piccadilly will depart from platform 1. Calling at ...", or "... is due
    to depart." without a platform; a replacement bus or a ferry says which
    it is after its stops."""
    platform = _platform(train)
    where = f"will depart from platform {platform}" if platform else "is due to depart"
    first = f"{_sentence(service(train, late, kind=False))} {where}."
    return " ".join([first, *_calling(train)])


def cancelled(train: Train, reason: str | None = None) -> str:
    """"We are sorry to announce that the 08:12 Northern service to
    Manchester Piccadilly has been cancelled.", then the reason."""
    first = f"We are sorry to announce that {service(train)} has been cancelled."
    return " ".join(part for part in (first, reason_sentence(reason)) if part)


def delayed(train: Train, minutes: int | None = None, reason: str | None = None) -> str:
    """"We are sorry to announce that the 08:12 ... is delayed by
    approximately 10 minutes.", or "... is delayed." when how long isn't
    known, then the reason."""
    if minutes is not None and minutes > 0:
        how_long = f" by approximately {minutes} {'minute' if minutes == 1 else 'minutes'}"
    else:
        how_long = ""
    first = f"We are sorry to announce that {service(train)} is delayed{how_long}."
    return " ".join(part for part in (first, reason_sentence(reason)) if part)


def platform_alteration(train: Train) -> str | None:
    """"Platform alteration: the 08:12 ... will now depart from platform 2.";
    None without a platform, and for a replacement bus or a ferry."""
    platform = _platform(train)
    if not platform:
        return None
    return f"Platform alteration: {service(train)} will now depart from platform {platform}."


def announcements(train: Train, *, is_cancelled: bool = False, late: bool = False, minutes_late: int | None = None,
                  platform_changed: bool = False, cancel_reason: str | None = None,
                  delay_reason: str | None = None, is_next: bool = False) -> list[tuple[str, str]]:
    """Every announcement for one train, as (kind, words), in the order to
    say them. A cancelled train has its apology alone. Any other has its
    platform alteration when its platform has changed, its apology when it
    is late (by a known number of minutes, or by how much isn't known), then
    the next-train announcement when `is_next`, or a later train's."""
    if is_cancelled:
        return [(CANCELLED, cancelled(train, cancel_reason))]
    said: list[tuple[str, str | None]] = []
    if platform_changed:
        said.append((PLATFORM, platform_alteration(train)))
    is_late = late and (minutes_late is None or minutes_late > 0)
    if is_late:
        said.append((DELAYED, delayed(train, minutes_late, delay_reason)))
    said.append((NEXT, next_train(train, is_late)) if is_next else (DEPARTS, departs(train, is_late)))
    return [(kind, text) for kind, text in said if text]
