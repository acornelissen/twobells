"""The server-drawn pages' icons: the dashboard's own shapes, hidden from screen readers."""

import re

import pytest

from departureboard.web.icons import SHAPES, icon
from tests.paths import ROOT

ICONS_JS = ROOT / "src" / "departureboard" / "dashboard_static" / "icons.js"


def _shapes_in_icons_js():
    """icons.js's SHAPES, by name: each a list of (tag, {attribute: value})."""
    found = {}
    for name, body in re.findall(r'^  "([a-z-]+)": \[(.*)\],$', ICONS_JS.read_text(encoding="utf-8"), re.M):
        found[name] = [(tag, dict(re.findall(r'(\w+): "([^"]*)"', attrs)))
                       for tag, attrs in re.findall(r'\["(\w+)", \{([^}]*)\}\]', body)]
    return found


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_each_shape_is_the_dashboards(name):
    assert list(SHAPES[name]) == _shapes_in_icons_js()[name]


def test_an_icon_is_hidden_svg_in_the_text_colour():
    drawn = icon("check")
    assert drawn.startswith('<svg class="icon icon-check" viewBox="0 0 24 24" aria-hidden="true" focusable="false"')
    assert 'stroke="currentColor"' in drawn and '<path d="M20 6 9 17l-5-5"/>' in drawn and drawn.endswith("</svg>")
    with pytest.raises(KeyError):
        icon("no-such-icon")
