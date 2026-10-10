"""The Board look's dot-matrix face, as committed, is what tools/build_dotfont.py
builds from board/dotfont.py today: change a glyph without rebuilding and this fails."""

import hashlib
import importlib.util

import pytest
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont

from departureboard.board.dotfont import GLYPHS
from tests.paths import ROOT

_SCRIPT = ROOT / "tools" / "build_dotfont.py"
FONTS = ROOT / "src" / "departureboard" / "dashboard_static" / "fonts"


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("build_dotfont", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _shapes(font):
    """Each character's outline and advance width, by character."""
    glyphs = font.getGlyphSet()
    shapes = {}
    for code, name in font.getBestCmap().items():
        pen = RecordingPen()
        glyphs[name].draw(pen)
        shapes[chr(code)] = (pen.value, font["hmtx"][name][0])
    return shapes


def test_the_committed_face_matches_dotfont(build):
    committed = TTFont(FONTS / build.FONT_FILE)
    assert committed.flavor == "woff2"
    rebuilt = build.font()
    assert set(_shapes(committed)) == set(GLYPHS)
    assert _shapes(committed) == _shapes(rebuilt), "run tools/build_dotfont.py and commit the result"
    for table, field in [("head", "unitsPerEm"), ("hhea", "ascent"), ("hhea", "descent"), ("OS/2", "sCapHeight")]:
        assert getattr(committed[table], field) == getattr(rebuilt[table], field), field
    assert committed["name"].getDebugName(1) == "Departureboard Dots"


def test_a_glyph_is_its_lit_dots_at_the_signs_width(build):
    shapes = _shapes(build.font())
    for char, rows in GLYPHS.items():
        contours, advance = shapes[char]
        assert sum(op == "closePath" for op, _ in contours) == sum(row.count("#") for row in rows), char
        assert advance == (len(rows[0]) + 1) * build.CELL, char
    # Digits share one width, so times line up.
    assert len({shapes[d][1] for d in "0123456789"}) == 1


def test_the_build_is_the_same_every_time(build):
    assert build.files() == build.files()


def test_the_folder_is_pinned_and_licensed(build):
    listed = {}
    for line in (FONTS / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        listed[name] = digest
    assert set(listed) == {build.FONT_FILE, "LICENSE"}
    for name, digest in listed.items():
        assert hashlib.sha256((FONTS / name).read_bytes()).hexdigest() == digest, name
    assert {p.name for p in FONTS.iterdir()} == set(listed) | {"SHA256SUMS"}, "a font file is not pinned"
    assert (FONTS / "LICENSE").read_bytes() == build.files()["LICENSE"]
    assert (ROOT / "LICENSE").read_text() in (FONTS / "LICENSE").read_text()
