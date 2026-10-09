"""An HDMI screen: the board drawn full screen with pygame, no desktop needed.

Made for Waveshare's 7.9" 400x1280 screen run on its side as 1280x400. A
screen redraws in a frame, so the board can show a running clock.
"""

from __future__ import annotations

from typing import Protocol

from PIL.Image import Image

from .panel import HDMI_1280X400
from .render import Palette

# Amber on black is 11:1 and red on white 5.7:1, both past WCAG AA. Amber on
# white is only 1.9:1, so the plain look should keep its default red.
SCREEN_RGB = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "yellow": (255, 170, 0),  # dot-matrix amber
    "red": (200, 30, 30),
}


class Screen(Protocol):
    def blit(self, image: Image) -> None: ...


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

    def blit(self, image: Image) -> None:
        pygame = self._pygame
        pygame.event.pump()  # keeps the window alive and touch events from queueing up
        frame = pygame.image.frombytes(image.tobytes(), image.size, "RGB")
        width, height = self._surface.get_size()
        self._surface.fill((0, 0, 0))
        self._surface.blit(frame, ((width - image.width) // 2, (height - image.height) // 2))
        pygame.display.flip()


class ScreenDisplay:
    def __init__(self, accent: str = "yellow", rotate: int = 0, screen: Screen | None = None) -> None:
        if accent not in ("yellow", "red"):
            raise ValueError(f"Unknown accent {accent!r}; choose from red, yellow")
        if rotate not in (0, 90, 180, 270):
            raise ValueError(f"rotate must be 0, 90, 180 or 270, not {rotate!r}")
        self.panel = HDMI_1280X400
        self.palette = Palette(white=0, black=1, accent=2)
        colours = [(0, 0, 0)] * 256
        colours[self.palette.white] = SCREEN_RGB["white"]
        colours[self.palette.black] = SCREEN_RGB["black"]
        colours[self.palette.accent] = SCREEN_RGB[accent]
        self._colours = [value for rgb in colours for value in rgb]
        self._rotate = rotate
        self._screen = screen

    def show(self, image: Image) -> None:
        frame = image.copy()
        frame.putpalette(self._colours)
        frame = frame.convert("RGB")
        if self._rotate:
            # Clockwise, to match how the screen is mounted.
            frame = frame.rotate(-self._rotate, expand=True)
        if self._screen is None:
            self._screen = PygameScreen()
        self._screen.blit(frame)
