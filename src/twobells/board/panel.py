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
# bus is: a little line of stops after the destination, then the words. From
# 420 a sign row keeps "Manchester Piccadilly" whole beside them ("3 stops
# away"), the platform and the status, and a bus's destination about 120
# dots, 22 letters, beside them, its "Leave in 12 min" and the status; at
# 400 the bus's would be down to 99. The 7.9" screen's 426 is in; the 10.85" e-paper's 340 is not, and e-paper never
# shows them anyway. A wider screen shares its spare room out by a ladder
# (render.share_out) between the destination, a bus's stop names and the
# where words.
STRIP_COLUMN_MIN_WIDTH = 420

# A sign board at least this wide gives a train's "Leave in 3 min" a column on
# its row rather than a line of its own under it: one line a row. On the bar
# screen's 480 a destination still keeps 124 dots, 24 letters, beside the
# where words; on the 7.9" screen's 426 it would keep 72, about 14, so only
# the bar screen does it.
LEAVE_COLUMN_MIN_WIDTH = 460

# A screen at least this wide, in the plain look's pHAT pixels, lines its
# sections' little lines up in one column down the board in the plain, TFT
# and split-flap looks, where the tightest section puts them; a narrower
# screen, and the station board look on any screen, puts each section's
# against its own columns. Fixed per screen and look, never from the rows
# shown, so they stay put. Lined up, the trains' lines follow the buses',
# which sit left of their leave times: on the bar screen's 640 the trains
# still keep "Manchester Piccadilly" whole, but on the 7.9" screen's 426 it
# would scroll, as it would in the station board look on either screen (the
# bar screen's trains have their leave words on their row).
LINED_UP_STRIPS_MIN_WIDTH = 460

# A screen at least this wide shows a disruption notice and the rain warning
# side by side on its bottom line (the plain look: in its header), rather than
# the notice hiding the rain. At 400 the notice keeps about 230 dots, some 40
# letters in view as it scrolls, beside the rain warning and the clock; with
# the "Not updating" badge too, still about 25. Both HDMI screens are wider;
# e-paper, which can't scroll a notice, keeps the notice alone.
NOTES_SIDE_BY_SIDE_MIN_WIDTH = 400

# A screen at least this wide calls each bus row's stop by its name ("Henry
# Street A") where e-paper shows its label ("A"). The ladder gives the names
# "Henry St A" before the bus destination its target ("Ashton-under-Lyne"),
# the longest whole after it, a longer one scrolling. On the bar screen's 480
# a sign destination keeps 103 dots, 20 letters, beside "Henry Street A" and
# 97 beside a long name; the plain look's 640 keeps over 100 pixels. On the
# 7.9" screen's 426 the sign look would keep 71 dots, about 14 letters, and
# the plain look 50 pixels, about 8, beside names cut to "Henry St A", so
# only the bar screen does it.
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

# How many modes (settings/modes.py) each panel has room for, a section
# each, in any look: as many as keep every one of them at least one whole
# row in every look the panel has, at every text size the plain look offers.
# The type is sized to the panel's height, so it is height that counts, not
# inches. Each section takes a heading (the sign look: a dotted rule) as well
# as its rows, and its leave words: the plain looks put them in the heading,
# the sign look on a line of their own under the train or tram they are for
# (on the bar screen, a column on its row).
#
# - The pHATs: 2. With 3, the plain look has room for 1 row each at the
#   standard size and none for the third from text size 13 (122 rows); the
#   original's 104 has none for it even at the standard size.
# - The 10.85" e-paper: 3. 160 pHAT rows in the plain look: 3 trains, 2 buses
#   and 1 tram at the standard size, a row each at the biggest; the sign look
#   (120 dots) 2, 1 and 1, with a leave line under the first train and tram.
# - The 7.9" screen: 2. At 3 pixels a dot it is 133 tall, little more than the
#   pHAT, and a screen's "Calling at" line takes a row: 1 row each at the
#   standard size, none for the third mode from text size 13.
# - The 11.3" bar screen: 2. 146 rows in the plain look and 110 dots in the
#   sign look: with the "Calling at" line, 2 trains, 1 bus and 1 tram at the
#   standard size, and none for the third from text size 14.
#
# Trains and buses have always shared every display, so none has room for
# fewer than 2 (the original pHAT's biggest text cuts its last row short).
# tests/board/test_panel.py measures these against the looks' layouts.
MOST_MODES = {PHAT.name: 2, PHAT_V1.name: 2, WAVESHARE_10IN85G.name: 3, HDMI_1280X400.name: 2, HDMI_1920X440.name: 2}

# The HDMI screens have room for one mode more in the looks that keep it a
# whole row too: the station board look (its dots are fixed; on the 7.9"
# screen 2 trains with their "Calling at" and leave lines, a bus, and a tram
# with its leave line), the TFT and split-flap looks (drawn at the standard
# text size, 11), and the plain look up to the biggest text size that does,
# given here. Bigger than that, the plain look keeps MOST_MODES.
#
# - The 7.9" screen: up to 12, with a row each for 3 modes at 12 and 2 rows
#   for the first two; at 13 the third has none.
# - The 11.3" bar screen: up to 13, a row each at 13; at 14 the third has none.
#
# The e-paper panels keep MOST_MODES whatever the look, so changing the look
# on e-paper never changes how many modes fit. tests/board/test_panel.py
# measures these too.
MORE_MODES_UP_TO = {HDMI_1280X400.name: 12, HDMI_1920X440.name: 13}
# What the settings call each panel, in a sentence: "The Inky pHAT has room for 2".
PANEL_NAMES = {
    PHAT.name: "the Inky pHAT",
    PHAT_V1.name: "the original Inky pHAT",
    WAVESHARE_10IN85G.name: "the 10.85-inch e-paper panel",
    HDMI_1280X400.name: "the 7.9-inch screen",
    HDMI_1920X440.name: "the 11.3-inch bar screen",
}


def most_modes(panel: Panel, look: str | None = None, text_size: int | None = None) -> int:
    """How many modes `panel` has room for: in any look (MOST_MODES), or,
    given the `look` by name, in that look, with `text_size` the plain look's
    text size (None: the standard). 2 for a panel of another size, as an Inky
    the library detects can be."""
    most = MOST_MODES.get(panel.name, 2)
    biggest = MORE_MODES_UP_TO.get(panel.name)
    if look is None or biggest is None:
        return most
    if look == "plain" and text_size is not None and text_size > biggest:
        return most
    return most + 1


def biggest_text_for(panel: Panel, count: int) -> int | None:
    """The biggest plain-look text size at which `panel` has room for
    `count` modes, where only some sizes do; None where every size does, or
    none (most_modes says which)."""
    biggest = MORE_MODES_UP_TO.get(panel.name)
    if biggest is None or count <= MOST_MODES.get(panel.name, 2):
        return None
    return biggest


def panel_name(panel: Panel) -> str:
    """The panel in words, for a sentence: "the Inky pHAT"."""
    return PANEL_NAMES.get(panel.name, "this display")
