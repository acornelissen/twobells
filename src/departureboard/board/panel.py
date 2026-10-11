"""What the board is drawn on: its pixel size, how big to draw, and whether it is e-paper."""

from __future__ import annotations

from dataclasses import dataclass

# The board was designed on the pHAT: 122 pixels tall at one pixel per LED dot.
_DESIGN_HEIGHT = 120

# A board at least this wide, in design units (sign dots, or the plain look's
# pHAT pixels), has room for a platform column. The pHAT is 250 across; the
# 10.85" panel and the HDMI screens are 340 or more in either look.
PLATFORM_COLUMN_MIN_WIDTH = 300

# A screen at least this wide, in the same units, shows where each train or
# bus is: the words and a little line of stops, after the destination. From
# 420 a destination still keeps about 100 dots, 18 letters ("Manchester
# Piccadilly" is 105), beside them, the platform, a bus's "Leave in 12 min" and
# the status; at 400 a train's would be down to 80. The 7.9" screen's 426 is
# in; the 10.85" e-paper's 340 is not, and e-paper never shows them anyway.
# A wider screen gives its spare room to the where words first, up to twenty
# letters, so they aren't cut short when there's room (see where_width).
STRIP_COLUMN_MIN_WIDTH = 420

# A sign board at least this wide gives a train's "Leave in 3 min" a column on
# its row rather than a line of its own under it: one line a row. On the bar
# screen's 480 a destination still keeps 94 dots, 17 letters, beside twenty
# letters of where words; on the 7.9" screen's 426 it would keep 64, about 11,
# so only the bar screen does it.
LEAVE_COLUMN_MIN_WIDTH = 460

# A screen at least this wide shows a disruption notice and the rain warning
# side by side on its bottom line (the plain look: in its header), rather than
# the notice hiding the rain. At 400 the notice keeps about 230 dots, some 40
# letters in view as it scrolls, beside the rain warning and the clock; with
# the "Not updating" badge too, still about 25. Both HDMI screens are wider;
# e-paper, which can't scroll a notice, keeps the notice alone.
NOTES_SIDE_BY_SIDE_MIN_WIDTH = 400

# A screen at least this wide calls each bus row's stop by its name ("Henry
# Street A") where e-paper shows its label ("A"). Train rows don't change: the
# where column stays where "Manchester Piccadilly" whole puts it, so the name
# comes out of the bus destination's room. On the bar screen's 480 a sign
# destination keeps 54 dots, 10 letters, beside "Henry Street A" and 46, 8,
# beside a name the column's full sixteen letters wide; the plain look's 640
# keeps over 200 pixels. On the 7.9" screen's 426 the sign look would keep
# nearly as much (50 and 42), but the plain look's would be down to 27
# pixels, 4 letters, beside "Henry Street A" and 11, next to nothing, beside a
# long name, so only the bar screen does it.
STOP_NAME_MIN_WIDTH = 460


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
    def sized(cls, name: str, width: int, height: int, epaper: bool = True) -> Panel:
        """A panel drawn as large as its height allows, whole pixels per dot."""
        return cls(name, width, height, max(1, height // _DESIGN_HEIGHT), epaper)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


PHAT = Panel.sized("inky-phat", 250, 122)
# The original Inky pHAT (before 2019): smaller, and no EEPROM to detect it by.
PHAT_V1 = Panel.sized("inky-phat-v1", 212, 104)
WAVESHARE_10IN85G = Panel.sized("waveshare-10in85g", 1360, 480)
# The 7.9" HDMI screen is 400x1280 portrait; the board runs it on its side.
HDMI_1280X400 = Panel.sized("hdmi-1280x400", 1280, 400, epaper=False)
# An 11.3" 1920x440 bar screen: four pixels a dot, 480x110 dots, so the text is
# a third bigger than the 7.9" screen's. 110 dots is short of the design's 120,
# but with a line a row it fits 3 trains, 3 buses and the clock.
HDMI_1920X440 = Panel("hdmi-1920x440", 1920, 440, scale=4, epaper=False)

PANELS = {p.name: p for p in (PHAT, PHAT_V1, WAVESHARE_10IN85G, HDMI_1280X400, HDMI_1920X440)}
