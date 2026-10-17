"""Old-school station departure board look: dot-matrix amber on black.

On the four-colour panel the amber is the panel's yellow. Meaning stays in
the words (Exp, Cancelled, Delayed, Not updating), so nothing relies on colour.
On a screen, text too long for its space scrolls a dot at a time, as on a real
sign; e-paper cuts it short.

The layout works in LED dots. On the pHAT a dot is one pixel; a bigger panel
draws each dot as a block of pixels, with a dark gap between LEDs once there is room.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from PIL import Image, ImageDraw

from ..core.model import Strip
from ..settings import modes as mode_list
from .dotfont import HEIGHT as GLYPH_H
from .dotfont import TINY, TINY_HEIGHT, DotFont
from .panel import (
    LEAVE_COLUMN_MIN_WIDTH,
    NOTES_SIDE_BY_SIDE_MIN_WIDTH,
    PHAT,
    PLATFORM_COLUMN_MIN_WIDTH,
    STOP_NAME_MIN_WIDTH,
    STRIP_COLUMN_MIN_WIDTH,
    Panel,
)
from .render import HEIGHT, WIDTH, Palette, apply_preview, fit_text, note_text
from .route import RouteView
from .scroll import Frame, Scroll, frame_of
from .view import BoardView, Row, Section, Style, destination_text

MARGIN = 3
_LINE = 11
_TOP = 6
# Space either side of the dotted divider between trains and buses.
_ABOVE_RULE = 8
_BELOW_RULE = 11
_COL_GAP = 5
# The least space either side of the rule over the bottom line.
_FOOTER_GAP = 3
# Either side of the rain warning beside a notice, so the three read as three.
_NOTES_GAP = 2 * _COL_GAP
_ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th")
# The ordinals' column is as wide as a board of four trains needs.
_ORDINAL_W = "4th"
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
    # On a screen, text too long for its space: the space's width, in which
    # the whole text scrolls. None for text drawn as it is.
    box: int | None = None


def item_size(item: Item) -> tuple[int, int]:
    """How much room an item takes: its text's, or a scrolling item's box."""
    font, height = (_SMALL, TINY_HEIGHT) if item.small else (_FONT, GLYPH_H)
    return (font.getlength(item.text) if item.box is None else item.box), height


# On a screen wide enough, where each train or bus is: the words, then a
# little line of its stops.
STRIP_WIDTH = 42  # eight stops, five or six dots apart
# Twice the usual gap after the destination, so it and the where words don't
# run together into one name.
_WHERE_GAP = 2 * _COL_GAP
# The where words' column: at least "3 stops away", at most eighteen letters
# ("Left Glossop 07:08"), which is as long as they are meant to be.
_WHERE_LEAST = _FONT.getlength("3 stops away")
_WHERE_MOST = _FONT.getlength("Left Glossop 07:08")
# What the rest of a full row needs beside the where words: ordinal and time,
# "Manchester Piccadilly" whole and the little line, then a bus's leave time
# and status, or (where trains get a leave column) a train's platform, leave
# time and status, whichever is wider.
_ROW_START = (
    MARGIN + _FONT.getlength("4th") + _COL_GAP + _FONT.getlength("00:00") + _COL_GAP
    + _FONT.getlength("Manchester Piccadilly") + _WHERE_GAP + _COL_GAP + STRIP_WIDTH + _COL_GAP
)
_REST_OF_BUS_ROW = _ROW_START + _FONT.getlength("Leave in 00 min") + _COL_GAP + _FONT.getlength("Exp 00:00") + MARGIN
_REST_OF_TRAIN_ROW = (
    _ROW_START + _FONT.getlength("Plat 1") + 2 * _COL_GAP + _FONT.getlength("Leave in 00 min") + _COL_GAP
    + _FONT.getlength("Cancelled") + MARGIN
)


# On a screen wide enough, a bus row's stop by its name: a column as wide as
# the longest of your stops' names, at most sixteen letters; a longer name
# scrolls in it. Twice the usual gap after it, so it and the destination
# don't read as one name.
STOP_NAME_MOST = _FONT.getlength("Norfolk Square W")
_STOP_GAP = 2 * _COL_GAP


def where_width(width: int) -> int:
    """How wide the where words' column is on a board `width` dots across: the
    room a full row leaves, from "3 stops away" to eighteen letters. It is
    worked out from the width alone, so it stays put as the words change."""
    rest = max(_REST_OF_BUS_ROW, _REST_OF_TRAIN_ROW) if width >= LEAVE_COLUMN_MIN_WIDTH else _REST_OF_BUS_ROW
    return max(_WHERE_LEAST, min(_WHERE_MOST, width - rest))


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
    # A screen, where text too long for its space scrolls (with `scroll`, the
    # scrolling setting); e-paper cuts it short, as a screen does without it.
    live: bool = False
    scroll: bool = True

    @property
    def scrolls(self) -> bool:
        return self.live and self.scroll

    def add(self, x: float, y: int, text: str, inverted: bool = False, small: bool = False) -> None:
        self.items.append(Item(x, y, text, inverted, small))

    def fit(self, x: float, y: int, text: str, room: float, inverted: bool = False, small: bool = False) -> None:
        """`text` in a space `room` dots wide from x: whole if it fits; if
        not, scrolling in that space on a screen that scrolls, cut short otherwise."""
        fitted = fit_text(text, _SMALL if small else _FONT, room)
        if self.scrolls and fitted != text and room >= 1:
            self.items.append(Item(x, y, text, inverted, small, box=int(room)))
        else:
            self.add(x, y, fitted, inverted, small)


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


def _train_columns(section: Section, width: int, most: int = _MAX_TRAINS) -> _Columns:
    """From the right, each column left-aligned clear of the widest one after
    it, so it lines up, for the first `most` rows."""
    rows = section.rows[:most]
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


def _columns(section: Section, width: int, most: int) -> _Columns:
    """A section's columns, as its mode's rows are drawn (modes.Mode.rows)."""
    return _train_columns(section, width, most) if section.kind == "train" else _bus_columns(section, width)


def _where_column(view: BoardView, columns: list[_Columns], width: int) -> float | None:
    """Where the where words start, with the little lines after them: one
    column down the board, clear of both sections' other columns. Only on a
    screen wide enough: a strip changes every few seconds, so e-paper, which
    redraws slowly and only on change, never shows one. It has a fixed width,
    so it stays put as the words change."""
    if view.clock is None or width < STRIP_COLUMN_MIN_WIDTH or not columns:
        return None
    return min(c.end for c in columns) - _COL_GAP - STRIP_WIDTH - _COL_GAP - where_width(width)


def _add_strip(layout: SignLayout, row: Row, where_x: float, y: int, width: int, train: bool, inverted: bool) -> None:
    # A row without one leaves the space blank.
    if row.strip is None:
        return
    words_w = where_width(width)
    if row.strip.where:
        layout.fit(where_x, y, row.strip.where, words_w, inverted)
    layout.strips.append(StripMark(where_x + words_w + _COL_GAP, y, row.strip, train, inverted))


def _stop_column(section: Section, width: int, live: bool) -> int | None:
    """How wide the bus rows' stop names are drawn, or None where they show
    their labels: e-paper, a board too narrow, no names known. From all your
    stops' names, not the rows shown, so the destinations stay put; a row
    whose stop has no name shows its label there."""
    if not live or width < STOP_NAME_MIN_WIDTH or not section.stop_names:
        return None
    labels = [r.stop for r in section.rows if r.stop and not r.stop_name]
    return min(STOP_NAME_MOST, max(map(_w, [*section.stop_names, *labels])))


def calling_line(section: Section, live: bool) -> str | None:
    """The first train's "Calling at" line, on a screen (`live`), which
    scrolls it under the train's row; None on e-paper, which can't."""
    if not live or section.kind != "train" or not section.rows:
        return None
    return section.rows[0].calling


def _trains(layout: SignLayout, section: Section, y: int, width: int, columns: _Columns,
            where_x: float | None, most: int = _MAX_TRAINS, bottom: int | None = None,
            calling: bool = True) -> int:
    """A train section's rows from `y`, the first `most`, while they fit
    above `bottom`; with `calling`, the first one's "Calling at" line under
    it. sign_layout gives it as many as fit with their lines; on a board too
    small for even one, the first row is drawn and the lines under it left
    out rather than run into the bottom line."""
    time_x = MARGIN + _w(_ORDINAL_W) + _COL_GAP
    dest_x = time_x + _w("00:00") + _COL_GAP
    rows = section.rows[:most]
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    if not any(r.strip for r in rows):
        where_x = None  # no room kept for strips that none of them has
    leave_for = section.leave_for or 0
    for index, row in enumerate(rows):
        if bottom is not None and index and y + GLYPH_H > bottom:
            break
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
        destination = destination_text(row, columns.platform_x is not None, width >= PLATFORM_COLUMN_MIN_WIDTH)
        layout.fit(dest_x, y, destination, dest_end - dest_x, inverted)
        if where_x is not None:
            _add_strip(layout, row, where_x, y, width, True, inverted)
        if columns.platform_x is not None and row.platform:
            layout.add(columns.platform_x, y, row.platform, inverted)
        if columns.leave_x is not None and section.leave and index == leave_for:
            layout.add(columns.leave_x, y, section.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
        fits = bottom is None or y + GLYPH_H <= bottom
        if index == 0 and calling and calling_line(section, layout.live) and fits:
            # As on the real board: under the first train, where it calls, scrolling.
            layout.fit(time_x, y, row.calling or "", width - MARGIN - time_x)
            y += _LINE
            fits = bottom is None or y + GLYPH_H <= bottom
        if section.leave and columns.leave_x is None and index == leave_for and fits:
            # Under its train, as the "Calling at" line is, say when to set off.
            layout.fit(time_x, y, section.leave, width - MARGIN - time_x)
            y += _LINE
    return y


def _buses(layout: SignLayout, section: Section, y: int, bottom: int, width: int, columns: _Columns,
           where_x: float | None, most: int | None = None) -> int:
    rows = list(section.rows[:most])
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    if not any(r.strip for r in rows):
        where_x = None
    stop_x = MARGIN + _w("000") + _COL_GAP - 1
    names_w = _stop_column(section, width, layout.live)
    if names_w is not None:
        dest_x = stop_x + names_w + _STOP_GAP
    else:
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
        layout.fit(MARGIN, y, row.left, stop_x - MARGIN - 2, inverted)
        if names_w is not None:
            if row.stop_name or row.stop:
                layout.fit(stop_x, y, row.stop_name or row.stop or "", names_w, inverted)
        elif row.stop:
            layout.add(stop_x, y, row.stop, inverted)
        layout.fit(dest_x, y, row.middle, dest_end - dest_x, inverted)
        if where_x is not None:
            _add_strip(layout, row, where_x, y, width, False, inverted)
        if row.leave and columns.leave_x is not None:
            layout.add(columns.leave_x, y, row.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
    return y


def _footer(layout: SignLayout, view: BoardView, width: int, height: int) -> None:
    y = height - TINY_HEIGHT - 1
    stale = [mode_list.mode(s.mode).noun for s in view.sections if s.stale]
    badge = "Not updating: " + ", ".join(stale) if stale else None
    # Where the free part of the bottom line ends: before the badge, if there is one.
    free_end = width - MARGIN
    if badge:
        free_end -= _SMALL.getlength(badge) + 2 + _COL_GAP  # the badge's band reaches 2 dots left
    stamp = f"Updated {view.updated}"
    if view.clock and note_text(view):
        # A screen keeps its clock on the bottom line, moved to the right, and
        # shows the notice (or rain warning, or last bus) in the room left of
        # it, scrolling if it is too long for it.
        clock_x = free_end - _w(view.clock)
        layout.add(clock_x, height - GLYPH_H - 1, view.clock)
        note_end, shown = clock_x - _COL_GAP, view
        if view.notice and view.rain and width >= NOTES_SIDE_BY_SIDE_MIN_WIDTH:
            # Wide enough for both: the rain warning, steady, left of the
            # clock, and the notice in its own space.
            rain_x = clock_x - _NOTES_GAP - _w(view.rain)
            layout.add(rain_x, height - GLYPH_H - 1, view.rain)
            note_end, shown = rain_x - _NOTES_GAP, replace(view, rain=None)
        layout.fit(MARGIN, height - GLYPH_H - 1, note_text(shown) or "", note_end - MARGIN)
    elif view.clock:
        # A screen shows the time centred along the bottom, like a station clock.
        layout.add((width - _w(view.clock)) // 2, height - GLYPH_H - 1, view.clock)
    elif note_text(view):
        # The notice (or rain warning) on the left, cut short if it must be,
        # keeping "Updated" on the right: e-paper keeps its last image if the
        # board stops, and the stamp is how to tell.
        stamp_x = free_end - _SMALL.getlength(stamp)
        room = stamp_x - _COL_GAP - MARGIN
        note = fit_text(note_text(view) or "", _SMALL, room)
        if _SMALL.getlength(note) <= room:  # not even an ellipsis fits on a board too narrow
            layout.add(MARGIN, y, note, small=True)
        layout.add(stamp_x, y, stamp, small=True)
    else:
        layout.add(MARGIN, y, stamp, small=True)
    if badge:
        # Small, but reversed out so a stalled feed still catches the eye.
        layout.add(width - MARGIN - _SMALL.getlength(badge), y, badge, inverted=True, small=True)


# From the bottom of a section's last line to the top of the next section's
# first: the dotted rule with its space either side (each line already
# carries 2 dots of leading).
_BETWEEN = _ABOVE_RULE - 2 + _BELOW_RULE


def _most_rows(section: Section, alone: bool, epaper: bool) -> int:
    """The most rows a section draws: a train section 3 while it shares the
    board, as many as there are ordinals alone; any other its mode's most
    while it shares the board (modes.Mode.most_rows)."""
    if section.kind == "train":
        return len(_ORDINALS) if alone else _MAX_TRAINS
    return mode_list.mode(section.mode).most_rows[0 if epaper else 1]


def _lines(section: Section, rows: int, calling: bool, leave_column: bool) -> int:
    """How many lines a section takes with its first `rows` rows: the rows
    ("No buses" is a line too), and a train's "Calling at" line, with
    `calling`, and its leave line, unless the leave words have a column on
    their train's row (`leave_column`)."""
    shown = max(1, min(rows, len(section.rows)))
    if section.kind != "train" or not section.rows:
        return shown
    leave = section.leave is not None and not leave_column and (section.leave_for or 0) < shown
    return shown + calling + leave


def _fits(lines: list[int], bottom: int) -> bool:
    """Whether sections of these many lines each, one under another from the
    top with a rule between each two, end above `bottom`."""
    last_top = _TOP + (sum(lines) - 1) * _LINE + (len(lines) - 1) * _BETWEEN
    return last_top + GLYPH_H <= bottom


def _row_counts(wants: list[int], lines: Callable[[int, int], int], bottom: int) -> list[int]:
    """How many rows each section draws so that they all fit above `bottom`,
    `lines(index, rows)` saying how many lines section `index` takes with
    that many: each its most (`wants`) where they fit; where not, the last
    gives rows up first, as it has the rest of the room, then the section
    with the most (of two with as many, the lower). So a train's leave line
    always has room under its row: its section shows a train fewer for it,
    or, down to one, another section does. Each keeps a row, even on a board
    too small for that."""
    counts = list(wants)
    while counts and not _fits([lines(index, count) for index, count in enumerate(counts)], bottom):
        if counts[-1] > 1:
            counts[-1] -= 1
            continue
        most = max(counts)
        if most == 1:
            break
        counts[max(index for index, count in enumerate(counts) if count == most)] -= 1
    return counts


def sign_layout(view: BoardView, width: int = WIDTH, height: int = HEIGHT) -> SignLayout:
    """Places the text on a board `width` x `height` dots: each mode's
    section, a dotted rule between each and the next, and the bottom line,
    with a rule over it too where there is room. Each section draws its most
    rows (_most_rows) and the last the rest of the room, as many as fit with
    the lines under them (_row_counts)."""
    layout = SignLayout(live=view.clock is not None, scroll=view.scroll)
    sections = view.sections
    alone = len(sections) == 1
    epaper = view.clock is None
    calling_for = next((s.mode for s in sections if calling_line(s, layout.live)), None)
    columns = [_columns(s, width, _most_rows(s, alone, epaper)) for s in sections]
    where_x = _where_column(view, columns, width)
    footer_y = height - (GLYPH_H if view.clock else TINY_HEIGHT) - 1  # the bottom line's top

    def lines(index: int, rows: int) -> int:
        section = sections[index]
        return _lines(section, rows, section.mode == calling_for, columns[index].leave_x is not None)

    wants = [_most_rows(s, alone, epaper) for s in sections]
    if sections and sections[-1].kind != "train":
        wants[-1] = len(sections[-1].rows)  # the last has the rest of the room
    wants = [max(1, min(want, len(s.rows))) for want, s in zip(wants, sections, strict=True)]
    counts = _row_counts(wants, lines, footer_y - 2)
    # The rule over the bottom line, as between sections. A screen always has
    # one, the last section giving rows up for it, unless the "Calling at"
    # line has left it room for the rule or a bus, not both (the bar screen's
    # third); e-paper only when it costs no row, as it can on a full board.
    ruled = footer_y - 2 * _FOOTER_GAP - 1
    if view.clock is not None and calling_for is None:
        while counts and counts[-1] > 1 and not _fits([lines(i, c) for i, c in enumerate(counts)], ruled):
            counts[-1] -= 1
    footer_rule = bool(counts) and _fits([lines(i, c) for i, c in enumerate(counts)], ruled)
    bottom = ruled if footer_rule else footer_y - 2
    y = _TOP
    for index, (section, cols, count) in enumerate(zip(sections, columns, counts, strict=True)):
        if index:
            rule = y + _ABOVE_RULE - 2  # each line already carries 2 dots of leading
            layout.rules.append(rule)
            y = rule + _BELOW_RULE
        if section.kind == "train":
            y = _trains(layout, section, y, width, cols, where_x, count, bottom, section.mode == calling_for)
        else:
            y = _buses(layout, section, y, bottom, width, cols, where_x, count)
    if footer_rule:
        # Halfway between the last line and the bottom line, so the gaps match.
        last_bottom = y - _LINE + GLYPH_H - 1
        layout.rules.append(last_bottom + (footer_y - last_bottom) // 2)
    _footer(layout, view, width, height)
    return layout


def _ink(palette: Palette) -> int:
    # A mono panel has no amber; white on black keeps the board look.
    return palette.white if palette.accent == palette.black else palette.accent


def render_sign(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The board, any scrolling text at its start."""
    return sign_frame(view, palette, panel).image


def sign_frame(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The board, and on a screen the text that scrolls."""
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
        if item.box is None:
            font = _SMALL if item.small else _FONT
            font.draw(draw, (item.x, item.y), item.text, fill=palette.black if item.inverted else ink)
    for mark in layout.strips:
        draw.point(sorted(strip_dots(mark)), fill=palette.black if mark.inverted else ink)
    image = _enlarge(image, panel, palette.black, bands)
    apply_preview(image, palette)
    return frame_of(image, _scrolls(layout, palette, panel, width, height))


def _scrolls(layout: SignLayout, palette: Palette, panel: Panel, width: int, height: int) -> list[Scroll]:
    """The scrolling items, each drawn whole on its own strip of LEDs (lit
    background in a reversed-out band) and moving a dot at a time."""
    ink, scale = _ink(palette), panel.scale
    # Where _enlarge puts the board on the panel.
    left, top = (panel.width - width * scale) // 2, (panel.height - height * scale) // 2
    scrolls = []
    for item in layout.items:
        if item.box is None:
            continue
        font, text_h = (_SMALL, TINY_HEIGHT) if item.small else (_FONT, GLYPH_H)
        dots = Image.new("P", (font.getlength(item.text), text_h), ink if item.inverted else palette.black)
        font.draw(ImageDraw.Draw(dots), (0, 0), item.text, fill=palette.black if item.inverted else ink)
        strip = _blocks(dots, scale, palette.black, gaps=not item.inverted)
        apply_preview(strip, palette)
        x = int(item.x)
        box = (left + x * scale, top + item.y * scale, left + (x + item.box) * scale, top + (item.y + text_h) * scale)
        scrolls.append(Scroll(box, strip, scale, item.text))
    return scrolls


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
    layout = SignLayout(live=view.clock is not None, scroll=view.scroll)
    layout.fit(MARGIN, _TOP, view.title, width - MARGIN * 2 - _w(view.due) - _COL_GAP)
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
        # A name that scrolls on a screen takes the whole of its room.
        text_w = room if layout.scrolls and text != name else _w(text)
        tx = min(max(MARGIN, x - text_w / 2), width - MARGIN - text_w)
        ty = road - _LABEL_GAP - GLYPH_H if i % 2 == 0 else road + _LABEL_GAP
        # Your stop, the last one, is reversed out.
        layout.fit(round(tx), ty, name, room, inverted=i == len(view.stops) - 1)
    if view.clock:
        layout.add((width - _w(view.clock)) // 2, height - GLYPH_H - 1, view.clock)
    return layout, road, xs


def render_route_sign(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The route page, any scrolling text at its start."""
    return route_sign_frame(view, palette, panel).image


def route_sign_frame(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The route page, and on a screen the text that scrolls."""
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
        if item.box is None:
            _FONT.draw(draw, (item.x, item.y), item.text, fill=palette.black if item.inverted else ink)
    image = _enlarge(image, panel, palette.black, solid)
    apply_preview(image, palette)
    return frame_of(image, _scrolls(layout, palette, panel, width, height))


def _enlarge(image: Image.Image, panel: Panel, background: int, bands) -> Image.Image:
    """Draws each dot as a block of panel pixels, centred on the panel.

    From 3 pixels a dot there is a dark gap between LEDs, as on a real matrix
    board, except in reversed-out bands: gaps there would break up the dark text.
    """
    scale = panel.scale
    if scale == 1:
        return image
    solid = _blocks(image, scale, background, gaps=False)
    big = _blocks(image, scale, background, gaps=True)
    if scale >= 3:
        for x0, y0, x1, y1 in bands:
            box = (int(x0 * scale), int(y0 * scale), int((x1 + 1) * scale), int((y1 + 1) * scale))
            big.paste(solid.crop(box), box[:2])
    canvas = Image.new("P", panel.size, background)
    canvas.paste(big, ((panel.width - big.width) // 2, (panel.height - big.height) // 2))
    return canvas


def _blocks(image: Image.Image, scale: int, background: int, gaps: bool) -> Image.Image:
    """Each dot as a block of `scale` pixels; with `gaps`, from 3 pixels a dot,
    a dark line between LEDs along each block's right and bottom edge."""
    big = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    if gaps and scale >= 3:
        draw = ImageDraw.Draw(big)
        for x in range(scale - 1, big.width, scale):
            draw.line((x, 0, x, big.height - 1), fill=background)
        for y in range(scale - 1, big.height, scale):
            draw.line((0, y, big.width - 1, y), fill=background)
    return big

