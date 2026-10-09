"""Plain look: draws a BoardView onto a paletted image, scaled up from the pHAT design."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .panel import PHAT, PLATFORM_COLUMN_MIN_WIDTH, Panel
from .route import RouteView
from .view import BoardView, CodeView, Row, Section, Style

# The pHAT, which every other size is scaled up from.
WIDTH, HEIGHT = PHAT.size

# Below the header, and either side of the rule between sections.
_SECTION_GAP = 6
_PAD = 3
_MAX_ROWS = {"Trains": 3, "Buses": 2}
# Beside a section's label when its feed has stopped, in the sign look's words.
_NOT_UPDATING = "! NOT UPDATING"

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_PREVIEW_ACCENTS = {"red": (200, 30, 30), "yellow": (230, 180, 0)}


@dataclass(frozen=True)
class Palette:
    """Panel palette indices. `preview` adds real RGB values for PNG output."""

    white: int
    black: int
    accent: int
    preview: str | None = None

    def with_preview(self, accent_name: str) -> "Palette":
        return replace(self, preview=accent_name)


@dataclass(frozen=True)
class Fonts:
    regular: ImageFont.FreeTypeFont
    bold: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = _FONT_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


# Body text in pHAT pixels; a text_size setting can make the plain look bigger.
TEXT_SIZE = 11


def load_fonts(scale: int = 1, text_size: int = TEXT_SIZE) -> Fonts:
    return Fonts(
        regular=_font("DejaVuSans.ttf", text_size * scale),
        bold=_font("DejaVuSans-Bold.ttf", text_size * scale),
        small=_font("DejaVuSans-Bold.ttf", (text_size - 2) * scale),
    )


def heights(text_size: int = TEXT_SIZE) -> tuple[int, int, int]:
    """(header, section label, row) heights for the plain look's text size;
    at the standard 11 they are the pHAT design's 15, 12 and 14."""
    return text_size + 4, text_size + 1, text_size + 3


def drawn_length(font: ImageFont.FreeTypeFont, text: str) -> float:
    """How far `text` reaches when drawn. The panels' images are paletted, so
    glyphs are hinted for one-bit output and can come out a few pixels wider
    than getlength() says; right-aligned text measured that way runs into the edge."""
    return font.getbbox(text, mode="1")[2] if text else 0


def left_column_width(fonts: Fonts, scale: int = 1) -> float:
    return fonts.bold.getlength("00:00") + 5 * scale


@dataclass(frozen=True)
class RowLayout:
    left: str
    middle: str
    middle_x: float
    leave_x: float | None
    right_x: float
    platform_x: float | None = None


_COL_GAP = 6


def _with_stop(row: Row) -> str:
    return f"{row.stop}: {row.middle}" if row.stop else row.middle


def row_layout(
    row: Row, fonts: Fonts, width: int = WIDTH, scale: int = 1, platform_x: float | None = None
) -> RowLayout:
    """Column positions for a row. The destination gives way so times stay whole.
    `platform_x` is where a section's platform column starts, if it has one."""
    pad, gap = _PAD * scale, _COL_GAP * scale
    right_x = width - pad - drawn_length(fonts.bold, row.right)
    middle_x = pad + left_column_width(fonts, scale)
    leave_x = None
    middle_end = right_x - 4 * scale
    if row.leave:
        leave_x = right_x - gap - drawn_length(fonts.small, row.leave)
        middle_end = leave_x - gap
    if platform_x is not None:
        middle_end = platform_x - gap
    return RowLayout(
        # Measured as drawn, like the right-aligned text, so a cut destination
        # doesn't run into the status.
        left=fit_text(row.left, fonts.bold, middle_x - pad - 3 * scale, partial(drawn_length, fonts.bold)),
        middle=fit_text(_with_stop(row), fonts.regular, middle_end - middle_x, partial(drawn_length, fonts.regular)),
        middle_x=middle_x,
        leave_x=leave_x,
        right_x=right_x,
        platform_x=platform_x if row.platform else None,
    )


def platform_column(rows, fonts: Fonts, width: int, scale: int) -> float | None:
    """Where a section's platform column starts: only on a board wide enough
    for one, and only if some row has a platform."""
    if width / scale < PLATFORM_COLUMN_MIN_WIDTH or not any(r.platform for r in rows):
        return None
    right_w = max(drawn_length(fonts.bold, r.right) for r in rows)
    platform_w = max(drawn_length(fonts.regular, r.platform) for r in rows if r.platform)
    return width - _PAD * scale - right_w - _COL_GAP * scale - platform_w


def fit_text(text: str, font: ImageFont.FreeTypeFont, width: float, measure=None) -> str:
    """`text`, cut short with an ellipsis if it is wider than `width`.
    `measure` overrides font.getlength, e.g. with drawn_length."""
    measure = measure or font.getlength
    if measure(text) <= width:
        return text
    while text and measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap_text(text: str, width: float, measure) -> list[str]:
    """Lines of whole words no wider than `width`; a word too long for a line is split."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if measure(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        while len(word) > 1 and measure(word) > width:
            cut = len(word) - 1
            while cut > 1 and measure(word[:cut]) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


# A message too long for a screen's line is shown a part at a time, each for this long.
TURN_SECONDS = 5


def in_turns(text: str, width: float, measure, clock: str, lead: str = "") -> str:
    """The part of `lead + text` to show at `clock` (HH:MM:SS). It changes every
    TURN_SECONDS of the board's own clock, so what is shown is predictable.
    Every part starts with `lead`, so each reads as what it is, and a part
    with more to come ends in an ellipsis."""
    if measure(lead + text) <= width:
        return lead + text
    parts = wrap_text(text, width - measure(lead) - measure(" …"), measure)
    hours, minutes, seconds = (int(v) for v in clock.split(":"))
    index = ((hours * 60 + minutes) * 60 + seconds) // TURN_SECONDS % len(parts)
    return lead + parts[index] + (" …" if index < len(parts) - 1 else "")


NOTICE_LEAD = "! "


def fitted_note(view: BoardView, width: float, measure, font) -> str | None:
    """The disruption notice, or failing one the rain warning, as it fits in
    `width`: in turns on a screen, cut short on e-paper, which can't change
    it every few seconds."""
    if view.notice:
        lead, text = NOTICE_LEAD, view.notice
    elif view.rain:
        lead, text = "", view.rain
    else:
        return None
    if view.clock:
        return in_turns(text, width, measure, view.clock, lead)
    return fit_text(lead + text, font, width, measure)


def plain_scale(panel: Panel) -> int:
    """Whole steps up from the pHAT design that still fit the panel's height:
    the 10.85" panel's dot scale of 4 would need 488 of its 480 rows."""
    return max(1, min(panel.scale, panel.height // HEIGHT))


def row_limits(panel: Panel, text_size: int = TEXT_SIZE) -> dict[str, int]:
    """Rows each section shows: 3 trains and 2 buses on the pHAT's 122 rows,
    fewer on a shorter panel (the original pHAT has 104) or with bigger text,
    rather than cut off."""
    s = plain_scale(panel)
    header_h, label_h, row_h = heights(text_size)
    fixed = header_h + _SECTION_GAP + label_h + 1 + _SECTION_GAP + label_h
    rows = max(2, (panel.height // s - fixed) // row_h)
    buses = max(1, min(_MAX_ROWS["Buses"], rows - _MAX_ROWS["Trains"]))
    return {"Trains": min(_MAX_ROWS["Trains"], rows - buses), "Buses": buses}


def header_text(view: BoardView, fonts: Fonts, width: int, scale: int) -> tuple[str, str, float]:
    """(left text, stamp, stamp x) for the header bar. The plain look has no
    footer, so a disruption notice, or else a rain warning, takes the title's
    place while there is one: cut short on e-paper, in turns on a screen."""
    pad = _PAD * scale
    stamp = view.clock or f"Upd {view.updated}"
    stamp_x = width - pad - drawn_length(fonts.regular, stamp)
    room = stamp_x - pad - 6 * scale
    note = fitted_note(view, room, partial(drawn_length, fonts.bold), fonts.bold)
    return note or fit_text(view.title, fonts.bold, room), stamp, stamp_x


def apply_preview(image: Image.Image, palette: Palette) -> None:
    """Real colours for a PNG preview; a panel's own image keeps its indices."""
    if not palette.preview:
        return
    colours = [(0, 0, 0)] * 256
    colours[palette.white] = (255, 255, 255)
    colours[palette.black] = (0, 0, 0)
    colours[palette.accent] = _PREVIEW_ACCENTS[palette.preview]
    image.putpalette([v for rgb in colours for v in rgb])


class _Painter:
    def __init__(self, palette: Palette, fonts: Fonts, panel: Panel, text_size: int = TEXT_SIZE) -> None:
        self.p = palette
        self.f = fonts
        self.s = plain_scale(panel)
        self.limits = row_limits(panel, text_size)
        self.header_h, self.label_h, self.row_h = heights(text_size)
        # Smaller type sits on the same baseline as the row text.
        self.small_drop = 3
        self.width = panel.width
        self.image = Image.new("P", panel.size, palette.white)
        self.draw = ImageDraw.Draw(self.image)

    def header(self, view: BoardView) -> None:
        s, pad = self.s, _PAD * self.s
        self.draw.rectangle((0, 0, self.width - 1, self.header_h * s - 1), fill=self.p.black)
        title, stamp, stamp_x = header_text(view, self.f, self.width, s)
        self.draw.text((pad, s), title, font=self.f.bold, fill=self.p.white)
        self.draw.text((stamp_x, s), stamp, font=self.f.regular, fill=self.p.white)

    def section(self, section: Section, y: int) -> int:
        s, pad = self.s, _PAD * self.s
        label = section.label.upper()
        self.draw.text((pad, y + s), label, font=self.f.small, fill=self.p.black)
        label_end = pad + self.f.small.getlength(label)
        if section.stale:
            x = label_end + 6 * s
            self.draw.text((x, y + s), _NOT_UPDATING, font=self.f.small, fill=self.p.accent)
            label_end = x + self.f.small.getlength(_NOT_UPDATING)
        rows = section.rows[: self.limits.get(section.label, 2)]
        # As on the sign, a leave time for a row that doesn't fit is left out.
        if section.leave and (section.leave_for is None or section.leave_for < len(rows)):
            leave = fit_text(section.leave, self.f.small, self.width - pad - label_end - 8 * s)
            x = self.width - pad - drawn_length(self.f.small, leave)
            self.draw.text((x, y + s), leave, font=self.f.small, fill=self.p.black)
        y += self.label_h * s
        if not rows:
            self.draw.text((pad, y + s), section.empty_text, font=self.f.regular, fill=self.p.black)
            return y + self.row_h * s
        platform_x = platform_column(rows, self.f, self.width, s)
        for row in rows:
            self.row(row, y, platform_x)
            y += self.row_h * s
        return y

    def row(self, row: Row, y: int, platform_x: float | None = None) -> None:
        s = self.s
        ink, status_ink = self.p.black, self.p.black
        if row.style is Style.INVERTED:
            self.draw.rectangle((0, y, self.width - 1, y + self.row_h * s - 1), fill=self.p.black)
            ink = status_ink = self.p.white
        elif row.style is Style.ALERT:
            status_ink = self.p.accent

        layout = row_layout(row, self.f, self.width, s, platform_x)
        self.draw.text((_PAD * s, y + s), layout.left, font=self.f.bold, fill=ink)
        self.draw.text((layout.middle_x, y + s), layout.middle, font=self.f.regular, fill=ink)
        if layout.platform_x is not None:
            self.draw.text((layout.platform_x, y + s), row.platform, font=self.f.regular, fill=ink)
        if layout.leave_x is not None:
            self.draw.text((layout.leave_x, y + self.small_drop * s), row.leave, font=self.f.small, fill=ink)
        self.draw.text((layout.right_x, y + s), row.right, font=self.f.bold, fill=status_ink)

    def rule(self, y: int) -> None:
        pad = _PAD * self.s
        self.draw.line((pad, y, self.width - pad, y), fill=self.p.black, width=self.s)

    def apply_preview_palette(self) -> None:
        apply_preview(self.image, self.p)


def render(view: BoardView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Image.Image:
    s = plain_scale(panel)
    painter = _Painter(palette, load_fonts(s, text_size), panel, text_size)
    painter.header(view)
    y = painter.section(view.trains, (painter.header_h + _SECTION_GAP) * s)
    painter.rule(y + s)
    painter.section(view.buses, y + (_SECTION_GAP + 1) * s)
    painter.apply_preview_palette()
    return painter.image


def render_route(view: RouteView, palette: Palette, panel: Panel = PHAT, text_size: int = TEXT_SIZE) -> Image.Image:
    """The bus's position on the stops before yours, in the plain look."""
    s = plain_scale(panel)
    pad = _PAD * s
    f = load_fonts(s, text_size)
    header_h, _, row_h = heights(text_size)
    image = Image.new("P", panel.size, palette.white)
    draw = ImageDraw.Draw(image)
    width, height = panel.size

    draw.rectangle((0, 0, width - 1, header_h * s - 1), fill=palette.black)
    stamp = view.clock or ""
    stamp_x = width - pad - drawn_length(f.regular, stamp)
    draw.text((pad, s), fit_text(view.title, f.bold, stamp_x - pad * 3), font=f.bold, fill=palette.white)
    draw.text((stamp_x, s), stamp, font=f.regular, fill=palette.white)

    y = (header_h + 4) * s
    lead = f"{view.due}   {view.leave}" if view.leave else view.due
    draw.text((pad, y), lead, font=f.bold, fill=palette.black)
    draw.text((width - pad - drawn_length(f.bold, view.where), y), view.where, font=f.bold, fill=palette.black)

    road = (y + row_h * s + height) // 2
    left, right = pad * 3, width - pad * 3
    step = (right - left) / (len(view.stops) - 1)
    xs = [left + i * step for i in range(len(view.stops))]
    draw.line((xs[0], road, xs[-1], road), fill=palette.black, width=2 * s)
    for i, (name, x) in enumerate(zip(view.stops, xs)):
        yours = i == len(xs) - 1
        r = (5 if yours else 3) * s
        draw.ellipse((x - r, road - r, x + r, road + r), fill=palette.black if yours else palette.white,
                     outline=palette.black, width=s)
        font = f.bold if yours else f.regular
        text = fit_text(name, font, step * 2 - _COL_GAP * s)
        tx = min(max(pad, x - font.getlength(text) / 2), width - pad - drawn_length(font, text))
        # Names above the road clear the bus, which sits on the line.
        ty = road - 30 * s if i % 2 == 0 else road + 9 * s
        draw.text((tx, ty), text, font=font, fill=palette.black)
    if view.bus_at is not None:
        bx = xs[0] + view.bus_at * step
        draw.rounded_rectangle((bx - 12 * s, road - 13 * s, bx + 12 * s, road - 4 * s), radius=2 * s, fill=palette.black)
        for wx in range(-9, 9, 5):
            draw.rectangle((bx + wx * s, road - 11 * s, bx + (wx + 3) * s, road - 8 * s), fill=palette.white)
    apply_preview(image, palette)
    return image


def _biggest_bold(text: str, width: float, height: float) -> ImageFont.FreeTypeFont:
    """The largest bold type `text` fits in `width` x `height` at, as drawn."""
    low, high = 6, max(6, int(height) * 2)
    while low < high:
        size = (low + high + 1) // 2
        left, top, right, bottom = _font("DejaVuSans-Bold.ttf", size).getbbox(text, mode="1")
        if right - left <= width and bottom - top <= height:
            low = size
        else:
            high = size - 1
    return _font("DejaVuSans-Bold.ttf", low)


def render_code(view: CodeView, palette: Palette, panel: Panel = PHAT, dark: bool = False) -> Image.Image:
    """The settings reset code, as big as the panel allows, with what it is and
    until when above and below it. `dark` draws it light on black, as the sign
    look does; on a mono panel that is white."""
    s = plain_scale(panel)
    pad = _PAD * s
    width, height = panel.size
    if dark:
        background = palette.black
        ink = palette.white if palette.accent == palette.black else palette.accent
    else:
        background, ink = palette.white, palette.black
    image = Image.new("P", panel.size, background)
    draw = ImageDraw.Draw(image)
    small = _font("DejaVuSans-Bold.ttf", TEXT_SIZE * s)
    label = fit_text("Settings reset code", small, width - 2 * pad, partial(drawn_length, small))
    until = fit_text(f"Valid until {view.until}", small, width - 2 * pad, partial(drawn_length, small))
    line_h = heights()[2] * s
    room = height - 2 * line_h - 4 * pad
    big = _biggest_bold(view.code, width * 0.9, room)
    left, top, right, bottom = big.getbbox(view.code, mode="1")

    def centred(text: str, font, y: float) -> None:
        draw.text(((width - drawn_length(font, text)) / 2, y), text, font=font, fill=ink)

    centred(label, small, pad)
    # The code in the middle of the space between the two lines, by its drawn box.
    middle = line_h + pad + (height - 2 * line_h - 2 * pad) / 2
    draw.text(((width - (right - left)) / 2 - left, middle - (bottom - top) / 2 - top), view.code, font=big, fill=ink)
    centred(until, small, height - line_h - pad)
    apply_preview(image, palette)
    return image
