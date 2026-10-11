"""Old-school station departure board look: dot-matrix amber on black.

On the four-colour panel the amber is the panel's yellow. Meaning stays in
the words (Exp, Cancelled, Delayed, Not updating), so nothing relies on colour.

The layout works in LED dots. On the pHAT a dot is one pixel; a bigger panel
draws each dot as a block of pixels, with a dark gap between LEDs once there is room.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from ..core.model import Strip
from .dotfont import HEIGHT as GLYPH_H
from .dotfont import TINY, TINY_HEIGHT, DotFont
from .panel import LEAVE_COLUMN_MIN_WIDTH, PHAT, PLATFORM_COLUMN_MIN_WIDTH, STRIP_COLUMN_MIN_WIDTH, Panel
from .render import HEIGHT, WIDTH, Palette, apply_preview, fit_text, fitted_note
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


# On a screen wide enough, where each train or bus is: the words, then a
# little line of its stops.
STRIP_WIDTH = 48  # eight stops, six dots apart
# Twice the usual gap after the destination, so it and the where words don't
# run together into one name.
_WHERE_GAP = 2 * _COL_GAP
# The where words' column: at least "3 stops away", at most twenty letters
# ("Left Hadfield 07:08"), which is as long as they are meant to be.
_WHERE_LEAST = _FONT.getlength("3 stops away")
_WHERE_MOST = _FONT.getlength("Leaving Hadfield now")
# What the rest of a full row needs beside the where words: ordinal and time,
# "Manchester Piccadilly" whole, the little line, a bus's leave time and a status.
_REST_OF_ROW = (
    MARGIN + _FONT.getlength("4th") + _COL_GAP + _FONT.getlength("00:00") + _COL_GAP
    + _FONT.getlength("Manchester Piccadilly") + _WHERE_GAP + _COL_GAP + STRIP_WIDTH + _COL_GAP
    + _FONT.getlength("Leave in 00 min") + _COL_GAP + _FONT.getlength("Exp 00:00") + MARGIN
)


def where_width(width: int) -> int:
    """How wide the where words' column is on a board `width` dots across: the
    room a full row leaves, from "3 stops away" to twenty letters. It is
    worked out from the width alone, so it stays put as the words change."""
    return max(_WHERE_LEAST, min(_WHERE_MOST, width - _REST_OF_ROW))


@dataclass(frozen=True)
class StripMark:
    """A row's little line of stops, STRIP_WIDTH dots from x, on the row at y."""

    x: float
    y: int
    strip: Strip
    train: bool
    inverted: bool = False
    width: int = STRIP_WIDTH


@dataclass
class SignLayout:
    items: list[Item] = field(default_factory=list)
    rules: list[int] = field(default_factory=list)
    strips: list[StripMark] = field(default_factory=list)

    def add(self, x: float, y: int, text: str, inverted: bool = False, small: bool = False) -> None:
        self.items.append(Item(x, y, text, inverted, small))


def _w(text: str) -> int:
    return _FONT.getlength(text)


def _right(text: str, width: int) -> float:
    return width - MARGIN - _w(text)


@dataclass(frozen=True)
class _Columns:
    """Where a section's columns between destination and status start, if it
    has them, and `end`: where the first of them (or the status) starts."""

    end: float
    leave_x: float | None = None
    platform_x: float | None = None


def _train_columns(section: Section, width: int) -> _Columns:
    """From the right, each column left-aligned clear of the widest one after
    it, so it lines up."""
    rows = section.rows[:_MAX_TRAINS]
    if not rows:
        return _Columns(width - MARGIN)
    end = width - MARGIN - max(_w(r.right) for r in rows)
    leave_x = platform_x = None
    if section.leave and width >= LEAVE_COLUMN_MIN_WIDTH and (section.leave_for or 0) < len(rows):
        # One line a row: the leave words on their train's row, not under it,
        # set further off the platform so the two don't read as one phrase.
        leave_x = end = end - _COL_GAP - _w(section.leave)
        end -= _COL_GAP
    if width >= PLATFORM_COLUMN_MIN_WIDTH and any(r.platform for r in rows):
        platform_x = end = end - _COL_GAP - max(_w(r.platform) for r in rows if r.platform)
    return _Columns(end, leave_x, platform_x)


def _bus_columns(section: Section, width: int) -> _Columns:
    rows = section.rows
    if not rows:
        return _Columns(width - MARGIN)
    end = width - MARGIN - max(_w(r.right) for r in rows)
    leaves = [r.leave for r in rows if r.leave]
    if not leaves:
        return _Columns(end)
    leave_x = end - _COL_GAP - max(map(_w, leaves))
    return _Columns(leave_x, leave_x)


def _where_column(view: BoardView, trains: _Columns, buses: _Columns, width: int) -> float | None:
    """Where the where words start, with the little lines after them: one
    column down the board, clear of both sections' other columns. Only on a
    screen wide enough: a strip changes every few seconds, so e-paper, which
    redraws slowly and only on change, never shows one. It has a fixed width,
    so it stays put as the words change."""
    if view.clock is None or width < STRIP_COLUMN_MIN_WIDTH:
        return None
    return min(trains.end, buses.end) - _COL_GAP - STRIP_WIDTH - _COL_GAP - where_width(width)


def _add_strip(layout: SignLayout, row: Row, where_x: float, y: int, width: int, train: bool, inverted: bool) -> None:
    # A row without one leaves the space blank.
    if row.strip is None:
        return
    words_w = where_width(width)
    if row.strip.where:
        layout.add(where_x, y, fit_text(row.strip.where, _FONT, words_w), inverted)
    layout.strips.append(StripMark(where_x + words_w + _COL_GAP, y, row.strip, train, inverted))


def _trains(layout: SignLayout, section: Section, y: int, width: int, columns: _Columns,
            where_x: float | None) -> int:
    time_x = MARGIN + _w(_ORDINALS[-1]) + _COL_GAP
    dest_x = time_x + _w("00:00") + _COL_GAP
    rows = section.rows[:_MAX_TRAINS]
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    if not any(r.strip for r in rows):
        where_x = None  # no room kept for strips that none of them has
    leave_for = section.leave_for or 0
    for index, row in enumerate(rows):
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
        if where_x is not None:
            dest_end = where_x - _WHERE_GAP
        elif columns.leave_x is not None or columns.platform_x is not None:
            dest_end = columns.end - _COL_GAP
        else:
            dest_end = status_x - _COL_GAP
        layout.add(MARGIN, y, _ORDINALS[index], inverted)
        layout.add(time_x, y, row.left, inverted)
        layout.add(dest_x, y, fit_text(row.middle, _FONT, dest_end - dest_x), inverted)
        if where_x is not None:
            _add_strip(layout, row, where_x, y, width, True, inverted)
        if columns.platform_x is not None and row.platform:
            layout.add(columns.platform_x, y, row.platform, inverted)
        if columns.leave_x is not None and section.leave and index == leave_for:
            layout.add(columns.leave_x, y, section.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
        if section.leave and columns.leave_x is None and index == leave_for:
            # Where the real board scrolls "Calling at", say when to set off.
            layout.add(time_x, y, fit_text(section.leave, _FONT, width - MARGIN - time_x))
            y += _LINE
    return y


def _buses(layout: SignLayout, section: Section, y: int, bottom: int, width: int, columns: _Columns,
           where_x: float | None) -> int:
    rows = list(section.rows)
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    if not any(r.strip for r in rows):
        where_x = None
    stop_x = MARGIN + _w("000") + _COL_GAP - 1
    stops = [r.stop for r in rows if r.stop]
    # Room for the widest stop label, and at least a "W" so one-letter stops line up.
    dest_x = stop_x + (max(map(_w, [*stops, "W"])) + _COL_GAP - 1 if stops else 0)
    for row in rows:
        if y + GLYPH_H > bottom:
            break
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
        if where_x is not None:
            dest_end = where_x - _WHERE_GAP
        else:
            dest_end = (status_x if columns.leave_x is None else columns.leave_x) - _COL_GAP
        layout.add(MARGIN, y, fit_text(row.left, _FONT, stop_x - MARGIN - 2), inverted)
        if row.stop:
            layout.add(stop_x, y, row.stop, inverted)
        layout.add(dest_x, y, fit_text(row.middle, _FONT, dest_end - dest_x), inverted)
        if where_x is not None:
            _add_strip(layout, row, where_x, y, width, False, inverted)
        if row.leave and columns.leave_x is not None:
            layout.add(columns.leave_x, y, row.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
    return y


def _footer(layout: SignLayout, view: BoardView, width: int, height: int) -> None:
    y = height - TINY_HEIGHT - 1
    stale = [name for name, s in (("trains", view.trains), ("buses", view.buses)) if s.stale]
    badge = "Not updating: " + ", ".join(stale) if stale else None
    # Where the free part of the bottom line ends: before the badge, if there is one.
    free_end = width - MARGIN
    if badge:
        free_end -= _SMALL.getlength(badge) + 2 + _COL_GAP  # the badge's band reaches 2 dots left
    stamp = f"Updated {view.updated}"
    if view.clock and (view.notice or view.rain):
        # A screen keeps its clock on the bottom line, moved to the right, and
        # shows the notice (or rain warning) a part at a time in the room left of it.
        clock_x = free_end - _w(view.clock)
        layout.add(clock_x, height - GLYPH_H - 1, view.clock)
        layout.add(MARGIN, height - GLYPH_H - 1, fitted_note(view, clock_x - _COL_GAP - MARGIN, _w, _FONT) or "")
    elif view.clock:
        # A screen shows the time centred along the bottom, like a station clock.
        layout.add((width - _w(view.clock)) // 2, height - GLYPH_H - 1, view.clock)
    elif view.notice or view.rain:
        # The notice (or rain warning) on the left, cut short if it must be,
        # keeping "Updated" on the right: e-paper keeps its last image if the
        # board stops, and the stamp is how to tell.
        stamp_x = free_end - _SMALL.getlength(stamp)
        room = stamp_x - _COL_GAP - MARGIN
        note = fitted_note(view, room, _SMALL.getlength, _SMALL) or ""
        if _SMALL.getlength(note) <= room:  # not even an ellipsis fits on a board too narrow
            layout.add(MARGIN, y, note, small=True)
        layout.add(stamp_x, y, stamp, small=True)
    else:
        layout.add(MARGIN, y, stamp, small=True)
    if badge:
        # Small, but reversed out so a stalled feed still catches the eye.
        layout.add(width - MARGIN - _SMALL.getlength(badge), y, badge, inverted=True, small=True)


def sign_layout(view: BoardView, width: int = WIDTH, height: int = HEIGHT) -> SignLayout:
    """Places the text on a board `width` x `height` dots."""
    layout = SignLayout()
    trains, buses = _train_columns(view.trains, width), _bus_columns(view.buses, width)
    where_x = _where_column(view, trains, buses, width)
    y = _trains(layout, view.trains, _TOP, width, trains, where_x)
    rule = y + _ABOVE_RULE - 2  # each line already carries 2 dots of leading
    layout.rules.append(rule)
    footer_top = height - (GLYPH_H if view.clock else TINY_HEIGHT) - 3
    _buses(layout, view.buses, rule + _BELOW_RULE, footer_top, width, buses, where_x)
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
    bands: list[tuple[float, float, float, float]] = [(0, y - 1, width - 1, y + GLYPH_H) for y in band_rows]
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
    for mark in layout.strips:
        draw.point(sorted(strip_dots(mark)), fill=palette.black if mark.inverted else ink)
    image = _enlarge(image, panel, palette.black, bands)
    apply_preview(image, palette)
    return image


# The vehicle over a row's little line, pointing down at it: a bus square
# with windows, a train rounder with a windscreen, so the two differ by shape.
_LITTLE_BUS = (
    "#########",
    "#.#.#.#.#",
    "#########",
    "#########",
    "....#....",
)
_LITTLE_TRAIN = (
    ".#######.",
    "##.....##",
    "#########",
    ".#######.",
    "....#....",
)
_STRIP_LINE = 6  # the line's row, down from the top of its row's text: the baseline


def strip_dots(mark: StripMark) -> set[tuple[int, int]]:
    """The lit dots of a row's little line, as the dashboard's row strip draws
    it: a line through the stops, solid up to yours and every other dot after
    it; stops before yours filled, yours bigger, those after it hollow; and the
    train or bus where it is, none when that isn't known. Drawing only: the
    words beside it say where the vehicle is."""
    strip = mark.strip
    line_y = mark.y + _STRIP_LINE
    left, right = round(mark.x) + 2, round(mark.x) + mark.width - 3  # room for your stop at either end
    step = (right - left) / (strip.stops - 1)
    xs = [round(left + i * step) for i in range(strip.stops)]
    yours = xs[strip.yours]
    lit = {(x, line_y) for x in range(xs[0], xs[-1] + 1) if x <= yours or (x - yours) % 2 == 0}
    for i, x in enumerate(xs):
        r = 2 if i == strip.yours else 1
        box = {(x + dx, line_y + dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)}
        lit |= box - {(x, line_y)} if i > strip.yours else box
    if strip.at is not None:
        glyph = _LITTLE_TRAIN if mark.train else _LITTLE_BUS
        glyph_w = len(glyph[0])
        x0 = min(max(round(left + strip.at * step) - glyph_w // 2, round(mark.x)), round(mark.x) + mark.width - glyph_w)
        # From a dot above the text, inside a reversed-out row's band, down to just over the stops.
        top = mark.y - 1
        lit |= {(x0 + dx, top + dy) for dy, row in enumerate(glyph) for dx, dot in enumerate(row) if dot == "#"}
    return lit


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
    for i, (name, x) in enumerate(zip(view.stops, xs, strict=True)):
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
    apply_preview(image, palette)
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
            box = (int(x0 * scale), int(y0 * scale), int((x1 + 1) * scale), int((y1 + 1) * scale))
            big.paste(solid.crop(box), box[:2])
    canvas = Image.new("P", panel.size, background)
    canvas.paste(big, ((panel.width - big.width) // 2, (panel.height - big.height) // 2))
    return canvas

