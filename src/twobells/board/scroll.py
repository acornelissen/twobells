"""Text too long for its space, scrolling slowly on a screen.

E-paper cuts such text short with an ellipsis: it can't animate. A screen
shows it from its start, holds it there for SCROLL_PAUSE_S, moves it left a
step at a time until its end is in view, holds that, then starts again. Each
field scrolls inside its own box and is clipped to it.

Where a field has got to comes from a time passed in, the board's own clock,
never the wall clock, so a frame can be drawn for any moment and every field
on the board moves by the same clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from .flaps import Flap

# How long text holds still at its start and again at its end.
SCROLL_PAUSE_S = 2.0
# Steps a second: a step is one LED dot in the sign look, one pHAT pixel
# (the design's unit) in the plain look. A character is about 5 of either
# with its gap, so 15 a second is 3 characters a second: slow enough to
# read walking past, quick enough that a long notice doesn't take all day.
SCROLL_STEPS_PER_S = 15
# A screen draws a frame this often while something scrolls: one step a
# frame, so the text moves steadily rather than in uneven jumps. 15 frames
# divide a second evenly, so they line up with the running clock's ticks,
# and a 60 Hz screen's refreshes too: every step shows for 4 of them (16 a
# second showed for 3 and 4 in turn, which judders).
# Only the moving boxes are drawn between ticks: well under a millisecond a
# frame on a laptop, a few on a Pi Zero 2 W.
SCROLL_FRAME_S = 1 / SCROLL_STEPS_PER_S

Box = tuple[int, int, int, int]  # left, top, right, bottom in panel pixels; right and bottom exclusive


def scroll_steps(overflow_steps: int, seconds: float) -> int:
    """How many steps text `overflow_steps` steps wider than its box has moved
    left, `seconds` into its scroll: none for the first SCROLL_PAUSE_S, then
    one more every 1/SCROLL_STEPS_PER_S until its end is in view, held there
    for SCROLL_PAUSE_S, and round again."""
    if overflow_steps <= 0:
        return 0
    cycle = 2 * SCROLL_PAUSE_S + overflow_steps / SCROLL_STEPS_PER_S
    into = seconds % cycle
    # A whisker over, as a frame's time (k / 15) multiplied back can fall just short of k.
    return min(overflow_steps, max(0, math.floor((into - SCROLL_PAUSE_S) * SCROLL_STEPS_PER_S + 1e-6)))


@dataclass(frozen=True)
class Scroll:
    """A field whose text is wider than its box: the whole text drawn on the
    field's own background (`strip`, as tall as the box), moved `step` panel
    pixels at a time."""

    box: Box
    strip: Image.Image
    step: int
    text: str  # what it says: the same text in the same box keeps scrolling across redraws

    @property
    def width(self) -> int:
        return self.box[2] - self.box[0]

    def offset(self, seconds: float) -> int:
        """How far left the text is moved, in panel pixels, `seconds` into its scroll."""
        overflow = self.strip.width - self.width
        if overflow <= 0:
            return 0
        return min(overflow, self.step * scroll_steps(math.ceil(overflow / self.step), seconds))

    def patch(self, offset: int) -> Image.Image:
        """What the box shows with the text moved `offset` pixels left."""
        return self.strip.crop((offset, 0, offset + self.width, self.box[3] - self.box[1]))


@dataclass(frozen=True)
class Frame:
    """A board as a screen draws it: the still image, every scrolling field
    at its start, and those fields; in the split-flap look, also the fields
    whose tiles turn over when they change (board/flaps.py)."""

    image: Image.Image
    scrolls: tuple[Scroll, ...] = ()
    flaps: tuple[Flap, ...] = ()

    def at(self, seconds: float) -> Image.Image:
        """The board with every field `seconds` into its scroll."""
        image = self.image.copy()
        for scroll in self.scrolls:
            image.paste(scroll.patch(scroll.offset(seconds)), scroll.box[:2])
        return image


def frame_of(image: Image.Image, scrolls: list[Scroll]) -> Frame:
    """The Frame for a board drawn as `image`, each of `scrolls` put in at its start."""
    for scroll in scrolls:
        image.paste(scroll.patch(0), scroll.box[:2])
    return Frame(image, tuple(scrolls))


class Scroller:
    """Keeps a screen's scrolling fields going between redraws. A field whose
    text and box are unchanged carries on where it was; new text starts from
    its beginning. `moved` gives only the fields that have moved since they
    were last drawn, so a screen updates just those boxes."""

    def __init__(self) -> None:
        self._scrolls: tuple[Scroll, ...] = ()
        self._started: dict[tuple[Box, str], float] = {}
        self._drawn: dict[tuple[Box, str], int] = {}

    @property
    def scrolling(self) -> bool:
        return bool(self._scrolls)

    def show(self, frame: Frame, seconds: float) -> Image.Image:
        """The board to draw for `frame` at `seconds` on the board's clock."""
        shown = {(scroll.box, scroll.text) for scroll in frame.scrolls}
        self._started = {key: begun for key, begun in self._started.items() if key in shown}
        self._scrolls, self._drawn = frame.scrolls, {}
        image = frame.image.copy()
        for scroll, offset in self._offsets(seconds):
            image.paste(scroll.patch(offset), scroll.box[:2])
        return image

    def moved(self, seconds: float) -> list[tuple[Box, Image.Image]]:
        """Each field that has moved since it was last drawn: its box, and what it shows now."""
        return [(scroll.box, scroll.patch(offset)) for scroll, offset in self._offsets(seconds)]

    def _offsets(self, seconds: float):
        for scroll in self._scrolls:
            key = (scroll.box, scroll.text)
            started = self._started.setdefault(key, seconds)  # new text starts now
            if started > seconds:  # the clock went back (midnight, or summer time ending)
                started = self._started[key] = seconds
            offset = scroll.offset(seconds - started)
            if self._drawn.get(key) != offset:
                self._drawn[key] = offset
                yield scroll, offset
