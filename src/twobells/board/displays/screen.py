"""An HDMI screen: the board drawn full screen with pygame, no desktop needed.

Made for Waveshare's 7.9" 400x1280 screen run on its side as 1280x400, and
an 11.3" 1920x440 bar screen. A screen redraws in a frame, so the board can
show a running clock, scroll text too long for its space, and turn the
split-flap look's tiles over. Each second only the parts of the board that
changed (the clock, the countdowns) are put on the screen again, and between
times only the boxes that move, to keep a Pi Zero 2 W's CPU free and each
second's frame quick: the whole of a 1920x440 board takes 10 ms or more to
put on a Pi 4's screen.
"""

from __future__ import annotations

import logging
from typing import Protocol

from PIL import ImageChops
from PIL.Image import Image

from ..flaps import Flapper
from ..panel import HDMI_1280X400, Panel
from ..render import SCREEN_ONLY_RGB, SCREEN_PALETTE, Palette
from ..scroll import Box, Frame, Scroller

log = logging.getLogger(__name__)

# Amber on black is 11:1 and red on white 5.7:1, both past WCAG AA. Amber on
# white is only 1.9:1, so the plain look should keep its default red.
SCREEN_RGB = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "yellow": (255, 170, 0),  # dot-matrix amber
    "red": (200, 30, 30),
}


def panel_colours(palette: Palette, accent: str, rgb: dict[str, tuple[int, int, int]] = SCREEN_RGB) -> list[int]:
    """A panel's palette in `rgb`'s colours (a screen's, or the emulator's
    e-paper), flat for putpalette, with the accent's colour, and on a screen
    (SCREEN_PALETTE) the TFT look's green and red and the split-flap look's
    tiles (SCREEN_ONLY_RGB)."""
    colours = [(0, 0, 0)] * 256
    colours[palette.white] = rgb["white"]
    colours[palette.black] = rgb["black"]
    colours[palette.accent] = rgb[accent]
    for name, colour in SCREEN_ONLY_RGB.items():
        index = getattr(palette, name)
        if index is not None:
            colours[index] = colour
    return [value for colour in colours for value in colour]


def fit_note(frame: tuple[int, int], screen: tuple[int, int]) -> str | None:
    """What to change when the board's frame doesn't fill the screen, or None
    when it does. The screen's size is the mode it came up in, as the Pi read
    it from the screen (or was told on the kernel's command line)."""
    if frame == screen:
        return None
    (frame_w, frame_h), (screen_w, screen_h) = frame, screen
    if (frame_h, frame_w) == screen:
        return (f"The screen is {screen_w}x{screen_h} and the board {frame_w}x{frame_h}: the screen is on its side. "
                "Turn the board with rotate = 90 or 270 in config.toml, or rotate = 0 if it is turned already.")
    return (f"The screen came up at {screen_w}x{screen_h}, but the board draws {frame_w}x{frame_h}, so it is "
            f"centred with a border or cut off. If the screen doesn't say its own size, set it: add "
            f"video=HDMI-A-1:{frame_w}x{frame_h}@60 to /boot/firmware/cmdline.txt and restart.")


# A new board is compared with the one on the screen in bands of rows this
# tall, and each band's changes go to the screen as the box round them.
BAND = 40
# Changes covering less of the board than this go to the screen as boxes;
# more (a new page, look or layout), and the whole board is drawn again.
MOST_PUSHED = 0.5


def changed_boxes(old: Image, new: Image, band: int = BAND) -> list[Box]:
    """Where `new` differs from `old`, two boards the same size in palette
    indices: in each band of rows, the box round its changes."""
    diff = ImageChops.difference(old, new)
    boxes = []
    for top in range(0, new.height, band):
        found = diff.crop((0, top, new.width, min(new.height, top + band))).getbbox()
        if found is not None:
            left, y0, right, y1 = found
            boxes.append((left, top + y0, right, top + y1))
    return boxes


class Screen(Protocol):
    def blit(self, image: Image) -> None: ...

    def patch(self, image: Image, at: tuple[int, int]) -> None: ...

    def update(self, parts: list[tuple[Image, tuple[int, int]]]) -> None: ...


class PygameScreen:
    """Full screen at the display's own resolution, the frame centred on it."""

    def __init__(self) -> None:
        try:
            import pygame  # imported lazily: only needed on a board with a screen
        except ImportError:
            raise RuntimeError(
                "pygame isn't installed; the HDMI screen needs it: sudo apt-get install python3-pygame"
            ) from None

        pygame.display.init()
        pygame.mouse.set_visible(False)
        self._pygame = pygame
        self._surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        self._noted = False  # said once, at the first frame, if it doesn't fit
        self._origin = (0, 0)  # where the frame's top left is on the screen

    def blit(self, image: Image) -> None:
        pygame = self._pygame
        pygame.event.pump()  # keeps the window alive and touch events from queueing up
        frame = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
        width, height = self._surface.get_size()
        if not self._noted:
            self._noted = True
            note = fit_note(image.size, (width, height))
            if note:
                log.warning("%s", note)
        self._surface.fill((0, 0, 0))
        self._origin = ((width - image.width) // 2, (height - image.height) // 2)
        self._surface.blit(frame, self._origin)
        pygame.display.flip()

    def patch(self, image: Image, at: tuple[int, int]) -> None:
        """Part of the last frame, `image` with its top left at `at` in the
        frame: only that part of the screen is updated."""
        self.update([(image, at)])

    def update(self, parts: list[tuple[Image, tuple[int, int]]]) -> None:
        """Parts of the last frame, each an image and where its top left is
        in the frame, put on the screen together: only those parts are updated."""
        pygame = self._pygame
        pygame.event.pump()
        rects = []
        for image, at in parts:
            x, y = self._origin[0] + at[0], self._origin[1] + at[1]
            self._surface.blit(pygame.image.frombytes(image.tobytes(), image.size, "RGB"), (x, y))
            rects.append(pygame.Rect(x, y, image.width, image.height))
        pygame.display.update(rects)


class ScreenDisplay:
    def __init__(self, accent: str = "yellow", rotate: int = 0, screen: Screen | None = None,
                 panel: Panel = HDMI_1280X400) -> None:
        if accent not in ("yellow", "red"):
            raise ValueError(f"Unknown accent {accent!r}; choose from red, yellow")
        if rotate not in (0, 90, 180, 270):
            raise ValueError(f"rotate must be 0, 90, 180 or 270, not {rotate!r}")
        self.panel = panel
        self.palette = SCREEN_PALETTE
        self._colours = panel_colours(self.palette, accent)
        self._rotate = rotate
        self._screen = screen
        self._scroller = Scroller()
        self._flapper = Flapper()
        self._shown: Image | None = None  # the board on the screen, in palette indices, unturned

    @property
    def scrolling(self) -> bool:
        """Whether anything moves between redraws: text scrolling, or tiles turning over."""
        return self._scroller.scrolling or self._flapper.flapping

    def show(self, image: Image) -> None:
        """A still board: nothing scrolls until the next Frame."""
        self.show_frame(Frame(image), 0)

    def show_frame(self, frame: Frame, seconds: float) -> None:
        """The whole board, its scrolling text and turning tiles where they are at `seconds` on the board's clock."""
        image = self._scroller.show(frame, seconds)
        for box, patch in self._flapper.show(frame.flaps, seconds):
            image.paste(patch, box[:2])
        self._push(image)

    def scroll(self, seconds: float) -> None:
        """Moves the scrolling text and turning tiles to `seconds`, updating only the boxes that moved."""
        for box, patch in [*self._scroller.moved(seconds), *self._flapper.moved(seconds)]:
            self._screen_now().patch(self._turned(patch), self._turned_at(box))
            if self._shown is not None:
                self._shown.paste(patch, box[:2])

    def _push(self, image: Image) -> None:
        """Put the board `image` on the screen: only the boxes that changed
        since the last, or the whole board when most of it did."""
        screen, last = self._screen_now(), self._shown
        boxes = changed_boxes(last, image) if last is not None and last.size == image.size else None
        changed = sum((right - left) * (bottom - top) for left, top, right, bottom in boxes or ())
        if boxes is None or changed >= MOST_PUSHED * image.width * image.height:
            screen.blit(self._turned(image))
        elif boxes:
            screen.update([(self._turned(image.crop(box)), self._turned_at(box)) for box in boxes])
        self._shown = image

    def _screen_now(self) -> Screen:
        if self._screen is None:
            self._screen = PygameScreen()
        return self._screen

    def _turned(self, image: Image) -> Image:
        """`image` in screen colours, turned clockwise to match how the screen is mounted."""
        frame = image.copy()
        frame.putpalette(self._colours)
        frame = frame.convert("RGB")
        return frame.rotate(-self._rotate, expand=True) if self._rotate else frame

    def _turned_at(self, box: Box) -> tuple[int, int]:
        """Where the top left of `box`, on the board, lands once the board is turned."""
        width, height = self.panel.size
        left, top, right, bottom = box
        return {0: (left, top), 90: (height - bottom, left), 180: (width - right, height - bottom),
                270: (top, width - right)}[self._rotate]
