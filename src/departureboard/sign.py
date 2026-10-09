"""Old-school station departure board look: dot-matrix amber on black.

On the four-colour panel the amber is the panel's yellow. Meaning stays in
the words (Exp, Cancelled, Delayed, Not updating), so nothing relies on colour.

The layout works in LED dots. On the pHAT a dot is one pixel; a bigger panel
draws each dot as a block of pixels, with a dark gap between LEDs once there is room.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from .dotfont import HEIGHT as GLYPH_H
from .dotfont import TINY, TINY_HEIGHT, DotFont
from .panel import PHAT, Panel
from .render import HEIGHT, WIDTH, Palette, fit_text
from .route import RouteView
from .view import BoardView, Row, Section, Style

MARGIN = 3
_LINE = 11
_TOP = 6
# Space either side of the dotted divider between trains and buses.
_ABOVE_RULE = 8
_BELOW_RULE = 11
_COL_GAP = 5
_ORDINALS = ("1st", "2nd", "3rd", "4th")
_MAX_TRAINS = 3
_FONT = DotFont()
_SMALL = DotFont(TINY)


@dataclass(frozen=True)
class Item:
    x: float
    y: int
    text: str
    inverted: bool = False
    small: bool = False


def item_size(item: Item) -> tuple[int, int]:
    if item.small:
        return _SMALL.getlength(item.text), TINY_HEIGHT
    return _FONT.getlength(item.text), GLYPH_H


@dataclass
class SignLayout:
    items: list[Item] = field(default_factory=list)
    rules: list[int] = field(default_factory=list)

    def add(self, x: float, y: int, text: str, inverted: bool = False, small: bool = False) -> None:
        self.items.append(Item(x, y, text, inverted, small))


def _w(text: str) -> int:
    return _FONT.getlength(text)


def _right(text: str, width: int) -> float:
    return width - MARGIN - _w(text)


def _trains(layout: SignLayout, section: Section, y: int, width: int) -> int:
    time_x = MARGIN + _w(_ORDINALS[-1]) + _COL_GAP
    dest_x = time_x + _w("00:00") + _COL_GAP
    rows = section.rows[:_MAX_TRAINS]
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    for index, row in enumerate(rows):
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
        layout.add(MARGIN, y, _ORDINALS[index], inverted)
        layout.add(time_x, y, row.left, inverted)
        layout.add(dest_x, y, fit_text(row.middle, _FONT, status_x - _COL_GAP - dest_x), inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
        if section.leave and index == (section.leave_for or 0):
            # Where the real board scrolls "Calling at", say when to set off.
            layout.add(time_x, y, fit_text(section.leave, _FONT, width - MARGIN - time_x))
            y += _LINE
    return y


def _buses(layout: SignLayout, section: Section, y: int, bottom: int, width: int) -> int:
    rows = list(section.rows)
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    stop_x = MARGIN + _w("000") + _COL_GAP - 1
    stops = [r.stop for r in rows if r.stop]
    # Room for the widest stop label, and at least a "W" so one-letter stops line up.
    dest_x = stop_x + (max(map(_w, stops + ["W"])) + _COL_GAP - 1 if stops else 0)
    right_w = max(_w(r.right) for r in rows)
    leaves = [r.leave for r in rows if r.leave]
    leave_x = width - MARGIN - right_w - _COL_GAP - max(map(_w, leaves)) if leaves else None
    for row in rows:
        if y + GLYPH_H > bottom:
            break
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
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


def _footer(layout: SignLayout, view: BoardView, width: int, height: int) -> None:
    y = height - TINY_HEIGHT - 1
    if view.clock:
        # A screen shows the time centred along the bottom, like a station clock.
        layout.add((width - _w(view.clock)) // 2, height - GLYPH_H - 1, view.clock)
    else:
        layout.add(MARGIN, y, f"Updated {view.updated}", small=True)
    stale = [name for name, s in (("trains", view.trains), ("buses", view.buses)) if s.stale]
    if stale:
        message = "Not updating: " + ", ".join(stale)
        # Small, but reversed out so a stalled feed still catches the eye.
        layout.add(width - MARGIN - _SMALL.getlength(message), y, message, inverted=True, small=True)


def sign_layout(view: BoardView, width: int = WIDTH, height: int = HEIGHT) -> SignLayout:
    """Places the text on a board `width` x `height` dots."""
    layout = SignLayout()
    y = _trains(layout, view.trains, _TOP, width)
    rule = y + _ABOVE_RULE - 2  # each line already carries 2 dots of leading
    layout.rules.append(rule)
    footer_top = height - (GLYPH_H if view.clock else TINY_HEIGHT) - 3
    _buses(layout, view.buses, rule + _BELOW_RULE, footer_top, width)
    _footer(layout, view, width, height)
    return layout


def _ink(palette: Palette) -> int:
    # A mono panel has no amber; white on black keeps the board look.
    return palette.white if palette.accent == palette.black else palette.accent


def render_sign(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    ink = _ink(palette)
    width, height = panel.width // panel.scale, panel.height // panel.scale
    image = Image.new("P", (width, height), palette.black)
    draw = ImageDraw.Draw(image)
    layout = sign_layout(view, width, height)
    band_rows = sorted({i.y for i in layout.items if i.inverted and not i.small})
    bands = [(0, y - 1, width - 1, y + GLYPH_H) for y in band_rows]
    for item in (i for i in layout.items if i.inverted and i.small):
        text_w, text_h = item_size(item)
        bands.append((item.x - 2, item.y - 1, item.x + text_w + 1, item.y + text_h))
    for band in bands:
        draw.rectangle(band, fill=ink)
    for y in layout.rules:
        draw.point([(x, y) for x in range(MARGIN, width - MARGIN, 2)], fill=ink)
    for item in layout.items:
        font = _SMALL if item.small else _FONT
        font.draw(draw, (item.x, item.y), item.text, fill=palette.black if item.inverted else ink)
    image = _enlarge(image, panel, palette.black, bands)
    if palette.preview:
        _apply_preview(image, palette)
    return image


# A bus side-on, drawn on the road line: windows, wheels.
_BUS = (
    "#####################",
    "#...#...#...#...#..##",
    "#...#...#...#...#..##",
    "#####################",
    "#####################",
    "..###.........###....",
)
_LABEL_GAP = 12  # dots between the road line and a stop name


def _route_layout(view: RouteView, width: int, height: int) -> tuple[SignLayout, int, list[float]]:
    """Text items, the road line's row, and each stop's x."""
    layout = SignLayout()
    layout.add(MARGIN, _TOP, fit_text(view.title, _FONT, width - MARGIN * 2 - _w(view.due) - _COL_GAP))
    layout.add(_right(view.due, width), _TOP, view.due)
    second = _TOP + _LINE
    if view.leave:
        layout.add(MARGIN, second, view.leave)
    layout.add(_right(view.where, width), second, view.where)

    road = (second + _LINE + height - GLYPH_H - 4) // 2
    left, right = MARGIN + 4, width - MARGIN - 4
    step = (right - left) / (len(view.stops) - 1)
    xs = [left + i * step for i in range(len(view.stops))]
    room = int(step * 2) - _COL_GAP  # names alternate above and below, so each has two gaps
    for i, (name, x) in enumerate(zip(view.stops, xs)):
        text = fit_text(name, _FONT, room)
        tx = min(max(MARGIN, x - _w(text) / 2), width - MARGIN - _w(text))
        ty = road - _LABEL_GAP - GLYPH_H if i % 2 == 0 else road + _LABEL_GAP
        # Your stop, the last one, is reversed out.
        layout.add(round(tx), ty, text, inverted=i == len(view.stops) - 1)
    if view.clock:
        layout.add((width - _w(view.clock)) // 2, height - GLYPH_H - 1, view.clock)
    return layout, road, xs


def render_route_sign(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    ink = _ink(palette)
    width, height = panel.width // panel.scale, panel.height // panel.scale
    image = Image.new("P", (width, height), palette.black)
    draw = ImageDraw.Draw(image)
    layout, road, xs = _route_layout(view, width, height)
    bands = []
    for item in (i for i in layout.items if i.inverted):
        text_w, text_h = item_size(item)
        bands.append((item.x - 2, item.y - 1, item.x + text_w + 1, item.y + text_h))
    for band in bands:
        draw.rectangle(band, fill=ink)
    solid = list(bands)
    draw.point([(x, road) for x in range(int(xs[0]), int(xs[-1]) + 1, 2)], fill=ink)
    for i, x in enumerate(xs):
        r = 2 if i == len(xs) - 1 else 1
        draw.rectangle((x - r, road - r, x + r, road + r), fill=ink)
    if view.bus_at is not None:
        bx = xs[0] + view.bus_at * (xs[1] - xs[0])
        x0, y0 = round(bx) - len(_BUS[0]) // 2, road - len(_BUS) - 2
        draw.point(
            [(x0 + dx, y0 + dy) for dy, row in enumerate(_BUS) for dx, dot in enumerate(row) if dot == "#"],
            fill=ink,
        )
        # Drawn solid, without LED gaps, so it reads as one shape.
        solid.append((x0, y0, x0 + len(_BUS[0]) - 1, y0 + len(_BUS) - 1))
    for item in layout.items:
        _FONT.draw(draw, (item.x, item.y), item.text, fill=palette.black if item.inverted else ink)
    image = _enlarge(image, panel, palette.black, solid)
    if palette.preview:
        _apply_preview(image, palette)
    return image


def _enlarge(image: Image.Image, panel: Panel, background: int, bands) -> Image.Image:
    """Draws each dot as a block of panel pixels, centred on the panel.

    From 3 pixels a dot there is a dark gap between LEDs, as on a real matrix
    board, except in reversed-out bands: gaps there would break up the dark text.
    """
    scale = panel.scale
    if scale == 1:
        return image
    solid = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    big = solid.copy()
    if scale >= 3:
        draw = ImageDraw.Draw(big)
        for x in range(scale - 1, big.width, scale):
            draw.line((x, 0, x, big.height - 1), fill=background)
        for y in range(scale - 1, big.height, scale):
            draw.line((0, y, big.width - 1, y), fill=background)
        for x0, y0, x1, y1 in bands:
            box = tuple(int(v * scale) for v in (x0, y0, x1 + 1, y1 + 1))
            big.paste(solid.crop(box), box[:2])
    canvas = Image.new("P", panel.size, background)
    canvas.paste(big, ((panel.width - big.width) // 2, (panel.height - big.height) // 2))
    return canvas


def _apply_preview(image: Image.Image, palette: Palette) -> None:
    from .render import _PREVIEW_ACCENTS

    colours = [(0, 0, 0)] * 256
    colours[palette.white] = (255, 255, 255)
    colours[palette.black] = (0, 0, 0)
    colours[palette.accent] = _PREVIEW_ACCENTS[palette.preview]
    image.putpalette([v for rgb in colours for v in rgb])
