"""The dashboard's settings form, saving into its own overlay, without the web server."""

import re
import tomllib

import pytest

from departureboard.dashconfig import dashboard_env, load_dashboard, overlay_target
from departureboard.dashform import DashboardForm

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


@pytest.fixture
def form(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    folder = tmp_path / "dash"
    folder.mkdir()
    page = DashboardForm(overlay_target(config, folder), lambda: dashboard_env(ENV, folder))
    page.config, page.folder = config, folder
    return page


def fields_of(form):
    """What a browser would submit for the form as rendered."""
    page = form.render("tok")
    values = dict(re.findall(r'<input id="([a-z_]+)" name="[a-z_]+" type="text" value="([^"]*)"', page))
    values |= {name: text for name, text in re.findall(r'<textarea id="([a-z_]+)"[^>]*>([^<]*)</textarea>', page)}
    for name, options in re.findall(r'<select id="([a-z_]+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
    return values


def loaded(form):
    dash = load_dashboard(form.config, ENV, form.folder, lock=False)
    return dash.train is not None, dash.bus is not None, dash.weather is not None


def test_trains_and_buses_are_on_unless_switched_off(form):
    fields = fields_of(form)
    assert (fields["train_enabled"], fields["bus_enabled"], fields["weather_enabled"]) == ("true", "true", "true")
    assert loaded(form) == (True, True, True)


def test_trains_or_buses_can_be_switched_off_and_back_on(form):
    # Off, the group's other fields are kept as saved, unchecked, however they were left.
    assert form.save(fields_of(form) | {"bus_enabled": "false", "bus_stops": "", "bus_rows": "99"}) == {}
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


def test_a_section_off_with_the_templates_placeholders_saves(form):
    form.config.write_text(CONFIG.replace('[bus]\nsource = "bods"', '[bus]\nenabled = false\nsource = "transportapi"')
                           .replace('A = "1000DGHS0900"', 'A = "CHANGE_ME"'))
    assert fields_of(form)["bus_enabled"] == "false"
    assert form.save(fields_of(form)) == {}
    assert loaded(form) == (True, False, True)


def test_the_on_off_choice_is_labelled_and_described(form):
    page = form.render("tok")
    assert '<label for="train_enabled">Trains</label>' in page
    assert 'id="bus_enabled" name="bus_enabled" aria-describedby="bus_enabled-hint"' in page
