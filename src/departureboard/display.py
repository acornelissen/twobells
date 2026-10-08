"""Output targets: the real Inky pHAT, or a PNG for development."""

from __future__ import annotations

from pathlib import Path

from PIL.Image import Image

from .render import Palette


class InkyDisplay:
    def __init__(self, accent: str = "red", device=None, rotate_180: bool = False) -> None:
        if device is None:
            from inky.auto import auto  # imported lazily: only present on the Pi

            device = auto()
        self._device = device
        self._rotate = rotate_180
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


class PngDisplay:
    def __init__(self, path: Path | str, accent: str = "red") -> None:
        self._path = Path(path)
        self.palette = Palette(white=0, black=1, accent=2).with_preview(accent)

    def show(self, image: Image) -> None:
        image.convert("RGB").save(self._path)
