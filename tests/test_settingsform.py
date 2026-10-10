import re
import tomllib

import pytest

from departureboard.config import load_config, read_config
from departureboard.settingsform import SettingsForm, to_toml

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


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    secrets = dict(ENV)
    form = SettingsForm(config, lambda: dict(secrets))
    return form, config, state, secrets


def fields_of(form):
    """What a browser would submit for the form as rendered."""
    page = form.render("tok")
    fields = {}
    for name, value in re.findall(r'<input id="(\w+)" name="\w+" type="text" value="([^"]*)"', page):
        fields[name] = value.replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    for name, value in re.findall(r'<textarea id="(\w+)" name="\w+"[^>]*>([^<]*)</textarea>', page):
        fields[name] = value
    for name, body in re.findall(r'<select id="(\w+)" name="\w+"[^>]*>(.*?)</select>', page):
        chosen = re.search(r'value="([^"]*)" selected', body)
        fields[name] = chosen.group(1) if chosen else ""
    return fields


def test_form_shows_the_current_settings(setup):
    form, *_ = setup
    fields = fields_of(form)
    assert fields["title"] == "Glossop > Manchester Picc"
    assert fields["train_crs"] == "GLO"
    assert fields["train_windows"] == "Mon-Fri 06:30-09:30\nMon-Fri 16:30-19:30"
    assert fields["bus_stops"] == "A = 1000DGHS0900\nB = 100000008350"
    assert fields["bus_walk_minutes"] == "A = 7\nB = 6"
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
    fields = fields_of(form) | {"title": "Home", "train_rows": "2", "bus_walk_minutes": "5"}
    assert form.save(fields) == {}
    assert config.read_text() == original
    settings = load_config(config, ENV)
    assert settings.board.title == "Home"
    assert dict(settings.board.walk.bus) == {"*": 5}
    assert (state / "settings.toml").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "name, value, message",
    [
        ("train_crs", "GLOSSOP", "3-letter"),
        ("train_rows", "99", "1 to 6"),
        ("train_windows", "weekdays", "isn't a window"),
        ("bus_stops", "A = not a code!", "6 to 12"),
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


def test_the_page_links_to_the_setup_wizard(setup):
    form, *_ = setup
    page = form.render("tok")
    assert '<a href="/setup/">Set up home, station and stops</a>' in page
    assert page.index('href="/setup/"') < page.index('<form method="post" action="/" ')


def test_keys_are_write_only(setup):
    form, *_ = setup
    page = form.render("tok")
    assert "rdm-secret-123" not in page and "tapi-secret-456" not in page
    assert "Currently set" in page and "Currently not set" in page  # BODS isn't


def test_a_pasted_key_is_saved_privately_and_merged(setup):
    form, config, state, _ = setup
    (state).mkdir(parents=True, exist_ok=True)
    (state / "secrets.env").write_text("TAPI_APP_ID=kept\nRDM_DETAILS_API_KEY=kept-too\n")
    assert form.save(fields_of(form) | {"BODS_API_KEY": "abc123DEF"}) == {}
    saved = (state / "secrets.env").read_text()
    assert "BODS_API_KEY=abc123DEF" in saved and "TAPI_APP_ID=kept" in saved
    # A key this page doesn't list is kept too, as the setup wizard keeps it.
    assert "RDM_DETAILS_API_KEY=kept-too" in saved
    assert (state / "secrets.env").stat().st_mode & 0o777 == 0o600


def test_keys_cannot_inject_extra_lines(setup):
    form, _, state, _ = setup
    errors = form.save(fields_of(form) | {"BODS_API_KEY": "abc\nRDM_API_KEY=evil"})
    assert "BODS_API_KEY" in errors
    assert not (state / "secrets.env").exists()


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
        "TAPI_APP_KEY": "Paste the key exactly",
        "_form": "Not saved: something",
    })
    assert '<a href="#train_crs">Trains – Station code: Enter a 3-letter station code, like GLO</a>' in page
    assert '<a href="#title">Board – Title: Enter some text</a>' in page
    assert '<a href="#TAPI_APP_KEY">Keys – TransportAPI app key: Paste the key exactly</a>' in page
    assert "<li>Not saved: something</li>" in page


def test_the_error_summary_takes_focus(setup):
    form, *_ = setup
    page = form.render("tok", errors={"title": "Enter some text"})
    assert re.search(r'<div class="summary"[^>]*tabindex="-1"[^>]*autofocus', page)


@pytest.mark.parametrize("name, value, quoted", [
    ("bus_stops", "A = 1000DGHS0900\nB = bad!", "“B = bad!”"),
    ("bus_stops", "A = 1000DGHS0900\nStop B = 100000008350", "“Stop B = 100000008350”"),
    ("bus_walk_minutes", "A = 7\nB = soon", "“B = soon”"),
])
def test_errors_in_a_many_line_field_quote_the_line(setup, name, value, quoted):
    form, *_ = setup
    errors = form.save(fields_of(form) | {name: value})
    assert quoted in errors[name]


def test_key_boxes_are_plain_text_so_password_managers_leave_them_alone(setup):
    form, *_ = setup
    page = form.render("tok")
    assert 'type="password"' not in page
    for name in ("RDM_API_KEY", "TAPI_APP_ID", "TAPI_APP_KEY", "BODS_API_KEY"):
        assert f'<input id="{name}" name="{name}" type="text" autocomplete="off" spellcheck="false"' in page


def _accents(page):
    select = re.search(r'<select id="accent"[^>]*>(.*?)</select>', page).group(1)
    return re.findall(r'value="([^"]*)"', select)


def _form_for(tmp_path, extra):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', extra))
    return SettingsForm(config, lambda: dict(ENV))


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


def test_outlined_buttons_have_a_border_that_stands_out():
    from importlib.resources import files

    css = files("departureboard").joinpath("web.css").read_text()
    assert re.search(r"button\.quiet \{[^}]*border: 1px solid var\(--muted\)", css)

    def luminance(hex_colour):
        rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    light, dark = re.findall(r"--bg: (#\w{6});\s*--fg: #\w{6};\s*--muted: (#\w{6});", css)
    for bg, muted in (light, dark):
        high, low = sorted((luminance(bg), luminance(muted)), reverse=True)
        assert (high + 0.05) / (low + 0.05) >= 3


def test_problem_text_is_readable_in_light_and_dark():
    from importlib.resources import files

    css = files("departureboard").joinpath("web.css").read_text()
    assert re.search(r"\.problem \{[^}]*color: var\(--error\)", css)

    def luminance(hex_colour):
        rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    backgrounds = re.findall(r"--bg: (#\w{6});", css)
    errors = re.findall(r"--error: (#\w{6});", css)
    assert len(backgrounds) == len(errors) == 2
    for bg, error in zip(backgrounds, errors):
        high, low = sorted((luminance(bg), luminance(error)), reverse=True)
        assert (high + 0.05) / (low + 0.05) >= 4.5  # body text, WCAG 1.4.3


def test_every_control_has_a_label(setup):
    form, *_ = setup
    page = form.render("tok")
    ids = set(re.findall(r'<(?:input|select|textarea) id="(\w+)"', page))
    labelled = set(re.findall(r'<label for="(\w+)"', page))
    assert ids and ids <= labelled


def test_a_screen_can_turn_a_quarter(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\ndisplay = "hdmi-1280x400"\nrotate = 90'))
    form = SettingsForm(config, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"rotate": "270"}) == {}
    assert load_config(config, ENV).rotate == 270


def test_removing_a_stop_takes_effect(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form) | {"bus_stops": "A = 1000DGHS0900", "bus_walk_minutes": "A = 7"}) == {}
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
    form = SettingsForm(config, lambda: dict(ENV))
    assert fields_of(form)["bus_directions"] == "Ashton"
    assert form.save(fields_of(form) | {"bus_directions": ""}) == {}
    assert read_config(config)["bus"]["directions"] == []
    assert fields_of(form)["bus_directions"] == ""


def test_clearing_walk_times_takes_effect(setup):
    form, config, *_ = setup
    assert form.save(fields_of(form) | {"train_walk_minutes": "", "bus_walk_minutes": ""}) == {}
    walk = load_config(config, ENV).board.walk
    assert walk.train is None and dict(walk.bus) == {}
    fields = fields_of(form)
    assert fields["train_walk_minutes"] == "" and fields["bus_walk_minutes"] == ""


def test_choosing_the_looks_own_accent_clears_one_set_in_config(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\naccent = "red"'))
    form = SettingsForm(config, lambda: dict(ENV))
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
    form = SettingsForm(config, lambda: dict(env))
    assert form.save(fields_of(form) | {"bus_stops": "A = 1000DGHS0900", "bus_walk_minutes": "5"}) == {}
    assert "routes" not in (state / "settings.toml").read_text()
    assert set(load_config(config, env).board.tracker.routes) == {"341"}


def test_toml_writer_round_trips():
    data = {"title": 'Say "hi" \\ there', "rotate": 90, "train": {"windows": ["Mon-Fri 06:30-09:30"]}, "bus": {"stops": {"A": "1000DGHS0900"}, "rows": 3}}
    assert tomllib.loads(to_toml(data)) == data


def test_toml_writer_round_trips_characters_toml_is_fussy_about():
    data = {"title": "Bus 🚌 café\x7f\x01\ttab", "train": {"directions": ["Hyde ➜ 🚏"]}}
    assert tomllib.loads(to_toml(data)) == data


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


def test_bank_holidays_are_treated_like_sundays_unless_turned_off(setup):
    form, config, state, _ = setup
    assert fields_of(form)["bank_holidays"] == "true"
    assert form.save({**fields_of(form), "bank_holidays": "false"}) == {}
    assert tomllib.loads((state / "settings.toml").read_text())["bank_holidays"] is False
    assert read_config(config)["bank_holidays"] is False
    assert fields_of(form)["bank_holidays"] == "false"
    assert form.save({**fields_of(form), "bank_holidays": "true"}) == {}
    assert read_config(config)["bank_holidays"] is True


def test_bank_holidays_setting_refuses_anything_else(setup):
    form, _, state, _ = setup
    errors = form.save({**fields_of(form), "bank_holidays": "sometimes"})
    assert "bank_holidays" in errors
    assert not (state / "settings.toml").exists()


def test_bank_holiday_division_defaults_to_england_and_wales_and_can_be_changed(setup):
    form, config, _, _ = setup
    assert fields_of(form)["bank_holiday_division"] == "england-and-wales"
    assert form.save({**fields_of(form), "bank_holiday_division": "scotland"}) == {}
    assert read_config(config)["bank_holiday_division"] == "scotland"
    assert fields_of(form)["bank_holiday_division"] == "scotland"


def test_bank_holiday_division_refuses_anything_else(setup):
    form, _, state, _ = setup
    errors = form.save({**fields_of(form), "bank_holiday_division": "wales"})
    assert "bank_holiday_division" in errors
    assert not (state / "settings.toml").exists()


def _status_form(tmp_path, state_text=None):
    from datetime import datetime

    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    if state_text is not None:
        state.mkdir()
        (state / "state.json").write_text(state_text)
    form = SettingsForm(config, lambda: dict(ENV), clock=lambda: datetime(2026, 10, 8, 8, 1))
    return form, state


def test_status_comes_before_the_form_under_its_own_heading(tmp_path):
    saved = '{"sources": {"train": {"last_success": "2026-10-08T07:58:00"}}, "last_render": {"at": "2026-10-08T07:58:00"}}'
    form, _ = _status_form(tmp_path, saved)
    page = form.render("tok")
    assert page.index("<h1>") < page.index('<h2 id="status-title">Status</h2>') < page.index("<form method=\"post\" action=\"/\"")
    assert page.index("Status</h2>") < page.index("<fieldset>")
    assert "07:58, 3 min ago" in page


def test_status_says_plainly_when_there_is_no_state_file(tmp_path):
    form, state = _status_form(tmp_path)
    assert "No status yet" in form.render("tok")
    assert not state.exists()  # the page reads the state file, never writes it


def test_status_copes_with_an_empty_state_file(tmp_path):
    form, state = _status_form(tmp_path, "")
    assert "No status yet" in form.render("tok")
    assert (state / "state.json").read_text() == ""


def test_status_says_when_the_state_file_cannot_be_read(tmp_path):
    form, state = _status_form(tmp_path, "{}")
    (state / "state.json").chmod(0)
    try:
        page = form.render("tok")
    finally:
        (state / "state.json").chmod(0o600)
    assert "Problem:" in page and "can’t be read" in page


def test_status_shows_no_keys_from_the_state_file(tmp_path):
    saved = ('{"sources": {"bus": {"last_error": {"at": "2026-10-08T08:00:00", '
             '"message": "https://transportapi.com/v3/x.json?app_id=id&app_key=tapi-secret-456 refused"}}}}')
    form, _ = _status_form(tmp_path, saved)
    page = form.render("tok")
    assert "tapi-secret-456" not in page and "Problem:" in page


def test_bank_holidays_setting_is_in_the_board_section_with_a_hint(setup):
    form, *_ = setup
    page = form.render("tok")
    board = page[page.index("<legend>Board</legend>"):page.index("<legend>Trains</legend>")]
    assert '<label for="bank_holidays">Bank holidays</label>' in board
    assert 'aria-describedby="bank_holidays-hint"' in board
    assert "Treat bank holidays like Sundays" in board and "Ignore bank holidays" in board


def _weather_form(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json") + "\n[weather]\nlat = 53.44\nlon = -1.95\n")
    return SettingsForm(config, lambda: dict(ENV)), config


def test_rain_warning_controls_are_on_the_page(tmp_path):
    form, _ = _weather_form(tmp_path)
    page = form.render("tok")
    assert "Rain warning" in page and 'id="weather_enabled"' in page and 'id="weather_threshold_percent"' in page
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
    assert "Rain warning" in page and "[weather]" in page
    assert 'id="weather_enabled"' not in page


def test_trains_or_buses_can_be_switched_off_and_back_on(setup):
    form, config, state, _ = setup
    assert fields_of(form)["train_enabled"] == "true" and fields_of(form)["bus_enabled"] == "true"
    # Off, the section's other fields are kept as saved, unchecked, however they were left.
    assert form.save(fields_of(form) | {"bus_enabled": "false", "bus_stops": "", "bus_rows": "many"}) == {}
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


def test_a_section_off_with_the_templates_placeholders_saves(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    off = CONFIG.replace('[bus]\n', '[bus]\nenabled = false\n').replace('B = "100000008350"', 'B = "CHANGE_ME"')
    config.write_text(off.format(state=state / "state.json"))
    form = SettingsForm(config, lambda: dict(ENV))
    assert fields_of(form)["bus_enabled"] == "false"
    assert form.save(fields_of(form)) == {}
    assert [c.source.name for c in load_config(config, ENV).board.sources] == ["train"]


def test_status_leaves_out_a_section_switched_off(setup):
    form, _, state, _ = setup
    state.mkdir()
    (state / "state.json").write_text('{"sources": {"bus": {"last_success": "2026-10-08T07:24:00"}}}')
    assert "<h3>Buses</h3>" in form.render("tok")
    assert form.save(fields_of(form) | {"bus_enabled": "false"}) == {}
    assert "<h3>Buses</h3>" not in form.render("tok")


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
    from departureboard.setup import Stop
    from departureboard.setupapi import SetupApi

    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json").replace('theme = "sign"', 'theme = "sign"\ndisplay = "hdmi-1280x400"'))
    env = {**ENV, "BODS_API_KEY": "bods-secret-key-123"}
    home = {"lat": 53.461964, "lon": -1.965633}
    stop = Stop("1800SB12341", "Norfolk Street", "Stop A", "N", 53.4440, -1.9500, 60)
    wizard = SetupApi(config, lambda: dict(env), state, nearby_stops=lambda lat, lon, **kw: [stop],
                      stations=lambda: (("Glossop", "GLO", 53.4446, -1.9656),), make_job=FoundJob)
    assert wizard.handle("stops", home)[0] == 200
    assert wizard.handle("start", {"stops": {"A": "1800SB12341"}, "routes": True})[0] == 200
    status, body = wizard.handle("finish", {"home": {**home, "country": "Scotland"}, "station_crs": "GLO",
                                            "stops": {"A": "1800SB12341"}, "walk": {"GLO": 6}})
    assert status == 200, body
    with (state / "settings.toml").open("a", encoding="utf-8") as saved:
        saved.write('\n[dashboard]\ntitle = "Our house"\n')  # a table this page knows nothing of

    form = SettingsForm(config, lambda: dict(env))
    assert form.save(fields_of(form) | {"title": "Home", "train_rows": "2"}) == {}

    data = read_config(config)
    assert (data["title"], data["train"]["rows"]) == ("Home", 2)
    assert data["bus"]["source"] == "bods" and data["bus"]["operators"] == ["BNML"]
    assert set(data["bus"]["routes"]) == {"341"}
    assert (data["weather"]["lat"], data["weather"]["lon"]) == (53.44, -1.95)
    assert data["bank_holiday_division"] == "scotland"
    assert data["dashboard"] == {"title": "Our house"}
    settings = load_config(config, env)
    assert settings.board.weather is not None and set(settings.board.tracker.routes) == {"341"}


def test_the_page_can_still_clear_a_field_setup_filled(tmp_path):
    state = tmp_path / "state"
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    state.mkdir()
    (state / "settings.toml").write_text('[train]\nwalk_minutes = 6\nfilter_crs = "MAN"\n')
    form = SettingsForm(config, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"train_walk_minutes": "", "train_filter_crs": ""}) == {}
    data = read_config(config)
    assert data["train"]["walk_minutes"] == "" and data["train"]["filter_crs"] == ""
    assert load_config(config, ENV).board.walk.train is None
