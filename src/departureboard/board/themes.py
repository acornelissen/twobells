"""Board looks: wording plus a renderer, and the accent colour each expects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial

from PIL.Image import Image

from .panel import Panel
from .render import TEXT_SIZE, Palette, board_frame, render, render_code, render_route, render_setup, route_frame
from .route import RouteView
from .scroll import Frame
from .sign import render_route_sign, render_sign, route_sign_frame, sign_frame
from .view import PLAIN, SIGN, BoardView, CodeView, SetupView, Wording


@dataclass(frozen=True)
class Theme:
    name: str
    wording: Wording
    render: Callable[[BoardView, Palette, Panel], Image]
    accent: str
    render_route: Callable[[RouteView, Palette, Panel], Image]
    # The same pages as a screen draws them: with the text that scrolls.
    frame: Callable[[BoardView, Palette, Panel], Frame]
    route_frame: Callable[[RouteView, Palette, Panel], Frame]
    text_size: int = TEXT_SIZE  # the plain look's body text; the sign's dots are fixed
    # The settings reset code page: big plain type in either look, for reading off.
    render_code: Callable[[CodeView, Palette, Panel], Image] = render_code
    # Until the board is set up: where to do it, drawn as the code page is.
    render_setup: Callable[[SetupView, Palette, Panel], Image] = render_setup


THEMES = {
    "plain": Theme("plain", PLAIN, render, accent="red", render_route=render_route, frame=board_frame,
                   route_frame=route_frame),
    "sign": Theme("sign", SIGN, render_sign, accent="yellow", render_route=render_route_sign, frame=sign_frame,
                  route_frame=route_sign_frame, render_code=partial(render_code, dark=True),
                  render_setup=partial(render_setup, dark=True)),
}


def with_text_size(theme: Theme, text_size: int) -> Theme:
    """The plain look drawn with bigger or smaller text; the sign look as it is."""
    if theme.name != "plain" or text_size == TEXT_SIZE:
        return theme
    return replace(
        theme,
        render=partial(render, text_size=text_size),
        render_route=partial(render_route, text_size=text_size),
        frame=partial(board_frame, text_size=text_size),
        route_frame=partial(route_frame, text_size=text_size),
        text_size=text_size,
    )


def theme_named(name: str) -> Theme:
    try:
        return THEMES[name]
    except KeyError:
        raise ValueError(f"Unknown theme {name!r}; choose from {', '.join(sorted(THEMES))}") from None
