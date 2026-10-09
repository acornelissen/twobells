"""Thin JSON-over-HTTPS adapter. Kept dependency-free for the Pi Zero."""

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
