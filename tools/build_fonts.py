"""Builds the dashboard's typeface from the Inter release.

    uv run python tools/build_fonts.py                 # downloads about 34 MB
    uv run python tools/build_fonts.py --zip Inter-4.1.zip

Run it when moving to a new Inter release (update INTER_URL and INTER_SHA256)
and commit the result. It writes, under
src/departureboard/dashboard_static/vendor/inter-4.1/:

- InterVariable.woff2: the upright variable font (weight and optical size
  axes), cut down to Latin text and the punctuation the page uses, with
  every OpenType feature kept (tnum for times, cv05, cv11 and the rest).
- LICENSE: the SIL Open Font License 1.1 it comes under, as released.
- SHA256SUMS: what tests/dashboard/test_static.py checks the files against.

and, for the board's plain look, under src/departureboard/board/fonts/:

- Inter-Regular.ttf, Inter-ExtraBold.ttf: the release's static TrueType fonts,
  hinted for small sizes on one-bit panels, cut down to the same Latin, with
  tabular figures and the dashboard's tailed l (cv05) made the default
  glyphs: Pillow draws without OpenType features on most installs.
- LICENSE and SHA256SUMS, checked by tests/board/test_render.py.

The download is checked against INTER_SHA256 before anything is read from it.
fonttools (with brotli, for woff2) is in the dev dependency group.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import zipfile
from pathlib import Path

from fontTools import subset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from departureboard.core.net import get_bytes  # noqa: E402

INTER_URL = "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip"
INTER_SHA256 = "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e"
OUT = ROOT / "src" / "departureboard" / "dashboard_static" / "vendor" / "inter-4.1"

# Basic Latin, Latin-1 (°, ·, £, accented names), the Latin Extended-A letters
# in British and Irish place names, and the punctuation the page writes:
# dashes, curly quotes, bullet, ellipsis, primes, arrows and the minus sign.
UNICODES = (
    "U+0020-007E,U+00A0-00FF,U+0100-017F,U+2013-2014,U+2018-201E,U+2022,U+2026,"
    "U+2032-2033,U+2039-203A,U+20AC,U+2122,U+2190-2193,U+2212"
)


def release(zip_path: Path | None) -> bytes:
    data = zip_path.read_bytes() if zip_path else get_bytes(INTER_URL, 64_000_000, timeout=60)
    digest = hashlib.sha256(data).hexdigest()
    if digest != INTER_SHA256:
        sys.exit(f"Inter release has SHA-256 {digest}, expected {INTER_SHA256}")
    return data


def build(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        ttf = archive.read("InterVariable.ttf")
        licence = archive.read("LICENSE.txt")
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.notdef_outline = True
    font = subset.load_font(io.BytesIO(ttf), options)
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=subset.parse_unicodes(UNICODES))
    subsetter.subset(font)
    out = io.BytesIO()
    subset.save_font(font, out, options)
    OUT.mkdir(parents=True, exist_ok=True)
    files = {"InterVariable.woff2": out.getvalue(), "LICENSE": licence}
    for name, body in files.items():
        (OUT / name).write_bytes(body)
    sums = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(files.items()))
    (OUT / "SHA256SUMS").write_text(sums)
    print(f"wrote {OUT.relative_to(ROOT)}: InterVariable.woff2 {len(files['InterVariable.woff2']):,} bytes")


BOARD_OUT = ROOT / "src" / "departureboard" / "board" / "fonts"
BOARD_FONTS = ("Inter-Regular.ttf", "Inter-ExtraBold.ttf")


def _feature_mapping(font, tag: str) -> dict[str, str]:
    """The single substitutions an OpenType feature makes, glyph name to glyph name."""
    gsub = font["GSUB"].table
    mapping = {}
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag != tag:
            continue
        for index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[index]
            for table in lookup.SubTable:
                table = getattr(table, "ExtSubTable", table)
                mapping.update(getattr(table, "mapping", {}))
    return mapping


def _freeze(font) -> None:
    """Point the cmap at the glyphs the dashboard asks for: tabular digits
    (only the digits: Inter's tnum widens brackets too) and the tailed l."""
    tnum = _feature_mapping(font, "tnum")
    swaps = {name: tnum[name] for name in ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
                                           "nine")}
    swaps |= _feature_mapping(font, "cv05")
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap = {code: swaps.get(name, name) for code, name in table.cmap.items()}


def build_board(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        ttfs = {name: archive.read(f"extras/ttf/{name}") for name in BOARD_FONTS}
        licence = archive.read("LICENSE.txt")
    files = {"LICENSE": licence}
    for name, ttf in ttfs.items():
        options = subset.Options()
        options.name_IDs = ["*"]
        options.notdef_outline = True
        options.hinting = True
        font = subset.load_font(io.BytesIO(ttf), options)
        _freeze(font)
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=subset.parse_unicodes(UNICODES))
        subsetter.subset(font)
        out = io.BytesIO()
        subset.save_font(font, out, options)
        files[name] = out.getvalue()
    BOARD_OUT.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (BOARD_OUT / name).write_bytes(body)
    sums = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(files.items()))
    (BOARD_OUT / "SHA256SUMS").write_text(sums)
    sizes = ", ".join(f"{name} {len(files[name]):,} bytes" for name in BOARD_FONTS)
    print(f"wrote {BOARD_OUT.relative_to(ROOT)}: {sizes}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--zip", type=Path, help="a downloaded Inter release zip, instead of downloading it")
    args = parser.parse_args()
    data = release(args.zip)
    build(data)
    build_board(data)


if __name__ == "__main__":
    main()
