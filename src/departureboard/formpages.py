"""What the two settings pages share: the Pi's (web.py, at /) and the
dashboard's (dashsettings.py, under /settings).

Each host routes its own requests, reads their bodies under its own limits
and sends its own security headers; the pieces here are the same on both.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from urllib.parse import parse_qs

TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
# The largest sign-in or settings form either page takes.
MAX_FORM_BODY = 64 * 1024
# The only kinds of file the setup wizard serves, each with its type said outright.
WIZARD_TYPES = {
    ".html": HTML,
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
# The same words whether the code was wrong, used, run out, voided or never
# issued, so a guesser learns nothing about whether there is one.
WRONG_CODE = "That code isn't right, or it has run out. Ask for a new code and try again."


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes = b""
    content_type: str = TEXT
    headers: dict[str, str] = field(default_factory=dict)  # Location, Set-Cookie


def document(title: str, body: str, stylesheet: str) -> str:
    """The HTML document around a page's content, with `stylesheet` its CSS's path."""
    return (
        '<!doctype html><html lang="en-GB"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="{stylesheet}"></head>'
        f"<body><main>{body}</main></body></html>"
    )


def parse_form(body: bytes) -> dict[str, str]:
    """An urlencoded form's fields, the last value of each. Bytes that aren't
    UTF-8 become U+FFFD; past 300 fields it is refused (ValueError)."""
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=300)
    return {key: values[-1] for key, values in parsed.items()}
