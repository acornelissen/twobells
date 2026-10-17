import pytest

from twobells.settings import modes, schema
from twobells.settings.config import ConfigError
from twobells.settings.schema import Invalid

BOTH = {"train": {"crs": "GLO"}, "bus": {"stops": {"A": "1000DGHS0900"}}}


def test_the_modes_are_trains_buses_and_trams_in_that_order():
    assert modes.KEYS == ("train", "bus", "metro")
    assert [m.label for m in modes.MODES] == ["Trains", "Buses", "Trams and metro"]
    assert modes.mode("bus").rows == "bus"
    assert modes.mode("metro").rows == "train"  # drawn like a train's: time, destination, platform, status


def test_trams_and_metro_have_no_source_yet_so_are_never_set_up():
    assert not modes.mode("metro").ready
    assert modes.set_up({**BOTH, "metro": {"stops": {}}}) == ("train", "bus")


def test_a_mode_is_set_up_while_its_table_is_there_and_not_switched_off():
    assert modes.set_up(BOTH) == ("train", "bus")
    assert modes.set_up({"bus": BOTH["bus"]}) == ("bus",)
    assert modes.set_up({**BOTH, "bus": {"enabled": False}}) == ("train",)
    assert modes.set_up({"train": "GLO"}) == ()  # not a table: its loader says so
    assert modes.set_up({}) == ()


@pytest.mark.parametrize("view", modes.VIEWS)
def test_without_a_list_a_view_shows_every_mode_set_up(view):
    assert modes.shown(BOTH, view) == ("train", "bus")
    assert modes.shown({"bus": BOTH["bus"]}, view) == ("bus",)


def test_a_list_says_which_modes_a_view_shows_in_the_registry_s_order():
    assert modes.shown({**BOTH, "board": {"modes": ["bus"]}}, "board") == ("bus",)
    assert modes.shown({**BOTH, "dashboard": {"modes": ["bus", "train"]}}, "dashboard") == ("train", "bus")
    # Each view its own.
    data = {**BOTH, "board": {"modes": ["train"]}, "dashboard": {"modes": ["bus"]}}
    assert (modes.shown(data, "board"), modes.shown(data, "dashboard")) == (("train",), ("bus",))


def test_a_mode_named_but_not_set_up_is_left_out():
    assert modes.shown({**BOTH, "board": {"modes": ["train", "metro"]}}, "board") == ("train",)
    assert modes.shown({"bus": BOTH["bus"], "board": {"modes": ["train", "bus"]}}, "board") == ("bus",)


def test_a_list_naming_no_mode_set_up_shows_every_mode_set_up():
    # Buses switched off, with a board that showed buses alone: trains, not nothing.
    data = {**BOTH, "bus": {"enabled": False}, "board": {"modes": ["bus"]}}
    assert modes.shown(data, "board") == ("train",)


def test_without_a_list_a_board_shows_the_first_modes_set_up_that_fit():
    assert modes.shown(BOTH, "board", most=1) == ("train",)


@pytest.mark.parametrize(("value", "says"), [
    ("bus", "must be a list of modes"),
    ([1], "must be a list of modes"),
    (["tram"], "'tram' is not a mode"),
    (["bus", "bus"], "more than once"),
    ([], "names no mode"),
])
def test_a_list_that_is_not_one_is_a_config_error(value, says):
    with pytest.raises(ConfigError, match=says):
        modes.shown({**BOTH, "board": {"modes": value}}, "board")


def test_a_list_naming_more_modes_than_the_display_has_room_for_is_refused():
    data = {**BOTH, "board": {"modes": ["train", "bus", "metro"]}}
    with pytest.raises(ConfigError, match=r"\[board\] modes: The Inky pHAT has room for 2 modes at most, each with "
                                          r"its own section, not 3"):
        modes.shown(data, "board", 2, "the Inky pHAT")
    # Counted as written, so the board doesn't stop loading the day trams are set up.
    assert modes.shown(data, "board", 3, "the 10.85-inch e-paper panel") == ("train", "bus")


def test_the_form_check_saves_the_ticked_modes_in_order():
    assert modes.check()("bus train") == ["train", "bus"]
    assert modes.check(2, "the Inky pHAT")("train bus") == ["train", "bus"]


@pytest.mark.parametrize(("raw", "says"), [
    ("", "Tick at least one"),
    ("train tram", "Tick only the modes listed"),
    ("train train", "Tick only the modes listed"),
])
def test_the_form_check_refuses_what_is_not_a_choice(raw, says):
    with pytest.raises(Invalid, match=says):
        modes.check()(raw)


def test_the_form_check_refuses_more_than_there_is_room_for_saying_why():
    with pytest.raises(Invalid) as refused:
        modes.check(2, "the Inky pHAT")("train bus metro")
    assert str(refused.value) == "The Inky pHAT has room for 2 modes at most, each with its own section, not 3. Untick 1."


def test_each_display_says_how_many_modes_it_has_room_for_and_why():
    assert schema.board_room("inky") == (2, "the Inky pHAT")
    assert schema.board_room("waveshare-10in85g") == (3, "the 10.85-inch e-paper panel")
    assert schema.board_room("no-such-display") == (2, "the Inky pHAT")  # its loader says it is unknown
    assert schema.room_words("hdmi-1280x400") == (
        "The 7.9-inch screen has room for 2: each takes a section with its own heading, and the board's text is "
        "sized to the display's height.")


def test_the_lists_are_saved_settings():
    saved = schema.saved_keys()
    assert "modes" in saved["board"] and "modes" in saved["dashboard"]


# A screen's room in its look and text size (board/panel.py: most_modes).


def test_a_screen_says_its_room_in_its_look_and_text_size():
    assert schema.board_room("hdmi-1280x400", "sign", 16) == (3, "the 7.9-inch screen in the station board look")
    assert schema.board_room("hdmi-1280x400", "tft", 16) == (3, "the 7.9-inch screen in the TFT look")
    assert schema.board_room("hdmi-1920x440", "plain", 13) == (3, "the 11.3-inch bar screen in the plain look at "
                                                                  "text size 13")
    assert schema.board_room("hdmi-1920x440", "plain", 14) == (2, "the 11.3-inch bar screen in the plain look at "
                                                                  "text size 14")
    # E-paper's room is the same in every look, so it says no look.
    assert schema.board_room("inky", "plain", 9) == (2, "the Inky pHAT")
    assert schema.board_room("waveshare-10in85g", "sign", 11) == (3, "the 10.85-inch e-paper panel")


def test_a_screen_says_what_would_give_it_room_for_one_more():
    assert schema.room_other("hdmi-1280x400", "plain", 13) == "text size 12 or smaller, or another look"
    assert schema.room_other("hdmi-1280x400", "plain", 12) == ""
    assert schema.room_other("hdmi-1280x400", "sign", 16) == ""
    assert schema.room_other("inky", "plain", 16) == ""
    assert schema.room_words("hdmi-1280x400", "plain", 14) == (
        "The 7.9-inch screen in the plain look at text size 14 has room for 2, and for 3 with text size 12 or "
        "smaller, or another look: each takes a section with its own heading, and the board's text is sized to "
        "the display's height.")
    assert schema.room_words("hdmi-1920x440", "plain", 11).startswith(
        "The 11.3-inch bar screen in the plain look at text size 11 has room for 3, and for 2 from text size 14: ")
    assert schema.room_words("hdmi-1280x400", "flap", 11).startswith(
        "The 7.9-inch screen in the split-flap look has room for 3: ")


def test_the_form_check_says_what_else_would_give_room():
    with pytest.raises(Invalid) as refused:
        modes.check(2, "the 7.9-inch screen in the plain look at text size 14",
                    "text size 12 or smaller, or another look")("train bus metro")
    assert str(refused.value) == ("The 7.9-inch screen in the plain look at text size 14 has room for 2 modes at most, "
                                  "each with its own section, not 3. Untick 1, or choose text size 12 or smaller, "
                                  "or another look.")


def test_while_the_board_names_three_modes_a_screen_offers_only_the_text_sizes_with_room_for_them():
    def field(name, *args):
        return next(f for f in schema.fields(*args) if f.name == name)

    sizes = [value for value, _ in field("text_size", "hdmi-1280x400", "plain", 11, 3).options]
    assert sizes == ["9", "10", "11", "12"]
    assert field("text_size", "hdmi-1280x400", "plain", 11, 3).hint.endswith(
        "With 3 modes, the 7.9-inch screen has room for text up to size 12.")
    assert [value for value, _ in field("text_size", "hdmi-1920x440", "sign", 11, 3).options][-1] == "13"
    # Without a list, or with two, or on e-paper: every size.
    for args in (("hdmi-1280x400", "plain", 11, 0), ("hdmi-1280x400", "plain", 11, 2), ("inky", "plain", 11, 2),
                 ("waveshare-10in85g", "plain", 16, 3)):
        assert len(field("text_size", *args).options) == 8, args
    # The size in force always shows, so the page never changes it unasked.
    assert [value for value, _ in field("text_size", "hdmi-1280x400", "sign", 14, 3).options][-1] == "14"


def test_while_the_board_names_three_modes_a_screen_offers_the_plain_look_only_at_a_size_with_room():
    looks = schema.fields("hdmi-1280x400", "sign", 14, 3)
    theme = next(f for f in looks if f.name == "theme")
    assert [value for value, _ in theme.options] == ["sign", "tft", "flap"]
    assert theme.hint == ("The plain look isn't offered: at text size 14, the 7.9-inch screen has room for 2 modes, "
                          "and the board shows 3. Choose text size 12 or smaller first, under Advanced, or fewer "
                          "modes.")
    theme = next(f for f in schema.fields("hdmi-1280x400", "sign", 12, 3) if f.name == "theme")
    assert [value for value, _ in theme.options] == ["sign", "plain", "tft", "flap"] and theme.hint == ""
    # The look in force always shows.
    theme = next(f for f in schema.fields("hdmi-1280x400", "plain", 14, 3) if f.name == "theme")
    assert "plain" in [value for value, _ in theme.options]


def test_the_board_s_modes_check_goes_by_the_look_and_text_size():
    def check(*args):
        return next(f for f in schema.fields(*args) if f.name == "board_modes").check

    assert check("hdmi-1280x400", "sign", 16)("train bus metro") == ["train", "bus", "metro"]
    assert check("hdmi-1280x400", "plain", 12)("train bus metro") == ["train", "bus", "metro"]
    with pytest.raises(Invalid, match="Untick 1, or choose text size 12 or smaller, or another look"):
        check("hdmi-1280x400", "plain", 13)("train bus metro")
    with pytest.raises(Invalid, match="The Inky pHAT has room for 2 modes at most"):
        check("inky", "sign", 11)("train bus metro")
