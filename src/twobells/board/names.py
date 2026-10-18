"""The board's displays and looks by name, as data: what the settings offer
and check, without the looks' drawing code (Pillow) that board/themes.py
and board/displays/ load. This module imports nothing of the app's but
board/panel.py, which is data too, so the settings can use it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .panel import HDMI_1280X400, HDMI_1920X440, PHAT, PHAT_V1, WAVESHARE_10IN85G

# What each `display` setting draws on. An Inky reports its own size once opened.
DISPLAY_PANELS = {
    "inky": PHAT,
    "inky-phat-v1": PHAT_V1,
    "waveshare-10in85g": WAVESHARE_10IN85G,
    "hdmi-1280x400": HDMI_1280X400,
    "hdmi-1920x440": HDMI_1920X440,
}


@dataclass(frozen=True)
class Look:
    # What the settings' Look calls it.
    label: str
    # Only for a screen: e-paper isn't offered it, and refuses it.
    screen_only: bool = False


# Each look in board/themes.py's THEMES, by the same name and in the same order.
LOOKS = {
    "sign": Look("Station board (amber dot matrix)"),
    "plain": Look("Plain (black on white)"),
    "tft": Look("TFT screen (white and yellow on black)", screen_only=True),
    "flap": Look("Split-flap (white letters on flipping tiles)", screen_only=True),
}


def themes_for(epaper: bool) -> tuple[str, ...]:
    """The looks a display can show, by name, in the order THEMES has them."""
    return tuple(name for name, look in LOOKS.items() if not (epaper and look.screen_only))
