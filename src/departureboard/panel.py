"""What the board is drawn on: its pixel size, how big to draw, and whether it is e-paper."""

from __future__ import annotations

from dataclasses import dataclass

# The board was designed on the pHAT: 122 pixels tall at one pixel per LED dot.
_DESIGN_HEIGHT = 120

# A board at least this wide, in design units (sign dots, or the plain look's
# pHAT pixels), has room for a platform column. The pHAT is 250 across; the
# 10.85" panel and the HDMI screen are 340 or more in either look.
PLATFORM_COLUMN_MIN_WIDTH = 300


@dataclass(frozen=True)
class Panel:
    name: str
    width: int
    height: int
    # Panel pixels per sign dot; the plain look scales its type by the same.
    scale: int = 1
    # E-paper is slow to refresh and wears; a screen can redraw every second.
    epaper: bool = True

    @classmethod
    def sized(cls, name: str, width: int, height: int, epaper: bool = True) -> "Panel":
        """A panel drawn as large as its height allows, whole pixels per dot."""
        return cls(name, width, height, max(1, height // _DESIGN_HEIGHT), epaper)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


PHAT = Panel.sized("inky-phat", 250, 122)
WAVESHARE_10IN85G = Panel.sized("waveshare-10in85g", 1360, 480)
# The 7.9" HDMI screen is 400x1280 portrait; the board runs it on its side.
HDMI_1280X400 = Panel.sized("hdmi-1280x400", 1280, 400, epaper=False)

PANELS = {p.name: p for p in (PHAT, WAVESHARE_10IN85G, HDMI_1280X400)}
