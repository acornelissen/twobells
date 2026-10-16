"""Draws the board pictures in docs/images/ from the sample data.

    .venv/bin/python tools/build_doc_images.py

Run it after a change to how the board looks, then look at each picture and
commit them. The departures pictures are `twobells demo`; the pHAT ones
are scaled up three times, dot for dot, so they read on a web page. The bus
position page is drawn here, as the board draws it, for the demo's 341 with
the emulator's stops and the bus three stops away.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from twobells.board.displays.display import DISPLAY_PANELS, PngDisplay  # noqa: E402
from twobells.board.route import RouteView, where  # noqa: E402
from twobells.board.themes import theme_named  # noqa: E402
from twobells.board.view import build_view  # noqa: E402
from twobells.cli.main import main  # noqa: E402
from twobells.core.model import Departure, Status  # noqa: E402
from twobells.core.walk import WalkTimes  # noqa: E402
from twobells.emulator.fake import DEMO_STRIP, DEMO_TITLE  # noqa: E402

IMAGES = ROOT / "docs" / "images"

# name: (demo options, scale)
DEMOS = {
    "preview.png": (["--display", "inky", "--theme", "sign"], 1),
    "phat-sign.png": (["--display", "inky", "--theme", "sign"], 3),
    "phat-rain.png": (["--display", "inky", "--theme", "sign", "--rain"], 3),
    "phat-notice.png": (["--display", "inky", "--theme", "sign", "--notice"], 3),
    "phat-plain.png": (["--display", "inky", "--theme", "plain"], 3),
    "epaper-10in85.png": (["--display", "waveshare-10in85g", "--theme", "sign"], 1),
    "screen-departures.png": (["--display", "hdmi-1280x400", "--theme", "sign"], 1),
    "screen-bar.png": (["--display", "hdmi-1920x440", "--theme", "sign"], 1),
    "screen-tft.png": (["--display", "hdmi-1280x400", "--theme", "tft"], 1),
    "screen-bar-tft.png": (["--display", "hdmi-1920x440", "--theme", "tft"], 1),
}


def keep(drawn: Path, scale: int = 1) -> None:
    """Copies a drawn picture into docs/images/, scaled dot for dot and compressed."""
    image = Image.open(drawn)
    if scale != 1:
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    image.save(IMAGES / drawn.name, optimize=True)
    print(f"Wrote {IMAGES / drawn.name}")


def draw_demo(drawn: Path, options: list[str]) -> None:
    if main(["demo", "--png", str(drawn), *options]) != 0:
        raise SystemExit(f"demo failed for {drawn.name}")


def draw_bus_page(drawn: Path) -> None:
    """The second page on the screen, a little after the departures picture."""
    clock = "07:12:40"
    bus = Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A")
    view = build_view(DEMO_TITLE, [], [bus], clock[:5], set(), WalkTimes(train=5, bus={"*": 8}),
                      theme_named("sign").wording, clock)
    row = view.buses.rows[0]
    stops = tuple(s.name for s in DEMO_STRIP.stops)
    bus_at = 2.45  # between Grapes Inn and Sunlaws Street
    route = RouteView(f"{bus.service} to {bus.destination}", row.right, row.leave, stops, bus_at,
                      where(stops, bus_at, stale=False), clock)
    theme = theme_named("sign")
    display = PngDisplay(drawn, theme.accent, DISPLAY_PANELS["hdmi-1280x400"])
    display.show(theme.render_route(route, display.palette, display.panel))


def run() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        for name, (options, scale) in DEMOS.items():
            drawn = Path(scratch) / name
            draw_demo(drawn, options)
            keep(drawn, scale)
        drawn = Path(scratch) / "screen-bus.png"
        draw_bus_page(drawn)
        keep(drawn)


if __name__ == "__main__":
    run()
