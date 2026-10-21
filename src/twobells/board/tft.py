"""TFT look: white and yellow on black, like the newer station screens.

The plain look's layout and Inter, drawn light on black: times bold in the
accent (yellow), destinations in white, and each status in a coloured pill.
The pill only backs up its words ("On time", "Exp 07:49", "Cancelled"), so
nothing relies on colour. For screens only: e-paper has no green, and its
slow refresh suits the sign and plain looks.
"""

from __future__ import annotations

from dataclasses import replace

from PIL import Image

from ..core import words
from .panel import PHAT, Panel
from .render import (
    PAD,
    TEXT_SIZE,
    Painter,
    Palette,
    RowLayout,
    draw_text,
    drawn_length,
    load_fonts,
    paint_board,
    paint_route,
    plain_scale,
    row_layout,
)
from .route import RouteView
from .scroll import Frame
from .view import BoardView, Row, Style

# Room either side of a status inside its pill, and its corners, in pHAT pixels.
PILL_PAD = 4
_PILL_RADIUS = 3


def pill_colours(row: Row, palette: Palette) -> tuple[int, int] | None:
    """(fill, words) for a row's status pill, or None for words alone: red for
    cancelled, the accent for late, green for on time. A bus's time, "sch" or
    "No report" has no pill. Without a screen's green or red (e-paper), on
    time has no pill and cancelled takes the accent."""
    if row.style is Style.INVERTED:
        if palette.red is None:
            return palette.accent, palette.black
        return palette.red, palette.white
    if row.style is Style.ALERT:
        return palette.accent, palette.black
    if row.right == words.ON_TIME and palette.green is not None:
        return palette.green, palette.black
    return None


class TftPainter(Painter):
    def __init__(self, palette: Palette, panel: Panel, live: bool, scroll: bool = True) -> None:
        s = plain_scale(panel)
        fonts = replace(load_fonts(s), pill=PILL_PAD * s)
        super().__init__(palette, fonts, panel, TEXT_SIZE, live, dark=True, scroll=scroll)

    def banner(self, shown: str, whole: str, room: float, stamp: str, stamp_x: float,
               rain: tuple[str, float] | None = None) -> None:
        """No bar: the title in white, the clock in the accent, and a line under them."""
        s, pad = self.s, PAD * self.s
        bottom = self.header_h * s
        self.field((pad, s), shown, whole, self.f.bold, room, self.ink, self.paper, (0, bottom))
        if rain is not None:
            draw_text(self.image, (rain[1], s), rain[0], self.f.bold, self.ink)
        draw_text(self.image, (stamp_x, s), stamp, self.f.regular, self.p.accent)
        self.draw.rectangle((0, bottom, self.width - 1, bottom + s - 1), fill=self.p.accent)

    def row(self, row: Row, y: int, platform_x: float | None = None, leave_x: float | None = None,
            strip_x: float | None = None, train: bool = True, stop_room: float | None = None) -> None:
        """A row in white with its time in the accent, and its status in a
        pill. A cancelled row isn't reversed out: its red pill says it."""
        layout = row_layout(row, self.f, self.width, self.s, platform_x, leave_x, strip_x, stop_room)
        self.row_text(row, layout, y, train, self.ink, self.paper, self.p.accent, time_ink=self.p.accent)
        self.status(row, layout, y)

    def status(self, row: Row, layout: RowLayout, y: int) -> None:
        s = self.s
        colours = pill_colours(row, self.p)
        ink = self.ink
        if colours is not None:
            fill, ink = colours
            box = (layout.right_x - self.f.pill, y + s, layout.right_x + drawn_length(self.f.bold, row.right)
                   + self.f.pill - 1, y + (self.row_h - 1) * s - 1)
            self.draw.rounded_rectangle(box, radius=_PILL_RADIUS * s, fill=fill)
        draw_text(self.image, (layout.right_x, y + s), row.right, self.f.bold, ink)


def render_tft(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The board, any scrolling text at its start."""
    return tft_frame(view, palette, panel).image


def tft_frame(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The board, and on a screen the text that scrolls."""
    return paint_board(TftPainter(palette, panel, live=view.clock is not None, scroll=view.scroll), view, panel)


def render_route_tft(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The bus's position on the stops before yours, in the TFT look."""
    return route_tft_frame(view, palette, panel).image


def route_tft_frame(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The route page, and on a screen the text that scrolls."""
    return paint_route(TftPainter(palette, panel, live=view.clock is not None, scroll=view.scroll), view, panel)
