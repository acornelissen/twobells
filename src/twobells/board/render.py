"""Plain look: draws a BoardView onto a paletted image, scaled up from the pHAT design.

On a screen, text too long for its space scrolls a pHAT pixel at a time;
e-paper cuts it short with an ellipsis."""

from __future__ import annotations

import functools
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw, ImageFont

from ..core import words
from ..core.model import Strip
from ..settings import modes as mode_list
from .panel import (
    NOTES_SIDE_BY_SIDE_MIN_WIDTH,
    PHAT,
    PLATFORM_COLUMN_MIN_WIDTH,
    STOP_NAME_MIN_WIDTH,
    STRIP_COLUMN_MIN_WIDTH,
    Panel,
)
from .route import RouteView
from .scroll import Frame, Scroll, frame_of
from .view import BoardView, CodeView, Row, Section, SetupView, Style, destination_text

# The pHAT, which every other size is scaled up from.
WIDTH, HEIGHT = PHAT.size

# Below the header, and either side of the rule between sections.
_SECTION_GAP = 6
PAD = 3
# Beside a section's label when its feed has stopped, in the sign look's words.
_NOT_UPDATING = "! NOT UPDATING"

# Inter, as on the dashboard: hinted static fonts built by tools/build_fonts.py.
# Bold is ExtraBold: at the pHAT's 9 and 11 px on a one-bit panel, Inter Bold
# hints to one-pixel stems and reads as regular.
FONT_DIR = Path(__file__).with_name("fonts")
REGULAR = "Inter-Regular.ttf"
BOLD = "Inter-ExtraBold.ttf"
_PREVIEW_ACCENTS = {"red": (200, 30, 30), "yellow": (230, 180, 0)}
# The colours a screen has that e-paper lacks, for the TFT look's status
# pills: black words on the green (7.8:1), white on the red (5.7:1).
PILL_RGB = {"green": (40, 180, 90), "red": (200, 30, 30)}
# And the split-flap look's tiles: dark grey, so its white letters on them are
# 14:1, amber 7:1, and the black between the tiles still shows.
TILE_RGB = (40, 40, 40)
# Every colour only a screen has, by its Palette name.
SCREEN_ONLY_RGB = {**PILL_RGB, "tile": TILE_RGB}


@dataclass(frozen=True)
class Palette:
    """Panel palette indices. `preview` adds real RGB values for PNG output.
    `green`, `red` and `tile` are a screen's own (SCREEN_ONLY_RGB): None on e-paper."""

    white: int
    black: int
    accent: int
    preview: str | None = None
    green: int | None = None
    red: int | None = None
    tile: int | None = None

    def with_preview(self, accent_name: str) -> Palette:
        return replace(self, preview=accent_name)


# A screen's palette: e-paper's three colours, and those only a screen has.
SCREEN_PALETTE = Palette(white=0, black=1, accent=2, green=3, red=4, tile=5)


@dataclass(frozen=True)
class Fonts:
    regular: ImageFont.FreeTypeFont
    bold: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont
    # Room either side of a row's status, in panel pixels, for a look that
    # draws it in a pill (the TFT look); none in the plain look.
    pill: int = 0


# A screen redraws every second for its clock, and FreeType hints each glyph
# afresh every time text is measured or drawn: that was most of a plain-look
# redraw, 85 ms on a laptop and near a second on a Pi Zero 2 W, where it held
# up scrolling text. So the fonts, what text measures and the glyphs it draws
# are kept between redraws, each in a bounded store: a Pi Zero has 512 MB.
# The board is drawn on one thread, so sharing the fonts is safe.
#
# Always Pillow's basic layout: left to choose, Pillow uses raqm wherever it
# can load FriBiDi (Debian's Pillow on a Pi, Ubuntu), which measures text
# differently, so what fits a row would change from one computer to another.


@functools.lru_cache(maxsize=32)
def load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size, layout_engine=ImageFont.Layout.BASIC)


# Body text in pHAT pixels; a text_size setting can make the plain look bigger.
TEXT_SIZE = 11


def load_fonts(scale: int = 1, text_size: int = TEXT_SIZE) -> Fonts:
    return Fonts(
        regular=load_font(REGULAR, text_size * scale),
        bold=load_font(BOLD, text_size * scale),
        small=load_font(BOLD, (text_size - 2) * scale),
    )


def heights(text_size: int = TEXT_SIZE) -> tuple[int, int, int]:
    """(header, section label, row) heights for the plain look's text size;
    at the standard 11 they are the pHAT design's 15, 12 and 14."""
    return text_size + 4, text_size + 1, text_size + 3


@functools.lru_cache(maxsize=2048)
def _drawn_box(font: ImageFont.FreeTypeFont, text: str) -> tuple[float, float, float, float]:
    """The box `text` is drawn in, as hinted for one-bit output."""
    return font.getbbox(text, mode="1")


def drawn_length(font: ImageFont.FreeTypeFont, text: str) -> float:
    """How far `text` reaches when drawn. The panels' images are paletted, so
    glyphs are hinted for one-bit output and can come out a few pixels wider
    than getlength() says; right-aligned text measured that way runs into the edge."""
    return _drawn_box(font, text)[2] if text else 0


def status_width(fonts: Fonts, text: str) -> float:
    """How wide a row's status is drawn: its words, and its pill if it has one."""
    return drawn_length(fonts.bold, text) + 2 * fonts.pill


@functools.lru_cache(maxsize=2048)
def text_length(font: ImageFont.FreeTypeFont, text: str) -> float:
    """font.getlength(text), kept between redraws."""
    return font.getlength(text)


class _Kept:
    """Values by key, the least recently used dropped once they take more
    than `most` bytes between them. A value bigger than a quarter of that
    isn't kept: it would push out everything else."""

    def __init__(self, most: int) -> None:
        self.most = most
        self.taken = 0
        self._values: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, make: Callable[[], tuple[Any, int]]) -> Any:
        """The value kept for `key`, or `make`'s (value, bytes) kept and given."""
        with self._lock:
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key][0]
        value, size = make()
        if size > self.most // 4:
            return value
        with self._lock:
            if key not in self._values:
                self._values[key] = (value, size)
                self.taken += size
            while self.taken > self.most:
                _, (_, dropped) = self._values.popitem(last=False)
                self.taken -= dropped
        return value


# A board's text is a few hundred kilobytes of glyphs on the bar screen, a
# long notice up to a few hundred more.
_GLYPHS = _Kept(4 * 1024 * 1024)


def _glyphs(font: ImageFont.FreeTypeFont, text: str, start: tuple[float, float]) -> tuple[Image.Image, int, int]:
    """`text` as ImageDraw.text draws it starting `start` (fractions of a
    pixel) along and down: a one-bit mask of its glyphs, and where the mask's
    top left goes from the whole pixel it is drawn at."""
    left, top, right, bottom = map(int, _drawn_box(font, text))
    # Clear of the text's box by two pixels: starting part way into a pixel can widen it by one.
    x, y = 2 - min(left, 0), 2 - min(top, 0)
    mask = Image.new("1", (x + right + 2, y + bottom + 2), 0)
    ImageDraw.Draw(mask).text((x + start[0], y + start[1]), text, font=font, fill=1)
    return mask, -x, -y


def draw_text(image: Image.Image, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont,
              fill: int) -> None:
    """ImageDraw.text, pixel for pixel, from glyphs kept between redraws."""
    if not text:
        return
    if xy[0] < 0 or xy[1] < 0:  # not expected on a board: drawn the slow way
        ImageDraw.Draw(image).text(xy, text, font=font, fill=fill)
        return
    start = (math.modf(xy[0])[0], math.modf(xy[1])[0])

    def make() -> tuple[tuple[Image.Image, int, int], int]:
        glyphs = _glyphs(font, text, start)
        return glyphs, glyphs[0].width * glyphs[0].height  # one byte a pixel, even in mode "1"

    mask, dx, dy = _GLYPHS.get((font, text, start), make)
    image.paste(fill, (int(xy[0]) + dx, int(xy[1]) + dy), mask)


def left_column_width(fonts: Fonts, scale: int = 1) -> float:
    return text_length(fonts.bold, "00:00") + 5 * scale


@dataclass(frozen=True)
class RowLayout:
    left: str
    middle: str
    middle_x: float
    leave_x: float | None
    right_x: float
    platform_x: float | None = None
    # The leave text as drawn: the time alone under a words.LEAVE_BY heading.
    leave: str | None = None
    # Where its train or bus is, as drawn, and its little line; None on a row
    # without a strip, or a board without the column.
    where: str | None = None
    where_x: float | None = None
    strip_x: float | None = None
    # How wide the left, middle and where columns are: a screen scrolls text
    # too long for them in that space, where e-paper cuts it short.
    left_room: float = 0
    middle_room: float = 0
    where_room: float = 0
    # On a screen wide enough, the stop's name (or its label, if the name
    # isn't known) as drawn, in a column of its own before the destination,
    # which is then the destination alone; None on other boards.
    stop: str | None = None
    stop_x: float | None = None
    stop_room: float = 0
    # A board wide enough for a platform column, where a train's via words
    # follow its destination.
    wide: bool = False


_COL_GAP = 6
# On a screen wide enough, in pHAT pixels: a little line of stops, then the
# where words in the small type.
STRIP_WIDTH = 48


def _with_stop(row: Row) -> str:
    return f"{row.stop}: {row.middle}" if row.stop else row.middle


def _middle(row: Row, stop_column: bool, platform_column: bool, wide: bool) -> str:
    if stop_column:
        return row.middle
    return _with_stop(row) if row.stop else destination_text(row, platform_column, wide)


def middle_text(row: Row, layout: RowLayout) -> str:
    """The row's middle text in full: the destination, after its stop's
    label ("A: Hyde") unless the stop has a column of its own, or after
    "Bus: " for a replacement bus with no platform column to say so, and
    a train's via words after it on a board wide enough."""
    return _middle(row, layout.stop_x is not None, layout.platform_x is not None, layout.wide)


def _leave_text(row: Row) -> str | None:
    return row.leave_at or row.leave


def leave_heading(rows) -> str | None:
    """The words over a section's leave column, when its rows give the time alone."""
    return words.LEAVE_BY if any(r.leave_at for r in rows) else None


def row_layout(
    row: Row,
    fonts: Fonts,
    width: int = WIDTH,
    scale: int = 1,
    platform_x: float | None = None,
    leave_x: float | None = None,
    strip_x: float | None = None,
    stop_room: float | None = None,
) -> RowLayout:
    """Column positions for a row. The destination gives way so times stay whole.
    `platform_x` and `leave_x` are where a section's platform and leave
    columns start, if it has them; without `leave_x` a leave time sits just
    left of the row's own status. `strip_x` is where the little line starts,
    with the where words after it, on a board with that column (strip_column).
    `stop_room` is how wide a bus row's stop names are, on a board that
    names them (stop_column)."""
    pad, gap = PAD * scale, _COL_GAP * scale
    right_x = width - pad - fonts.pill - drawn_length(fonts.bold, row.right)
    status_x = right_x - fonts.pill  # where the status starts, with its pill
    middle_x = pad + left_column_width(fonts, scale)
    left_room = middle_x - pad - 3 * scale
    stop = stop_x = None
    if stop_room is not None:
        stop_x, whole_stop = middle_x, row.stop_name or row.stop
        if whole_stop:
            stop = fit_text(whole_stop, fonts.regular, stop_room, partial(drawn_length, fonts.regular))
        # Twice the gap, so the stop and the destination don't read as one name.
        middle_x = stop_x + stop_room + 2 * gap
    leave = _leave_text(row)
    middle_end = status_x - 4 * scale
    if leave:
        if leave_x is None:
            leave_x = status_x - gap - drawn_length(fonts.small, leave)
        middle_end = leave_x - gap
    else:
        leave_x = None
    if platform_x is not None:
        middle_end = platform_x - gap
    where = where_x = None
    words_w = 0.0
    if strip_x is not None:
        # Twice the gap, so the destination and the little line don't run together.
        middle_end = strip_x - 2 * gap
        if row.strip is not None:
            # The where words after the line, to twice the gap short of the
            # row's next column, so "Left Glossop" and "Plat 1" don't read as one.
            next_x = platform_x if platform_x is not None else (leave_x if leave_x is not None else status_x)
            where_x = strip_x + STRIP_WIDTH * scale + gap
            words_w = max(0.0, next_x - 2 * gap - where_x)
            where = fit_text(row.strip.where, fonts.small, words_w, partial(drawn_length, fonts.small))
        else:
            strip_x = None
    wide = width / scale >= PLATFORM_COLUMN_MIN_WIDTH
    middle = _middle(row, stop_x is not None, platform_x is not None and bool(row.platform), wide)
    return RowLayout(
        # Measured as drawn, like the right-aligned text, so a cut destination
        # doesn't run into the status.
        left=fit_text(row.left, fonts.bold, left_room, partial(drawn_length, fonts.bold)),
        middle=fit_text(middle, fonts.regular, middle_end - middle_x, partial(drawn_length, fonts.regular)),
        middle_x=middle_x,
        leave_x=leave_x,
        right_x=right_x,
        platform_x=platform_x if row.platform else None,
        leave=leave,
        where=where or None,
        where_x=where_x,
        strip_x=strip_x,
        left_room=left_room,
        middle_room=middle_end - middle_x,
        where_room=words_w,
        stop=stop,
        stop_x=stop_x,
        stop_room=stop_room or 0,
        wide=wide,
    )


def platform_column(rows, fonts: Fonts, width: int, scale: int) -> float | None:
    """Where a section's platform column starts: only on a board wide enough
    for one, and only if some row has a platform."""
    if width / scale < PLATFORM_COLUMN_MIN_WIDTH or not any(r.platform for r in rows):
        return None
    right_w = max(status_width(fonts, r.right) for r in rows)
    platform_w = max(drawn_length(fonts.regular, r.platform) for r in rows if r.platform)
    return width - PAD * scale - right_w - _COL_GAP * scale - platform_w


def leave_column(rows, fonts: Fonts, width: int, scale: int) -> float | None:
    """Where a section's leave times start: one column, clear of the widest
    status, so the times line up under the heading. None if no row has one."""
    leaves = [text for text in map(_leave_text, rows) if text]
    if not leaves:
        return None
    right_w = max(status_width(fonts, r.right) for r in rows)
    leave_w = max(drawn_length(fonts.small, text) for text in leaves)
    return width - PAD * scale - right_w - _COL_GAP * scale - leave_w


# On a screen wide enough, a bus row's stop by its name, in a column as wide
# as the longest of your stops' names, as far as the ladder gives it room,
# and never narrower than "Henry St A"; a longer name scrolls in it.
_STOP_NAME_LEAST = "Henry St A"
# A row's room between its lead (time, or route and stop) and its section's
# next column is shared out by a ladder (share_out), in turn: the where words
# "3 stops away"; a bus's stop names "Henry St A"; the destination its
# target, a train's (or tram's) "Manchester Piccadilly", a bus's
# "Ashton-under-Lyne"; the stop names the longest whole; the destination
# "Heathrow Terminals 2 & 3"; the where words "Left Manchester Oxford Road";
# the destination the rest. The sign look shares its rows out the same way.
_WHERE_LEAST = "3 stops away"
_WHERE_MOST = "Left Manchester Oxford Road"
_DEST_TARGET = {"train": "Manchester Piccadilly", "bus": "Ashton-under-Lyne"}
_DEST_MOST = "Heathrow Terminals 2 & 3"


def share_out(room: float, steps) -> dict[str, float]:
    """`room` shared out by a ladder of `steps`, each (field, width): in
    turn, each gives its field ("words", "names" or "destination") more
    room, up to that width in all, until the room runs out. The destination
    has whatever is left at the end."""
    got = {"words": 0.0, "names": 0.0, "destination": 0.0}
    for name, most in steps:
        give = max(0.0, min(most - got[name], room))
        got[name] += give
        room -= give
    got["destination"] += max(0.0, room)
    return got


def _names_wanted(rows, stop_names, fonts: Fonts, width: int, scale: int, live: bool) -> float | None:
    """How wide a section's stop names want to be, the longest whole, or None
    where its rows show their labels ("A: Hyde"): e-paper (not `live`), a
    board too narrow, no names known. From all your stops' names
    (`stop_names`), not the rows shown, so the destinations stay put; a row
    whose stop has no name shows its label in the column."""
    if not live or width / scale < STOP_NAME_MIN_WIDTH or not stop_names:
        return None
    labels = [r.stop for r in rows if r.stop and not r.stop_name]
    return max(drawn_length(fonts.regular, text) for text in [*stop_names, *labels])


def stop_column(rows, stop_names, fonts: Fonts, width: int, scale: int, live: bool,
                dest_end: float) -> float | None:
    """How wide a section's stop names are drawn, in panel pixels, or None
    where its rows show their labels (_names_wanted), for destinations that
    end at `dest_end`: as wide as the longest, as far as the bus
    destination's target leaves room, and never narrower than _STOP_NAME_LEAST."""
    wanted = _names_wanted(rows, stop_names, fonts, width, scale, live)
    if wanted is None:
        return None
    stop_x = PAD * scale + left_column_width(fonts, scale)
    room = dest_end - drawn_length(fonts.regular, _DEST_TARGET["bus"]) - 2 * _COL_GAP * scale - stop_x
    return min(wanted, max(drawn_length(fonts.regular, _STOP_NAME_LEAST), room))


def section_end(rows, fonts: Fonts, width: int, scale: int) -> float:
    """Where a section's first column after the destination starts: its
    platforms, its leave times, or failing both its widest status."""
    ends = [width - PAD * scale - max(status_width(fonts, r.right) for r in rows)]
    ends += [x for x in (platform_column(rows, fonts, width, scale), leave_column(rows, fonts, width, scale))
             if x is not None]
    return min(ends)


def _own_strip_x(section: Section, rows, fonts: Fonts, width: int, scale: int) -> float:
    """Where a section's little lines start against its own columns, given
    its rows as drawn: after what the ladder gives its destinations (and a
    bus's stop names) of the row's room, the where words having their share
    before the next column."""
    gap = _COL_GAP * scale
    small, regular = partial(drawn_length, fonts.small), partial(drawn_length, fonts.regular)
    lead = PAD * scale + left_column_width(fonts, scale)
    names = _names_wanted(rows, section.stop_names, fonts, width, scale, True) if section.kind == "bus" else None
    target = regular(_DEST_TARGET[section.kind])
    if names is not None:
        lead += 2 * gap
        steps = [("words", small(_WHERE_LEAST)), ("names", min(names, regular(_STOP_NAME_LEAST))),
                 ("destination", target), ("names", names)]
    else:
        if section.kind == "bus" and any(r.stop for r in rows):
            lead += max(regular(f"{r.stop}: ") for r in rows if r.stop)
        steps = [("words", small(_WHERE_LEAST)), ("destination", target)]
    steps += [("destination", regular(_DEST_MOST)), ("words", small(_WHERE_MOST))]
    # Twice the gap either side of the line and after the words, as row_layout keeps them.
    words_end = section_end(rows, fonts, width, scale) - 2 * gap
    room = words_end - (2 * gap + STRIP_WIDTH * scale + gap) - lead
    got = share_out(room, steps)
    return lead + got["names"] + got["destination"] + 2 * gap


def strip_column(sections, drawn, fonts: Fonts, width: int, scale: int, live: bool) -> list[float | None]:
    """Where each section's little lines start, the where words after them,
    given each section's rows as drawn (`drawn`); None for a section without
    one among them. Only on a screen (`live`) wide enough: a strip changes
    every few seconds, and e-paper redraws slowly, only on change. Each
    against its own section's columns."""
    if not live or width / scale < STRIP_COLUMN_MIN_WIDTH:
        return [None] * len(sections)
    return [_own_strip_x(section, rows, fonts, width, scale) if any(r.strip for r in rows) else None
            for section, rows in zip(sections, drawn, strict=True)]


def draw_strip(draw: ImageDraw.ImageDraw, strip: Strip, train: bool, x: float, y: int, scale: int, row_h: int,
               ink: int, paper: int, yours_fill: int) -> None:
    """A row's little line, as the dashboard's row strip draws it: a line
    through the stops, solid up to yours and dashed after it; stops before
    yours filled, yours bigger and in `yours_fill`, those after it hollow; and
    the train or bus where it is, none when that isn't known. Drawing only:
    the words beside it say where the vehicle is. `y` is the row's top."""
    s = scale
    line_y = y + (row_h - 4) * s
    left, right = x + 4 * s, x + (STRIP_WIDTH - 4) * s
    step = (right - left) / (strip.stops - 1)
    xs = [left + i * step for i in range(strip.stops)]
    yours = xs[strip.yours]
    draw.line((xs[0], line_y, yours, line_y), fill=ink, width=s)
    dash = 2 * s
    for start in range(round(yours), round(xs[-1]), 2 * dash):
        draw.line((start, line_y, min(start + dash, xs[-1]), line_y), fill=ink, width=s)
    for i, cx in enumerate(xs):
        if i == strip.yours:
            r = 3 * s
            draw.ellipse((cx - r, line_y - r, cx + r, line_y + r), fill=yours_fill, outline=ink, width=s)
        else:
            r = 2 * s
            draw.ellipse((cx - r, line_y - r, cx + r, line_y + r), fill=ink if i < strip.yours else paper,
                         outline=ink, width=s)
    if strip.at is None:
        return
    vx = left + strip.at * step
    top = max(y, line_y - 9 * s)  # inside the row, even with small text
    # A bus square, a train rounder with a windscreen, so the two differ by
    # shape; each points down at the line.
    if train:
        draw.rounded_rectangle((vx - 5 * s, top, vx + 5 * s, top + 5 * s), radius=2 * s, fill=ink)
        draw.rectangle((vx - 3 * s, top + s, vx + 3 * s, top + 2 * s), fill=paper)
    else:
        draw.rounded_rectangle((vx - 6 * s, top, vx + 6 * s, top + 5 * s), radius=s, fill=ink)
    draw.polygon([(vx - 2 * s, top + 5 * s), (vx + 2 * s, top + 5 * s), (vx, top + 7 * s)], fill=ink)


class Measurable(Protocol):
    """A font fit_text can measure with: FreeTypeFont, or the sign look's DotFont."""

    def getlength(self, text: str) -> float: ...


def _longest_fit(low: int, high: int, fits: Callable[[int], bool]) -> int:
    """The largest n from `low` to `high` that fits, or `low` if none does.
    A halving search: text only gets wider as it gets longer, so where it
    stops fitting can be found in a few measures rather than one per
    character (drawn_length is slow, and a notice can be hundreds long)."""
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def fit_text(text: str, font: Measurable, width: float, measure=None) -> str:
    """`text`, cut short with an ellipsis if it is wider than `width`.
    `measure` overrides font.getlength, e.g. with drawn_length."""
    if measure is None:
        measure = partial(text_length, font) if isinstance(font, ImageFont.FreeTypeFont) else font.getlength
    if measure(text) <= width:
        return text
    keep = _longest_fit(0, len(text) - 1, lambda n: measure(text[:n] + "…") <= width)
    return text[:keep].rstrip() + "…"


NOTICE_LEAD = "! "


def note_text(view: BoardView) -> str | None:
    """The disruption notice, marked as one, or failing one the rain warning,
    or failing both when the last bus goes. Too long for its space, it
    scrolls on a screen and is cut short on e-paper."""
    if view.notice:
        return NOTICE_LEAD + view.notice
    return view.rain or view.last_bus


def plain_scale(panel: Panel) -> int:
    """Whole steps up from the pHAT design that still fit the panel's height:
    the 10.85" panel's dot scale of 4 would need 488 of its 480 rows."""
    return max(1, min(panel.scale, panel.height // HEIGHT))


def calling_height(text_size: int = TEXT_SIZE) -> int:
    """How tall a screen's "Calling at" line is, in pHAT pixels: a line of
    the small type, a pixel less than the text size, so both HDMI screens
    keep every row they show without it."""
    return text_size - 1


def calling_line(section: Section, live: bool) -> str | None:
    """The first train's "Calling at" line, on a screen (`live`), which
    scrolls it under the train's row; None on e-paper, which can't."""
    if not live or section.kind != "train" or not section.rows:
        return None
    return section.rows[0].calling


def calling_section(sections: tuple[Section, ...], live: bool) -> str | None:
    """The mode whose first train has its "Calling at" line drawn under it:
    the first section with one. A board has room for one."""
    return next((section.mode for section in sections if calling_line(section, live)), None)


def share_rows(rows: int, modes: tuple[str, ...], epaper: bool) -> dict[str, int]:
    """`rows` shared among the sections of `modes`, in order: a row each,
    then each in turn as many more as it takes at most while it shares the
    board (modes.Mode.most_rows), the first first. A mode alone takes them all."""
    if len(modes) == 1:
        return {modes[0]: rows}
    shared = dict.fromkeys(modes, 1)
    left = rows - len(modes)
    for mode in modes:
        more = max(0, min(mode_list.mode(mode).most_rows[0 if epaper else 1] - 1, left))
        shared[mode] += more
        left -= more
    return shared


def row_limits(panel: Panel, text_size: int = TEXT_SIZE, calling: bool = False,
               modes: tuple[str, ...] = ("train", "bus")) -> dict[str, int]:
    """Rows each mode's section shows: 3 trains and 2 buses on the pHAT's
    122 rows, fewer on a shorter panel (the original pHAT has 104) or with
    bigger text, rather than cut off; at least one each. `calling` makes
    room for the "Calling at" line."""
    if not modes:
        return {}
    rows = max(len(modes), rows_room(panel, len(modes), text_size, calling))
    return share_rows(rows, modes, panel.epaper)


def rows_room(panel: Panel, sections: int, text_size: int = TEXT_SIZE, calling: bool = False) -> int:
    """How many whole rows fit on `panel` beside the header and `sections`
    sections' labels, with the rule and its gaps between each two, and with
    `calling` the "Calling at" line."""
    s = plain_scale(panel)
    header_h, label_h, row_h = heights(text_size)
    fixed = header_h + _SECTION_GAP + sections * label_h + (sections - 1) * (1 + _SECTION_GAP)
    if calling:
        fixed += calling_height(text_size)
    return (panel.height // s - fixed) // row_h


def header_rain(view: BoardView, fonts: Fonts, width: int, scale: int) -> tuple[str, float] | None:
    """(rain warning, its x) when a screen's header shows it beside a
    disruption notice, left of the clock: only on a screen wide enough, with
    both. Otherwise the notice takes the title's place alone, as on e-paper."""
    if not (view.clock and view.notice and view.rain) or width / scale < NOTES_SIDE_BY_SIDE_MIN_WIDTH:
        return None
    stamp_x = width - PAD * scale - drawn_length(fonts.regular, view.clock)
    # Twice the usual gap either side, so the notice, the warning and the clock read as three.
    return view.rain, stamp_x - 12 * scale - drawn_length(fonts.bold, view.rain)


def header_field(view: BoardView, fonts: Fonts, width: int, scale: int) -> tuple[str, float]:
    """(the header's left text in full, the room for it). The plain look has
    no footer, so a disruption notice, or else a rain warning, or else the
    last bus, takes the title's place while there is one. A screen wide enough shows both, the
    rain warning beside (header_rain)."""
    pad = PAD * scale
    stamp = view.clock or f"Upd {view.updated}"
    room = width - pad - drawn_length(fonts.regular, stamp) - pad - 6 * scale
    rain = header_rain(view, fonts, width, scale)
    if rain is not None:
        room = rain[1] - pad - 12 * scale
        view = replace(view, rain=None)
    return note_text(view) or view.title, room


def header_text(view: BoardView, fonts: Fonts, width: int, scale: int) -> tuple[str, str, float]:
    """(left text, stamp, stamp x) for the header bar, the left text cut
    short if it must be (a screen scrolls it instead: see header_field)."""
    pad = PAD * scale
    stamp = view.clock or f"Upd {view.updated}"
    stamp_x = width - pad - drawn_length(fonts.regular, stamp)
    text, room = header_field(view, fonts, width, scale)
    if note_text(view):
        return fit_text(text, fonts.bold, room, partial(drawn_length, fonts.bold)), stamp, stamp_x
    return fit_text(text, fonts.bold, room), stamp, stamp_x


def apply_preview(image: Image.Image, palette: Palette) -> None:
    """Real colours for a PNG preview; a panel's own image keeps its indices."""
    if not palette.preview:
        return
    colours = [(0, 0, 0)] * 256
    colours[palette.white] = (255, 255, 255)
    colours[palette.black] = (0, 0, 0)
    colours[palette.accent] = _PREVIEW_ACCENTS[palette.preview]
    for name, rgb in SCREEN_ONLY_RGB.items():
        index = getattr(palette, name)
        if index is not None:
            colours[index] = rgb
    image.putpalette([v for rgb in colours for v in rgb])


class Painter:
    """Draws the plain look's pages. `dark` draws them light on black, for a
    look built on this one (the TFT and split-flap looks, board/tft.py and
    board/splitflap.py)."""

    def __init__(self, palette: Palette, fonts: Fonts, panel: Panel, text_size: int = TEXT_SIZE,
                 live: bool = False, dark: bool = False, scroll: bool = True) -> None:
        self.p = palette
        self.f = fonts
        self.s = plain_scale(panel)
        self.text_size = text_size
        self.limits = row_limits(panel, text_size)
        # The mode whose first train's "Calling at" line is drawn, if any (calling_section).
        self.calling_for: str | None = None
        self.header_h, self.label_h, self.row_h = heights(text_size)
        self.calling_h = calling_height(text_size)
        # Smaller type sits on the same baseline as the row text.
        self.small_drop = 3
        self.width = panel.width
        # What the board is drawn on, and in.
        self.paper, self.ink = (palette.black, palette.white) if dark else (palette.white, palette.black)
        self.image = Image.new("P", panel.size, self.paper)
        self.draw = ImageDraw.Draw(self.image)
        # A screen; with `scroll` (the scrolling setting), text cut short for
        # its space scrolls there instead.
        self.live = live
        self.scroll = live and scroll
        self.scrolls: list[Scroll] = []

    def field(self, xy: tuple[float, float], shown: str, whole: str, font: ImageFont.FreeTypeFont, room: float,
              fill: int, paper: int, band: tuple[int, int]) -> None:
        """Text in a space `room` wide from xy: `shown` is `whole` as it fits
        there, cut short if it must be. On a screen that scrolls, cut-short
        text scrolls whole instead, clipped to the space and to `band` (top and bottom of
        its row), on the row's `paper`."""
        if not self.scroll or shown == whole or room < 1:
            self.text(xy, shown, font, fill)
            return
        x, (top, bottom) = int(xy[0]), band
        strip = Image.new("P", (max(math.ceil(drawn_length(font, whole)), int(room)), bottom - top), paper)
        self.text((xy[0] - x, xy[1] - top), whole, font, fill, strip)
        apply_preview(strip, self.p)
        self.scrolls.append(Scroll((x, top, x + int(room), bottom), strip, self.s, whole))

    def text(self, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill: int,
             image: Image.Image | None = None) -> None:
        """`text` drawn from `xy` on the board, or on `image` (a scrolling
        field's strip): every word a page draws goes through here, so a look
        can draw its letters its own way (the split-flap look, board/flap.py)."""
        draw_text(self.image if image is None else image, xy, text, font, fill)

    def frame(self) -> Frame:
        self.apply_preview_palette()
        return frame_of(self.image, self.scrolls)

    def header(self, view: BoardView) -> None:
        s = self.s
        title, stamp, stamp_x = header_text(view, self.f, self.width, s)
        whole, room = header_field(view, self.f, self.width, s)
        self.banner(title, whole, room, stamp, stamp_x, header_rain(view, self.f, self.width, s))

    def banner(self, shown: str, whole: str, room: float, stamp: str, stamp_x: float,
               rain: tuple[str, float] | None = None) -> None:
        """The header bar, reversed out: the title (or notice) on the left,
        `shown` as it fits `room`, the rain warning if `rain` places it, and
        the stamp (the clock, on a screen) on the right."""
        s, pad = self.s, PAD * self.s
        bottom = self.header_h * s
        self.draw.rectangle((0, 0, self.width - 1, bottom - 1), fill=self.ink)
        self.field((pad, s), shown, whole, self.f.bold, room, self.paper, self.ink, (0, bottom))
        if rain is not None:
            self.text((rain[1], s), rain[0], self.f.bold, self.paper)
        self.text((stamp_x, s), stamp, self.f.regular, self.paper)

    def drawn_rows(self, section: Section) -> tuple[Row, ...]:
        return section.rows[: self.limits.get(section.mode, 1)]

    def section(self, section: Section, y: int, strip_x: float | None = None) -> int:
        s, pad = self.s, PAD * self.s
        label = section.label.upper()
        self.text((pad, y + s), label, self.f.small, self.ink)
        label_end = pad + text_length(self.f.small, label)
        if section.stale:
            x = label_end + 6 * s
            self.text((x, y + s), _NOT_UPDATING, self.f.small, self.p.accent)
            label_end = x + text_length(self.f.small, _NOT_UPDATING)
        rows = self.drawn_rows(section)
        # As on the sign, a leave time for a row that doesn't fit is left out.
        if section.leave and (section.leave_for is None or section.leave_for < len(rows)):
            room = self.width - pad - label_end - 8 * s
            leave = fit_text(section.leave, self.f.small, room)
            # Right-aligned; if it scrolls, its space reaches back to the label.
            scrolls = self.scroll and leave != section.leave
            x = self.width - pad - (room if scrolls else drawn_length(self.f.small, leave))
            self.field((x, y + s), leave, section.leave, self.f.small, room, self.ink, self.paper,
                       (y, y + self.label_h * s))
        leave_x = leave_column(rows, self.f, self.width, s)
        heading = leave_heading(rows)
        if heading and leave_x is not None:
            if leave_x - label_end >= 6 * s and leave_x + drawn_length(self.f.small, heading) <= self.width - pad:
                # Said once, over the column, as station boards label theirs.
                self.text((leave_x, y + s), heading, self.f.small, self.ink)
            else:
                # No room beside the label (a "! NOT UPDATING" on a small panel): each row says it in full.
                rows = tuple(replace(r, leave_at=None) for r in rows)
                leave_x = leave_column(rows, self.f, self.width, s)
        y += self.label_h * s
        if not rows:
            self.text((pad, y + s), section.empty_text, self.f.regular, self.ink)
            return y + self.row_h * s
        platform_x = platform_column(rows, self.f, self.width, s)
        dest_end = (strip_x if strip_x is not None else section_end(rows, self.f, self.width, s)) - 2 * _COL_GAP * s
        stop_room = stop_column(rows, section.stop_names, self.f, self.width, s, self.live, dest_end)
        calling = calling_line(section, self.live) if section.mode == self.calling_for else None
        for index, row in enumerate(rows):
            self.row(row, y, platform_x, leave_x, strip_x, train=section.kind == "train", stop_room=stop_room)
            y += self.row_h * s
            if index == 0 and calling:
                y = self.calling(calling, y)
        return y

    def calling(self, text: str, y: int) -> int:
        """The "Calling at" line at `y`, under the first train: in the small
        type from the destinations' column to the edge, scrolling when it is
        too long for that. Returns where the next row starts."""
        s, pad = self.s, PAD * self.s
        x = pad + left_column_width(self.f, s)
        room = self.width - pad - x
        shown = fit_text(text, self.f.small, room, partial(drawn_length, self.f.small))
        # A pixel up, so the small type's capitals and descenders fit its line.
        bottom = y + self.calling_h * s
        self.field((x, y - s), shown, text, self.f.small, room, self.ink, self.paper, (y, bottom))
        return bottom

    def row(self, row: Row, y: int, platform_x: float | None = None, leave_x: float | None = None,
            strip_x: float | None = None, train: bool = True, stop_room: float | None = None) -> None:
        """A row, a cancelled one reversed out and a late one's status in the accent."""
        s = self.s
        ink, status_ink, paper, yours_fill = self.ink, self.ink, self.paper, self.p.accent
        if row.style is Style.INVERTED:
            self.draw.rectangle((0, y, self.width - 1, y + self.row_h * s - 1), fill=self.ink)
            ink = status_ink = yours_fill = self.paper
            paper = self.ink
        elif row.style is Style.ALERT:
            status_ink = self.p.accent
        layout = row_layout(row, self.f, self.width, s, platform_x, leave_x, strip_x, stop_room)
        self.row_text(row, layout, y, train, ink, paper, yours_fill)
        self.text((layout.right_x, y + s), row.right, self.f.bold, status_ink)

    def row_text(self, row: Row, layout: RowLayout, y: int, train: bool, ink: int, paper: int, yours_fill: int,
                 time_ink: int | None = None) -> None:
        """Everything in a row but its status: in `ink` on `paper`, its time
        in `time_ink` if given, and the strip's stop in `yours_fill`."""
        s = self.s
        band = (y, y + self.row_h * s)
        self.field((PAD * s, y + s), layout.left, row.left, self.f.bold, layout.left_room,
                   ink if time_ink is None else time_ink, paper, band)
        if layout.stop_x is not None and layout.stop:
            self.field((layout.stop_x, y + s), layout.stop, row.stop_name or row.stop or "", self.f.regular,
                       layout.stop_room, ink, paper, band)
        self.field((layout.middle_x, y + s), layout.middle, middle_text(row, layout), self.f.regular,
                   layout.middle_room, ink, paper, band)
        # row_layout only places a platform or leave time the row has.
        if layout.platform_x is not None and row.platform:
            # A changed platform says so in words; the accent only backs them up,
            # on any row not reversed out.
            platform_ink = self.p.accent if row.platform_changed and paper == self.paper else ink
            self.text((layout.platform_x, y + s), row.platform, self.f.regular, platform_ink)
        if layout.leave_x is not None and layout.leave:
            self.text((layout.leave_x, y + self.small_drop * s), layout.leave, self.f.small, ink)
        if layout.where_x is not None and layout.where and row.strip is not None:
            self.field((layout.where_x, y + self.small_drop * s), layout.where, row.strip.where, self.f.small,
                       layout.where_room, ink, paper, band)
        if layout.strip_x is not None and row.strip is not None:
            draw_strip(self.draw, row.strip, train, layout.strip_x, y, s, self.row_h, ink, paper, yours_fill)

    def rule(self, y: int) -> None:
        pad = PAD * self.s
        self.draw.line((pad, y, self.width - pad, y), fill=self.ink, width=self.s)

    def apply_preview_palette(self) -> None:
        apply_preview(self.image, self.p)


def render(view: BoardView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Image.Image:
    """The board, any scrolling text at its start."""
    return board_frame(view, palette, panel, text_size).image


def board_frame(view: BoardView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Frame:
    """The board, and on a screen the text that scrolls."""
    s = plain_scale(panel)
    painter = Painter(palette, load_fonts(s, text_size), panel, text_size, live=view.clock is not None,
                      scroll=view.scroll)
    return paint_board(painter, view, panel)


def paint_board(painter: Painter, view: BoardView, panel: Panel) -> Frame:
    """The board as `painter` draws it: the header, then each mode's
    section, a rule between each and the next."""
    s = painter.s
    painter.calling_for = calling_section(view.sections, painter.live)
    painter.limits = row_limits(panel, painter.text_size, calling=painter.calling_for is not None,
                                modes=tuple(section.mode for section in view.sections))
    painter.header(view)
    drawn = [painter.drawn_rows(section) for section in view.sections]
    strip_xs = strip_column(view.sections, drawn, painter.f, panel.width, s, painter.live)
    y = (painter.header_h + _SECTION_GAP) * s
    for index, section in enumerate(view.sections):
        if index:
            painter.rule(y + s)
            y += (_SECTION_GAP + 1) * s
        y = painter.section(section, y, strip_xs[index])
    return painter.frame()


def render_route(view: RouteView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Image.Image:
    """The bus's position on the stops before yours, in the plain look."""
    return route_frame(view, palette, panel, text_size).image


def route_frame(view: RouteView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Frame:
    """The route page, and on a screen the text that scrolls."""
    s = plain_scale(panel)
    painter = Painter(palette, load_fonts(s, text_size), panel, text_size, live=view.clock is not None,
                      scroll=view.scroll)
    return paint_route(painter, view, panel)


def paint_route(painter: Painter, view: RouteView, panel: Panel) -> Frame:
    """The route page as `painter` draws it."""
    s, f, draw = painter.s, painter.f, painter.draw
    pad = PAD * s
    ink, paper = painter.ink, painter.paper
    width, height = panel.size

    stamp = view.clock or ""
    stamp_x = width - pad - drawn_length(f.regular, stamp)
    room = stamp_x - pad * 3
    painter.banner(fit_text(view.title, f.bold, room), view.title, room, stamp, stamp_x)

    y = (painter.header_h + 4) * s
    lead = f"{view.due}   {view.leave}" if view.leave else view.due
    painter.text((pad, y), lead, f.bold, ink)
    painter.text((width - pad - drawn_length(f.bold, view.where), y), view.where, f.bold, ink)

    row_h = painter.row_h
    road = (y + row_h * s + height) // 2
    left, right = pad * 3, width - pad * 3
    step = (right - left) / (len(view.stops) - 1)
    xs = [left + i * step for i in range(len(view.stops))]
    draw.line((xs[0], road, xs[-1], road), fill=ink, width=2 * s)
    for i, (name, x) in enumerate(zip(view.stops, xs, strict=True)):
        yours = i == len(xs) - 1
        r = (5 if yours else 3) * s
        draw.ellipse((x - r, road - r, x + r, road + r), fill=ink if yours else paper, outline=ink, width=s)
        font = f.bold if yours else f.regular
        room = step * 2 - _COL_GAP * s
        text = fit_text(name, font, room)
        if painter.scroll and text != name:
            tx = min(max(pad, x - room / 2), width - pad - room)  # a scrolling name takes its whole room
        else:
            tx = min(max(pad, x - text_length(font, text) / 2), width - pad - drawn_length(font, text))
        # Names above the road clear the bus, which sits on the line.
        ty = road - 30 * s if i % 2 == 0 else road + 9 * s
        painter.field((tx, ty), text, name, font, room, ink, paper, (int(ty), int(ty) + row_h * s))
    if view.bus_at is not None:
        bx = xs[0] + view.bus_at * step
        draw.rounded_rectangle((bx - 12 * s, road - 13 * s, bx + 12 * s, road - 4 * s), radius=2 * s, fill=ink)
        for wx in range(-9, 9, 5):
            draw.rectangle((bx + wx * s, road - 11 * s, bx + (wx + 3) * s, road - 8 * s), fill=paper)
    return painter.frame()


# The settings address, in pixels high, below which it is hard to read: then
# it breaks onto two lines instead of shrinking further.
MIN_ADDRESS_PX = 8


def address_lines(url: str, width: float, height: float) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """The settings address as drawn in `width` x `height`: on one line if it
    fits there at MIN_ADDRESS_PX or more, else broken before its port (a
    long board name), both lines at the one size that fits half the height."""
    font = _biggest_bold(url, width, height)
    left, _, right, _ = font.getbbox(url, mode="1")
    cut = url.rfind(":")
    if (font.size >= MIN_ADDRESS_PX and right - left <= width) or cut <= len("https:"):
        return [url], font
    lines = [url[:cut], url[cut:]]
    return lines, min((_biggest_bold(line, width, height / 2) for line in lines), key=lambda f: f.size)


def _biggest_bold(text: str, width: float, height: float) -> ImageFont.FreeTypeFont:
    """The largest bold type `text` fits in `width` x `height` at, as drawn."""
    low, high = 6, max(6, int(height) * 2)
    while low < high:
        size = (low + high + 1) // 2
        left, top, right, bottom = load_font(BOLD, size).getbbox(text, mode="1")
        if right - left <= width and bottom - top <= height:
            low = size
        else:
            high = size - 1
    return load_font(BOLD, low)


def render_code(view: CodeView, palette: Palette, panel: Panel = PHAT, dark: bool = False) -> Image.Image:
    """The settings reset code, as big as the panel allows, with what it is and
    until when above and below it. `dark` draws it light on black, as the sign
    look does; on a mono panel that is white."""
    s = plain_scale(panel)
    pad = PAD * s
    width, height = panel.size
    if dark:
        background = palette.black
        ink = palette.white if palette.accent == palette.black else palette.accent
    else:
        background, ink = palette.white, palette.black
    image = Image.new("P", panel.size, background)
    draw = ImageDraw.Draw(image)
    small = load_font(BOLD, TEXT_SIZE * s)
    label = fit_text("Settings reset code", small, width - 2 * pad, partial(drawn_length, small))
    until = fit_text(f"Valid until {view.until}", small, width - 2 * pad, partial(drawn_length, small))
    line_h = heights()[2] * s
    room = height - 2 * line_h - 4 * pad
    big = _biggest_bold(view.code, width * 0.9, room)
    left, top, right, bottom = big.getbbox(view.code, mode="1")

    def centred(text: str, font, y: float) -> None:
        draw.text(((width - drawn_length(font, text)) / 2, y), text, font=font, fill=ink)

    centred(label, small, pad)
    # The code in the middle of the space between the two lines, by its drawn box.
    middle = line_h + pad + (height - 2 * line_h - 2 * pad) / 2
    draw.text(((width - (right - left)) / 2 - left, middle - (bottom - top) / 2 - top), view.code, font=big, fill=ink)
    centred(until, small, height - line_h - pad)
    apply_preview(image, palette)
    return image


def render_setup(view: SetupView, palette: Palette, panel: Panel = PHAT, dark: bool = False) -> Image.Image:
    """Where to set the board up, as the code page draws its code: the
    heading ("Set me up") big, then the note saying what to do, then the
    settings site's address as big as it fits. `dark` as for render_code."""
    s = plain_scale(panel)
    pad = PAD * s
    width, height = panel.size
    if dark:
        background = palette.black
        ink = palette.white if palette.accent == palette.black else palette.accent
    else:
        background, ink = palette.white, palette.black
    image = Image.new("P", panel.size, background)
    draw = ImageDraw.Draw(image)
    small = load_font(BOLD, TEXT_SIZE * s)
    note = fit_text(view.note, small, width - 2 * pad, partial(drawn_length, small))
    line_h = heights()[2] * s
    # The heading over the top half of what the note leaves; the address below.
    room = height - line_h - 4 * pad
    heading_h, address_h = room * 0.55, room * 0.45

    def boxed(text: str, top: float, band: float, limit: float, font: ImageFont.FreeTypeFont | None = None) -> None:
        """`text` as big as fits `limit` wide and the band high (or in `font`), centred in the band by its drawn box."""
        font = font or _biggest_bold(text, limit, band)
        left, box_top, right, bottom = font.getbbox(text, mode="1")
        x = (width - (right - left)) / 2 - left
        draw.text((x, top + (band - (bottom - box_top)) / 2 - box_top), text, font=font, fill=ink)

    boxed(view.heading, pad, heading_h, width * 0.9)
    note_y = pad + heading_h + pad
    draw.text(((width - drawn_length(small, note)) / 2, note_y), note, font=small, fill=ink)
    limit = width - 2 * pad - 2 * s
    lines, font = address_lines(view.shown, limit, address_h)
    band = address_h / len(lines)
    for i, line in enumerate(lines):
        boxed(line, note_y + line_h + pad + i * band, band, limit, font)
    apply_preview(image, palette)
    return image
