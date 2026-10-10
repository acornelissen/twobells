"""Icons for the pages drawn on the server: the dashboard's own shapes
(dashboard_static/icons.js), as inline SVG markup. Always decoration beside
words that say the same thing, so every icon is aria-hidden; its size comes
from CSS (.icon, 1em). tests/web/test_icons.py keeps the shapes the same as
icons.js's.

The shapes are Lucide's (lucide-static 0.460.0), under the ISC licence:
Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part
of Feather (MIT). All other copyright (c) for Lucide are held by Lucide
Contributors 2022. Permission to use, copy, modify, and/or distribute this
software for any purpose with or without fee is hereby granted, provided that
the above copyright notice and this permission notice appear in all copies.
THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""

from __future__ import annotations

# Each icon's shapes: (tag, attributes), in icons.js's order.
SHAPES: dict[str, tuple[tuple[str, dict[str, str]], ...]] = {
    "check": (("path", {"d": "M20 6 9 17l-5-5"}),),
    "info": (
        ("circle", {"cx": "12", "cy": "12", "r": "10"}),
        ("path", {"d": "M12 16v-4"}),
        ("path", {"d": "M12 8h.01"}),
    ),
    "triangle-alert": (
        ("path", {"d": "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"}),
        ("path", {"d": "M12 9v4"}),
        ("path", {"d": "M12 17h.01"}),
    ),
    "x": (("path", {"d": "M18 6 6 18"}), ("path", {"d": "m6 6 12 12"})),
}


def icon(name: str) -> str:
    """An icon's SVG markup by name; KeyError for one not in the set. Every
    value is a constant above, so nothing here needs escaping."""
    shapes = ""
    for tag, attrs in SHAPES[name]:
        attributes = " ".join(f'{key}="{value}"' for key, value in attrs.items())
        shapes += f"<{tag} {attributes}/>"
    return (
        f'<svg class="icon icon-{name}" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{shapes}</svg>"
    )
