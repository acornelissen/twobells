"""Errors as one safe line: no keys, no query strings, no control characters.

Also the two ways a key is kept out of text: redact_keys, for anything that
looks like one by its name, and scrub_secrets, for the values known; and
ConfigError, what settings that don't load raise, wherever they are read."""

from __future__ import annotations

import re
import socket
import ssl
from collections.abc import Callable, Iterable, Iterator
from typing import overload
from urllib.error import HTTPError, URLError

MESSAGE_LIMIT = 160
# The most of a message read. A server chooses what an error says (a reason
# phrase can be 64 KB), and _QUERY takes time with the square of what it
# reads. A key past this is never shown: its name comes before it.
_READ_LIMIT = 4 * MESSAGE_LIMIT

# A query string can carry a key whatever its name, so a URL keeps only its path.
_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")
# key=..., api_key=..., app_id=..., token=..., x-apikey: ... and 'x-apikey': '...'
# wherever they appear. A name is read whole from the start of its run of
# letters, digits, _ and -, and only once, so the time taken grows with the
# text, not its square: this runs on whole server answers too.
_SECRET = re.compile(
    r"(?i)(?<![\w-])((?>[\w-]+)(?:(?<=key)|(?<=token)|(?<=secret)|(?<=password)|(?<=app_id)))"
    r"([\"']?\s*[=:]\s*[\"']?)([^\s&\"',;}<>)\]]+)"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ConfigError(Exception):
    pass


class NoTimetable(RuntimeError):
    """A bus or tram timetable is wanted and there is none to use."""


# What the dashboard says publicly of a feed that failed (public_problem): a
# short sentence per kind, never the error's own words, which can carry
# hostnames and internals. Each reads after "Problem: " and takes a full stop.
KEY_REFUSED = "The service refused the key; check it in Settings > Keys"
UNREACHABLE = "Can't reach the service"
SERVICE_TROUBLE = "The service is having trouble"
NO_TIMETABLE = "There is no timetable yet; it is being built or needs building in Settings"
SOMETHING_WRONG = "Something went wrong getting this; it will try again"


def safe_message(error: BaseException | str) -> str:
    """One short line about a failure, safe to log, save and show: anything
    that looks like a key is replaced, and URLs lose their query strings."""
    text = error if isinstance(error, str) else (str(error) or type(error).__name__)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())[:_READ_LIMIT]
    text = _QUERY.sub(r"\1?…", text)
    text = redact_keys(text)
    # Cut after redacting, so the cut can't leave part of a key behind.
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1].rstrip() + "…"
    return text


@overload
def redact_keys(value: str) -> str: ...
@overload
def redact_keys(value: dict) -> dict: ...
@overload
def redact_keys(value: object) -> object: ...


def redact_keys(value):
    """`value` with anything that looks like a key, by its name, replaced
    with REDACTED in every string, a dict's keys included: the rest, such as
    the other parts of a URL's query, is left as it was."""
    return _each_string(value, lambda text: _SECRET.sub(r"\1\2REDACTED", text))


@overload
def scrub_secrets(value: str, secrets: Iterable[str]) -> str: ...
@overload
def scrub_secrets(value: dict, secrets: Iterable[str]) -> dict: ...
@overload
def scrub_secrets(value: object, secrets: Iterable[str]) -> object: ...


def scrub_secrets(value, secrets: Iterable[str]):
    """`value` with every one of `secrets` replaced with … in every string,
    a dict's keys included. The longest go first, so one that holds another
    goes whole; empty ones are passed over."""
    ordered = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    if not ordered:
        return value

    def scrub(text: str) -> str:
        for secret in ordered:
            text = text.replace(secret, "…")
        return text

    return _each_string(value, scrub)


def _each_string(value: object, change: Callable[[str], str]) -> object:
    """`value`, from JSON or as built for it, with `change` made to every string."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        return {_each_string(k, change): _each_string(v, change) for k, v in value.items()}
    if isinstance(value, list):
        return [_each_string(v, change) for v in value]
    return value


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error`, then what it was raised from or while handling, nearest first.
    A source that re-raises without the key or URL (`from None`) still has
    the original as its context."""
    seen: set[int] = set()
    found: BaseException | None = error
    while found is not None and id(found) not in seen and len(seen) < 8:
        seen.add(id(found))
        yield found
        found = found.__cause__ or found.__context__


def public_problem(error: BaseException) -> str:
    """A feed's failure as one plain sentence for anyone who can see the
    dashboard, by the kind of error and its HTTP status, never its text:
    a refused key, no way to reach the service, the service in trouble, no
    timetable, else a general one. The error itself goes in the log
    (safe_message) and, for the settings' Overview, nowhere public."""
    for found in _chain(error):
        if isinstance(found, NoTimetable):
            return NO_TIMETABLE
        if isinstance(found, HTTPError):
            if found.code in (401, 403):
                return KEY_REFUSED
            if found.code == 429 or found.code >= 500:
                return SERVICE_TROUBLE
            return SOMETHING_WRONG
        if isinstance(found, (URLError, TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError)):
            return UNREACHABLE
    return SOMETHING_WRONG
