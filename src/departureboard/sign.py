"""Old-school station departure board look: dot-matrix amber on black.

On the four-colour panel the amber is the panel's yellow. Meaning stays in
the words (Exp, Cancelled, Delayed, Not updating), so nothing relies on colour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from .dotfont import HEIGHT as GLYPH_H
from .dotfont import DotFont
from .render import HEIGHT, WIDTH, Palette, fit_text
from .view import BoardView, Row, Section, Style

MARGIN = 3
_LINE = 11
_TOP = 2
_COL_GAP = 5
_ORDINALS = ("1st", "2nd", "3rd", "4th")
_MAX_TRAINS = 3
_FONT = DotFont()


@dataclass(frozen=True)
class Item:
    x: float
    y: int
    text: str
    inverted: bool = False


@dataclass
class SignLayout:
    items: list[Item] = field(default_factory=list)
    rules: list[int] = field(default_factory=list)

    def add(self, x: float, y: int, text: str, inverted: bool = False) -> None:
        self.items.append(Item(x, y, text, inverted))


def _w(text: str) -> int:
    return _FONT.getlength(text)


def _right(text: str) -> float:
    return WIDTH - MARGIN - _w(text)


def _trains(layout: SignLayout, section: Section, y: int) -> int:
    time_x = MARGIN + _w(_ORDINALS[-1]) + _COL_GAP
    dest_x = time_x + _w("00:00") + _COL_GAP
    rows = section.rows[:_MAX_TRAINS]
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    for index, row in enumerate(rows):
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right)
        layout.add(MARGIN, y, _ORDINALS[index], inverted)
        layout.add(time_x, y, row.left, inverted)
        layout.add(dest_x, y, fit_text(row.middle, _FONT, status_x - _COL_GAP - dest_x), inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
        if section.leave and index == (section.leave_for or 0):
            # Where the real board scrolls "Calling at", say when to set off.
            layout.add(time_x, y, fit_text(section.leave, _FONT, WIDTH - MARGIN - time_x))
            y += _LINE
    return y


def _buses(layout: SignLayout, section: Section, y: int, bottom: int) -> int:
    rows = list(section.rows)
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    stop_x = MARGIN + _w("000") + _COL_GAP - 1
    has_stops = any(r.stop for r in rows)
    dest_x = stop_x + (_w("W") + _COL_GAP - 1 if has_stops else 0)
    right_w = max(_w(r.right) for r in rows)
    leaves = [r.leave for r in rows if r.leave]
    leave_x = WIDTH - MARGIN - right_w - _COL_GAP - max(map(_w, leaves)) if leaves else None
    for row in rows:
        if y + GLYPH_H > bottom:
            break
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right)
        dest_end = (leave_x if leave_x is not None else status_x) - _COL_GAP
        layout.add(MARGIN, y, fit_text(row.left, _FONT, stop_x - MARGIN - 2), inverted)
        if row.stop:
            layout.add(stop_x, y, row.stop, inverted)
        layout.add(dest_x, y, fit_text(row.middle, _FONT, dest_end - dest_x), inverted)
        if row.leave and leave_x is not None:
            layout.add(leave_x, y, row.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
    return y


def _footer(layout: SignLayout, view: BoardView) -> None:
    y = HEIGHT - GLYPH_H - 1
    layout.add(MARGIN, y, f"Updated {view.updated}")
    stale = [name for name, s in (("trains", view.trains), ("buses", view.buses)) if s.stale]
    if stale:
        message = "Not updating: " + ", ".join(stale)
        layout.add(_right(message), y, message)


def sign_layout(view: BoardView) -> SignLayout:
    layout = SignLayout()
    y = _trains(layout, view.trains, _TOP)
    rule = y
    layout.rules.append(rule)
    footer_top = HEIGHT - GLYPH_H - 3
    _buses(layout, view.buses, rule + 4, footer_top)
    _footer(layout, view)
    return layout


def _ink(palette: Palette) -> int:
    # A mono panel has no amber; white on black keeps the board look.
    return palette.white if palette.accent == palette.black else palette.accent


def render_sign(view: BoardView, palette: Palette) -> Image.Image:
    ink = _ink(palette)
    image = Image.new("P", (WIDTH, HEIGHT), palette.black)
    draw = ImageDraw.Draw(image)
    layout = sign_layout(view)
    for y in sorted({i.y for i in layout.items if i.inverted}):
        draw.rectangle((0, y - 1, WIDTH - 1, y + GLYPH_H), fill=ink)
    for y in layout.rules:
        draw.point([(x, y) for x in range(MARGIN, WIDTH - MARGIN, 2)], fill=ink)
    for item in layout.items:
        _FONT.draw(draw, (item.x, item.y), item.text, fill=palette.black if item.inverted else ink)
    if palette.preview:
        _apply_preview(image, palette)
    return image


def _apply_preview(image: Image.Image, palette: Palette) -> None:
    from .render import _PREVIEW_ACCENTS

    colours = [(0, 0, 0)] * 256
    colours[palette.white] = (255, 255, 255)
    colours[palette.black] = (0, 0, 0)
    colours[palette.accent] = _PREVIEW_ACCENTS[palette.preview]
    image.putpalette([v for rgb in colours for v in rgb])
