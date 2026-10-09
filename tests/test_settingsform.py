import re
import tomllib

import pytest

from departureboard.config import load_config
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


def test_keys_are_write_only(setup):
    form, *_ = setup
    page = form.render("tok")
    assert "rdm-secret-123" not in page and "tapi-secret-456" not in page
    assert "Currently set" in page and "Currently not set" in page  # BODS isn't


def test_a_pasted_key_is_saved_privately_and_merged(setup):
    form, config, state, _ = setup
    (state).mkdir(parents=True, exist_ok=True)
    (state / "secrets.env").write_text("TAPI_APP_ID=kept\nSOMETHING_ELSE=dropped\n")
    assert form.save(fields_of(form) | {"BODS_API_KEY": "abc123DEF"}) == {}
    saved = (state / "secrets.env").read_text()
    assert "BODS_API_KEY=abc123DEF" in saved and "TAPI_APP_ID=kept" in saved
    assert "SOMETHING_ELSE" not in saved
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
