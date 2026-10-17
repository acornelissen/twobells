"""A GroupForm of every page a board's settings have, together: the
board's settings, saved and shown. The settings site's pages
(test_settingsite.py) are each one of these."""

import re
import tomllib
from datetime import datetime

import pytest

from twobells.settings.config import load_config, read_config, read_overlay_in
from twobells.settings.installs import install_named
from twobells.settings.schema import saved_keys
from twobells.settings.store import Store
from twobells.web.groupform import GroupForm

ENV = {"RDM_API_KEY": "rdm-secret-123", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "tapi-secret-456"}

CONFIG = """
title = "Glossop > Manchester Picc"
state_file = "{state}"
theme = "sign"
min_redraw_minutes = 2

[train]
crs = "GLO"
filter_crs = "MAN"
rows = 3
interval_minutes = 2
stale_after_minutes = 6
windows = ["Mon-Fri 06:30-09:30", "Mon-Fri 16:30-19:30"]
walk_minutes = 7

[bus]
source = "transportapi"
rows = 3
directions = []
interval_minutes = 12
stale_after_minutes = 30
daily_budget = 28
windows = ["Mon-Fri 07:00-08:30"]

[bus.stops]
A = "1000DGHS0900"
B = "100000008350"

[bus.walk_minutes]
A = 7
B = 6
"""


def board_form(store_or_config, load_secrets):
    """A board's settings pages as one form, on a Pi."""
    return GroupForm(Store.of(store_or_config), ("trains_buses", "board", "general"), "/settings/board",
                     load_secrets, install=install_named("pi"), noun="board")


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    secrets = dict(ENV)
    form = board_form(config, lambda: dict(secrets))
    return form, config, state, secrets


def fields_of(form):
    """What a browser would submit for the form as rendered."""
    page = form.render("tok")
    fields = {}
    for name, value in re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="text" value="([^"]*)"', page):
        fields[name] = value.replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    for name, value in re.findall(r'<textarea id="(\w+)" name="\w+"[^>]*>([^<]*)</textarea>', page):
        fields[name] = value
    for name, body in re.findall(r'<select id="(\w+)" name="\w+"[^>]*>(.*?)</select>', page):
        chosen = re.search(r'value="([^"]*)" selected', body)
        fields[name] = chosen.group(1) if chosen else ""
    for name, value in re.findall(r'<input type="radio" id="[\w-]+" name="(\w+)" value="([^"]*)" checked', page):
        fields[name] = value
    # A ticked box is sent as "on"; an unticked one isn't sent (the views, on a Pi that offers two).
    fields |= dict.fromkeys(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="checkbox" value="on" checked', page), "on")
    return fields


def test_form_shows_the_current_settings(setup):
    form, *_ = setup
    fields = fields_of(form)
    assert fields["title"] == "Glossop > Manchester Picc"
    assert fields["train_crs"] == "GLO"
    weekdays = {f"train_windows-{row}-{day}" for row in (1, 2) for day in ("mon", "tue", "wed", "thu", "fri")}
    assert {name for name in fields if name.startswith("train_windows-") and fields[name] == "on"} == weekdays
    assert [fields[f"train_windows-{row}-{box}"] for row in (1, 2, 3, 4) for box in ("from", "to")] == [
        "06:30", "09:30", "16:30", "19:30", "", "", "", ""]  # and two spare rows
    assert fields["bus_stops"] == "A = 1000DGHS0900\nB = 100000008350"
    assert fields["bus_walk_minutes_A"] == "7" and fields["bus_walk_minutes_B"] == "6"
    assert fields["theme"] == "sign" and fields["rotate"] == "0"


def test_saving_the_form_unchanged_keeps_the_same_board(setup):
    form, config, state, _ = setup
    before = load_config(config, ENV)
    assert form.save(fields_of(form)) == {}
    after = load_config(config, ENV)
    assert (after.board.title, after.board.walk, after.rotate) == (before.board.title, before.board.walk, before.rotate)
    assert (state / "settings.toml").exists()


def test_a_change_is_saved_to_the_overlay_not_config_toml(setup):
    form, config, state, _ = setup
    original = config.read_text()
    fields = fields_of(form) | {"title": "Home", "board_train_rows": "2", "bus_walk_minutes_B": "5"}
    assert form.save(fields) == {}
    assert config.read_text() == original
    settings = load_config(config, ENV)
    assert settings.board.title == "Home"
    assert dict(settings.board.walk.bus) == {"A": 7, "B": 5}
    assert (state / "settings.toml").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "name, value, message",
    [
        ("train_crs", "GLOSSOP", "3-letter"),
        ("board_train_rows", "99", "1 to 6"),
        ("train_windows-1-from", "weekdays", "24-hour time"),
        ("bus_stops", "A = not a code!", "starts with 3 digits"),
        ("bus_stops", "A = DGHS09001000", "starts with 3 digits"),  # an ATCO code starts with its area's number
        ("bus_stops", "A = 1000DGHS09001", "starts with 3 digits"),  # 13 characters, one too many
        ("bus_stops", "Stop A = 1000DGHS0900", "LABEL"),
        ("title", "x" * 61, "60"),
        ("title", "bad\x07bell", "ordinary"),
        ("rotate", "90", "options"),  # e-paper turns only 0 or 180
        ("theme", "neon", "options"),
    ],
)
def test_bad_values_are_refused_and_nothing_is_written(setup, name, value, message):
    form, config, state, _ = setup
    errors = form.save(fields_of(form) | {name: value})
    assert message in errors[name]
    assert not (state / "settings.toml").exists()


def test_the_whole_configuration_must_still_load(setup):
    form, config, state, secrets = setup
    secrets.pop("RDM_API_KEY")
    errors = form.save(fields_of(form))
    assert "RDM_API_KEY" in errors["_form"]
    assert not (state / "settings.toml").exists()


def test_values_are_escaped_in_the_page(setup):
    form, *_ = setup
    page = form.render("tok", values={"title": '"><script>alert(1)</script>'})
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_errors_are_linked_from_a_summary_and_tied_to_their_fields(setup):
    form, *_ = setup
    page = form.render("tok", errors={"train_crs": "Enter a 3-letter station code, like GLO"})
    assert 'role="alert"' in page and 'href="#train_crs"' in page
    assert re.search(r'id="train_crs"[^>]*aria-describedby="train_crs-hint train_crs-error"[^>]*aria-invalid="true"', page)


def test_summary_items_name_their_section_and_field(setup):
    form, *_ = setup
    page = form.render("tok", errors={
        "train_crs": "Enter a 3-letter station code, like GLO",
        "title": "Enter some text",
        "_form": "Not saved: something",
    })
    assert '<a href="#train_crs">Trains – Station code: Enter a 3-letter station code, like GLO</a>' in page
    assert '<a href="#title">Title: Enter some text</a>' in page  # its section's heading is its label
    assert "<li>Not saved: something</li>" in page


def test_the_error_summary_takes_focus(setup):
    form, *_ = setup
    page = form.render("tok", errors={"title": "Enter some text"})
    assert re.search(r'<div class="summary"[^>]*tabindex="-1"[^>]*autofocus', page)


@pytest.mark.parametrize("name, value, quoted", [
    ("bus_stops", "A = 1000DGHS0900\nB = bad!", "“B = bad!”"),
    ("bus_stops", "A = 1000DGHS0900\nStop B = 100000008350", "“Stop B = 100000008350”"),
])
def test_errors_in_a_many_line_field_quote_the_line(setup, name, value, quoted):
    form, *_ = setup
    errors = form.save(fields_of(form) | {name: value})
    assert quoted in errors[name]


def test_each_stop_has_its_own_walk_box_labelled_with_its_name(setup):
    from twobells.timetable import build as timetable
    from twobells.timetable.model import Schedule, StopPosition

    form, config, state, _ = setup
    page = form.render("tok")
    walks = re.search(r'<fieldset class="field walks"[^>]*>(.*?)</fieldset>', page).group(1)
    assert "<legend>Walk to each stop</legend>" in walks
    assert '<label for="bus_walk_minutes_A">Stop A<span class="vh"> (minutes)</span></label>' in walks
    assert re.search(r'<input id="bus_walk_minutes_B" name="bus_walk_minutes_B" type="text" value="6" '
                     r'inputmode="numeric" autocomplete="off" class="w-3"', walks)
    assert '<span class="unit" aria-hidden="true">minutes</span>' in walks
    assert "<textarea" not in walks
    # With the bus timetable, each box and the stops are named, the names escaped.
    state.mkdir(exist_ok=True)
    places = {"1000DGHS0900": StopPosition("Henry Street", 53.4, -1.9, "Stop A"),
              "100000008350": StopPosition("<i>Market</i>", 53.4, -1.9, "")}
    timetable.save(state, Schedule(datetime(2026, 10, 8), None, {}, (), places))
    page = form.render("tok")
    assert '<label for="bus_walk_minutes_A">Henry Street (A)<span class="vh">' in page
    assert '<label for="bus_walk_minutes_B">&lt;i&gt;Market&lt;/i&gt; (B)<span class="vh">' in page
    stops = re.search(r"<dt>Stops</dt><dd>(.*?)</dd>", page).group(1)
    assert "<li>A: Henry Street (1000DGHS0900)</li>" in stops and "&lt;i&gt;Market&lt;/i&gt;" in stops


def test_the_walk_boxes_save_the_same_table_as_setup(setup):
    form, config, state, _ = setup
    assert form.save(fields_of(form) | {"bus_walk_minutes_A": "4", "bus_walk_minutes_B": ""}) == {}
    assert tomllib.loads((state / "settings.toml").read_text())["bus"]["walk_minutes"] == {"A": 4}
    assert dict(load_config(config, ENV).board.walk.bus) == {"A": 4}
    fields = fields_of(form)
    assert fields["bus_walk_minutes_A"] == "4" and fields["bus_walk_minutes_B"] == ""
    changed = []
    assert form.save(fields_of(form) | {"bus_walk_minutes_B": "9"}, changed) == {}
    assert changed == ["Walk to each stop"]


def test_one_walk_for_every_stop_shows_in_each_box(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace("[bus.walk_minutes]\nA = 7\nB = 6\n", "")
                      .replace("daily_budget = 28", "daily_budget = 28\nwalk_minutes = 5"))
    form = board_form(config, lambda: dict(ENV))
    fields = fields_of(form)
    assert fields["bus_walk_minutes_A"] == fields["bus_walk_minutes_B"] == "5"
    assert form.save(fields) == {}
    assert dict(load_config(config, ENV).board.walk.bus) == {"A": 5, "B": 5}


@pytest.mark.parametrize("value", ["soon", "61", "-1", "5.5"])
def test_a_wrong_walk_is_that_boxs_error_named_by_its_stop(setup, value):
    form, *_ = setup
    errors = form.save(fields_of(form) | {"bus_walk_minutes_B": value})
    assert set(errors) == {"bus_walk_minutes_B"}
    page = form.render("tok", errors=errors, values=fields_of(form) | {"bus_walk_minutes_B": value})
    assert '<a href="#bus_walk_minutes_B">Buses – Walk to each stop, Stop B:' in page
    assert re.search(r'<input id="bus_walk_minutes_B" name="bus_walk_minutes_B" type="text" value="'
                     + re.escape(value) + r'"[^>]*aria-describedby="bus_walk_minutes_B-error" aria-invalid="true"',
                     page)


def _accents(page):
    return re.findall(r'<input type="radio" id="[\w-]+" name="accent" value="([^"]*)"', page)


def _form_for(tmp_path, extra):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', extra))
    return board_form(config, lambda: dict(ENV))


def test_the_plain_look_does_not_offer_yellow_on_white(tmp_path):
    form = _form_for(tmp_path, 'theme = "plain"')
    assert _accents(form.render("tok")) == ["", "red"]
    errors = form.save(fields_of(form) | {"accent": "yellow"})
    assert "accent" in errors
    errors = form.save(fields_of(form) | {"theme": "sign", "accent": "yellow"})  # switching look and accent together
    assert errors == {}


def test_the_sign_look_on_a_screen_does_not_offer_red_on_black(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    assert _accents(form.render("tok")) == ["", "yellow"]
    assert "accent" in form.save(fields_of(form) | {"accent": "red"})


def test_the_sign_look_on_e_paper_offers_both_accents(setup):
    form, *_ = setup
    assert _accents(form.render("tok")) == ["", "yellow", "red"]
    assert "amber" in form.render("tok") and "plain look uses red" in form.render("tok")


def _looks(page):
    return re.findall(r'<input type="radio" id="[\w-]+" name="theme" value="([^"]*)"', page)


def test_a_screen_is_offered_the_tft_look_and_saves_it(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    page = form.render("tok")
    assert _looks(page) == ["sign", "plain", "tft", "flap"] and "TFT screen (white and yellow on black)" in page
    assert form.save(fields_of(form) | {"theme": "tft", "accent": ""}) == {}
    config = tmp_path / "config.toml"
    assert load_config(config, ENV).board.theme.name == "tft"
    assert _accents(form.render("tok")) == ["", "yellow"]  # red is too dim on its black
    assert "accent" in form.save(fields_of(form) | {"accent": "red"})


def test_a_screen_is_offered_the_split_flap_look_in_yellow_and_saves_it(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1920x440"')
    assert "Split-flap (white letters on flipping tiles)" in form.render("tok")
    assert form.save(fields_of(form) | {"theme": "flap", "accent": ""}) == {}
    assert load_config(tmp_path / "config.toml", ENV).board.theme.name == "flap"
    assert _accents(form.render("tok")) == ["", "yellow"]  # red is too dim on its grey tiles
    assert "accent" in form.save(fields_of(form) | {"accent": "red"})


def test_e_paper_is_not_offered_the_tft_look_and_refuses_it(setup):
    form, config, state, _ = setup
    assert _looks(form.render("tok")) == ["sign", "plain"]
    errors = form.save(fields_of(form) | {"theme": "tft"})
    assert errors["theme"] == "That look is for a screen; pick one for e-paper"
    assert not (state / "settings.toml").exists()


def test_outlined_buttons_have_a_border_that_stands_out():
    from importlib.resources import files

    css = files("twobells.web").joinpath("web.css").read_text()
    assert re.search(r"\nbutton \{[^}]*border: 1px solid var\(--edge\)", css)

    def luminance(hex_colour):
        rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    backgrounds = re.findall(r"--bg: (#\w{6});", css)
    edges = re.findall(r"--edge: (#\w{6});", css)
    assert len(backgrounds) == len(edges) == 2
    for bg, edge in zip(backgrounds, edges, strict=True):
        high, low = sorted((luminance(bg), luminance(edge)), reverse=True)
        assert (high + 0.05) / (low + 0.05) >= 3


def test_every_control_has_a_label(setup):
    form, *_ = setup
    page = form.render("tok")
    ids = set(re.findall(r'<(?:input|select|textarea)[^>]* id="([\w-]+)"', page))
    labelled = set(re.findall(r'<label for="([\w-]+)"', page))
    assert {"title", "theme", "theme-1", "rotate-1", "board_train_rows"} <= ids <= labelled


def test_a_screen_can_turn_a_quarter(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\ndisplay = "hdmi-1280x400"\nrotate = 90'))
    form = board_form(config, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"rotate": "270"}) == {}
    assert load_config(config, ENV).rotate == 270


def test_the_clock_page_is_off_until_ticked_and_saves_in_the_show_list(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    page = form.render("tok")
    assert '<label for="board_show-clock_page">Clock page</label>' in page and "Screens only." in page
    assert "clock_page" not in fields_of(form) and "board_show-clock_page" not in fields_of(form)
    assert form.save(fields_of(form) | {"board_show-clock_page": "on"}) == {}
    assert "clock_page" in load_config(tmp_path / "config.toml", ENV).board.show
    assert "board_show" in form.save(fields_of(form) | {"board_show-clock_page": "sometimes"})


def test_the_older_clock_page_setting_shows_ticked_and_moves_into_the_list(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"\nclock_page = true')
    assert fields_of(form)["board_show-clock_page"] == "on"
    # Saved with the list unchanged, nothing is written: the older setting still says it.
    assert form.save(fields_of(form)) == {}
    assert "board" not in tomllib.loads(_saved_text(form))
    # Changed, the list is saved, the clock page in it as it was.
    assert form.save(fields_of(form) | {"board_show-calling_at": ""}) == {}
    written = tomllib.loads(_saved_text(form))["board"]["show"]
    assert written == ["platforms", "where", "disruption", "rain", "last_bus", "bus_page", "clock_page"]
    assert load_config(tmp_path / "config.toml", ENV).board.show == set(written)


def _saved_text(form):
    path = form._store.settings_file
    return path.read_text() if path.exists() else ""


def test_the_board_s_show_list_is_a_labelled_group_of_tickboxes_with_presets(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    page = form.render("tok")
    group = page[page.index('<fieldset class="field show-list"'):]
    group = group[:group.index('<div class="savebar">')]
    # One group, named by its (visually hidden) legend and described by its hint.
    assert group.startswith('<fieldset class="field show-list" aria-describedby="board_show-hint">'
                            '<legend class="vh">What to show</legend>')
    # The presets first, as a choice of their own, with none picked.
    presets = re.findall(r'<input type="radio" id="[\w-]+" name="board_show_preset" value="(\w*)"( checked)?', group)
    assert presets == [("", " checked"), ("times", ""), ("usual", ""), ("everything", "")]
    assert "<legend>Fill in from a preset</legend>" in group
    # Then a tickbox each, its label beside it, saying which presets include it.
    boxes = re.findall(r'<div class="field check" data-presets="([\w ]*)"><input id="board_show-(\w+)" '
                       r'name="board_show-\w+" type="checkbox" value="on"( checked)? '
                       r'aria-describedby="board_show-\w+-hint"><label for="board_show-(\w+)">', group)
    assert [(key, label_for) for _, key, _, label_for in boxes] == [(k, k) for k in [*BOARD_ITEMS, SPEAKER]]
    # The speaker is in no preset: sound in the room only when chosen.
    assert [presets for presets, *_ in boxes] == ["usual everything"] * 6 + ["everything"] * 2 + [""]
    assert [bool(on) for _, _, on, _ in boxes] == [True] * 7 + [False] * 2  # all but the clock page and speaker
    assert "Needs a speaker and espeak-ng. Screens only." in group


BOARD_ITEMS = ["platforms", "where", "disruption", "rain", "last_bus", "bus_page", "calling_at", "clock_page"]
SPEAKER = "announce_speaker"


def test_unticking_items_saves_the_board_s_show_list(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    changed = []
    sent = fields_of(form) | {"board_show-rain": "", "board_show-where": ""}
    assert form.save({k: v for k, v in sent.items() if v}, changed) == {}
    assert tomllib.loads(_saved_text(form))["board"]["show"] == [
        "platforms", "disruption", "last_bus", "bus_page", "calling_at"]
    assert "What to show" in changed
    shown = fields_of(form)
    assert "board_show-rain" not in shown and shown["board_show-platforms"] == "on"


@pytest.mark.parametrize("preset, expected", [
    ("times", []),
    ("usual", ["platforms", "where", "disruption", "rain", "last_bus", "bus_page"]),
    ("everything", BOARD_ITEMS),
])
def test_a_preset_picked_on_saving_replaces_the_ticks(tmp_path, preset, expected):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    # Without JavaScript the boxes stay as they were; the preset says what shows.
    assert form.save(fields_of(form) | {"board_show_preset": preset, "board_show-calling_at": ""}) == {}
    assert tomllib.loads(_saved_text(form))["board"]["show"] == expected
    assert fields_of(form)["board_show_preset"] == ""  # drawn again with no preset picked


@pytest.mark.parametrize("sent", [{"board_show_preset": "most"}, {"board_show-weather": "on"},
                                  {"board_show-rain": "yes"}])
def test_a_show_list_takes_only_what_the_form_offers(tmp_path, sent):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    errors = form.save(fields_of(form) | sent)
    if "board_show-weather" in sent:  # a box the form doesn't draw isn't read
        assert errors == {}
        assert "board" not in tomllib.loads(_saved_text(form))
        return
    assert set(errors) == {"board_show"}
    assert not _saved_text(form)
    page = form.render("tok", errors=errors, values=fields_of(form) | sent)
    assert '<a href="#board_show-platforms">What to show:' in page
    assert 'class="field show-list has-error"' in page


def test_a_refused_save_draws_the_ticks_again_as_sent(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    sent = {k: v for k, v in fields_of(form).items() if k != "board_show-rain"} | {"title": ""}
    errors = form.save(sent)
    assert "title" in errors
    page = form.render("tok", errors=errors, values=sent)
    assert 'id="board_show-rain" name="board_show-rain" type="checkbox" value="on" aria' in page
    assert 'id="board_show-platforms" name="board_show-platforms" type="checkbox" value="on" checked' in page
    assert '<fieldset class="field show-list" aria-describedby="board_show-hint" data-unsaved>' in page


def test_a_broken_show_list_shows_the_default_and_saving_says_what_is_wrong(tmp_path):
    form = _form_for(tmp_path, 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    (tmp_path / "config.toml").write_text((tmp_path / "config.toml").read_text() + '[board]\nshow = ["weather"]\n')
    assert fields_of(form)["board_show-platforms"] == "on"
    errors = form.save(fields_of(form))
    assert "weather" in errors["_form"]
    assert form.save(fields_of(form) | {"board_show-rain": ""}) == {}  # a new list replaces it
    assert "rain" not in load_config(tmp_path / "config.toml", ENV).board.show


def test_removing_a_stop_takes_effect(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form) | {"bus_stops": "A = 1000DGHS0900"}) == {}  # B's walk box is still sent
    settings = load_config(config, ENV)
    assert settings.board.sources[1].source.stops == {"A": "1000DGHS0900"}
    assert dict(settings.board.walk.bus) == {"A": 7}
    assert fields_of(form)["bus_stops"] == "A = 1000DGHS0900"


def test_clearing_the_station_filter_takes_effect(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form) | {"train_filter_crs": ""}) == {}
    assert not read_config(config)["train"].get("filter_crs")
    assert fields_of(form)["train_filter_crs"] == ""


def test_clearing_directions_takes_effect(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace("directions = []", 'directions = ["Ashton"]'))
    form = board_form(config, lambda: dict(ENV))
    assert fields_of(form)["bus_directions"] == "Ashton"
    assert form.save(fields_of(form) | {"bus_directions": ""}) == {}
    assert read_config(config)["bus"]["directions"] == []
    assert fields_of(form)["bus_directions"] == ""


def test_clearing_walk_times_takes_effect(setup):
    form, config, *_ = setup
    cleared = {"train_walk_minutes": "", "bus_walk_minutes_A": "", "bus_walk_minutes_B": ""}
    assert form.save(fields_of(form) | cleared) == {}
    walk = load_config(config, ENV).board.walk
    assert walk.train is None and dict(walk.bus) == {}
    fields = fields_of(form)
    assert fields["train_walk_minutes"] == "" and fields["bus_walk_minutes_A"] == fields["bus_walk_minutes_B"] == ""


def test_choosing_the_looks_own_accent_clears_one_set_in_config(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\naccent = "red"'))
    form = board_form(config, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"accent": ""}) == {}
    assert load_config(config, ENV).accent == "yellow"


ROUTES = """
[bus.routes.341]
stop = "A"
stops = [
  { name = "Arundel Street", lat = 53.443519, lon = -1.95326 },
  { name = "Henry Street", lat = 53.44384, lon = -1.948999 },
]
"""


def test_routes_set_in_config_survive_a_save(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    text = CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\ndisplay = "hdmi-1280x400"')
    config.write_text(text + ROUTES)
    env = {**ENV, "BODS_API_KEY": "bods"}
    form = board_form(config, lambda: dict(env))
    assert form.save(fields_of(form) | {"bus_stops": "A = 1000DGHS0900", "bus_walk_minutes_A": "5"}) == {}
    assert "routes" not in (state / "settings.toml").read_text()
    assert set(load_config(config, env).board.tracker.routes) == {"341"}


def test_an_emoji_title_saves_and_loads(setup):
    form, config, state, _ = setup
    assert form.save(fields_of(form) | {"title": "Bus 🚌"}) == {}
    assert load_config(config, ENV).board.title == "Bus 🚌"
    assert "🚌" in (state / "settings.toml").read_text(encoding="utf-8")


def test_text_toml_cannot_hold_is_refused_not_written(setup):
    form, config, state, _ = setup
    errors = form.save(fields_of(form) | {"bus_directions": "Hyde\ud800"})  # a lone surrogate
    assert errors and not (state / "settings.toml").exists()


def test_a_broken_settings_file_still_shows_the_page_and_saving_replaces_it(setup):
    form, config, state, _ = setup
    state.mkdir()
    (state / "settings.toml").write_text("title = \n")
    page = form.render("tok")
    assert "saved settings file is broken" in page and 'role="alert"' in page
    fields = fields_of(form)
    assert fields["title"] == "Glossop > Manchester Picc"  # config.toml's
    assert form.save(fields | {"title": "Fixed"}) == {}
    assert load_config(config, ENV).board.title == "Fixed"
    assert "broken" not in form.render("tok")


def test_bank_holidays_are_treated_like_england_and_wales_sundays_unless_turned_off(setup):
    form, config, state, _ = setup
    assert fields_of(form)["bank_holidays"] == "england-and-wales"
    assert form.save({**fields_of(form), "bank_holidays": "ignore"}) == {}
    assert tomllib.loads((state / "settings.toml").read_text())["bank_holidays"] is False
    assert read_config(config)["bank_holidays"] is False
    assert fields_of(form)["bank_holidays"] == "ignore"
    assert form.save({**fields_of(form), "bank_holidays": "england-and-wales"}) == {}
    assert read_config(config)["bank_holidays"] is True
    assert read_config(config)["bank_holiday_division"] == "england-and-wales"


def test_the_part_of_the_uk_is_part_of_the_bank_holidays_choice(setup):
    form, config, state, _ = setup
    assert form.save({**fields_of(form), "bank_holidays": "scotland"}) == {}
    assert read_config(config)["bank_holidays"] is True
    assert read_config(config)["bank_holiday_division"] == "scotland"
    assert fields_of(form)["bank_holidays"] == "scotland"
    # Turned off, the part of the UK is kept for turning them back on: the settings file's shape is unchanged.
    assert form.save({**fields_of(form), "bank_holidays": "ignore"}) == {}
    written = tomllib.loads((state / "settings.toml").read_text())
    assert (written["bank_holidays"], written["bank_holiday_division"]) == (False, "scotland")


@pytest.mark.parametrize("chosen", ["sometimes", "true", "wales", ""])
def test_bank_holidays_setting_refuses_anything_else(setup, chosen):
    form, _, state, _ = setup
    errors = form.save({**fields_of(form), "bank_holidays": chosen})
    assert "bank_holidays" in errors
    assert not (state / "settings.toml").exists()


def test_the_board_page_points_to_the_overview_for_how_the_board_is_doing(tmp_path):
    # Each feed and the last redraw are on the Overview; the page doesn't read the board's state.
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    form = GroupForm(Store.of(config), ("board",), "/settings/board", lambda: dict(ENV), install=install_named("pi"),
                     noun="board", overview="/settings")
    page = form.render("tok")
    assert '<p class="lead"><a href="/settings">See how the board is doing</a> on the Overview.</p>' in page
    assert page.index("See how the board") < page.index('<form method="post" action="/settings/board"')
    assert "status-title" not in page and not state.exists()
    other = GroupForm(Store.of(config), ("trains_buses",), "/settings/trains-and-buses", lambda: dict(ENV),
                      install=install_named("pi"), noun="board", overview="/settings")
    assert "See how the board" not in other.render("tok")


def test_bank_holidays_are_one_choice_with_a_hint_under_dates_and_times(setup):
    form, *_ = setup
    page = form.render("tok")
    dates = page[page.index('<h2 id="section-dates-and-times">'):]
    dates = dates[:dates.index("</details>")]
    assert ('<fieldset class="field radios" aria-describedby="bank_holidays-hint"><legend>Bank holidays</legend>'
            in dates)
    words = re.findall(r'<label for="bank_holidays(?:-\d)?">([^<]+)</label>', dates)
    assert words == ["Like Sundays, for England and Wales", "Like Sundays, for Scotland",
                     "Like Sundays, for Northern Ireland", "Ignore them"]
    # The time zone waits under Advanced, with every zone suggested.
    assert dates.index("<summary>Advanced</summary>") < dates.index('id="timezone"')
    assert 'list="timezone-suggestions"' in dates and '<option value="Europe/London">' in dates


def _weather_form(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json") + "\n[weather]\nlat = 53.44\nlon = -1.95\n")
    return board_form(config, lambda: dict(ENV)), config


def test_rain_warning_controls_are_on_the_page(tmp_path):
    form, _ = _weather_form(tmp_path)
    page = form.render("tok")
    assert '<h2 id="section-weather">Weather</h2>' in page
    assert 'id="weather_enabled"' in page and 'id="weather_threshold_percent"' in page
    assert "53.44" in page  # the location is shown, not editable
    assert 'name="weather_lat"' not in page


def test_rain_warning_can_be_switched_off_and_its_threshold_changed(tmp_path):
    form, config = _weather_form(tmp_path)
    assert form.save(fields_of(form) | {"weather_enabled": "false"}) == {}
    assert load_config(config, ENV).board.weather is None
    assert form.save(fields_of(form) | {"weather_enabled": "true", "weather_threshold_percent": "70"}) == {}
    weather = load_config(config, ENV).board.weather
    assert weather is not None and weather.threshold == 70


def test_without_a_location_the_page_says_where_to_set_it(setup):
    form, *_ = setup
    page = form.render("tok")
    weather = page[page.index('<h2 id="section-weather">'):]
    weather = weather[:weather.index("</fieldset>")]
    assert "Not set up" in weather and "No location is set" in weather and "[weather]" in weather
    assert 'id="weather_enabled"' not in page


def test_trains_or_buses_can_be_switched_off_and_back_on(setup):
    form, config, state, _ = setup
    assert fields_of(form)["train_enabled"] == "true" and fields_of(form)["bus_enabled"] == "true"
    # Off, the section's other fields are kept as saved, for switching it back on.
    assert form.save(fields_of(form) | {"bus_enabled": "false"}) == {}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["train"]
    saved = tomllib.loads((state / "settings.toml").read_text())
    assert saved["bus"] == {"enabled": False}
    page = form.render("tok")
    assert fields_of(form)["bus_enabled"] == "false" and 'id="bus_stops"' in page  # shown off, to turn back on
    assert form.save(fields_of(form) | {"bus_enabled": "true"}) == {}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["train", "bus"]
    assert read_config(config)["bus"]["stops"] == {"A": "1000DGHS0900", "B": "100000008350"}


def test_switching_a_section_back_on_checks_its_fields(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form) | {"train_enabled": "false"}) == {}
    errors = form.save(fields_of(form) | {"train_enabled": "true", "train_crs": "Glossop"})
    assert set(errors) == {"train_crs"}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["bus"]  # not saved


def test_edits_to_a_section_switched_off_are_kept_for_switching_it_back_on(setup):
    form, config, *_ = setup
    edits = {"bus_enabled": "false", "board_bus_rows": "5", "bus_stops": "A = 1800SB12341\r\nB = 100000008350"}
    assert form.save(fields_of(form) | edits) == {}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["train"]
    shown = fields_of(form)
    assert (shown["board_bus_rows"], shown["bus_stops"]) == ("5", "A = 1800SB12341\nB = 100000008350")
    assert form.save(shown | {"bus_enabled": "true"}) == {}
    bus = load_config(config, ENV).board.sources[1].source
    assert bus._stops == {"A": "1800SB12341", "B": "100000008350"} and bus._rows == 5


def test_checking_times_are_rows_saved_as_the_windows_the_board_reads(setup):
    form, config, state, _ = setup
    # The second row emptied, and a weekend window in a spare row.
    fields = {name: value for name, value in fields_of(form).items() if not name.startswith("train_windows-2-")}
    fields |= {"train_windows-2-from": "", "train_windows-2-to": "", "train_windows-3-sat": "on",
               "train_windows-3-sun": "on", "train_windows-3-from": "9:00", "train_windows-3-to": "12:00"}
    changed = []
    assert form.save(fields, changed) == {}
    saved = tomllib.loads((state / "settings.toml").read_text())
    assert saved["train"]["windows"] == ["Mon-Fri 06:30-09:30", "Sat,Sun 09:00-12:00"]
    assert saved["bus"]["windows"] == ["Mon-Fri 07:00-08:30"]
    assert changed == ["When to check for trains"]  # which of the two, said in the notice
    train = load_config(config, ENV).board.sources[0]
    assert len(train.policy.windows) == 2
    again = []
    assert form.save(fields_of(form), again) == {} and again == []


def test_a_row_s_mistake_is_linked_from_the_summary_to_its_box(setup):
    form, _, state, _ = setup
    fields = fields_of(form) | {"train_windows-1-from": "6.3", "bus_windows-2-from": "07:00", "bus_windows-2-to": "08:00"}
    errors = form.save(fields)
    assert set(errors) == {"train_windows-1-from", "bus_windows-2-days"}
    assert not (state / "settings.toml").exists()
    page = form.render("tok", errors=errors, values=fields)
    assert ('<a href="#train_windows-1-from">Trains – Checking time 1, From: Enter a 24-hour time, like 06:30</a>'
            in page)
    assert '<a href="#bus_windows-2-mon">Buses – Checking time 2, Days: Tick at least one day</a>' in page
    assert 'id="train_windows-1-from" name="train_windows-1-from" type="text" value="6.3"' in page  # as typed


def test_the_board_needs_at_least_one_checking_time(setup):
    form, _, state, _ = setup
    fields = {name: value for name, value in fields_of(form).items() if not name.startswith("bus_windows-")}
    fields |= {f"bus_windows-{row}-{box}": "" for row in (1, 2, 3) for box in ("from", "to")}
    errors = form.save(fields)
    assert set(errors) == {"bus_windows"} and not (state / "settings.toml").exists()
    page = form.render("tok", errors=errors, values=fields)
    assert '<a href="#bus_windows-1-mon">Buses – When to check for buses: Add at least one checking time' in page


def test_a_mistake_in_a_section_switched_off_is_still_reported(setup):
    form, *_ = setup
    # Unchanged fields are left as saved, unchecked; a field changed is checked, as it is kept.
    fields = fields_of(form)
    unchanged = {"bus_windows-1-from": "7:00"}  # the time saved, put another way
    errors = form.save(fields | unchanged | {"bus_enabled": "false", "board_bus_rows": "many"})
    assert set(errors) == {"board_bus_rows"}


def test_trains_and_buses_cannot_both_be_switched_off(setup):
    form, config, state, _ = setup
    fields = fields_of(form) | {"train_enabled": "false", "bus_enabled": "false"}
    errors = form.save(fields)
    assert set(errors) == {"train_enabled"} and "board would show nothing" in errors["train_enabled"]
    assert not (state / "settings.toml").exists()  # not saved
    page = form.render("tok", errors=errors, values=fields)
    assert '<a href="#train_enabled">Trains – Show trains: Trains and buses can' in page
    # A choice's hint and error describe its group of buttons; its first button is the link's target.
    assert ('<fieldset class="field radios has-error" aria-describedby="train_enabled-hint train_enabled-error"'
            ' data-unsaved>'
            "<legend>Show trains</legend>") in page
    assert '<input type="radio" id="train_enabled" name="train_enabled" value="true">' in page


def test_the_only_section_set_up_cannot_be_switched_off(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").split("[bus]")[0])
    form = board_form(config, lambda: dict(ENV))
    assert set(form.save(fields_of(form) | {"train_enabled": "false"})) == {"train_enabled"}


def test_a_section_off_with_the_templates_placeholders_saves(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    off = CONFIG.replace('[bus]\nsource = "transportapi"\n', '[bus]\nsource = "transportapi"\nenabled = false\n').replace('B = "100000008350"', 'B = "CHANGE_ME"')
    config.write_text(off.format(state=state / "state.json"))
    form = board_form(config, lambda: dict(ENV))
    assert fields_of(form)["bus_enabled"] == "false"
    assert form.save(fields_of(form)) == {}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["train"]


def test_text_size_can_be_set_on_the_page(setup):
    form, config, *_ = setup
    assert 'id="text_size"' in form.render("tok")
    assert form.save(fields_of(form) | {"theme": "plain", "accent": "", "text_size": "12"}) == {}
    assert load_config(config, ENV).board.theme.text_size == 12


# --- Saving keeps what the setup wizard wrote ---


class FoundJob:
    """The wizard's bus search, done as soon as it starts."""

    def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None):
        pass

    def start(self):
        pass

    def cancel(self):
        pass

    def status(self):
        route = {"stop": "A", "stops": [{"name": "Hadfield", "lat": 53.46, "lon": -1.965},
                                        {"name": "Norfolk Street", "lat": 53.4452, "lon": -1.9512}]}
        bus = {"source": "bods", "operators": ["BNML"], "routes": {"341": route}}
        return {"state": "done", "step": "", "done": 1, "total": 1, "message": "Found 1",
                "result": {"fragment": {"bus": bus}, "lines": {}, "stop_lines": {}, "notes": []}}


def test_a_save_keeps_what_the_setup_wizard_wrote(tmp_path):
    # Regression: the page wrote settings.toml whole from its own fields, so
    # the first save after setup dropped [bus] source, operators and routes,
    # [weather] lat and lon, and anything else it doesn't show.
    from twobells.settings.overlay import board_target
    from twobells.setup.api import SetupApi
    from twobells.setup.lookups import Stop

    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\ndisplay = "hdmi-1280x400"'))
    env = {**ENV, "BODS_API_KEY": "bods-secret-key-123"}
    home = {"lat": 53.461964, "lon": -1.965633}
    stop = Stop("1800SB12341", "Station Road", "Stop A", "N", 53.4615, -1.9662, 64)
    wizard = SetupApi(board_target(config), lambda: dict(env), state, nearby_stops=lambda lat, lon, **kw: [stop],
                      stations=lambda: (("Glossop", "GLO", 53.4445, -1.9491),), make_job=FoundJob)
    assert wizard.handle("stops", home)[0] == 200
    assert wizard.handle("start", {"stops": {"A": "1800SB12341"}, "routes": True})[0] == 200
    status, body = wizard.handle("finish", {"home": {**home, "country": "Scotland"}, "station_crs": "GLO",
                                            "stops": {"A": "1800SB12341"}, "walk": {"GLO": 6}})
    assert status == 200, body
    with (state / "settings.toml").open("a", encoding="utf-8") as saved:
        saved.write('\n[dashboard]\ntitle = "Our house"\n')  # a table neither manages: not read, so dropped

    form = board_form(config, lambda: dict(env))
    assert form.save(fields_of(form) | {"title": "Home", "board_train_rows": "2"}) == {}
    # Everything setup and the page wrote is read back: none of it is outside schema.saved_keys.
    written = tomllib.loads((state / "settings.toml").read_text())
    assert read_overlay_in(state) == written and "dashboard" not in written

    data = read_config(config)
    assert (data["title"], data["train"]["rows"]) == ("Home", 2)
    assert data["bus"]["source"] == "bods" and data["bus"]["operators"] == ["BNML"]
    assert set(data["bus"]["routes"]) == {"341"}
    assert (data["weather"]["lat"], data["weather"]["lon"]) == (53.46, -1.97)
    assert data["bank_holiday_division"] == "scotland"
    settings = load_config(config, env)
    assert settings.board.weather is not None and set(settings.board.tracker.routes) == {"341"}


def test_every_field_the_page_saves_is_read_back():
    from twobells.board.displays.display import DISPLAY_PANELS
    from twobells.board.themes import THEMES
    from twobells.settings.schema import fields

    for display in DISPLAY_PANELS:
        for theme in THEMES:
            for f in fields(display, theme):
                if f.group in ("general", "board"):
                    assert f.key in saved_keys()[f.section], f.name


def test_the_page_can_still_clear_a_field_setup_filled(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    state.mkdir()
    (state / "settings.toml").write_text('[train]\nwalk_minutes = 6\nfilter_crs = "MAN"\n')
    form = board_form(config, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"train_walk_minutes": "", "train_filter_crs": ""}) == {}
    data = read_config(config)
    assert data["train"]["walk_minutes"] == "" and data["train"]["filter_crs"] == ""
    assert load_config(config, ENV).board.walk.train is None


# The board's modes: [board] modes (settings/modes.py).


def test_the_board_s_modes_are_a_tickbox_each_saying_how_many_the_display_has_room_for(setup):
    form, *_ = setup
    page = form.render("tok")
    section = page[page.index('<h2 id="section-modes-of-transport">'):]
    section = section[:section.index("</fieldset></fieldset>")]
    assert "Which departures the board shows, each in a section of its own." in section
    assert '<legend class="vh">Modes of transport</legend>' in section
    assert "The Inky pHAT has room for 2: each takes a section with its own heading" in section
    boxes = re.findall(r'<input id="board_modes-(\w+)" name="board_modes-\w+" type="checkbox" value="on"( checked)?'
                       r'><label for="board_modes-\w+">(\w+)</label>', section)
    assert boxes == [("train", " checked", "Trains"), ("bus", " checked", "Buses")]
    assert page.index('id="section-display"') < page.index('id="section-modes-of-transport"') < page.index(
        'id="section-what-to-show"')


def test_unticking_buses_shows_trains_alone_on_the_board_and_saves_the_list(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form)) == {}
    assert "modes" not in read_overlay_in(config.parent / "state").get("board", {})  # as it was: nothing saved
    changed = []
    sent = {k: v for k, v in fields_of(form).items() if k != "board_modes-bus"}
    assert form.save(sent, changed) == {}
    assert changed == ["Modes of transport"]
    assert read_overlay_in(config.parent / "state")["board"]["modes"] == ["train"]
    settings = load_config(config, ENV)
    assert settings.board.modes == ("train",) and [c.source.name for c in settings.board.sources] == ["train"]
    # Buses stay set up: the page still offers them, unticked.
    assert "board_modes-bus" not in fields_of(form) and fields_of(form)["board_modes-train"] == "on"


def test_the_board_needs_a_mode_ticked(setup):
    form, *_ = setup
    sent = {k: v for k, v in fields_of(form).items() if not k.startswith("board_modes-")}
    assert form.save(sent) == {"board_modes": "Tick at least one, or the departures show nowhere here"}


def test_with_one_mode_set_up_there_is_nothing_to_choose(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace("[bus]\n", "[bus]\nenabled = false\n"))
    page = board_form(config, lambda: dict(ENV)).render("tok")
    assert "board_modes" not in page and "Modes of transport" not in page


# A screen's room for modes goes by its look and text size (board/panel.py: most_modes).


def _screen_form(tmp_path, extra=""):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    text = CONFIG.format(state=state / "state.json").replace(
        'theme = "sign"\n', 'theme = "sign"\ndisplay = "hdmi-1280x400"\n' + extra)
    config.write_text(text + '\n[board]\nmodes = ["train", "bus", "metro"]\n')
    return board_form(config, lambda: dict(ENV)), config


def test_a_screen_naming_three_modes_offers_only_the_text_sizes_with_room_for_them(tmp_path):
    form, _ = _screen_form(tmp_path)
    page = form.render("tok")
    select = page[page.index('<select id="text_size"'):]
    select = select[:select.index("</select>")]
    assert re.findall(r'<option value="(\d+)"', select) == ["9", "10", "11", "12"]
    assert "With 3 modes, the 7.9-inch screen has room for text up to size 12." in page
    assert "The 7.9-inch screen in the station board look has room for 3: each takes a section" in page


def test_a_screen_refuses_a_look_and_text_size_without_room_for_the_modes_named(tmp_path):
    form, config = _screen_form(tmp_path)
    errors = form.save(fields_of(form) | {"theme": "plain", "accent": "", "text_size": "13"})
    assert errors == {"board_modes": "The 7.9-inch screen in the plain look at text size 13 has room for 2 modes at "
                                     "most, each with its own section, not 3. Untick 1, or choose text size 12 or "
                                     "smaller, or another look."}
    assert form.save(fields_of(form) | {"theme": "plain", "accent": "", "text_size": "12"}) == {}
    assert load_config(config, ENV).board.theme.text_size == 12


def test_a_screen_naming_three_modes_at_a_big_text_size_offers_no_plain_look(tmp_path):
    form, _ = _screen_form(tmp_path, "text_size = 14\n")
    page = form.render("tok")
    assert 'value="plain"' not in page
    assert "The plain look isn&#x27;t offered: at text size 14, the 7.9-inch screen has room for 2 modes" in page
    assert form.save(fields_of(form)) == {}  # as it is, in the station board look, it saves
