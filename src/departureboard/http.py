"""Thin JSON-over-HTTPS adapter. Kept dependency-free for the Pi Zero."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


def get_json(url: str, headers: dict | None = None, timeout: float = 15.0) -> dict:
    request = Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)
