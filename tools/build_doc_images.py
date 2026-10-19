"""Draws the board pictures in docs/images/ from the sample data.

    .venv/bin/python tools/build_doc_images.py

Run it after a change to how the board looks, then look at each picture and
commit them. The departures pictures are `twobells demo`; the pHAT ones
are scaled up three times, dot for dot, so they read on a web page. The bus
position page is drawn here, as the board draws it, for the demo's 341 with
the emulator's stops and the bus three stops away, and so is the clock page,
and a screen with trams too, for three modes of transport. The README's
pictures of busy places are drawn here too, from tools/doc_scenes.py: each
is named for its scene, like screen-bar-piccadilly.png.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from doc_scenes import (  # noqa: E402
    SCENES,
    Scene,
    board_departures,
    board_labels,
    board_notices,
    board_strips,
)

from twobells.board.displays.display import PngDisplay  # noqa: E402
from twobells.board.names import DISPLAY_PANELS  # noqa: E402
from twobells.board.platformclock import clock_view, render_clock  # noqa: E402
from twobells.board.route import RouteView  # noqa: E402
from twobells.board.themes import theme_named  # noqa: E402
from twobells.board.view import build_view  # noqa: E402
from twobells.cli.main import main as twobells  # noqa: E402
from twobells.core.model import Departure, Status  # noqa: E402
from twobells.core.walk import WalkTimes  # noqa: E402
from twobells.emulator.fake import DEMO_CALLING, DEMO_STRIP, DEMO_TITLE  # noqa: E402
from twobells.timetable.route import where  # noqa: E402

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
    "screen-flap.png": (["--display", "hdmi-1280x400", "--theme", "flap"], 1),
    "screen-bar-flap.png": (["--display", "hdmi-1920x440", "--theme", "flap"], 1),
}

# name: (scene, display, look), for the README
SCENE_PICTURES = {
    "epaper-10in85-piccadilly.png": ("piccadilly", "waveshare-10in85g", "sign"),
    "screen-bar-piccadilly.png": ("piccadilly", "hdmi-1920x440", "sign"),
    "screen-bar-flap-greenwich.png": ("greenwich", "hdmi-1920x440", "flap"),
}


def keep(drawn: Path, scale: int = 1) -> None:
    """Copies a drawn picture into docs/images/, scaled dot for dot and compressed."""
    image = Image.open(drawn)
    if scale != 1:
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    image.save(IMAGES / drawn.name, optimize=True)
    print(f"Wrote {IMAGES / drawn.name}")


def draw_demo(drawn: Path, options: list[str]) -> None:
    if twobells(["demo", "--png", str(drawn), *options]) != 0:
        raise SystemExit(f"demo failed for {drawn.name}")


def draw_bus_page(drawn: Path) -> None:
    """The second page on the screen, a little after the departures picture."""
    clock = "07:12:40"
    bus = Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A")
    view = build_view(DEMO_TITLE, {"train": [], "bus": [bus]}, clock[:5], set(), WalkTimes(train=5, bus={"*": 8}),
                      theme_named("sign").wording, clock)
    row = view.section("bus").rows[0]
    stops = tuple(s.name for s in DEMO_STRIP.stops)
    bus_at = 2.45  # between Grapes Inn and Sunlaws Street
    route = RouteView(f"{bus.service} to {bus.destination}", row.right, row.leave, stops, bus_at,
                      where(stops, bus_at, stale=False), clock)
    theme = theme_named("sign")
    display = PngDisplay(drawn, theme.accent, DISPLAY_PANELS["hdmi-1280x400"])
    display.show(theme.render_route(route, display.palette, display.panel))


def draw_clock_page(drawn: Path) -> None:
    """The clock page on the screen, on a Friday morning."""
    display = PngDisplay(drawn, theme_named("sign").accent, DISPLAY_PANELS["hdmi-1280x400"])
    display.show(render_clock(clock_view(datetime(2026, 10, 16, 7, 12, 40)), display.palette, display.panel))


def draw_three_modes(drawn: Path) -> None:
    """The 7.9" screen with trains, buses and trams, as the TFT look shares
    it between three modes: made-up trams from Piccadilly, from the
    timetable alone."""
    clock = "07:03:15"
    trains = [
        Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "1",
                  calling_at=DEMO_CALLING),
        Departure("train", "Northern", "Manchester Piccadilly", "07:42", "07:49", Status.LATE, "1"),
    ]
    buses = [
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A"),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B"),
    ]
    trams = [
        Departure("metro", "Purple Line", "Altrincham", "07:14", None, Status.UNKNOWN, stop="T"),
        Departure("metro", "Blue Line", "Bury", "07:17", None, Status.UNKNOWN, stop="T"),
    ]
    theme = theme_named("tft")
    view = build_view(DEMO_TITLE, {"train": trains, "bus": buses, "metro": trams}, clock[:5], set(),
                      WalkTimes(train=5, bus={"*": 8}, metro={"T": 6}), theme.wording, clock)
    display = PngDisplay(drawn, theme.accent, DISPLAY_PANELS["hdmi-1280x400"])
    display.show(theme.render(view, display.palette, display.panel))


def draw_scene(drawn: Path, scene: Scene, display_name: str, look: str) -> None:
    """A busy place's departures, with every mode it has: a screen with its
    running clock and where each live bus is, e-paper as it redraws. The bus
    rows say their stops' letters: a stop's whole name, as a wide screen
    shows it, is cut short at "Piccadilly Garden"."""
    theme = theme_named(look)
    display = PngDisplay(drawn, theme.accent, DISPLAY_PANELS[display_name])
    clock = None if display.panel.epaper else scene.now.strftime("%H:%M:%S")
    view = build_view(scene.title, board_departures(scene), scene.now.strftime("%H:%M"), set(), scene.walk(),
                      theme.wording, clock, notices=board_notices(scene),
                      strips=board_strips(scene) if clock else None,
                      labels=board_labels(scene))
    display.show(theme.render(view, display.palette, display.panel))


def run() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        for name, (options, scale) in DEMOS.items():
            drawn = Path(scratch) / name
            draw_demo(drawn, options)
            keep(drawn, scale)
        drawn = Path(scratch) / "screen-bus.png"
        draw_bus_page(drawn)
        keep(drawn)
        drawn = Path(scratch) / "screen-clock.png"
        draw_clock_page(drawn)
        keep(drawn)
        drawn = Path(scratch) / "screen-trams.png"
        draw_three_modes(drawn)
        keep(drawn)
        for name, (scene, display_name, look) in SCENE_PICTURES.items():
            drawn = Path(scratch) / name
            draw_scene(drawn, SCENES[scene], display_name, look)
            keep(drawn)


def main(argv: list[str] | None = None) -> None:
    # It takes no arguments: one, --help among them, stops it before it draws over docs/images/.
    argparse.ArgumentParser(description=__doc__.split("\n")[0]).parse_args(argv)
    run()


if __name__ == "__main__":
    main()
