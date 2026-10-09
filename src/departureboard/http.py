"""Thin HTTPS adapter. Kept dependency-free for the Pi Zero."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Some gateways (the Rail Data Marketplace among them) refuse urllib's default
# "Python-urllib/x.y" agent with a 403, so always identify ourselves.
USER_AGENT = "inky-departures/0.1 (e-paper departure board)"

# Headers that may follow a redirect to another host; anything else (an API
# key header such as Darwin's x-apikey) stays with the host it was meant for.
_PORTABLE_HEADERS = {"user-agent", "accept"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or ""), parts.port or _DEFAULT_PORTS.get(parts.scheme)


class SafeRedirects(HTTPRedirectHandler):
    """urllib copies every request header to wherever a redirect points. This
    refuses https to http, and drops all but harmless headers on a change of
    host or port, so a key header never reaches a host it wasn't sent to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = _origin(req.full_url), _origin(newurl)
        if old[0] == "https" and new[0] != "https":
            raise HTTPError(req.full_url, code, f"Refusing a redirect from https to {new[0]}", headers, fp)
        followed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if followed is not None and new != old:
            for name in list(followed.headers):
                if name.lower() not in _PORTABLE_HEADERS:
                    del followed.headers[name]
        return followed


_opener = build_opener(SafeRedirects)


def urlopen(request: Request, timeout: float):
    return _opener.open(request, timeout=timeout)


def get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


class TooLarge(ValueError):
    """The response was bigger than the caller allowed."""


class NotJSON(ValueError):
    """The response was empty, or not UTF-8 JSON."""


def get_json_capped(url: str, headers: dict | None, max_bytes: int, timeout: float = 15.0):
    """Like get_json, but the body is refused if it is larger than `max_bytes`,
    and a body that isn't UTF-8 JSON (or nests too deeply to read) gives one
    plain ValueError (NotJSON; TooLarge for the size). The error text never
    repeats the URL or the headers."""
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise TooLarge(f"Response larger than {max_bytes} bytes")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError too
        # `from None`: the decoder's own message can quote part of the body.
        raise NotJSON("Response is not valid JSON") from None


def get_bytes(url: str, max_bytes: int, timeout: float = 15.0) -> bytes:
    """A response body, refused if it is larger than `max_bytes`."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"Response larger than {max_bytes} bytes")
    return body
