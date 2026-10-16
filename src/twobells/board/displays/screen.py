"""An HDMI screen: the board drawn full screen with pygame, no desktop needed.

Made for Waveshare's 7.9" 400x1280 screen run on its side as 1280x400, and
an 11.3" 1920x440 bar screen. A screen redraws in a frame, so the board can
show a running clock, and scroll text too long for its space: the whole board
is drawn once a second, and between times only the scrolling boxes, to keep
a Pi Zero 2 W's CPU free.
"""

from __future__ import annotations

import logging
from typing import Protocol

from PIL.Image import Image

from ..panel import HDMI_1280X400, Panel
from ..render import PILL_RGB, SCREEN_PALETTE, Palette
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
    (SCREEN_PALETTE) the TFT look's green and red (PILL_RGB)."""
    colours = [(0, 0, 0)] * 256
    colours[palette.white] = rgb["white"]
    colours[palette.black] = rgb["black"]
    colours[palette.accent] = rgb[accent]
    for name in ("green", "red"):
        index = getattr(palette, name)
        if index is not None:
            colours[index] = PILL_RGB[name]
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


class Screen(Protocol):
    def blit(self, image: Image) -> None: ...

    def patch(self, image: Image, at: tuple[int, int]) -> None: ...


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
        pygame = self._pygame
        pygame.event.pump()
        part = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
        x, y = self._origin[0] + at[0], self._origin[1] + at[1]
        self._surface.blit(part, (x, y))
        pygame.display.update(pygame.Rect(x, y, image.width, image.height))


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

    @property
    def scrolling(self) -> bool:
        return self._scroller.scrolling

    def show(self, image: Image) -> None:
        """A still board: nothing scrolls until the next Frame."""
        self.show_frame(Frame(image), 0)

    def show_frame(self, frame: Frame, seconds: float) -> None:
        """The whole board, its scrolling text where it is at `seconds` on the board's clock."""
        self._screen_now().blit(self._turned(self._scroller.show(frame, seconds)))

    def scroll(self, seconds: float) -> None:
        """Moves the scrolling text to `seconds`, updating only the boxes that moved."""
        for box, patch in self._scroller.moved(seconds):
            self._screen_now().patch(self._turned(patch), self._turned_at(box))

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
