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
