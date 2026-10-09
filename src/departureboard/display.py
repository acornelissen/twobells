"""Output targets: a Pimoroni Inky, a Waveshare e-paper panel, or a PNG for development."""

from __future__ import annotations

from pathlib import Path

from PIL.Image import Image

from .panel import PHAT, WAVESHARE_10IN85G, Panel
from .render import Palette


# What each `display` setting draws on. An Inky reports its own size once opened.
DISPLAY_PANELS = {"inky": PHAT, "waveshare-10in85g": WAVESHARE_10IN85G}


class InkyDisplay:
    def __init__(self, accent: str = "red", device=None, rotate_180: bool = False) -> None:
        if device is None:
            from inky.auto import auto  # imported lazily: only present on the Pi

            device = auto()
        self._device = device
        self._rotate = rotate_180
        # The pHAT, or any other Inky the library detects, at its own size.
        width, height = getattr(device, "resolution", PHAT.size)
        if (width, height) == PHAT.size:
            self.panel = PHAT
        else:
            self.panel = Panel.sized(f"inky-{width}x{height}", width, height)
        black = device.BLACK
        self.palette = Palette(
            white=device.WHITE,
            black=black,
            # A mono panel has no accent; text still carries the meaning.
            accent=getattr(device, accent.upper(), black),
        )

    def show(self, image: Image) -> None:
        if self._rotate:
            image = image.rotate(180)
        self._device.set_image(image)
        self._device.show()


def open_display(name: str, accent: str, rotate_180: bool = False):
    """The display a `display` setting names. Hardware libraries load only when used."""
    if name == "inky":
        return InkyDisplay(accent, rotate_180=rotate_180)
    if name == "waveshare-10in85g":
        from .waveshare import WaveshareDisplay

        return WaveshareDisplay(accent, rotate_180)
    raise ValueError(f"Unknown display {name!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")


class PngDisplay:
    def __init__(self, path: Path | str, accent: str = "red", panel: Panel = PHAT) -> None:
        self._path = Path(path)
        self.panel = panel
        self.palette = Palette(white=0, black=1, accent=2).with_preview(accent)

    def show(self, image: Image) -> None:
        image.convert("RGB").save(self._path)
