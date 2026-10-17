"""Split-flap look: white letters on dark tiles with a split across each, like a Solari board.

The plain look's layout, drawn on black: every letter of the title, the
clock, and a row's time, destination, platform and status sits on a tile of
its own, the tiles all one width apart; the small words (section labels,
leave times, where a train is) stay in Inter between them. A late status is
amber, a cancelled one black on amber tiles, and the words say it either
way. On a screen, a row's tiles (its time, destination, platform and
status) turn over to new letters when they change (board/flaps.py); the
title and the clock change at once, so the clock doesn't set the screen
animating every second.

For screens only: e-paper has no grey for the tiles and can't animate.
"""

from __future__ import annotations

import functools
from functools import partial

from PIL import Image, ImageDraw, ImageFont

from .flaps import Flap
from .panel import PHAT, Panel
from .render import (
    _BOLD,
    _FONT_DIR,
    PAD,
    TEXT_SIZE,
    Fonts,
    Painter,
    Palette,
    apply_preview,
    load_fonts,
    paint_board,
    paint_route,
    plain_scale,
    row_layout,
)
from .route import RouteView
from .scroll import Frame
from .view import BoardView, Row, Section, Style

# In pHAT pixels: tiles this far apart, with this much black between them,
# this tall, and the letters on them this big. Tiles take more room than
# Inter: the bar screen keeps "Manchester Piccadilly" whole, but beside the
# where column the 7.9" screen has room for 16 letters, and longer scrolls.
TILE_PITCH = 7
TILE_GAP = 1
TILE_HEIGHT = 12
GLYPH_SIZE = 10


class TileFont(ImageFont.FreeTypeFont):
    """Inter ExtraBold, measured as tiles: every letter TILE_PITCH apart, so
    the plain look's layout, which measures with it, lines the tiles up.
    Drawn only by SplitFlapPainter, letter by letter onto tiles."""

    def __init__(self, scale: int) -> None:
        super().__init__(str(_FONT_DIR / _BOLD), GLYPH_SIZE * scale, layout_engine=ImageFont.Layout.BASIC)
        self.scale = scale
        self.pitch = TILE_PITCH * scale
        self.gap = TILE_GAP * scale
        self.tile_height = TILE_HEIGHT * scale

    def width(self, text: str) -> int:
        """How far `text`'s tiles reach, from the left of the first to the right of the last."""
        return len(text) * self.pitch - self.gap if text else 0

    def getlength(self, text, mode="", direction=None, features=None, language=None) -> float:
        return len(text) * self.pitch

    def getbbox(self, text, mode="", direction=None, features=None, language=None, stroke_width=0,
                anchor=None) -> tuple[float, float, float, float]:
        return 0, 0, self.width(str(text)), self.tile_height


@functools.lru_cache(maxsize=4)
def tile_font(scale: int) -> TileFont:
    return TileFont(scale)


@functools.lru_cache(maxsize=2048)
def _tile(font: TileFont, letter: str, ink: int, fill: int, paper: int) -> Image.Image:
    """One letter on its tile: rounded corners on `paper`, the letter in
    `ink` on `fill`, squeezed to fit if it is wider than the tile, and the
    split across the middle in `paper`."""
    s = font.scale
    width, height = font.pitch - font.gap, font.tile_height
    tile = Image.new("P", (width, height), paper)
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=s, fill=fill)
    if letter.strip():
        # Measured with the font's own metrics, not the tiles'.
        left, top, right, bottom = ImageFont.FreeTypeFont.getbbox(font, letter)
        _, cap_top, _, cap_bottom = ImageFont.FreeTypeFont.getbbox(font, "H")
        glyph = Image.new("L", (max(1, int(right - left)), height), 0)
        # Capitals sit in the middle of the tile; descenders hang below.
        y = (height - (cap_bottom - cap_top)) / 2 - cap_top
        ImageDraw.Draw(glyph).text((-left, y), letter, font=font, fill=255)
        room = width - s
        if glyph.width > room:
            glyph = glyph.resize((room, height), Image.Resampling.BOX)
        # Paletted panels have no shades: the glyph's edge falls at half its ink.
        mask = glyph.point([0] * 128 + [255] * 128).convert("1")
        tile.paste(ink, ((width - glyph.width) // 2, 0), mask)
    split = height // 2
    draw.line((0, split, width - 1, split), fill=paper, width=max(1, s // 2))
    return tile


def draw_tiles(image: Image.Image, xy: tuple[float, float], text: str, font: TileFont, ink: int, fill: int,
               paper: int) -> None:
    """`text` a letter a tile, the first tile's top left at `xy`."""
    x, y = int(xy[0]), int(xy[1])
    for i, letter in enumerate(text):
        image.paste(_tile(font, letter, ink, fill, paper), (x + i * font.pitch, y))


def tiles_image(text: str, font: TileFont, ink: int, fill: int, paper: int, palette: Palette) -> Image.Image:
    """`text` on its tiles, as an image of their own: a field's box as a frame of it turning over shows it."""
    image = Image.new("P", (font.width(text), font.tile_height), paper)
    draw_tiles(image, (0, 0), text, font, ink, fill, paper)
    apply_preview(image, palette)
    return image


def status_colours(row: Row, palette: Palette) -> tuple[int, int]:
    """(letters, tiles) for a row's status: black on amber tiles when it is
    cancelled, amber when late, otherwise white, as the rest of the row."""
    tile = palette.black if palette.tile is None else palette.tile
    if row.style is Style.INVERTED:
        return palette.black, palette.accent
    if row.style is Style.ALERT:
        return palette.accent, tile
    return palette.white, tile


class SplitFlapPainter(Painter):
    def __init__(self, palette: Palette, panel: Panel, live: bool, scroll: bool = True) -> None:
        s = plain_scale(panel)
        tiles = tile_font(s)
        fonts = Fonts(regular=tiles, bold=tiles, small=load_fonts(s).small)
        super().__init__(palette, fonts, panel, TEXT_SIZE, live, dark=True, scroll=scroll)
        self.tiles = tiles
        self.tile_fill = palette.black if palette.tile is None else palette.tile
        self.flaps: list[Flap] = []
        # The section being drawn and how many of its rows are drawn so far.
        self._section: str | None = None
        self._index = 0
        # The row being drawn, whose fields turn over when they change: its
        # section and place in it, not its top, which the "Calling at" line moves.
        self._row: tuple[str, int] | None = None

    def text(self, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill: int,
             image: Image.Image | None = None) -> None:
        """Tiles for the tile font, Inter for the small words."""
        if not isinstance(font, TileFont):
            super().text(xy, text, font, fill, image)
            return
        self.letters(xy, text, fill, self.tile_fill, image)

    def field(self, xy: tuple[float, float], shown: str, whole: str, font: ImageFont.FreeTypeFont, room: float,
              fill: int, paper: int, band: tuple[int, int]) -> None:
        """As the plain look's, but a field on tiles is whole tiles wide, so
        scrolling text isn't cut part way across one."""
        if isinstance(font, TileFont):
            room = max(0, (room + font.gap) // font.pitch * font.pitch - font.gap)
        super().field(xy, shown, whole, font, room, fill, paper, band)

    def letters(self, xy: tuple[float, float], text: str, ink: int, fill: int, image: Image.Image | None = None,
                right: bool = False) -> None:
        """`text` on tiles. A field of the row being drawn, on the board
        itself and not scrolling, turns over when it changes: its key is the
        row's section and place in it, and where the field starts, or `right`
        for the status, which is lined up on the right."""
        draw_tiles(self.image if image is None else image, xy, text, self.tiles, ink, fill, self.paper)
        if image is not None or self._row is None or not self.live or not text:
            return
        x, y = int(xy[0]), int(xy[1])
        key = (*self._row, "right" if right else x)
        box = (x, y, x + self.tiles.width(text), y + self.tiles.tile_height)
        paint = partial(tiles_image, font=self.tiles, ink=ink, fill=fill, paper=self.paper, palette=self.p)
        self.flaps.append(Flap(key, box, text, paint, right))

    def banner(self, shown: str, whole: str, room: float, stamp: str, stamp_x: float,
               rain: tuple[str, float] | None = None) -> None:
        """No bar: the title and the clock on tiles, the rain warning beside the clock."""
        s, pad = self.s, PAD * self.s
        bottom = self.header_h * s
        self.field((pad, s), shown, whole, self.f.bold, room, self.ink, self.paper, (0, bottom))
        if rain is not None:
            self.letters((rain[1], s), rain[0], self.p.accent, self.tile_fill)
        self.letters((stamp_x, s), stamp, self.ink, self.tile_fill)

    def section(self, section: Section, y: int, where_x: float | None = None) -> int:
        """As the plain look's, counting the section's rows for their fields' keys."""
        self._section, self._index = section.label, 0
        try:
            return super().section(section, y, where_x)
        finally:
            self._section = None

    def row(self, row: Row, y: int, platform_x: float | None = None, leave_x: float | None = None,
            where_x: float | None = None, train: bool = True, stop_room: float | None = None) -> None:
        """A row on tiles, its status coloured as status_colours says. A
        cancelled row isn't reversed out: its amber tiles say it."""
        layout = row_layout(row, self.f, self.width, self.s, platform_x, leave_x, where_x, stop_room)
        if self._section is not None:
            self._row = (self._section, self._index)
            self._index += 1
        try:
            self.row_text(row, layout, y, train, self.ink, self.paper, self.p.accent)
            ink, fill = status_colours(row, self.p)
            self.letters((layout.right_x, y + self.s), row.right, ink, fill, right=True)
        finally:
            self._row = None

    def frame(self) -> Frame:
        frame = super().frame()
        return Frame(frame.image, frame.scrolls, tuple(self.flaps))


def render_split_flap(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The board, any scrolling text at its start."""
    return split_flap_frame(view, palette, panel).image


def split_flap_frame(view: BoardView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The board, and on a screen the text that scrolls and the fields that turn over."""
    return paint_board(SplitFlapPainter(palette, panel, live=view.clock is not None, scroll=view.scroll), view, panel)


def render_route_split_flap(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Image.Image:
    """The bus's position on the stops before yours, in the split-flap look."""
    return route_split_flap_frame(view, palette, panel).image


def route_split_flap_frame(view: RouteView, palette: Palette, panel: Panel = PHAT) -> Frame:
    """The route page, and on a screen the text that scrolls."""
    return paint_route(SplitFlapPainter(palette, panel, live=view.clock is not None, scroll=view.scroll), view, panel)
