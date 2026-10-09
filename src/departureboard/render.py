"""Draws a BoardView onto a 250x122 paletted image for the Inky pHAT."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .view import BoardView, Row, Section, Style

WIDTH, HEIGHT = 250, 122

_HEADER_H = 15
_LABEL_H = 12
_ROW_H = 14
_GAP = 2
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


def load_fonts() -> Fonts:
    return Fonts(
        regular=_font("DejaVuSans.ttf", 11),
        bold=_font("DejaVuSans-Bold.ttf", 11),
        small=_font("DejaVuSans-Bold.ttf", 9),
    )


def left_column_width(fonts: Fonts) -> float:
    return fonts.bold.getlength("00:00") + 5


@dataclass(frozen=True)
class RowLayout:
    left: str
    middle: str
    middle_x: float
    leave_x: float | None
    right_x: float


_GAP = 6


def _with_stop(row: Row) -> str:
    return f"{row.stop}: {row.middle}" if row.stop else row.middle


def row_layout(row: Row, fonts: Fonts) -> RowLayout:
    """Column positions for a row. The destination gives way so times stay whole."""
    right_x = WIDTH - _PAD - fonts.bold.getlength(row.right)
    middle_x = _PAD + left_column_width(fonts)
    leave_x = None
    middle_end = right_x - 4
    if row.leave:
        leave_x = right_x - _GAP - fonts.small.getlength(row.leave)
        middle_end = leave_x - _GAP
    return RowLayout(
        left=fit_text(row.left, fonts.bold, middle_x - _PAD - 3),
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
    def __init__(self, palette: Palette, fonts: Fonts) -> None:
        self.p = palette
        self.f = fonts
        self.image = Image.new("P", (WIDTH, HEIGHT), palette.white)
        self.draw = ImageDraw.Draw(self.image)

    def header(self, view: BoardView) -> None:
        self.draw.rectangle((0, 0, WIDTH - 1, _HEADER_H - 1), fill=self.p.black)
        stamp = f"Upd {view.updated}"
        stamp_x = WIDTH - _PAD - self.f.regular.getlength(stamp)
        title = fit_text(view.title, self.f.bold, stamp_x - _PAD - 6)
        self.draw.text((_PAD, 1), title, font=self.f.bold, fill=self.p.white)
        self.draw.text((stamp_x, 1), stamp, font=self.f.regular, fill=self.p.white)

    def section(self, section: Section, y: int) -> int:
        label = section.label.upper()
        self.draw.text((_PAD, y + 1), label, font=self.f.small, fill=self.p.black)
        label_end = _PAD + self.f.small.getlength(label)
        if section.stale:
            x = label_end + 6
            self.draw.text((x, y + 1), "! STALE", font=self.f.small, fill=self.p.accent)
            label_end = x + self.f.small.getlength("! STALE")
        if section.leave:
            leave = fit_text(section.leave, self.f.small, WIDTH - _PAD - label_end - 8)
            x = WIDTH - _PAD - self.f.small.getlength(leave)
            self.draw.text((x, y + 1), leave, font=self.f.small, fill=self.p.black)
        y += _LABEL_H
        rows = section.rows[: _MAX_ROWS.get(section.label, 2)]
        if not rows:
            self.draw.text((_PAD, y + 1), section.empty_text, font=self.f.regular, fill=self.p.black)
            return y + _ROW_H
        for row in rows:
            self.row(row, y)
            y += _ROW_H
        return y

    def row(self, row: Row, y: int) -> None:
        ink, status_ink = self.p.black, self.p.black
        if row.style is Style.INVERTED:
            self.draw.rectangle((0, y, WIDTH - 1, y + _ROW_H - 1), fill=self.p.black)
            ink = status_ink = self.p.white
        elif row.style is Style.ALERT:
            status_ink = self.p.accent

        layout = row_layout(row, self.f)
        self.draw.text((_PAD, y + 1), layout.left, font=self.f.bold, fill=ink)
        self.draw.text((layout.middle_x, y + 1), layout.middle, font=self.f.regular, fill=ink)
        if layout.leave_x is not None:
            # Smaller type sits on the same baseline as the row text.
            self.draw.text((layout.leave_x, y + 3), row.leave, font=self.f.small, fill=ink)
        self.draw.text((layout.right_x, y + 1), row.right, font=self.f.bold, fill=status_ink)

    def rule(self, y: int) -> None:
        self.draw.line((_PAD, y, WIDTH - _PAD, y), fill=self.p.black)

    def apply_preview_palette(self) -> None:
        if not self.p.preview:
            return
        colours = [(0, 0, 0)] * 256
        colours[self.p.white] = (255, 255, 255)
        colours[self.p.black] = (0, 0, 0)
        colours[self.p.accent] = _PREVIEW_ACCENTS[self.p.preview]
        self.image.putpalette([v for rgb in colours for v in rgb])


def render(view: BoardView, palette: Palette, fonts: Fonts | None = None) -> Image.Image:
    painter = _Painter(palette, fonts or load_fonts())
    painter.header(view)
    y = painter.section(view.trains, _HEADER_H + _GAP)
    painter.rule(y + 1)
    painter.section(view.buses, y + _GAP + 1)
    painter.apply_preview_palette()
    return painter.image
