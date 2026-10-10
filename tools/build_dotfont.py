"""Builds the dashboard's dot-matrix face from the e-paper sign's font.

    uv run python tools/build_dotfont.py

Run it after changing src/departureboard/board/dotfont.py and commit the
result; tests/tools/test_build_dotfont.py fails until you do. It writes, under
src/departureboard/dashboard_static/fonts/:

- DepartureboardDots.woff2: every glyph in dotfont.GLYPHS, each lit dot a
  square on the font's grid, at the widths the sign draws them (digits share
  one width, the rest are proportional). The Board look uses it for times,
  destinations and status.
- LICENSE: a note saying where it comes from, and the project's licence.
- SHA256SUMS: what tests/dashboard/test_static.py checks the files against.

The output is the same on every run: no timestamps, and the glyphs in
dotfont.py's order. fonttools (with brotli, for woff2) is in the dev
dependency group.
"""

from __future__ import annotations

import hashlib
import io
import sys
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.misc.timeTools import timestampSinceEpoch
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from departureboard.board.dotfont import _GAP, GLYPHS, HEIGHT  # noqa: E402

OUT = ROOT / "src" / "departureboard" / "dashboard_static" / "fonts"
FONT_FILE = "DepartureboardDots.woff2"
FAMILY = "Departureboard Dots"

# One dot is a 100-unit cell; the lit part is a square inset on every side,
# leaving the dark gap between LEDs that the sign draws on its bigger panels.
CELL = 100
DOT = 80
INSET = (CELL - DOT) // 2
ABOVE = 7  # rows above the baseline; the other HEIGHT - ABOVE are descenders
UNITS_PER_EM = 1000
ASCENT = (ABOVE + 1) * CELL  # a row of room above the capitals
DESCENT = (HEIGHT - ABOVE) * CELL
# A fixed date, so a rebuild with unchanged glyphs is byte for byte the same.
BUILT = timestampSinceEpoch(1767225600)  # 2026-01-01 00:00 UTC


def _glyph_name(char: str) -> str:
    return f"uni{ord(char):04X}"


def _outline(rows: list[str]):
    pen = TTGlyphPen(None)
    for row, line in enumerate(rows):
        bottom = (ABOVE - 1 - row) * CELL + INSET
        for column, dot in enumerate(line):
            if dot != "#":
                continue
            left = column * CELL + INSET
            # Clockwise, as TrueType wants an outer contour.
            pen.moveTo((left, bottom))
            pen.lineTo((left, bottom + DOT))
            pen.lineTo((left + DOT, bottom + DOT))
            pen.lineTo((left + DOT, bottom))
            pen.closePath()
    return pen.glyph()


def _left_bearing(rows: list[str]) -> int:
    columns = [column for line in rows for column, dot in enumerate(line) if dot == "#"]
    return min(columns) * CELL + INSET if columns else 0


def font() -> TTFont:
    """The face as an in-memory TrueType font, before it is packed as woff2."""
    names = {char: _glyph_name(char) for char in GLYPHS}
    order = [".notdef", *names.values()]
    builder = FontBuilder(UNITS_PER_EM, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap({ord(char): name for char, name in names.items()})
    glyphs = {".notdef": TTGlyphPen(None).glyph()}
    metrics = {".notdef": ((5 + _GAP) * CELL, 0)}
    for char, rows in GLYPHS.items():
        glyphs[names[char]] = _outline(rows)
        metrics[names[char]] = ((len(rows[0]) + _GAP) * CELL, _left_bearing(rows))
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=ASCENT, descent=-DESCENT)
    copyright_line = next(line for line in _licence().splitlines() if line.startswith("Copyright"))
    builder.setupNameTable({
        "copyright": copyright_line,
        "familyName": FAMILY,
        "styleName": "Regular",
        "uniqueFontIdentifier": f"{FAMILY} Regular",
        "fullName": f"{FAMILY} Regular",
        "version": "Version 1.000",
        "psName": FAMILY.replace(" ", "") + "-Regular",
        "description": "Drawn for departureboard; built from src/departureboard/board/dotfont.py.",
        "licenseDescription": "MIT License",
    })
    builder.setupOS2(
        sTypoAscender=ASCENT, sTypoDescender=-DESCENT, sTypoLineGap=0,
        usWinAscent=ASCENT, usWinDescent=DESCENT,
        sCapHeight=ABOVE * CELL, sxHeight=5 * CELL,
        fsSelection=0x40 | 0x80,  # REGULAR, USE_TYPO_METRICS
        achVendID="NONE", version=4,
    )
    builder.setupPost(keepGlyphNames=False)
    head = builder.font["head"]
    head.created = head.modified = BUILT
    builder.font.recalcTimestamp = False
    return builder.font


def _licence() -> str:
    return (ROOT / "LICENSE").read_text(encoding="utf-8")


def files() -> dict[str, bytes]:
    """Every file the folder holds but SHA256SUMS, by name."""
    face = font()
    face.flavor = "woff2"
    out = io.BytesIO()
    face.save(out)
    note = (
        f"{FAMILY}\n\n"
        "The dot-matrix face of the dashboard's Board look. It is drawn for this\n"
        "project: tools/build_dotfont.py builds it from the glyphs in\n"
        "src/departureboard/board/dotfont.py, the font the e-paper sign uses, each\n"
        "lit dot a square. It comes under the project's licence, which follows.\n\n"
    )
    return {FONT_FILE: out.getvalue(), "LICENSE": (note + _licence()).encode("utf-8")}


def main() -> None:
    built = files()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in built.items():
        (OUT / name).write_bytes(body)
    sums = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(built.items()))
    (OUT / "SHA256SUMS").write_text(sums)
    print(f"wrote {OUT.relative_to(ROOT)}: {FONT_FILE} {len(built[FONT_FILE]):,} bytes, {len(GLYPHS)} glyphs")


if __name__ == "__main__":
    main()
