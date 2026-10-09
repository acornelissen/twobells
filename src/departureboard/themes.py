"""Board looks: wording plus a renderer, and the accent colour each expects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PIL.Image import Image

from .panel import Panel
from .render import Palette, render, render_route
from .route import RouteView
from .sign import render_route_sign, render_sign
from .view import PLAIN, SIGN, BoardView, Wording


@dataclass(frozen=True)
class Theme:
    name: str
    wording: Wording
    render: Callable[[BoardView, Palette, Panel], Image]
    accent: str
    render_route: Callable[[RouteView, Palette, Panel], Image]


THEMES = {
    "plain": Theme("plain", PLAIN, render, accent="red", render_route=render_route),
    "sign": Theme("sign", SIGN, render_sign, accent="yellow", render_route=render_route_sign),
}


def theme_named(name: str) -> Theme:
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"Unknown theme {name!r}; choose from {', '.join(sorted(THEMES))}") from None
