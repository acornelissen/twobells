"""What the board has been doing: safe error messages, and the settings page's status panel."""

from __future__ import annotations

import re

MESSAGE_LIMIT = 160

# A query string can carry a key whatever its name, so a URL keeps only its path.
_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")
# key=..., api_key=..., token=..., x-apikey: ... and 'x-apikey': '...' wherever they appear.
_SECRET = re.compile(
    r"(?i)\b([\w-]*(?:key|token|secret|password)|app_id)([\"']?\s*[=:]\s*[\"']?)([^\s&\"',;}<>)\]]+)"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_message(error: BaseException | str) -> str:
    """One short line about a failure, safe to log, save and show: anything
    that looks like a key is replaced, and URLs lose their query strings."""
    text = str(error) or type(error).__name__
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    text = _QUERY.sub(r"\1?…", text)
    text = _SECRET.sub(r"\1\2REDACTED", text)
    # Cut after redacting, so the cut can't leave part of a key behind.
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1].rstrip() + "…"
    return text
