import hashlib
import json
from dataclasses import replace

import pytest

from tests.paths import FIXTURES
from twobells.board.dotfont import HEIGHT as GLYPH_H
from twobells.board.dotfont import TINY_HEIGHT
from twobells.board.names import themes_for
from twobells.board.panel import PANELS, most_modes
from twobells.board.render import SCREEN_PALETTE, Painter, Palette, render
from twobells.board.route import RouteView
from twobells.board.sign import item_size, render_sign, sign_layout
from twobells.board.splitflap import render_split_flap
from twobells.board.tft import render_tft
from twobells.board.themes import THEMES, theme_named, with_text_size
from twobells.board.view import PLAIN, SIGN, BoardView, CodeView, Row, Section, SetupView, Style, build_view
from twobells.core.model import Departure, Status, Strip
from twobells.core.walk import WalkTimes
from twobells.emulator.fake import DEMO_CALLING, DEMO_TITLE
from twobells.settings.modes import KEYS


def test_plain_theme():
    theme = theme_named("plain")
    assert (theme.wording, theme.render, theme.accent) == (PLAIN, render, "red")


def test_sign_theme_uses_yellow_as_amber():
    theme = theme_named("sign")
    assert (theme.wording, theme.render, theme.accent) == (SIGN, render_sign, "yellow")


def test_tft_theme_is_the_plain_wording_in_yellow_for_screens_only():
    theme = theme_named("tft")
    assert (theme.wording, theme.render, theme.accent, theme.screen_only) == (PLAIN, render_tft, "yellow", True)


def test_split_flap_theme_is_the_plain_wording_in_yellow_for_screens_only():
    theme = theme_named("flap")
    assert (theme.wording, theme.render, theme.accent, theme.screen_only) == (PLAIN, render_split_flap, "yellow",
                                                                              True)


def test_unknown_theme_lists_the_choices():
    with pytest.raises(ValueError, match=r"flap, plain, sign, tft"):
        theme_named("neon")


def test_registry_names_match():
    assert all(theme.name == name for name, theme in THEMES.items())


def test_every_look_has_a_label_for_the_settings():
    assert all(theme.label for theme in THEMES.values())


# --- Every page, pixel for pixel as recorded ---
#
# Each look draws a set of pages on every panel, and each image's digest must
# match the one recorded in the fixture, first by the code from before the
# plain look kept its text measurements and glyphs between redraws: keeping
# them must not change a pixel. After a deliberate change to how a board
# looks, record them again with:
#   .venv/bin/python -c "from tests.board.test_themes import record_digests; record_digests()"

DIGESTS = FIXTURES / "board_digests.json"
PALETTE = Palette(white=0, black=1, accent=2)
CLOCK = "07:03:15"
NOTICE = ("Disruption between Glossop and Manchester Piccadilly: trains may be cancelled or delayed by up to "
          "20 minutes. Check before you travel.")


def _trains(leave):
    return Section("Trains", (
        Row("07:12", "Manchester Piccadilly", "On time", platform="Plat 1", strip=Strip("Left Hadfield", 4, 2, 0.5)),
        Row("07:42", "Manchester Piccadilly", "Exp 07:49", Style.ALERT, platform="Plat 1",
            strip=Strip("At Dinting", 4, 2, 1.0)),
        Row("08:12", "Manchester Piccadilly", "Cancelled", Style.INVERTED, platform="Plat 2"),
    ), "No trains", leave=leave, leave_for=0)


def _buses(leaves, leave_ats=(None, None, None)):
    return Section("Buses", (
        Row("341", "Hyde", "Exp 07:24", Style.ALERT, leaves[0], "A", "Henry Street A", leave_at=leave_ats[0],
            strip=Strip("3 stops away", 7, 5, 2.4)),
        Row("61", "Buxton", "07:35 sch", leave=leaves[1], stop="B", stop_name="Henry Street B", leave_at=leave_ats[1]),
        Row("237", "Ashton", "07:41", leave=leaves[2], stop="A", stop_name="Henry Street A", leave_at=leave_ats[2],
            strip=Strip("Not started", 7, 5, None)),
    ), "No buses", stop_names=("Henry Street A", "Henry Street B"), mode="bus")


def demo_page(look, screen):
    """The demo's Glossop morning, with a notice and the rain warning, as each look's view of it."""
    plain = THEMES[look].wording is PLAIN  # says which train beside the section's label
    if screen:
        trains = _trains("Leave in 3 min (07:12)" if plain else "Leave in 3 min")
        buses = _buses(("Leave in 12 min", "Leave in 23 min", "Leave in 29 min"))
    else:
        trains = _trains("Leave by 07:07 (07:12)" if plain else "Leave by 07:07")
        buses = _buses(("Leave by 07:16", "Leave by 07:27", "Leave by 07:33"), ("07:16", "07:27", "07:33"))
    return BoardView("Glossop > Manchester Picc", (trains, buses), "07:03", CLOCK if screen else None, NOTICE,
                     "Rain from 07:03 (70%)")


def crowded_page(screen):
    """Long words everywhere and a feed not updating: much of it cut short, or scrolling on a screen."""
    long_stop = "Glossop Norfolk Street Bus Station Stand 3"
    trains = Section("Trains", (
        Row("23:59", "Manchester Piccadilly via Hadfield, Dinting and Broadbottom", "Delayed", Style.ALERT,
            platform="Plat 12", strip=Strip("Between Dinting and Broadbottom stations", 8, 6, 3.3)),
        Row("10:00", "Hyde", "On time", platform="Plat 1", strip=Strip("At platform", 3, 1, 1.0)),
    ), "No trains", stale=True, leave="Leave now for the 23:59 to Manchester Piccadilly", leave_for=0)
    buses = Section("Buses", (
        Row("X57A", "Sheffield Interchange via Snake Pass and Ladybower", "Exp 23:58", Style.INVERTED,
            "Leave in 1 min", "C", long_stop, strip=Strip("Leaving Glossop Norfolk Street", 5, 4, 0.0)),
        Row("6", "Glossop Circular", "Due", stop="D", leave_at="23:50", leave="Leave by 23:50"),
    ), "No buses", stop_names=(long_stop, "Henry Street B"), mode="bus")
    return BoardView("Glossop > Sheffield and everywhere in between", (trains, buses), "23:47",
                     "23:47:59" if screen else None, None, ("Heavy rain from 23:50 (95%) until the morning, with thunder and hail over the Pennines "
                      "and flooding on the Snake Pass"))


def empty_page(screen):
    return BoardView("GLO > MAN", (Section("Trains", (), "No trains"), Section("Buses", (), "No buses", stale=True, mode="bus")),
                     "07:03", CLOCK if screen else None)


ROUTE_PAGE = RouteView("341 to Hyde via Glossop Norfolk Street, Hadfield, Padfield, Tintwistle and Hollingworth", "Exp 07:24", "Leave in 6 min",
                       ("Glossop Norfolk Street Bus Station", "Henry Street", "Hadfield Station Road",
                        "Padfield Main Road Post Office and General Stores, opposite the church", "Your stop at the end of the road"), 1.6, "3 stops away")


def drawn_pages():
    """(name, image) for each page each look draws on each panel; a screen's
    pages also with their text part way through scrolling. A screen-only
    look on screens alone, and a screen's pages in a screen's palette."""
    for look in sorted(THEMES):
        for panel in PANELS.values():
            screen = not panel.epaper
            if THEMES[look].screen_only and not screen:
                continue
            palette = SCREEN_PALETTE if screen else PALETTE
            themes = [("", theme_named(look))]
            if look == "plain":
                themes.append(("-text13", with_text_size(theme_named(look), 13)))
            for size, theme in themes:
                pages = {"demo": demo_page(look, screen), "crowded": crowded_page(screen), "empty": empty_page(screen)}
                for page, view in pages.items():
                    frame = theme.frame(view, palette, panel)
                    yield f"{look}{size}/{panel.name}/{page}", frame.image
                    if frame.scrolls:
                        for seconds in (2.5, 7.0, 30.0):
                            yield f"{look}{size}/{panel.name}/{page}@{seconds}", frame.at(seconds)
                route = replace(ROUTE_PAGE, clock=CLOCK if screen else None)
                yield f"{look}{size}/{panel.name}/route", theme.route_frame(route, palette, panel).at(5.0)
            theme = theme_named(look)
            yield f"{look}/{panel.name}/code", theme.render_code(CodeView("123 456", "23:52", 1.0), palette, panel)
            setup = SetupView("http://raspberrypi.local:8090/settings")
            yield f"{look}/{panel.name}/setup", theme.render_setup(setup, palette, panel)
        if THEMES[look].screen_only:  # in a screen's colours, with the TFT look's green and red and the tiles
            yield f"{look}/preview", theme_named(look).render(demo_page(look, True), SCREEN_PALETTE.with_preview(
                "yellow"), PANELS["hdmi-1280x400"])
        else:
            yield f"{look}/preview", theme_named(look).render(demo_page(look, False), PALETTE.with_preview("red"))


def digest(image):
    palette = image.getpalette() if image.mode == "P" else None
    return hashlib.sha256(repr((image.mode, image.size, palette)).encode() + image.tobytes()).hexdigest()


def record_digests():
    DIGESTS.write_text(json.dumps({name: digest(image) for name, image in drawn_pages()}, indent=1) + "\n")


def test_every_page_draws_pixel_for_pixel_as_recorded():
    recorded = json.loads(DIGESTS.read_text())
    # Twice: the second time from whatever text the first kept.
    for _ in range(2):
        drawn = {name: digest(image) for name, image in drawn_pages()}
        assert drawn.keys() == recorded.keys()
        assert [name for name in drawn if drawn[name] != recorded[name]] == []


# --- As many modes as the display has room for, in every look ---
#
# Trains, buses and trams with walking times, so the trains and the trams
# each have leave words (the station board look draws them on a line under
# their train), on every panel in every look, with as many modes as the panel
# has room for in that look: every section keeps a row and its leave words,
# and nothing runs into the bottom line (the clock, or "Updated") or off the
# bottom of the panel.

def _room_view(look, panel, calling):
    trains = [
        Departure("train", "Northern", "Manchester Piccadilly", "07:12", None, Status.ON_TIME, "1",
                  calling_at=DEMO_CALLING if calling else None),
        Departure("train", "Northern", "Manchester Piccadilly", "07:42", "07:49", Status.LATE, "1"),
        Departure("train", "Northern", "Manchester Piccadilly", "08:12", None, Status.ON_TIME, "1"),
    ]
    buses = [
        Departure("bus", "341", "Hyde", "07:20", "07:24", Status.LATE, stop="A"),
        Departure("bus", "61", "Buxton", "07:35", None, Status.UNKNOWN, stop="B"),
        Departure("bus", "237", "Ashton", "07:41", None, Status.ON_TIME, stop="A"),
    ]
    trams = [
        Departure("metro", "Purple Line", "Altrincham", "07:14", None, Status.UNKNOWN, stop="T"),
        Departure("metro", "Blue Line", "Bury", "07:17", None, Status.UNKNOWN, stop="T"),
        Departure("metro", "Blue Line", "Ashton", "07:20", None, Status.UNKNOWN, stop="T"),
    ]
    found = {"train": trains, "bus": buses, "metro": trams}
    shown = {mode: found[mode] for mode in KEYS[:most_modes(panel, look)]}
    return build_view(DEMO_TITLE, shown, "07:03", set(), WalkTimes(train=5, bus={"*": 8}, metro={"T": 6}),
                      theme_named(look).wording, None if panel.epaper else CLOCK)


_ROOM_CASES = [(panel, look, calling) for panel in PANELS.values() for look in themes_for(panel.epaper)
               for calling in ((False,) if panel.epaper else (True, False))]


def _sign_room(view, panel):
    """(rows drawn of each section, the words drawn, the lowest any of them
    reach, how low they may reach), from the sign look's layout."""
    width, height = panel.width // panel.scale, panel.height // panel.scale
    layout = sign_layout(view, width, height)
    bottom_line = {view.clock, f"Updated {view.updated}"}
    drawn = [item for item in layout.items if item.text not in bottom_line]
    rows = [sum(item.text in {row.left for row in section.rows} for item in drawn) for section in view.sections]
    lowest = max(item.y + item_size(item)[1] for item in drawn)
    bottom_line_top = height - (GLYPH_H if view.clock else TINY_HEIGHT) - 1
    # Above the rule over the bottom line where there is one (a rule more than
    # the sections have between them), and clear of the bottom line anyway.
    limit = layout.rules[-1] if len(layout.rules) == len(view.sections) else bottom_line_top - 1
    return rows, {item.text for item in drawn}, lowest, limit


def _painted_room(view, panel, look, monkeypatch):
    """The same from the plain look's painter, which the TFT and split-flap looks build on."""
    rows, words, ends = [], set(), []
    real_rows, real_field, real_section = Painter.drawn_rows, Painter.field, Painter.section

    def drawn_rows(self, section):
        found = real_rows(self, section)
        rows.append(len(found))
        return found

    def field(self, xy, shown, whole, *args):
        words.add(whole)
        return real_field(self, xy, shown, whole, *args)

    def section(self, section, y, where_x=None):
        ends.append(real_section(self, section, y, where_x))
        return ends[-1]

    monkeypatch.setattr(Painter, "drawn_rows", drawn_rows)
    monkeypatch.setattr(Painter, "field", field)
    monkeypatch.setattr(Painter, "section", section)
    theme_named(look).frame(view, PALETTE if panel.epaper else SCREEN_PALETTE, panel)
    return rows, words, max(ends), panel.height


@pytest.mark.parametrize(("panel", "look", "calling"), _ROOM_CASES,
                         ids=[f"{p.name}-{look}-{'calling' if c else 'plain'}" for p, look, c in _ROOM_CASES])
def test_every_section_keeps_a_row_and_its_leave_words_clear_of_the_bottom_line(panel, look, calling, monkeypatch):
    view = _room_view(look, panel, calling)
    room = _sign_room(view, panel) if look == "sign" else _painted_room(view, panel, look, monkeypatch)
    rows, words, lowest, limit = room
    assert all(count >= 1 for count in rows), rows
    leaves = [section.leave for section in view.sections if section.kind == "train"]
    assert None not in leaves
    assert [leave for leave in leaves if leave not in words] == []
    assert lowest <= limit
