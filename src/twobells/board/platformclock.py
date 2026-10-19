"""The clock page: a railway platform clock, for a screen's page rotation.

Our own drawing of the classic British platform clock: a white face with
black batons for the hours and minutes, black baton hands and a red seconds
hand, and no maker's name or logo. The time and the date are written beside
it, so the page reads without the face. A screen only: e-paper never shows it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from PIL import Image, ImageDraw

from .panel import Panel
from .render import BOLD, PAD, REGULAR, Palette, apply_preview, drawn_length, load_font, plain_scale


@dataclass(frozen=True)
class ClockView:
    hour: int
    minute: int
    second: int
    date: str  # "Friday 16 October"

    @property
    def time(self) -> str:
        """The time in words beside the face, as "07:24"; the seconds are the face's."""
        return f"{self.hour:02d}:{self.minute:02d}"

    def signature(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def clock_view(now: datetime) -> ClockView:
    # Not strftime's %A and %B, which follow the computer's locale.
    day = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[now.weekday()]
    month = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December")[now.month - 1]
    return ClockView(now.hour, now.minute, now.second, f"{day} {now.day} {month}")


def hand_angles(hour: int, minute: int, second: int) -> tuple[float, float, float]:
    """The hour, minute and seconds hands, in degrees clockwise from 12. The
    hour and minute hands move on a little every second, as a real clock's do."""
    return (hour % 12) * 30 + minute * 0.5 + second / 120, minute * 6 + second * 0.1, second * 6.0


# The face's parts, as fractions of its radius: where each baton starts and
# ends out from the centre (negative is the tail, behind it), and how wide it is.
HOUR_MARK = (0.66, 0.88, 0.075)
MINUTE_MARK = (0.80, 0.88, 0.024)
HOUR_HAND = (-0.14, 0.54, 0.095)
MINUTE_HAND = (-0.16, 0.82, 0.068)
SECONDS_HAND = (-0.24, 0.86, 0.022)
RIM = 0.955  # a thin black ring just inside the face's edge
RIM_WIDTH = 0.022
SECONDS_BOSS = 0.055  # the red disc at the centre the seconds hand turns on


def baton(centre: tuple[float, float], radius: float, degrees: float,
          part: tuple[float, float, float]) -> list[tuple[float, float]]:
    """The corners of a straight bar along `degrees` (clockwise from 12): one
    of the face's batons, or a hand."""
    start, end, width = part
    cx, cy = centre
    angle = math.radians(degrees)
    along = (math.sin(angle), -math.cos(angle))
    across = (math.cos(angle) * width * radius / 2, math.sin(angle) * width * radius / 2)

    def at(out: float, side: float) -> tuple[float, float]:
        return (cx + along[0] * out * radius + across[0] * side, cy + along[1] * out * radius + across[1] * side)

    return [at(start, -1), at(end, -1), at(end, 1), at(start, 1)]


def draw_face(draw: ImageDraw.ImageDraw, centre: tuple[float, float], radius: float, view: ClockView,
              palette: Palette) -> None:
    cx, cy = centre
    white, black = palette.white, palette.black
    red = palette.red if palette.red is not None else palette.accent

    def disc(r: float, fill: int) -> None:
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)

    disc(radius, white)
    ring = RIM * radius
    draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), outline=black,
                 width=max(2, round(RIM_WIDTH * radius)))
    for mark in range(60):
        draw.polygon(baton(centre, radius, mark * 6, HOUR_MARK if mark % 5 == 0 else MINUTE_MARK), fill=black)
    hour, minute, second = hand_angles(view.hour, view.minute, view.second)
    draw.polygon(baton(centre, radius, hour, HOUR_HAND), fill=black)
    draw.polygon(baton(centre, radius, minute, MINUTE_HAND), fill=black)
    draw.polygon(baton(centre, radius, second, SECONDS_HAND), fill=red)
    disc(SECONDS_BOSS * radius, red)


def render_clock(view: ClockView, palette: Palette, panel: Panel) -> Image.Image:
    """The clock as big as the panel is high, on black as a station's would
    be, with the time and the date beside it in white."""
    s = plain_scale(panel)
    pad = PAD * s
    width, height = panel.size
    image = Image.new("P", panel.size, palette.black)
    draw = ImageDraw.Draw(image)
    radius = (height - 2 * pad) / 2
    big = load_font(BOLD, round(height * 0.3))
    small = load_font(REGULAR, round(height * 0.09))
    gap = radius * 0.5
    words_width = max(drawn_length(big, view.time), drawn_length(small, view.date))
    # The face and the words together, in the middle of the panel; on a panel
    # too narrow for both, the face alone.
    together = 2 * radius + gap + words_width
    left = (width - together) / 2 if together <= width - 2 * pad else (width - 2 * radius) / 2
    draw_face(draw, (left + radius, height / 2), radius, view, palette)
    if together <= width - 2 * pad:
        x = left + 2 * radius + gap
        big_box = big.getbbox(view.time, mode="1")
        small_box = small.getbbox(view.date, mode="1")
        big_h, small_h = big_box[3] - big_box[1], small_box[3] - small_box[1]
        line_gap = small_h * 0.8
        top = (height - big_h - line_gap - small_h) / 2
        draw.text((x - big_box[0], top - big_box[1]), view.time, font=big, fill=palette.white)
        draw.text((x - small_box[0], top + big_h + line_gap - small_box[1]), view.date, font=small,
                  fill=palette.white)
    apply_preview(image, palette)
    return image
