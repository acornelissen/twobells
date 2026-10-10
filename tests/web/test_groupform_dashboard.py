"""A GroupForm of every page a dashboard's settings have, together: the
dashboard's settings, saved into the store, without the web server. The
settings site's pages (test_settingsite.py) are each one of these."""

import re
import tomllib

import pytest

from departureboard.dashboard.config import dashboard_target, load_dashboard
from departureboard.settings.installs import install_named
from departureboard.settings.store import Store
from departureboard.web.groupform import GroupForm

CONFIG = """
title = "Glossop"
[train]
crs = "GLO"
[bus]
source = "bods"
operators = ["BNML"]
[bus.stops]
A = "1000DGHS0900"
[weather]
lat = 53.44
lon = -1.95
"""
ENV = {"RDM_API_KEY": "rail-key-1234567", "BODS_API_KEY": "bods-key-7654321"}


def dashboard_form(store, load_secrets):
    """A dashboard's settings pages as one form, on a desktop."""
    return GroupForm(store, ("trains_buses", "dashboard", "keys", "general"), "/settings", load_secrets,
                     install=install_named("desktop"), noun="dashboard")


@pytest.fixture
def form(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    folder = tmp_path / "dash"
    folder.mkdir()
    store = Store.open(config, state_dir=folder)
    page = dashboard_form(store, lambda: store.env(ENV))
    page.config, page.folder = config, folder
    return page


def fields_of(form):
    """What a browser would submit for the form as rendered."""
    page = form.render("tok")
    values = dict(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="text" value="([^"]*)"', page))
    values |= dict(re.findall(r'<textarea id="([a-z_]+)"[^>]*>([^<]*)</textarea>', page))
    for name, options in re.findall(r'<select id="([a-z_]+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
    values |= dict(re.findall(r'<input type="radio" id="[\w-]+" name="(\w+)" value="([^"]*)" checked', page))
    values |= dict.fromkeys(re.findall(r'<input id="([\w-]+)" name="[\w-]+" type="checkbox" value="on" checked', page),
                            "on")
    return values


def loaded(form):
    dash = load_dashboard(form.config, ENV, form.folder, lock=False)
    return dash.train is not None, dash.bus is not None, dash.weather is not None


def test_trains_and_buses_are_on_unless_switched_off(form):
    fields = fields_of(form)
    assert (fields["train_enabled"], fields["bus_enabled"], fields["weather_enabled"]) == ("true", "true", "true")
    assert loaded(form) == (True, True, True)


def test_trains_or_buses_can_be_switched_off_and_back_on(form):
    # Off, the group's other fields are kept as saved, for switching it back on.
    assert form.save(fields_of(form) | {"bus_enabled": "false"}) == {}
    assert loaded(form) == (True, False, True)
    saved = tomllib.loads((form.folder / "settings.toml").read_text())
    assert saved["bus"] == {"enabled": False} and "bus_rows" not in saved["dashboard"]
    page = form.render("tok")
    assert fields_of(form)["bus_enabled"] == "false" and 'id="bus_stops"' in page  # shown off, to turn back on
    assert form.save(fields_of(form) | {"bus_enabled": "true", "train_enabled": "false"}) == {}
    assert loaded(form) == (False, True, True)


def test_switching_a_section_back_on_checks_its_fields(form):
    assert form.save(fields_of(form) | {"train_enabled": "false"}) == {}
    errors = form.save(fields_of(form) | {"train_enabled": "true", "train_crs": "Glossop"})
    assert set(errors) == {"train_crs"}
    assert loaded(form) == (False, True, True)  # not saved


def test_edits_to_a_group_switched_off_are_kept_for_switching_it_back_on(form):
    edits = {"bus_enabled": "false", "dashboard_bus_rows": "4", "bus_stops": "A = 1800SB12341\r\nB = 1800SB12342"}
    assert form.save(fields_of(form) | edits) == {}
    assert loaded(form) == (True, False, True)
    shown = fields_of(form)
    assert (shown["dashboard_bus_rows"], shown["bus_stops"]) == ("4", "A = 1800SB12341\nB = 1800SB12342")
    assert form.save(shown | {"bus_enabled": "true"}) == {}
    dash = load_dashboard(form.config, ENV, form.folder, lock=False)
    assert dash.bus_stops == {"A": "1800SB12341", "B": "1800SB12342"} and dash.options.bus_rows == 4


def test_a_mistake_in_a_group_switched_off_is_still_reported(form):
    errors = form.save(fields_of(form) | {"bus_enabled": "false", "dashboard_bus_rows": "99", "bus_stops": ""})
    assert set(errors) == {"dashboard_bus_rows", "bus_stops"}


def test_trains_and_buses_cannot_both_be_switched_off(form):
    fields = fields_of(form) | {"train_enabled": "false", "bus_enabled": "false"}
    errors = form.save(fields)
    assert set(errors) == {"train_enabled"} and "dashboard would show nothing" in errors["train_enabled"]
    assert not (form.folder / "settings.toml").exists()  # not saved
    page = form.render("tok", errors=errors, values=fields)
    assert '<a href="#train_enabled">Trains – Show trains: Trains and buses can' in page
    # A choice's hint and error describe its group of buttons; its first button is the link's target.
    assert ('<fieldset class="field radios has-error" aria-describedby="train_enabled-hint train_enabled-error">'
            "<legend>Show trains</legend>") in page
    assert '<input type="radio" id="train_enabled" name="train_enabled" value="true">' in page


def test_a_section_off_with_the_templates_placeholders_saves(form):
    form.config.write_text(CONFIG.replace('[bus]\nsource = "bods"', '[bus]\nenabled = false\nsource = "transportapi"')
                           .replace('A = "1000DGHS0900"', 'A = "CHANGE_ME"'))
    assert fields_of(form)["bus_enabled"] == "false"
    assert form.save(fields_of(form)) == {}
    assert loaded(form) == (True, False, True)


def test_the_on_off_choice_is_a_labelled_and_described_pair_of_radio_buttons(form):
    page = form.render("tok")
    assert ('<fieldset class="field radios" aria-describedby="bus_enabled-hint"><legend>Show buses</legend>'
            in page)
    assert ('<div class="option"><input type="radio" id="bus_enabled" name="bus_enabled" value="true" checked>'
            '<label for="bus_enabled">On</label></div>') in page
    assert ('<div class="option"><input type="radio" id="bus_enabled-1" name="bus_enabled" value="false">'
            '<label for="bus_enabled-1">Off</label></div>') in page


# What is saved is read back


class FoundJob:
    """The wizard's bus search, done as soon as it starts."""

    def __init__(self, folder, stops, api_key, *, routes=False, walk_minutes=None):
        pass

    def start(self):
        pass

    def status(self):
        route = {"stop": "A", "stops": [{"name": "Hadfield", "lat": 53.46, "lon": -1.965},
                                        {"name": "Norfolk Street", "lat": 53.4452, "lon": -1.9512}]}
        bus = {"source": "bods", "operators": ["BNML"], "routes": {"341": route}}
        return {"state": "done", "step": "", "done": 1, "total": 1, "message": "Found 1",
                "result": {"fragment": {"bus": bus}, "lines": {}, "stop_lines": {}, "notes": []}}


def test_position_strips_are_on_until_switched_off(form):
    assert fields_of(form)["row_strips"] == "true"
    assert form.save(fields_of(form) | {"row_strips": "false"}) == {}
    written = tomllib.loads((form.folder / "settings.toml").read_text())
    assert written["dashboard"]["row_strips"] is False
    assert load_dashboard(form.config, ENV, form.folder, lock=False).options.row_strips is False
    assert fields_of(form)["row_strips"] == "false"


def test_every_field_the_page_saves_is_read_back():
    from departureboard.settings.schema import FIELDS, saved_keys

    for f in FIELDS:
        if f.group in ("general", "dashboard"):
            assert f.key in saved_keys()[f.section], f.name


def test_everything_setup_and_the_page_save_is_read_back(form):
    from departureboard.settings.config import read_overlay_in
    from departureboard.settings.schema import keys
    from departureboard.setup.api import SetupApi

    stations = (("Glossop", "GLO", 53.4445, -1.9491), ("Manchester Piccadilly", "MAN", 53.4774, -2.2309))
    store = Store.open(form.config, state_dir=form.folder)
    wizard = SetupApi(dashboard_target(store), lambda: store.env(ENV), form.folder, stations=lambda: stations,
                      make_job=FoundJob, keys=keys("general", "dashboard"))
    assert wizard.handle("start", {"stops": {"A": "1800SB12341"}, "routes": True})[0] == 200
    status, body = wizard.handle("finish", {
        "home": {"lat": 53.46, "lon": -1.96, "country": "Scotland"}, "station_crs": "GLO", "filter_crs": "MAN",
        "stops": {"A": "1800SB12341"}, "walk": {"GLO": 6, "1800SB12341": 3}})
    assert status == 200, body
    changed = {"bus_directions": "Ashton", "kb_incidents_url": "https://api1.raildata.org.uk/kb/incidents"}
    assert form.save(fields_of(form) | changed) == {}
    written = tomllib.loads((form.folder / "settings.toml").read_text())
    assert read_overlay_in(form.folder) == written
    assert written["bus"]["routes"] and written["weather"]["lat"] and written["dashboard"]["kb_incidents_url"]


def test_quiet_hours_are_rows_and_may_all_be_removed(form):
    fields = fields_of(form)
    assert (fields["quiet_windows-1-from"], fields["quiet_windows-1-to"]) == ("01:00", "05:00")
    assert all(fields[f"quiet_windows-1-{day}"] == "on" for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    page = form.render("tok")
    assert "<legend>Quiet hours 1</legend>" in page and "A wall tablet keeps it checking, and dims." in page
    emptied = {name: value for name, value in fields.items() if not name.startswith("quiet_windows-1-")}
    emptied |= {"quiet_windows-1-from": "", "quiet_windows-1-to": ""}
    assert form.save(emptied) == {}
    assert tomllib.loads((form.folder / "settings.toml").read_text())["dashboard"]["quiet_windows"] == []
    assert load_dashboard(form.config, ENV, form.folder, lock=False).options.quiet_windows == ()
