"""Board looks: wording plus a renderer, and the accent colour each expects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PIL.Image import Image

from .render import Palette, render
from .sign import render_sign
from .view import PLAIN, SIGN, BoardView, Wording


@dataclass(frozen=True)
class Theme:
    name: str
    wording: Wording
    render: Callable[[BoardView, Palette], Image]
    accent: str


THEMES = {
    "plain": Theme("plain", PLAIN, render, accent="red"),
    "sign": Theme("sign", SIGN, render_sign, accent="yellow"),
}


def theme_named(name: str) -> Theme:
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"Unknown theme {name!r}; choose from {', '.join(sorted(THEMES))}") from None
