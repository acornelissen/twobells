"""The settings registry: fields, keys, what may be saved, and [app] views."""

import logging
from collections import Counter

import pytest

from twobells.board.displays.display import DISPLAY_PANELS
from twobells.board.themes import THEMES
from twobells.settings import schema
from twobells.settings.config import ConfigError
from twobells.settings.installs import INSTALLS, install_named


def every_form():
    for display in DISPLAY_PANELS:
        for theme in THEMES:
            yield schema.fields(display, theme)


def _look(display):
    return next(f for f in schema.fields(display, "sign") if f.name == "theme")


@pytest.mark.parametrize("display", [d for d, panel in DISPLAY_PANELS.items() if not panel.epaper])
def test_a_screen_offers_every_look_by_its_label(display):
    look = _look(display)
    assert look.options == tuple((name, theme.label) for name, theme in THEMES.items())
    assert [look.check(name) for name in THEMES] == list(THEMES)


@pytest.mark.parametrize("display", [d for d, panel in DISPLAY_PANELS.items() if panel.epaper])
@pytest.mark.parametrize("screen_only", ["tft", "flap"])
def test_e_paper_refuses_the_screen_only_looks(display, screen_only):
    look = _look(display)
    assert [name for name, _ in look.options] == ["sign", "plain"]
    with pytest.raises(schema.Invalid, match="for a screen"):
        look.check(screen_only)
    with pytest.raises(schema.Invalid, match="options"):
        look.check("neon")


def test_every_field_is_in_exactly_one_known_group():
    for form in every_form():
        for f in form:
            assert f.group in schema.GROUPS, f.name


def test_field_names_are_unique():
    for form in every_form():
        names = Counter(f.name for f in form)
        assert [name for name, count in names.items() if count > 1] == []


def test_each_setting_has_one_field_but_the_views_checkboxes():
    # [app] views is the one setting drawn as several fields: a checkbox per view.
    for form in every_form():
        places = Counter((f.section, f.key) for f in form if f.kind != "check")
        assert [place for place, count in places.items() if count > 1] == []
        assert {f.name for f in form if f.kind == "check"} == {f"view_{view}" for view in schema.VIEWS}


def test_the_rows_fields_have_names_of_their_own():
    names = {f.name: (f.section, f.key) for f in schema.FIELDS}
    assert names["board_train_rows"] == ("train", "rows")
    assert names["board_bus_rows"] == ("bus", "rows")
    assert names["dashboard_train_rows"] == ("dashboard", "train_rows")
    assert names["dashboard_bus_rows"] == ("dashboard", "bus_rows")


def test_a_field_s_needs_is_a_section_that_can_be_set_up():
    for f in schema.FIELDS:
        assert f.needs in (None, "train", "bus", "weather"), f.name
        if f.section in ("train", "bus", "weather"):
            assert f.needs == f.section, f.name


def test_every_field_is_on_one_settings_page_under_a_heading():
    for form in every_form():
        for f in form:
            assert f.page in schema.PAGES and f.page != "overview", f.name  # the overview only shows
            assert f.heading and f.tier in schema.TIERS, f.name
            assert f.width in ("", *schema.WIDTHS) and f.keyboard in ("", *schema.KEYBOARDS), f.name


def test_a_page_s_fields_are_for_its_view():
    # The board's and the dashboard's pages are there only while their view is on.
    for f in schema.FIELDS:
        if f.page in ("board", "dashboard"):
            assert f.group == f.page, f.name
        if f.page in ("trains_buses", "general"):
            assert f.group == "general", f.name


def test_short_choices_are_radios_and_long_ones_selects():
    for form in every_form():
        for f in form:
            if f.kind == "radios":
                assert 2 <= len(f.options) <= 4, f.name
            if f.kind == "select":
                assert len(f.options) > 4, f.name


def test_time_windows_are_rows_each_with_a_name():
    assert {f.name for f in schema.FIELDS if f.kind == "windows"} == {"train_windows", "bus_windows", "quiet_windows"}
    for f in schema.FIELDS:
        assert bool(f.item) == (f.kind == "windows"), f.name


def test_a_unit_is_for_a_number_in_a_narrow_box():
    for f in schema.FIELDS:
        if f.unit:
            assert f.kind in ("number", "per_stop") and f.width, f.name


@pytest.mark.parametrize("data, chosen", [
    ({}, "england-and-wales"),
    ({"bank_holiday_division": "scotland"}, "scotland"),
    ({"bank_holidays": True, "bank_holiday_division": "northern-ireland"}, "northern-ireland"),
    ({"bank_holidays": False, "bank_holiday_division": "scotland"}, "ignore"),
    ({"bank_holiday_division": "wales"}, "england-and-wales"),  # not one: saving puts it right
])
def test_bank_holidays_are_one_choice_from_the_two_settings(data, chosen):
    assert schema.holiday_choice(data) == chosen


@pytest.mark.parametrize("chosen, saved", [
    ("scotland", {"bank_holidays": True, "bank_holiday_division": "scotland"}),
    ("england-and-wales", {"bank_holidays": True, "bank_holiday_division": "england-and-wales"}),
    ("ignore", {"bank_holidays": False}),  # the part of the UK kept, for turning them back on
])
def test_the_bank_holidays_choice_saves_the_same_two_settings(chosen, saved):
    assert schema.holiday_settings(schema.holidays(chosen)) == saved
    assert {"bank_holidays", "bank_holiday_division"} <= schema.saved_keys()[None]


@pytest.mark.parametrize("raw", ["", "wales", "true", "false"])
def test_the_bank_holidays_choice_refuses_anything_else(raw):
    with pytest.raises(schema.Invalid):
        schema.holidays(raw)


def test_the_time_zone_suggests_every_zone_london_first():
    zone = {f.name: f for f in schema.FIELDS}["timezone"]
    names = [value for value, _ in zone.options]
    assert names[0] == "Europe/London" and "UTC" in names and "America/New_York" in names
    assert len(names) == len(set(names)) and all(schema.timezone(name) == name for name in names[:50])


def test_each_key_is_in_exactly_one_group():
    names = Counter(k.name for k in schema.KEYS)
    assert set(names.values()) == {1}
    assert {k.group for k in schema.KEYS} <= set(schema.GROUPS)
    assert [name for name, _, _ in schema.keys("general")] == ["RDM_API_KEY", "BODS_API_KEY"]
    assert [name for name, _, _ in schema.keys("board")] == ["TAPI_APP_ID", "TAPI_APP_KEY"]
    assert [name for name, _, _ in schema.keys("dashboard")] == ["RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"]


@pytest.mark.parametrize("views, display, details", [
    (("board",), "hdmi-1280x400", True),  # a screen's rows say where each train is
    (("board",), "hdmi-1920x440", True),
    (("board",), "inky", False),  # e-paper never asks
    (("board",), "waveshare-10in85g", False),
    (("dashboard",), "inky", True),
    (("board", "dashboard"), "hdmi-1280x400", True),
    (("dashboard",), "hdmi-1280x400", True),
    ((), "hdmi-1280x400", False),  # the board view off
])
def test_the_where_each_train_is_key_is_used_by_the_dashboard_and_a_screen_board(views, display, details):
    names = [name for name, _, _ in schema.keys_used(views, display)]
    assert ("RDM_DETAILS_API_KEY" in names) is details
    assert ("RDM_KB_API_KEY" in names) is ("dashboard" in views)  # engineering works: the dashboard's alone
    assert ("TAPI_APP_KEY" in names) is ("board" in views)
    assert names[:2] == ["RDM_API_KEY", "BODS_API_KEY"] and len(names) == len(set(names))


def test_saved_keys_cover_every_field_and_what_setup_manages():
    saved = schema.saved_keys()
    for form in every_form():
        for f in form:
            assert f.key in saved[f.section], f.name
    for table, names in schema.MANAGED.items():
        assert names <= saved[table], table


@pytest.mark.parametrize("table, key", [
    (None, "state_file"), (None, "display"), (None, "panel_colour"), (None, "rotate_180"),
    ("train", "base_url"), ("bus", "base_url"), ("bus", "fallback_windows"), ("bus", "fallback_interval_minutes"),
    ("bus", "position_interval_seconds"), ("dashboard", "state_dir"), ("dashboard", "allowed_hosts"),
])
def test_saved_keys_leave_out_what_only_config_toml_sets(table, key):
    assert key not in schema.saved_keys().get(table, frozenset())


@pytest.mark.parametrize("url, kept", [
    ("https://api1.raildata.org.uk/kb/incidents", True),
    ("https://raildata.org.uk/kb", True),
    ("https://set-kb-incidents-url-from-the-rdm-product-page.invalid/incidents", True),  # the placeholder
    ("https://api.example.test/kb", False),
    ("https://raildata.org.uk.example.test/kb", False),
    ("https://evilraildata.org.uk/kb", False),
    ("http://api1.raildata.org.uk/kb", False),
    ("https://me:pw@api1.raildata.org.uk/kb", False),
    ("https://api1.raildata.org.uk\\@example.test/kb", False),
    ("https://api1.raildata.org.uk:x/kb", False),
    (42, False),
])
def test_a_saved_knowledgebase_address_must_be_at_raildata(url, kept):
    assert (schema.refused("dashboard", "kb_incidents_url", url) is None) is kept


def test_other_saved_values_are_not_refused():
    assert schema.refused(None, "title", "Anything") is None
    assert schema.refused("train", "crs", "GLO") is None


@pytest.mark.parametrize("raw", ["https://api1.raildata.org.uk/kb/incidents", " https://raildata.org.uk/x "])
def test_the_knowledgebase_field_takes_a_raildata_address(raw):
    assert schema.https_feed(raw) == raw.strip()


def test_the_knowledgebase_field_left_empty_keeps_engineering_works_off():
    from twobells.sources.disruptions import kb_url_is_placeholder

    assert kb_url_is_placeholder(schema.https_feed("  "))


@pytest.mark.parametrize("raw", ["https://api.example.test/kb", "http://api1.raildata.org.uk/kb", "raildata.org.uk",
                                 "https://api1.raildata.org.uk/a b", "https://api1.raildata.org.uk/" + "x" * 300])
def test_the_knowledgebase_field_refuses_anywhere_else(raw):
    with pytest.raises(schema.Invalid, match=r"raildata\.org\.uk"):
        schema.https_feed(raw)


@pytest.mark.parametrize("raw", ["Europe/London", "UTC", "America/New_York", "Etc/GMT+5"])
def test_a_time_zone_is_checked_against_the_zone_database(raw):
    assert schema.timezone(f" {raw} ") == raw


@pytest.mark.parametrize("raw", ["", "Mars/Olympus", "Europe", "../etc/passwd", "Europe/Lon\ndon", "x" * 70])
def test_a_time_zone_that_is_not_one_is_refused(raw):
    with pytest.raises(schema.Invalid, match="time zone"):
        schema.timezone(raw)


@pytest.mark.parametrize(("data", "name"), [
    ({"timezone": "Asia/Kolkata"}, "Asia/Kolkata"),
    ({"timezone": " UTC "}, "UTC"),
    ({}, "Europe/London"),  # the board's default
    ({"timezone": "Mars/Olympus"}, "Europe/London"),
    ({"timezone": "../etc/passwd"}, "Europe/London"),
    ({"timezone": ""}, "Europe/London"),
    ({"timezone": 7}, "Europe/London"),
])
def test_the_zone_in_settings_is_the_default_when_they_name_none(data, name):
    assert schema.zone(data).key == name


# [app] views


PI, DOCKER = install_named("pi"), install_named("docker")


def test_every_install_offers_views_and_defaults_to_some_of_them():
    for install in INSTALLS.values():
        assert install.offered and set(install.offered) <= set(schema.VIEWS)
        assert install.default_views and set(install.default_views) <= set(install.offered)


@pytest.mark.parametrize("data", [{}, {"app": {}}, {"title": "x", "app": {"other": 1}}])
def test_without_views_an_install_has_its_default_ones(data):
    assert schema.views(data, PI) == ("board",)
    assert schema.views(data, DOCKER) == ("dashboard",)
    assert schema.views(data, default=("board",)) == ("board",)
    assert schema.views(data) == ()


def test_views_are_the_ones_named():
    data = {"app": {"views": ["dashboard", "board"]}}
    assert schema.views(data) == ("dashboard", "board")


def test_no_views_at_all_is_allowed():
    assert schema.views({"app": {"views": []}}, PI) == ()
    assert schema.views({"app": {"views": []}}, default=("board",)) == ()


def test_a_view_the_install_does_not_offer_is_left_off_and_said(caplog):
    caplog.set_level(logging.INFO)
    assert schema.views({"app": {"views": ["board", "dashboard"]}}, DOCKER) == ("dashboard",)
    assert "no board view" in caplog.text
    # The board renderer asks without an install: what is named is what is on.
    assert schema.views({"app": {"views": ["dashboard"]}}, default=("board",)) == ("dashboard",)


@pytest.mark.parametrize("data, words", [
    ({"app": "board"}, "[app] must be a table"),
    ({"app": {"views": "board"}}, "must be a list"),
    ({"app": {"views": [1]}}, "must be a list"),
    ({"app": {"views": ["board", "screen"]}}, "'screen' is not a view"),
    ({"app": {"views": ["board", "board"]}}, "more than once"),
])
def test_views_that_are_not_a_list_of_views_are_a_config_error(data, words):
    with pytest.raises(ConfigError, match=words.replace("[", r"\[").replace("]", r"\]")):
        schema.views(data, PI)


def test_the_board_s_loader_refuses_malformed_views_too(tmp_path):
    from twobells.settings.config import settings_from

    with pytest.raises(ConfigError, match="views"):
        settings_from({"state_file": str(tmp_path / "state.json"), "app": {"views": ["tv"]}}, {})


# The defaults agree with the loaders


ENV = {"RDM_API_KEY": "rail", "BODS_API_KEY": "bods", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "tapi"}
STOPS = {"A": "1000DGHS0900"}


def parsed(windows):
    from twobells.core.polling import parse_window

    return tuple(parse_window(w) for w in windows)


def test_the_board_loads_with_the_schema_s_defaults(tmp_path):
    from datetime import timedelta

    from twobells.settings.config import settings_from

    data = {"state_file": str(tmp_path / "state.json"), "train": {"crs": "GLO"}, "bus": {"source": "transportapi", "stops": STOPS}}
    settings = settings_from(data, ENV)
    top = schema.BOARD_DEFAULTS
    assert (settings.board.title, settings.timezone, settings.display, settings.rotate) == (
        top["title"], top["timezone"], top["display"], top["rotate"])
    assert settings.board.min_redraw == timedelta(minutes=top["min_redraw_minutes"])
    assert (settings.board.theme.name, settings.board.theme.text_size) == (top["theme"], top["text_size"])
    assert settings.board.clock_page is top["clock_page"] is False
    train, bus = settings.board.sources
    for config, defaults in ((train, top["train"]), (bus, top["bus"])):
        assert config.source._rows == defaults["rows"]
        assert config.policy.interval == timedelta(minutes=defaults["interval_minutes"])
        assert config.policy.windows == parsed(defaults["windows"])
        assert config.stale_after == timedelta(minutes=defaults["stale_after_minutes"])
    assert bus.daily_budget == top["bus"]["daily_budget"]


def test_buses_from_bods_load_with_the_schema_s_defaults(tmp_path):
    from datetime import timedelta

    from twobells.settings.config import settings_from

    data = {"state_file": str(tmp_path / "state.json"),
            "bus": {"stops": STOPS, "source": "bods", "operators": ["BNML"]}}
    (bus,) = settings_from(data, ENV).board.sources
    defaults = schema.bus_defaults("bods")
    assert bus.source._rows == defaults["rows"]
    assert bus.policy.interval == timedelta(minutes=defaults["interval_minutes"])
    assert bus.policy.windows == parsed(schema.BODS_DEFAULTS["windows"])
    # TransportAPI, standing in, keeps to its own pace and windows, so its allowance lasts.
    assert bus.fallback_policy.interval == timedelta(minutes=defaults["fallback_interval_minutes"])
    assert bus.fallback_policy.windows == parsed(schema.BOARD_DEFAULTS["bus"]["windows"])


def test_the_board_s_fields_show_the_loader_s_defaults():
    board = schema.BOARD_DEFAULTS
    for f in schema.FIELDS:
        if f.group != "board" or f.name == "accent":  # the accent's default is the look's own
            continue
        defaults = board if f.section is None else board[f.section]
        assert f.default == defaults[f.key], f.name


def test_the_dashboard_s_fields_show_dashboard_options_defaults():
    from twobells.dashboard.config import DashboardOptions
    from twobells.sources.disruptions import kb_url_is_placeholder

    options = DashboardOptions()
    for f in schema.FIELDS:
        if f.group != "dashboard":
            continue
        loaded = getattr(options, f.key)
        if f.key == "quiet_windows":
            assert parsed(f.default) == loaded
        elif f.key == "kb_incidents_url":
            assert f.default == "" and kb_url_is_placeholder(loaded)  # shown empty: engineering works are off
        else:
            assert f.default == loaded, f.name


def test_with_only_general_settings_saved_each_view_loads_with_its_defaults(tmp_path):
    # What setup saves: the station and stops, walk times and where home is; nothing of either view's own.
    from twobells.dashboard.config import DashboardOptions, load_dashboard
    from twobells.settings.config import load_config
    from twobells.settings.store import Store

    folder = tmp_path / "store"
    folder.mkdir()
    (folder / "settings.toml").write_text(
        'title = "Home"\nbank_holiday_division = "scotland"\n[app]\nviews = ["board", "dashboard"]\n'
        '[train]\ncrs = "GLO"\nwalk_minutes = 6\n'
        '[bus]\nsource = "bods"\noperators = ["BNML"]\n[bus.stops]\nA = "1000DGHS0900"\n'
        '[weather]\nlat = 53.44\nlon = -1.95\n')
    store = Store.open(tmp_path / "config.toml", state_dir=folder, environ={})
    assert not store.config_path.exists()
    general = {(f.section, key) for f in schema.FIELDS if f.group == "general" for key in (f.key, *f.also)}
    general |= {(table, key) for table, keys in schema.MANAGED.items() for key in keys}
    saved = store.saved()
    assert {(t, k) for t, table in saved.items() if isinstance(table, dict) for k in table} <= general
    assert {(None, k) for k, value in saved.items() if not isinstance(value, dict)} <= general

    board = load_config(store, ENV)
    train, bus = board.board.sources
    assert train.policy.windows == parsed(schema.BOARD_DEFAULTS["train"]["windows"])
    assert bus.policy.windows == parsed(schema.BODS_DEFAULTS["windows"])
    assert board.board.weather is not None and board.board.title == "Home"

    dash = load_dashboard(store, ENV, lock=False)
    assert dash.train is not None and dash.bus is not None and dash.weather is not None
    assert dash.options == DashboardOptions() and dash.title == "Home"
