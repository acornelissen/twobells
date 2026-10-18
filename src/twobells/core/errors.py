"""Errors as one safe line: no keys, no query strings, no control characters.

Also the two ways a key is kept out of text: redact_keys, for anything that
looks like one by its name, and scrub_secrets, for the values known; and
ConfigError, what settings that don't load raise, wherever they are read."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import overload

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
