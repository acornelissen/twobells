"""Plain look: draws a BoardView onto a paletted image, scaled up from the pHAT design."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .panel import PHAT, Panel
from .route import RouteView
from .view import BoardView, Row, Section, Style

# The pHAT, which every other size is scaled up from.
WIDTH, HEIGHT = PHAT.size

_HEADER_H = 15
_LABEL_H = 12
_ROW_H = 14
# Below the header, and either side of the rule between sections.
_SECTION_GAP = 6
_PAD = 3
_MAX_ROWS = {"Trains": 3, "Buses": 2}

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


def load_fonts(scale: int = 1) -> Fonts:
    return Fonts(
        regular=_font("DejaVuSans.ttf", 11 * scale),
        bold=_font("DejaVuSans-Bold.ttf", 11 * scale),
        small=_font("DejaVuSans-Bold.ttf", 9 * scale),
    )


def left_column_width(fonts: Fonts, scale: int = 1) -> float:
    return fonts.bold.getlength("00:00") + 5 * scale


@dataclass(frozen=True)
class RowLayout:
    left: str
    middle: str
    middle_x: float
    leave_x: float | None
    right_x: float


_COL_GAP = 6


def _with_stop(row: Row) -> str:
    return f"{row.stop}: {row.middle}" if row.stop else row.middle


def row_layout(row: Row, fonts: Fonts, width: int = WIDTH, scale: int = 1) -> RowLayout:
    """Column positions for a row. The destination gives way so times stay whole."""
    pad, gap = _PAD * scale, _COL_GAP * scale
    right_x = width - pad - fonts.bold.getlength(row.right)
    middle_x = pad + left_column_width(fonts, scale)
    leave_x = None
    middle_end = right_x - 4 * scale
    if row.leave:
        leave_x = right_x - gap - fonts.small.getlength(row.leave)
        middle_end = leave_x - gap
    return RowLayout(
        left=fit_text(row.left, fonts.bold, middle_x - pad - 3 * scale),
        middle=fit_text(_with_stop(row), fonts.regular, middle_end - middle_x),
        middle_x=middle_x,
        leave_x=leave_x,
        right_x=right_x,
    )


def fit_text(text: str, font: ImageFont.FreeTypeFont, width: float) -> str:
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


class _Painter:
    def __init__(self, palette: Palette, fonts: Fonts, panel: Panel) -> None:
        self.p = palette
        self.f = fonts
        self.s = panel.scale
        self.width = panel.width
        self.image = Image.new("P", panel.size, palette.white)
        self.draw = ImageDraw.Draw(self.image)

    def header(self, view: BoardView) -> None:
        s, pad = self.s, _PAD * self.s
        self.draw.rectangle((0, 0, self.width - 1, _HEADER_H * s - 1), fill=self.p.black)
        stamp = view.clock or f"Upd {view.updated}"
        stamp_x = self.width - pad - self.f.regular.getlength(stamp)
        title = fit_text(view.title, self.f.bold, stamp_x - pad - 6 * s)
        self.draw.text((pad, s), title, font=self.f.bold, fill=self.p.white)
        self.draw.text((stamp_x, s), stamp, font=self.f.regular, fill=self.p.white)

    def section(self, section: Section, y: int) -> int:
        s, pad = self.s, _PAD * self.s
        label = section.label.upper()
        self.draw.text((pad, y + s), label, font=self.f.small, fill=self.p.black)
        label_end = pad + self.f.small.getlength(label)
        if section.stale:
            x = label_end + 6 * s
            self.draw.text((x, y + s), "! STALE", font=self.f.small, fill=self.p.accent)
            label_end = x + self.f.small.getlength("! STALE")
        rows = section.rows[: _MAX_ROWS.get(section.label, 2)]
        # As on the sign, a leave time for a row that doesn't fit is left out.
        if section.leave and (section.leave_for is None or section.leave_for < len(rows)):
            leave = fit_text(section.leave, self.f.small, self.width - pad - label_end - 8 * s)
            x = self.width - pad - self.f.small.getlength(leave)
            self.draw.text((x, y + s), leave, font=self.f.small, fill=self.p.black)
        y += _LABEL_H * s
        if not rows:
            self.draw.text((pad, y + s), section.empty_text, font=self.f.regular, fill=self.p.black)
            return y + _ROW_H * s
        for row in rows:
            self.row(row, y)
            y += _ROW_H * s
        return y

    def row(self, row: Row, y: int) -> None:
        s = self.s
        ink, status_ink = self.p.black, self.p.black
        if row.style is Style.INVERTED:
            self.draw.rectangle((0, y, self.width - 1, y + _ROW_H * s - 1), fill=self.p.black)
            ink = status_ink = self.p.white
        elif row.style is Style.ALERT:
            status_ink = self.p.accent

        layout = row_layout(row, self.f, self.width, s)
        self.draw.text((_PAD * s, y + s), layout.left, font=self.f.bold, fill=ink)
        self.draw.text((layout.middle_x, y + s), layout.middle, font=self.f.regular, fill=ink)
        if layout.leave_x is not None:
            # Smaller type sits on the same baseline as the row text.
            self.draw.text((layout.leave_x, y + 3 * s), row.leave, font=self.f.small, fill=ink)
        self.draw.text((layout.right_x, y + s), row.right, font=self.f.bold, fill=status_ink)

    def rule(self, y: int) -> None:
        pad = _PAD * self.s
        self.draw.line((pad, y, self.width - pad, y), fill=self.p.black, width=self.s)

    def apply_preview_palette(self) -> None:
        if not self.p.preview:
            return
        colours = [(0, 0, 0)] * 256
        colours[self.p.white] = (255, 255, 255)
        colours[self.p.black] = (0, 0, 0)
        colours[self.p.accent] = _PREVIEW_ACCENTS[self.p.preview]
        self.image.putpalette([v for rgb in colours for v in rgb])


def render(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    s = panel.scale
    painter = _Painter(palette, load_fonts(s), panel)
    painter.header(view)
    y = painter.section(view.trains, (_HEADER_H + _SECTION_GAP) * s)
    painter.rule(y + s)
    painter.section(view.buses, y + (_SECTION_GAP + 1) * s)
    painter.apply_preview_palette()
    return painter.image


def render_route(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The bus's position on the stops before yours, in the plain look."""
    s, pad = panel.scale, _PAD * panel.scale
    f = load_fonts(s)
    image = Image.new("P", panel.size, palette.white)
    draw = ImageDraw.Draw(image)
    width, height = panel.size

    draw.rectangle((0, 0, width - 1, _HEADER_H * s - 1), fill=palette.black)
    stamp = view.clock or ""
    stamp_x = width - pad - f.regular.getlength(stamp)
    draw.text((pad, s), fit_text(view.title, f.bold, stamp_x - pad * 3), font=f.bold, fill=palette.white)
    draw.text((stamp_x, s), stamp, font=f.regular, fill=palette.white)

    y = (_HEADER_H + 4) * s
    lead = f"{view.due}   {view.leave}" if view.leave else view.due
    draw.text((pad, y), lead, font=f.bold, fill=palette.black)
    draw.text((width - pad - f.bold.getlength(view.where), y), view.where, font=f.bold, fill=palette.black)

    road = (y + _ROW_H * s + height) // 2
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
        tx = min(max(pad, x - font.getlength(text) / 2), width - pad - font.getlength(text))
        # Names above the road clear the bus, which sits on the line.
        ty = road - 30 * s if i % 2 == 0 else road + 9 * s
        draw.text((tx, ty), text, font=font, fill=palette.black)
    if view.bus_at is not None:
        bx = xs[0] + view.bus_at * step
        draw.rounded_rectangle((bx - 12 * s, road - 13 * s, bx + 12 * s, road - 4 * s), radius=2 * s, fill=palette.black)
        for wx in range(-9, 9, 5):
            draw.rectangle((bx + wx * s, road - 11 * s, bx + (wx + 3) * s, road - 8 * s), fill=palette.white)
    if palette.preview:
        colours = [(0, 0, 0)] * 256
        colours[palette.white] = (255, 255, 255)
        colours[palette.black] = (0, 0, 0)
        colours[palette.accent] = _PREVIEW_ACCENTS[palette.preview]
        image.putpalette([v for rgb in colours for v in rgb])
    return image
