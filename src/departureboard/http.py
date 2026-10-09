"""Thin HTTPS adapter. Kept dependency-free for the Pi Zero."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

# Some gateways (the Rail Data Marketplace among them) refuse urllib's default
# "Python-urllib/x.y" agent with a 403, so always identify ourselves.
USER_AGENT = "inky-departures/0.1 (e-paper departure board)"


def get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def get_bytes(url: str, max_bytes: int, timeout: float = 15.0) -> bytes:
    """A response body, refused if it is larger than `max_bytes`."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"Response larger than {max_bytes} bytes")
    return body
