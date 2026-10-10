"""One form for any groups of settings: what the board's and the dashboard's
pages share (their own tests are in test_settingsform and test_dashform)."""

import re
import stat
import tomllib

import pytest

from departureboard.config import load_config
from departureboard.dashconfig import load_dashboard
from departureboard.dashform import DashboardForm
from departureboard.groupform import GroupForm
from departureboard.installs import Install
from departureboard.settingsform import SettingsForm
from departureboard.store import Store

ENV = {"RDM_API_KEY": "rail-key-1234567", "BODS_API_KEY": "bods-key-7654321"}

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
# An install that offers both views, as a Pi will.
BOTH = Install("both", ("board", "dashboard"), ("board",), None)


@pytest.fixture
def store(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    folder = tmp_path / "store"
    folder.mkdir()
    return Store.open(config, state_dir=folder, environ={})


def saved(store):
    return tomllib.loads(store.settings_file.read_text())


def form_for(store, groups, install=BOTH, env=ENV):
    return GroupForm(store, groups, "/settings", lambda: dict(env), install=install, noun="board")


def fields_of(page_or_form):
    """What a browser would submit for the form as rendered: unticked boxes aren't sent."""
    page = page_or_form if isinstance(page_or_form, str) else page_or_form.render("tok")
    values = dict(re.findall(r'<input id="(\w+)" name="\w+" type="text" value="([^"]*)"', page))
    values |= {name: text for name, text in re.findall(r'<textarea id="(\w+)"[^>]*>([^<]*)</textarea>', page)}
    for name, options in re.findall(r'<select id="(\w+)"[^>]*>(.*?)</select>', page):
        values[name] = re.search(r'<option value="([^"]*)" selected', options).group(1)
    values |= dict.fromkeys(re.findall(r'<input id="(\w+)" name="\w+" type="checkbox" value="on" checked', page), "on")
    return values


# A save of one group leaves the other's alone


def test_saving_the_board_s_settings_keeps_the_dashboard_s(store):
    store.make()
    store.settings_file.write_text('[dashboard]\ntrain_rows = 4\nhistory_days = 30\n')
    form = SettingsForm(store, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"theme": "plain", "accent": ""}) == {}
    written = saved(store)
    assert written["dashboard"] == {"train_rows": 4, "history_days": 30}
    assert written["theme"] == "plain"


def test_saving_the_dashboard_s_settings_keeps_the_board_s(store):
    store.make()
    store.settings_file.write_text('theme = "plain"\ntext_size = 14\n[train]\nrows = 2\nwindows = ["Mon-Fri 07:00-08:00"]\n')
    form = DashboardForm(store, lambda: dict(ENV))
    assert form.save(fields_of(form) | {"dashboard_train_rows": "12"}) == {}
    written = saved(store)
    assert (written["theme"], written["text_size"]) == ("plain", 14)
    assert written["train"]["rows"] == 2 and written["train"]["windows"] == ["Mon-Fri 07:00-08:00"]
    assert written["dashboard"]["train_rows"] == 12


def test_a_form_shows_only_its_own_groups(store):
    board = SettingsForm(store, lambda: dict(ENV)).render("tok")
    dashboard = DashboardForm(store, lambda: dict(ENV)).render("tok")
    assert 'id="board_train_rows"' in board and 'id="dashboard_train_rows"' not in board
    assert 'id="dashboard_train_rows"' in dashboard and 'id="board_train_rows"' not in dashboard
    assert 'id="TAPI_APP_ID"' in board and 'id="TAPI_APP_ID"' not in dashboard
    assert 'id="RDM_KB_API_KEY"' in dashboard and 'id="RDM_KB_API_KEY"' not in board
    for page in (board, dashboard):
        assert 'id="title"' in page and 'id="timezone"' in page and 'id="train_crs"' in page


# The views


def test_the_views_are_checkboxes_in_a_fieldset_of_their_own(store):
    page = form_for(store, ("general", "board")).render("tok")
    views = page[page.index("<legend>Views</legend>"):]
    views = views[:views.index("</fieldset>")]
    for name, label in (("view_board", "The board"), ("view_dashboard", "The dashboard")):
        assert re.search(rf'<input id="{name}" name="{name}" type="checkbox" value="on"', views)
        assert f'<label for="{name}">{label}</label>' in views
    # On by the install's default until saved.
    assert fields_of(page).get("view_board") == "on" and "view_dashboard" not in fields_of(page)


def test_the_views_ticked_are_saved_and_shown(store):
    form = form_for(store, ("general", "dashboard"))
    assert form.save(fields_of(form) | {"view_dashboard": "on"}) == {}
    assert saved(store)["app"] == {"views": ["board", "dashboard"]}
    shown = fields_of(form)
    assert (shown.get("view_board"), shown.get("view_dashboard")) == ("on", "on")
    unticked = {name: value for name, value in fields_of(form).items() if name != "view_board"}
    assert form.save(unticked) == {}
    assert saved(store)["app"] == {"views": ["dashboard"]}
    assert "view_board" not in fields_of(form)


def test_at_least_one_view_must_be_on(store):
    form = form_for(store, ("general", "board"))
    fields = {name: value for name, value in fields_of(form).items() if not name.startswith("view_")}
    errors = form.save(fields)
    assert set(errors) == {"view_board"} and "at least one view" in errors["view_board"]
    assert not store.settings_file.exists()
    page = form.render("tok", errors=errors, values=fields)
    assert '<a href="#view_board">Views – The board: Pick at least one view' in page
    assert re.search(r'id="view_board"[^>]*aria-describedby="view_board-hint view_board-error"[^>]*aria-invalid="true"',
                     page)


def test_a_view_box_sending_anything_but_on_is_refused(store):
    form = form_for(store, ("general", "board"))
    errors = form.save(fields_of(form) | {"view_dashboard": "yes"})
    assert set(errors) == {"view_dashboard"}


def test_with_one_view_offered_there_is_no_choice_and_none_is_saved(store):
    for form in (SettingsForm(store, lambda: dict(ENV)), DashboardForm(store, lambda: dict(ENV))):
        page = form.render("tok")
        assert "<legend>Views</legend>" not in page and 'type="checkbox"' not in page
        assert form.save(fields_of(page)) == {}
        assert "app" not in saved(store)


# A save must leave every view that is on able to load


def test_a_save_that_would_break_the_other_view_is_refused(store):
    # Buses from TransportAPI: the dashboard shows none and loads, but the board needs TransportAPI's keys.
    store.config_path.write_text(CONFIG.replace('source = "bods"', 'source = "transportapi"'))
    store.make()
    store.settings_file.write_text('[app]\nviews = ["board", "dashboard"]\n')
    dashboard = form_for(store, ("general", "dashboard"))
    errors = dashboard.save(fields_of(dashboard) | {"title": "Home"})
    assert set(errors) == {"_form"} and "TAPI_APP_ID" in errors["_form"]
    assert saved(store) == {"app": {"views": ["board", "dashboard"]}}  # not saved
    # With the board off, the same change saves: only the dashboard has to load.
    assert dashboard.save(fields_of(dashboard) | {"title": "Home", "view_board": ""}) == {}
    assert saved(store)["title"] == "Home" and saved(store)["app"] == {"views": ["dashboard"]}


def test_turning_a_view_on_checks_that_it_loads(store):
    store.make()
    store.settings_file.write_text('[app]\nviews = ["board"]\n')
    form = form_for(store, ("general", "board"), env={"RDM_API_KEY": "rail-key-1234567", "BODS_API_KEY": "bods"})
    assert form.save(fields_of(form) | {"view_dashboard": "on", "kb_incidents_url": "x"}) == {}  # not this form's
    store.settings_file.write_text('[app]\nviews = ["board"]\n')
    form = form_for(store, ("general", "board"), env={**ENV, "RDM_DETAILS_API_KEY": "bad key"})
    errors = form.save(fields_of(form) | {"view_dashboard": "on"})
    assert "RDM_DETAILS_API_KEY" in errors["_form"]
    assert saved(store) == {"app": {"views": ["board"]}}


def test_the_dashboard_s_form_says_when_its_buses_can_t_be_shown(store):
    store.config_path.write_text(CONFIG.replace('source = "bods"', 'source = "transportapi"'))
    page = DashboardForm(store, lambda: dict(ENV)).render("tok")
    buses = page[page.index("<legend>Buses</legend>"):]
    assert "only from the free BODS timetable" in buses[:buses.index("</fieldset>")]
    assert "only from the free BODS timetable" not in SettingsForm(store, lambda: dict(ENV)).render("tok")


# Every setting a field shows round-trips


def test_text_sizes_from_9_to_16_are_offered_and_kept(store):
    form = SettingsForm(store, lambda: dict(ENV))
    page = form.render("tok")
    select = re.search(r'<select id="text_size"[^>]*>(.*?)</select>', page).group(1)
    assert re.findall(r'value="(\d+)"', select) == [str(size) for size in range(9, 17)]
    for size in (9, 16):
        store.config_path.write_text(f'theme = "plain"\ntext_size = {size}\n' + CONFIG)  # the plain look's
        store.settings_file.unlink(missing_ok=True)
        assert fields_of(form)["text_size"] == str(size)
        assert form.save(fields_of(form)) == {}
        assert saved(store)["text_size"] == size
        assert load_config(store, ENV).board.theme.text_size == size


def test_a_time_zone_is_saved_and_used(store):
    form = DashboardForm(store, lambda: dict(ENV))
    assert fields_of(form)["timezone"] == "Europe/London"  # the default, shown
    assert set(form.save(fields_of(form) | {"timezone": "Mars/Olympus"})) == {"timezone"}
    assert form.save(fields_of(form) | {"timezone": "Europe/Dublin"}) == {}
    assert load_dashboard(store, ENV, lock=False).timezone == "Europe/Dublin"
    assert load_config(store, ENV).timezone == "Europe/Dublin"


def test_defaults_are_shown_where_the_settings_say_nothing(store):
    board = fields_of(SettingsForm(store, lambda: dict(ENV)))
    assert board["train_windows"] == "Mon-Fri 06:30-09:30\nMon-Fri 16:30-19:30"
    assert (board["board_bus_rows"], board["bus_interval_minutes"]) == ("3", "2")  # bods: checked often
    assert board["bus_windows"] == "Mon-Sun 06:00-23:00"
    dashboard = fields_of(DashboardForm(store, lambda: dict(ENV)))
    assert (dashboard["dashboard_train_rows"], dashboard["quiet_windows"]) == ("10", "Mon-Sun 01:00-05:00")


def test_the_knowledgebase_address_must_be_at_raildata(store):
    form = DashboardForm(store, lambda: dict(ENV))
    errors = form.save(fields_of(form) | {"kb_incidents_url": "https://api.example.test/kb"})
    assert set(errors) == {"kb_incidents_url"} and "raildata.org.uk" in errors["kb_incidents_url"]
    url = "https://api1.raildata.org.uk/1010-knowledgebase-incidents/incidents"
    assert form.save(fields_of(form) | {"kb_incidents_url": url}) == {}
    assert saved(store)["dashboard"]["kb_incidents_url"] == url
    assert fields_of(form)["kb_incidents_url"] == url


# A broken settings.toml


@pytest.mark.parametrize("make", [
    lambda store: SettingsForm(store, lambda: dict(ENV)),
    lambda store: DashboardForm(store, lambda: dict(ENV)),
])
def test_a_broken_settings_file_is_replaced_and_kept_aside(store, make):
    store.make()
    store.settings_file.write_bytes(b"title = \n\xff")
    form = make(store)
    page = form.render("tok")
    assert "saved settings file is broken" in page and "settings.toml.broken" in page
    assert form.save(fields_of(page) | {"title": "Fixed"}) == {}
    assert saved(store)["title"] == "Fixed"
    kept = store.folder / "settings.toml.broken"
    assert kept.read_bytes() == b"title = \n\xff"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert "broken" not in form.render("tok")


def test_a_broken_file_is_not_replaced_when_the_save_is_refused(store):
    store.make()
    store.settings_file.write_text("title = \n")
    form = SettingsForm(store, lambda: {})  # no keys: the board won't load
    assert "_form" in form.save(fields_of(form))
    assert store.settings_file.read_text() == "title = \n"
    assert not (store.folder / "settings.toml.broken").exists()


def test_a_config_toml_that_can_t_be_read_is_said_and_nothing_saved(store):
    store.config_path.write_text("this is [not toml")
    form = DashboardForm(store, lambda: dict(ENV))
    page = form.render("tok")
    assert "The settings can't be read" in page and "config.toml" in page
    assert "_form" in form.save({"title": "Home"})
    assert not store.settings_file.exists()


def test_every_control_has_a_label_and_every_group_a_legend(store):
    page = form_for(store, ("general", "board", "dashboard")).render("tok")
    ids = set(re.findall(r'<(?:input|select|textarea) id="(\w+)"', page))
    assert ids and ids <= set(re.findall(r'<label for="(\w+)"', page))
    assert page.count("<fieldset>") == page.count("<legend>") == page.count("</fieldset>")


def test_unknown_groups_are_refused(store):
    with pytest.raises(ValueError, match="screen"):
        form_for(store, ("general", "screen"))
