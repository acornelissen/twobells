"""Output targets: a Pimoroni Inky, a Waveshare e-paper panel, or a PNG for development."""

from __future__ import annotations

from pathlib import Path

from PIL.Image import Image

from .panel import HDMI_1280X400, PHAT, PHAT_V1, WAVESHARE_10IN85G, Panel
from .render import Palette


# What each `display` setting draws on. An Inky reports its own size once opened.
DISPLAY_PANELS = {
    "inky": PHAT,
    "inky-phat-v1": PHAT_V1,
    "waveshare-10in85g": WAVESHARE_10IN85G,
    "hdmi-1280x400": HDMI_1280X400,
}


def _detect():
    from inky.auto import auto  # imported lazily: only present on the Pi

    return auto()


def _original_phat(colour: str):
    """The original pHAT can't be detected; the library is told its colour."""
    from inky import InkyPHAT  # imported lazily: only present on the Pi

    return InkyPHAT(colour if colour in ("red", "yellow") else "black")


class InkyDisplay:
    def __init__(self, accent: str = "red", device=None, rotate_180: bool = False, detect=_detect) -> None:
        if device is None:
            try:
                device = detect()
            except RuntimeError as error:
                raise RuntimeError(
                    f"{error} An original Inky pHAT (before 2019) has no EEPROM: "
                    'set display = "inky-phat-v1" in config.toml.'
                ) from error
        self._device = device
        self._rotate = rotate_180
        # The pHAT, or any other Inky the library detects, at its own size.
        width, height = getattr(device, "resolution", PHAT.size)
        known = {panel.size: panel for panel in (PHAT, PHAT_V1)}
        if (width, height) in known:
            self.panel = known[(width, height)]
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


def open_display(name: str, accent: str, rotate: int = 0):
    """The display a `display` setting names. Hardware libraries load only when used.
    E-paper panels turn 0 or 180 degrees; a screen turns any quarter."""
    if name == "inky":
        return InkyDisplay(accent, rotate_180=rotate == 180)
    if name == "inky-phat-v1":
        return InkyDisplay(accent, device=_original_phat(accent), rotate_180=rotate == 180)
    if name == "waveshare-10in85g":
        from .waveshare import WaveshareDisplay

        return WaveshareDisplay(accent, rotate == 180)
    if name == "hdmi-1280x400":
        from .screen import ScreenDisplay

        return ScreenDisplay(accent, rotate)
    raise ValueError(f"Unknown display {name!r}; choose from {', '.join(sorted(DISPLAY_PANELS))}")


class PngDisplay:
    def __init__(self, path: Path | str, accent: str = "red", panel: Panel = PHAT) -> None:
        self._path = Path(path)
        self.panel = panel
        self.palette = Palette(white=0, black=1, accent=2).with_preview(accent)

    def show(self, image: Image) -> None:
        image.convert("RGB").save(self._path)
