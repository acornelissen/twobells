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
from .dotfont import SUB, SUB_HEIGHT, TINY, TINY_HEIGHT, DotFont
from .panel import (
    LEAVE_COLUMN_MIN_WIDTH,
    NOTES_SIDE_BY_SIDE_MIN_WIDTH,
    OWN_STRIPS_MIN_WIDTH,
    PHAT,
    PLATFORM_COLUMN_MIN_WIDTH,
    STOP_NAME_MIN_WIDTH,
    STRIP_COLUMN_MIN_WIDTH,
    Panel,
)
from .render import HEIGHT, WIDTH, Palette, apply_preview, fit_text, note_text, share_out
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
# A screen's lines under a train's row ("Calling at", the leave line), as its
# subtitles: the smaller font on a shorter line, with the same 2 dots of leading.
_SUB = DotFont(SUB)
_LEADING = _LINE - GLYPH_H
_SUB_LINE = SUB_HEIGHT + _LEADING


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
    sub: bool = False  # a screen's line under a row, in the subtitle font


def _font(small: bool = False, sub: bool = False) -> tuple[DotFont, int]:
    """The font an item is drawn in, and its height in dots."""
    if small:
        return _SMALL, TINY_HEIGHT
    return (_SUB, SUB_HEIGHT) if sub else (_FONT, GLYPH_H)


def item_size(item: Item) -> tuple[int, int]:
    """How much room an item takes: its text's, or a scrolling item's box."""
    font, height = _font(item.small, item.sub)
    return (font.getlength(item.text) if item.box is None else item.box), height


# On a screen wide enough, where each train or bus is: after the destination,
# a little line of its stops, then the words, which run on to the section's
# next column (platform, leave time or status).
STRIP_WIDTH = 42  # eight stops, five or six dots apart
# Twice the usual gap after the destination, so it and the little line don't
# run together, and after the where words, so "Left Glossop" and "Plat 1"
# don't read as one phrase.
_WHERE_GAP = 2 * _COL_GAP
# A row's room between its lead (ordinal and time, or route and stop) and its
# section's next column is shared out by a ladder (share_out), in turn: the
# where words "3 stops away"; a bus's stop names "Henry St A"; the
# destination its target, a train's (or tram's) "Manchester Piccadilly", a
# bus's "Ashton-under-Lyne"; the stop names the longest whole; the
# destination "Heathrow Terminals 2 & 3"; the where words "Left Manchester
# Oxford Road"; the destination the rest.
_WHERE_LEAST = _FONT.getlength("3 stops away")
_WHERE_MOST = _FONT.getlength("Left Manchester Oxford Road")
_DEST_TARGET = {"train": _FONT.getlength("Manchester Piccadilly"), "bus": _FONT.getlength("Ashton-under-Lyne")}
_DEST_MOST = _FONT.getlength("Heathrow Terminals 2 & 3")
# Where a train row's destination starts.
_TRAIN_DEST_X = MARGIN + _FONT.getlength(_ORDINAL_W) + _COL_GAP + _FONT.getlength("00:00") + _COL_GAP
# On a screen, twice the usual gap between the leave words and the status,
# so "Leave in 2 min" and "Exp 07:24" don't read as one phrase; e-paper keeps
# the one.
_LEAVE_GAP = 2 * _COL_GAP
# On a screen, a bus row's route column: as wide as the widest route shown,
# from "000" to four characters ("201A", "N199"); a longer one scrolls.
# E-paper keeps it "000" wide, cutting a longer route short.
_ROUTE_LEAST = _FONT.getlength("000")
_ROUTE_MOST = _FONT.getlength("N199")


# On a screen wide enough, a bus row's stop by its name: a column as wide as
# the longest of your stops' names, as far as the ladder gives it room, and
# never narrower than "Henry St A"; a longer name scrolls in it. Twice the
# usual gap after it, so it and the destination don't read as one name.
STOP_NAME_LEAST = _FONT.getlength("Henry St A")
_STOP_GAP = 2 * _COL_GAP


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

    def add(self, x: float, y: int, text: str, inverted: bool = False, small: bool = False,
            sub: bool = False) -> None:
        self.items.append(Item(x, y, text, inverted, small, sub=sub))

    def fit(self, x: float, y: int, text: str, room: float, inverted: bool = False, small: bool = False,
            sub: bool = False) -> None:
        """`text` in a space `room` dots wide from x: whole if it fits; if
        not, scrolling in that space on a screen that scrolls, cut short otherwise."""
        fitted = fit_text(text, _font(small, sub)[0], room)
        if self.scrolls and fitted != text and room >= 1:
            self.items.append(Item(x, y, text, inverted, small, box=int(room), sub=sub))
        else:
            self.add(x, y, fitted, inverted, small, sub)


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


def _train_columns(section: Section, width: int, most: int, live: bool) -> _Columns:
    """From the right, each column left-aligned clear of the widest one after
    it, so it lines up, for the first `most` rows; on a screen (`live`),
    the leave words twice the gap clear of the status."""
    rows = section.rows[:most]
    if not rows:
        return _Columns(width - MARGIN)
    end = width - MARGIN - max(_w(r.right) for r in rows)
    leave_x = platform_x = None
    if section.leave and width >= LEAVE_COLUMN_MIN_WIDTH and (section.leave_for or 0) < len(rows):
        # One line a row: the leave words on their train's row, not under it,
        # set further off the platform so the two don't read as one phrase.
        leave_x = end = end - (_LEAVE_GAP if live else _COL_GAP) - _w(section.leave)
        end -= _COL_GAP
    if width >= PLATFORM_COLUMN_MIN_WIDTH and any(r.platform for r in rows):
        platform_x = end = end - _COL_GAP - max(_w(r.platform) for r in rows if r.platform)
    return _Columns(end, leave_x, platform_x)


def _bus_columns(section: Section, width: int, live: bool) -> _Columns:
    rows = section.rows
    if not rows:
        return _Columns(width - MARGIN)
    end = width - MARGIN - max(_w(r.right) for r in rows)
    leaves = [r.leave for r in rows if r.leave]
    if not leaves:
        return _Columns(end)
    leave_x = end - (_LEAVE_GAP if live else _COL_GAP) - max(map(_w, leaves))
    return _Columns(leave_x, leave_x)


def _columns(section: Section, width: int, most: int, live: bool) -> _Columns:
    """A section's columns, as its mode's rows are drawn (modes.Mode.rows)."""
    if section.kind == "train":
        return _train_columns(section, width, most, live)
    return _bus_columns(section, width, live)


def _stop_x(section: Section, live: bool) -> float:
    """Where a bus section's stop column starts: after its route column, on a
    screen (`live`) as wide as its widest route, "000" to four characters."""
    widest = max((_w(r.left) for r in section.rows), default=0) if live else 0
    return MARGIN + min(_ROUTE_MOST, max(_ROUTE_LEAST, widest)) + _COL_GAP - 1


def _names_wanted(section: Section, width: int, live: bool) -> float | None:
    """How wide the bus rows' stop names want to be, the longest whole, or
    None where they show their labels: e-paper, a board too narrow, no names
    known. From all your stops' names, not the rows shown, so the
    destinations stay put; a row whose stop has no name shows its label there."""
    if not live or width < STOP_NAME_MIN_WIDTH or not section.stop_names:
        return None
    labels = [r.stop for r in section.rows if r.stop and not r.stop_name]
    return max(map(_w, [*section.stop_names, *labels]))


def _bus_lead(section: Section, width: int, live: bool, dest_end: float) -> tuple[float, float | None, float]:
    """(stop_x, names_w, dest_x) for a bus section whose destinations end at
    `dest_end`: the names as wide as they want, as far as the bus
    destination's target leaves room, and at least STOP_NAME_LEAST; None
    for names_w where the rows show their labels."""
    stop_x = _stop_x(section, live)
    wanted = _names_wanted(section, width, live)
    if wanted is None:
        stops = [r.stop for r in section.rows if r.stop]
        # Room for the widest stop label, and at least a "W" so one-letter stops line up.
        return stop_x, None, stop_x + (max(map(_w, [*stops, "W"])) + _COL_GAP - 1 if stops else 0)
    room = dest_end - _DEST_TARGET["bus"] - _STOP_GAP - stop_x
    names_w = min(wanted, max(STOP_NAME_LEAST, room))
    return stop_x, names_w, stop_x + names_w + _STOP_GAP


def _own_strip_x(section: Section, columns: _Columns, width: int) -> float:
    """Where a section's little lines start against its own columns: after
    what the ladder gives its destinations (and a bus's stop names) of the
    row's room, the where words having their share before the next column."""
    words_end = columns.end - _WHERE_GAP
    wanted = _names_wanted(section, width, True) if section.kind == "bus" else None
    if section.kind == "train":
        lead = _TRAIN_DEST_X
        steps = [("words", _WHERE_LEAST), ("destination", _DEST_TARGET["train"])]
    elif wanted is None:
        lead = _bus_lead(section, width, True, 0)[2]
        steps = [("words", _WHERE_LEAST), ("destination", _DEST_TARGET["bus"])]
    else:
        lead = _stop_x(section, True) + _STOP_GAP
        steps = [("words", _WHERE_LEAST), ("names", min(wanted, STOP_NAME_LEAST)),
                 ("destination", _DEST_TARGET["bus"]), ("names", wanted)]
    steps += [("destination", _DEST_MOST), ("words", _WHERE_MOST)]
    room = words_end - (_WHERE_GAP + STRIP_WIDTH + _COL_GAP) - lead
    got = share_out(room, steps)
    return lead + got["names"] + got["destination"] + _WHERE_GAP


def strips_lined_up(width: int) -> bool:
    """Whether a board `width` dots across lines its sections' little lines up
    in one column (OWN_STRIPS_MIN_WIDTH), rather than each against its own
    columns. Fixed for the board, never from the rows shown, so they stay put."""
    return width < OWN_STRIPS_MIN_WIDTH


def _strip_columns(view: BoardView, columns: list[_Columns], width: int, counts: list[int]) -> list[float | None]:
    """Where each section's little lines start, its first `counts` rows as
    drawn, None for a section without one among them. Only on a screen wide
    enough: a strip changes every few seconds, so e-paper, which redraws
    slowly and only on change, never shows one. Lined up, they are where the
    tightest section's ladder puts them."""
    sections = view.sections
    if view.clock is None or width < STRIP_COLUMN_MIN_WIDTH:
        return [None] * len(sections)
    own = [_own_strip_x(s, c, width) if any(r.strip for r in s.rows[:count]) else None
           for s, c, count in zip(sections, columns, counts, strict=True)]
    placed = [x for x in own if x is not None]
    if not placed or not strips_lined_up(width):
        return own
    return [min(placed) if x is not None else None for x in own]


def _add_strip(layout: SignLayout, row: Row, strip_x: float, y: int, end: float, train: bool,
               inverted: bool) -> None:
    """The row's little line at `strip_x`, then its where words, up to the
    section's next column at `end`. A row without one leaves the space blank."""
    if row.strip is None:
        return
    layout.strips.append(StripMark(strip_x, y, row.strip, train, inverted))
    words_x = strip_x + STRIP_WIDTH + _COL_GAP
    if row.strip.where:
        layout.fit(words_x, y, row.strip.where, end - _WHERE_GAP - words_x, inverted)


def calling_line(section: Section, live: bool) -> str | None:
    """The first train's "Calling at" line, on a screen (`live`), which
    scrolls it under the train's row; None on e-paper, which can't."""
    if not live or section.kind != "train" or not section.rows:
        return None
    return section.rows[0].calling


def _trains(layout: SignLayout, section: Section, y: int, width: int, columns: _Columns,
            strip_x: float | None, most: int = _MAX_TRAINS, bottom: int | None = None,
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
    leave_for = section.leave_for or 0
    # The lines under a row: a screen's subtitles, e-paper's full size.
    sub = layout.live
    under = _SUB_LINE if sub else _LINE
    under_h = SUB_HEIGHT if sub else GLYPH_H
    for index, row in enumerate(rows):
        if bottom is not None and index and y + GLYPH_H > bottom:
            break
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
        if strip_x is not None:
            dest_end = strip_x - _WHERE_GAP
        elif columns.leave_x is not None or columns.platform_x is not None:
            dest_end = columns.end - _COL_GAP
        else:
            dest_end = status_x - _COL_GAP
        layout.add(MARGIN, y, _ORDINALS[index], inverted)
        layout.add(time_x, y, row.left, inverted)
        destination = destination_text(row, columns.platform_x is not None, width >= PLATFORM_COLUMN_MIN_WIDTH)
        layout.fit(dest_x, y, destination, dest_end - dest_x, inverted)
        if strip_x is not None:
            _add_strip(layout, row, strip_x, y, columns.end, True, inverted)
        if columns.platform_x is not None and row.platform:
            layout.add(columns.platform_x, y, row.platform, inverted)
        if columns.leave_x is not None and section.leave and index == leave_for:
            layout.add(columns.leave_x, y, section.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
        fits = bottom is None or y + under_h <= bottom
        if index == 0 and calling and calling_line(section, layout.live) and fits:
            # As on the real board: under the first train, where it calls, scrolling.
            layout.fit(time_x, y, row.calling or "", width - MARGIN - time_x, sub=sub)
            y += under
            fits = bottom is None or y + under_h <= bottom
        if section.leave and columns.leave_x is None and index == leave_for and fits:
            # Under its train, as the "Calling at" line is, say when to set off.
            layout.fit(time_x, y, section.leave, width - MARGIN - time_x, sub=sub)
            y += under
    return y


def _buses(layout: SignLayout, section: Section, y: int, bottom: int, width: int, columns: _Columns,
           strip_x: float | None, most: int | None = None) -> int:
    rows = list(section.rows[:most])
    if not rows:
        layout.add(MARGIN, y, section.empty_text)
        return y + _LINE
    lead_end = strip_x - _WHERE_GAP if strip_x is not None else columns.end - _COL_GAP
    stop_x, names_w, dest_x = _bus_lead(section, width, layout.live, lead_end)
    for row in rows:
        if y + GLYPH_H > bottom:
            break
        inverted = row.style is Style.INVERTED
        status_x = _right(row.right, width)
        if strip_x is not None:
            dest_end = strip_x - _WHERE_GAP
        else:
            dest_end = (status_x if columns.leave_x is None else columns.leave_x) - _COL_GAP
        layout.fit(MARGIN, y, row.left, stop_x - MARGIN - 2, inverted)
        if names_w is not None:
            if row.stop_name or row.stop:
                layout.fit(stop_x, y, row.stop_name or row.stop or "", names_w, inverted)
        elif row.stop:
            layout.add(stop_x, y, row.stop, inverted)
        layout.fit(dest_x, y, row.middle, dest_end - dest_x, inverted)
        if strip_x is not None:
            _add_strip(layout, row, strip_x, y, columns.end, False, inverted)
        if row.leave and columns.leave_x is not None:
            layout.add(columns.leave_x, y, row.leave, inverted)
        layout.add(status_x, y, row.right, inverted)
        y += _LINE
    return y


def _footer(layout: SignLayout, view: BoardView, width: int, height: int) -> None:
    y = height - TINY_HEIGHT - 1
    # Each by its section's heading, as London's metro is "Underground" or
    # "DLR": the small print draws it in capitals.
    stale = [s.label.lower() for s in view.sections if s.stale]
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


def _height(section: Section, rows: int, calling: bool, leave_column: bool, live: bool) -> int:
    """How many dots tall a section is with its first `rows` rows, each line
    with its leading: the rows ("No buses" is a line too), and a train's
    "Calling at" line, with `calling`, and its leave line, unless the leave
    words have a column on their train's row (`leave_column`). Those two are
    a screen's (`live`) subtitles, on shorter lines."""
    shown = max(1, min(rows, len(section.rows)))
    if section.kind != "train" or not section.rows:
        return shown * _LINE
    leave = section.leave is not None and not leave_column and (section.leave_for or 0) < shown
    return shown * _LINE + (calling + leave) * (_SUB_LINE if live else _LINE)


def _fits(heights: list[int], bottom: int) -> bool:
    """Whether sections this many dots tall each, one under another from the
    top with a rule between each two, end above `bottom` (the last line's
    leading is below its text, so it doesn't count)."""
    return _TOP + sum(heights) + (len(heights) - 1) * _BETWEEN - _LEADING <= bottom


def _row_counts(wants: list[int], height: Callable[[int, int], int], bottom: int) -> list[int]:
    """How many rows each section draws so that they all fit above `bottom`,
    `height(index, rows)` saying how tall section `index` is with that
    many: each its most (`wants`) where they fit; where not, the last
    gives rows up first, as it has the rest of the room, then the section
    with the most (of two with as many, the lower). So a train's leave line
    always has room under its row: its section shows a train fewer for it,
    or, down to one, another section does. Each keeps a row, even on a board
    too small for that."""
    counts = list(wants)
    while counts and not _fits([height(index, count) for index, count in enumerate(counts)], bottom):
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
    columns = [_columns(s, width, _most_rows(s, alone, epaper), layout.live) for s in sections]
    footer_y = height - (GLYPH_H if view.clock else TINY_HEIGHT) - 1  # the bottom line's top

    def section_height(index: int, rows: int) -> int:
        section = sections[index]
        return _height(section, rows, section.mode == calling_for, columns[index].leave_x is not None, layout.live)

    wants = [_most_rows(s, alone, epaper) for s in sections]
    if sections and sections[-1].kind != "train":
        wants[-1] = len(sections[-1].rows)  # the last has the rest of the room
    wants = [max(1, min(want, len(s.rows))) for want, s in zip(wants, sections, strict=True)]
    counts = _row_counts(wants, section_height, footer_y - 2)
    # The rule over the bottom line, as between sections. A screen always has
    # one, the last section giving rows up for it, unless the "Calling at"
    # line has left it room for the rule or a bus, not both (the bar screen's
    # third); e-paper only when it costs no row, as it can on a full board.
    ruled = footer_y - 2 * _FOOTER_GAP - 1
    if view.clock is not None and calling_for is None:
        while counts and counts[-1] > 1 and not _fits([section_height(i, c) for i, c in enumerate(counts)], ruled):
            counts[-1] -= 1
    footer_rule = bool(counts) and _fits([section_height(i, c) for i, c in enumerate(counts)], ruled)
    bottom = ruled if footer_rule else footer_y - 2
    strip_xs = _strip_columns(view, columns, width, counts)
    y = _TOP
    for index, (section, cols, count, strip_x) in enumerate(zip(sections, columns, counts, strip_xs, strict=True)):
        if index:
            rule = y + _ABOVE_RULE - 2  # each line already carries 2 dots of leading
            layout.rules.append(rule)
            y = rule + _BELOW_RULE
        if section.kind == "train":
            y = _trains(layout, section, y, width, cols, strip_x, count, bottom, section.mode == calling_for)
        else:
            y = _buses(layout, section, y, bottom, width, cols, strip_x, count)
    if footer_rule:
        # Halfway between the last line and the bottom line, so the gaps match.
        last_bottom = y - _LEADING - 1
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
            font = _font(item.small, item.sub)[0]
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
        font, text_h = _font(item.small, item.sub)
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

