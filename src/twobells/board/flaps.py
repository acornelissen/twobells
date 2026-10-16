"""Letters on tiles turning over to new ones, as a split-flap board's do, on a screen.

When a row's time, destination or status changes, each tile whose letter
changes turns through the letters between, in the order a Solari drum holds
them, and settles on the new one: FLAP_STEPS frames at most, one a frame of
the screen loop (SCROLL_FRAME_S), so a change is over in half a second. Only
the fields that changed are drawn again, a frame at a time, so a screen's
CPU stays free between changes. E-paper and the PNG preview show the new
letters at once.

As with scrolling text, where a field has got to comes from the board's
clock passed in, never the wall clock.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from PIL import Image

from .scroll import SCROLL_FRAME_S, Box

# The letters on a tile's drum, in the order it turns through them: a blank,
# the alphabet, the digits and the marks a board shows. A lower-case letter
# turns with its capital; anything else counts from the blank.
DRUM = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:.-'&()/!?,>…"
# The most frames a change takes: 8 at 16 a second is half a second.
FLAP_STEPS = 8
FLAP_FRAME_S = SCROLL_FRAME_S


def _drum_index(char: str) -> int | None:
    index = DRUM.find(char.upper())
    return None if index < 0 else index


def _turns(old: str, new: str, steps: int) -> list[str]:
    """What one tile shows, frame by frame, as it turns from `old` to `new`."""
    if old == new:
        return []
    start = _drum_index(old) or 0
    end = _drum_index(new)
    # On the drum: forward round to the new letter. Off it: a turn of every step.
    distance = steps if end is None else (end - start) % len(DRUM)
    frames = min(max(distance, 1), steps)
    shown = []
    for frame in range(1, frames):
        # Evenly round the drum, halves rounded up (as the dashboard's flap.js does).
        letter = DRUM[(start + (2 * frame * distance + frames) // (2 * frames)) % len(DRUM)]
        shown.append(letter.lower() if new.islower() else letter)
    return [*shown, new]


def flap_frames(old: str, new: str, steps: int = FLAP_STEPS, right: bool = False) -> list[str]:
    """`new` turning over from `old`, frame by frame: each frame as long as
    `new`, the last `new` itself; none when they are the same. A tile settles
    once it reaches its letter, so the frames are as many as the farthest
    turn needs, `steps` at most. `old` lines up with `new` from its start, or
    from its end when the field is `right` aligned (a status), padded with
    blank tiles or cut to `new`'s length."""
    if old == new or steps < 1:
        return []
    width = len(new)
    lined_up = old.rjust(width)[-width:] if right else old.ljust(width)[:width]
    tiles = [_turns(a, b, steps) for a, b in zip(lined_up, new, strict=True)]
    count = max((len(turns) for turns in tiles), default=0)
    if count == 0:
        return [new]  # the same letters, longer or shorter: the new ones at once
    return ["".join(turns[min(frame, len(turns) - 1)] if turns else new[i] for i, turns in enumerate(tiles))
            for frame in range(count)]


@dataclass(frozen=True)
class Flap:
    """A field drawn on tiles that turn over when its text changes: on a
    screen, a row's time, destination or status. The same `key` across
    redraws is the same field (a row's place on the board, and which field)."""

    key: Hashable
    box: Box
    text: str
    # The box showing a string as long as `text`, tile for tile.
    paint: Callable[[str], Image.Image]
    right: bool = False


@dataclass
class _Turning:
    flap: Flap
    frames: list[str]
    started: float
    drawn: int = -1


class Flapper:
    """Keeps a screen's changed fields turning over between redraws. A redraw
    whose field has new text starts it turning from what was there; `moved`
    gives each turning field's box and what it shows now, only when that has
    changed since it was last drawn."""

    def __init__(self) -> None:
        self._shown: dict[Hashable, Flap] = {}
        self._turning: dict[Hashable, _Turning] = {}

    @property
    def flapping(self) -> bool:
        return bool(self._turning)

    def show(self, flaps: tuple[Flap, ...], seconds: float) -> list[tuple[Box, Image.Image]]:
        """What to put over a redraw with these fields at `seconds` on the
        board's clock: each field still turning, where it has got to."""
        shown = {flap.key: flap for flap in flaps}
        turning = {}
        for key, flap in shown.items():
            before, going = self._shown.get(key), self._turning.get(key)
            if going is not None and going.flap.text == flap.text and going.flap.box == flap.box:
                turning[key] = _Turning(flap, going.frames, going.started)  # carries on, drawn again
            elif before is not None and before.text != flap.text:
                frames = flap_frames(before.text, flap.text, FLAP_STEPS, flap.right)
                if len(frames) > 1:
                    turning[key] = _Turning(flap, frames, seconds)
        self._shown, self._turning = shown, turning
        return self.moved(seconds)

    def moved(self, seconds: float) -> list[tuple[Box, Image.Image]]:
        """Each turning field whose frame has changed since it was last drawn:
        its box, and what it shows now. A field drawn at its last frame stops."""
        patches = []
        for key, going in list(self._turning.items()):
            if seconds < going.started:  # the clock went back (midnight, or summer time ending)
                going.started = seconds
            # A hair over, so a frame exactly on time isn't taken for the one before.
            frame = min(len(going.frames) - 1, math.floor((seconds - going.started) / FLAP_FRAME_S + 1e-6))
            if frame != going.drawn:
                going.drawn = frame
                patches.append((going.flap.box, going.flap.paint(going.frames[frame])))
            if frame == len(going.frames) - 1:
                del self._turning[key]
        return patches
