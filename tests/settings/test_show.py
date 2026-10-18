"""What each view shows: the registry of items, the presets, and reading a view's show list."""

from collections import Counter

import pytest

from twobells.core.errors import ConfigError
from twobells.settings import schema, show

BOARD = ("platforms", "where", "disruption", "rain", "last_bus", "bus_page", "calling_at", "clock_page",
         "announce_speaker")
DASHBOARD = ("next_to_catch", "where", "disruptions", "weather", "last_buses", "area_map", "remind", "calling_at",
             "coaches", "punctuality", "performance", "fleet_numbers", "announce")


def test_each_view_has_its_items_in_the_order_the_settings_list_them():
    assert show.keys("board") == BOARD
    assert show.keys("dashboard") == DASHBOARD
    with pytest.raises(ValueError, match="Unknown view"):
        show.items("kiosk")


@pytest.mark.parametrize("view", show.VIEWS)
def test_items_are_named_once_and_said_in_words(view):
    items = show.items(view)
    assert not [key for key, n in Counter(item.key for item in items).items() if n > 1]
    assert not [label for label, n in Counter(item.label for item in items).items() if n > 1]
    for item in items:
        assert item.key.isidentifier() and item.key.islower()
        assert item.label and item.hint.endswith(".")
        assert set(item.presets) <= {name for name, _ in show.PRESETS}


def test_only_the_board_has_items_for_screens_only():
    assert {item.key for item in show.items("board") if item.screen} == {
        "where", "last_bus", "bus_page", "calling_at", "clock_page", "announce_speaker"}
    assert not [item for item in show.items("dashboard") if item.screen]


@pytest.mark.parametrize("view", show.VIEWS)
def test_each_preset_adds_to_the_one_before(view):
    times, usual, everything = (set(show.preset(view, name)) for name, _ in show.PRESETS)
    # Everything is every item but those in no preset: the board's speaker, on only when chosen.
    assert times <= usual <= everything == set(show.keys(view)) - {"announce_speaker"}


def test_the_board_s_speaker_is_in_no_preset_and_off_unless_chosen():
    [speaker] = [item for item in show.items("board") if item.key == "announce_speaker"]
    assert speaker.presets == () and speaker.screen
    assert "speaker" in speaker.hint and "espeak-ng" in speaker.hint
    assert "announce_speaker" not in show.default("board", {"clock_page": True})
    assert "announce_speaker" not in show.shown({}, "board")
    assert show.shown({"board": {"show": ["announce_speaker"]}}, "board") == {"announce_speaker"}


def test_the_presets_show_what_was_decided():
    assert show.preset("board", "times") == ()
    assert show.preset("board", "usual") == ("platforms", "where", "disruption", "rain", "last_bus", "bus_page")
    assert show.preset("dashboard", "times") == ("next_to_catch",)
    assert show.preset("dashboard", "usual") == (
        "next_to_catch", "where", "disruptions", "weather", "last_buses", "area_map", "remind")
    # Everything adds calling-at, coaches, punctuality, Performance, fleet numbers, announcements and the clock page.
    assert set(show.preset("board", "everything")) - set(show.preset("board", "usual")) == {
        "calling_at", "clock_page"}
    assert set(show.keys("dashboard")) - set(show.preset("dashboard", "usual")) == {
        "calling_at", "coaches", "punctuality", "performance", "fleet_numbers", "announce"}
    assert show.NEW_INSTALL == "usual"
    with pytest.raises(ValueError, match="Unknown preset"):
        show.preset("board", "most")


def test_a_list_is_known_by_its_preset():
    assert show.preset_of("dashboard", ["next_to_catch"]) == "times"
    assert show.preset_of("board", reversed(show.preset("board", "everything"))) == "everything"
    assert show.preset_of("board", show.keys("board")) is None  # the speaker is in none
    assert show.preset_of("board", ["rain"]) is None


def test_without_a_list_a_view_shows_everything_but_the_board_s_clock_page():
    assert show.shown({}, "dashboard") == set(DASHBOARD)
    assert show.shown({}, "board") == set(BOARD) - {"clock_page", "announce_speaker"}
    assert show.shown({"board": {}}, "board") == set(BOARD) - {"clock_page", "announce_speaker"}


def test_the_older_clock_page_setting_counts_until_there_is_a_list():
    assert show.shown({"clock_page": True}, "board") == set(BOARD) - {"announce_speaker"}
    assert show.shown({"clock_page": False}, "board") == set(BOARD) - {"clock_page", "announce_speaker"}
    assert show.shown({"clock_page": True, "board": {"show": ["rain"]}}, "board") == {"rain"}
    assert show.shown({"clock_page": True}, "dashboard") == set(DASHBOARD)


def test_a_list_says_what_shows():
    data = {"board": {"show": ["rain", "clock_page"]}, "dashboard": {"show": []}}
    assert show.shown(data, "board") == {"rain", "clock_page"}
    assert show.shown(data, "dashboard") == set()
    assert show.shows(data, "board", "rain") and not show.shows(data, "board", "platforms")
    with pytest.raises(ValueError, match="no item"):
        show.shows(data, "board", "weather")  # the dashboard's, not the board's


@pytest.mark.parametrize("value, words", [
    ("rain", "must be a list"),
    (["rain", 3], "must be a list"),
    (["rain", "weather"], "'weather' is not something the board shows"),
    (["rain", "rain"], "more than once"),
    (["rain"] * 65, "must be a list"),
])
def test_a_list_that_isn_t_one_is_refused(value, words):
    with pytest.raises(ConfigError, match=r"\[board\] show") as error:
        show.shown({"board": {"show": value}}, "board")
    assert words in str(error.value)


def test_a_form_s_ticks_become_the_list_in_order():
    check = show.check("dashboard")
    assert check("weather next_to_catch") == ["next_to_catch", "weather"]
    assert check("") == []
    with pytest.raises(schema.Invalid, match="Tick only the items listed"):
        check("weather clock_page")


def test_the_show_lists_are_settings_the_pages_save():
    saved = schema.saved_keys()
    assert "show" in saved["board"] and "show" in saved["dashboard"]
    # The older clock page setting is still read from the saved settings, until a list replaces it.
    assert show.OLDER_CLOCK_PAGE in saved[None]
    assert {f.name for f in schema.FIELDS if f.kind == "show"} == {"board_show", "dashboard_show"}
    assert not [f for f in schema.FIELDS if f.key == "clock_page"]  # no field of its own any more
