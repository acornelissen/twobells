import stat
from pathlib import Path

import pytest

from departureboard.config import ConfigError
from departureboard.dashconfig import (
    DashboardOptions,
    FileWatch,
    dashboard_options,
    default_state_dir,
    load_dashboard,
    overlay_target,
    settings_files,
)
from departureboard.overlay import OverlayError, save_to
from departureboard.darwin import DETAILS_BASE_URL, DarwinSource, ServiceDetailsClient
from departureboard.disruptions import KB_URL_PLACEHOLDER, BusDisruptionSource, TrainIncidentSource
from departureboard.bodsbus import BodsBusSource
from departureboard.weather import OpenMeteoSource

ENV = {"RDM_API_KEY": "rail-key", "BODS_API_KEY": "bods-key", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "tapi-key"}

CONFIG = """
title = "Glossop"
state_file = "{board}/state.json"
[train]
crs = "GLO"
filter_crs = "man"
rows = 3
walk_minutes = 12
[bus]
source = "bods"
operators = ["BNML"]
rows = 2
walk_minutes = {{ A = 5 }}
[bus.stops]
A = "1000DGHS0900"
[weather]
lat = 53.44
lon = -1.95
threshold_percent = 60
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(board=tmp_path / "board"))
    return path


@pytest.fixture
def opened():
    loaded = []
    yield loaded
    for dash in loaded:
        dash.close()


def load(config, folder, env=ENV, opened=None):
    dash = load_dashboard(config, env, folder)
    if opened is not None:
        opened.append(dash)
    return dash


# The [dashboard] table


def test_defaults_without_a_dashboard_table():
    options = dashboard_options({})
    assert options == DashboardOptions()
    assert (options.train_rows, options.bus_rows) == (10, 10)
    assert (options.train_interval_seconds, options.bus_interval_seconds, options.weather_interval_minutes) == (
        60, 30, 30,
    )
    assert (options.late_minutes_train, options.late_minutes_bus, options.history_days) == (3, 2, 90)
    assert options.allowed_hosts == frozenset()
    [window] = options.quiet_windows
    assert window.days == frozenset(range(7))
    assert (str(window.start), str(window.end)) == ("01:00:00", "05:00:00")


def test_reads_every_setting():
    options = dashboard_options({"dashboard": {
        "state_dir": "/data", "train_rows": 20, "bus_rows": 1, "train_interval_seconds": 30,
        "bus_interval_seconds": 15, "weather_interval_minutes": 10, "quiet_windows": ["Mon-Fri 00:30-05:30"],
        "late_minutes_train": 5, "late_minutes_bus": 1, "history_days": 30,
        "allowed_hosts": ["Departures.Example.com", "nas"],
    }})
    assert options.state_dir == "/data"
    assert (options.train_rows, options.bus_rows) == (20, 1)
    assert options.quiet_windows[0].days == frozenset(range(5))
    assert options.allowed_hosts == frozenset({"departures.example.com", "nas"})


@pytest.mark.parametrize("key, value, words", [
    ("train_rows", 0, "train_rows must be a whole number from 1 to 20"),
    ("bus_rows", 21, "bus_rows must be a whole number from 1 to 20"),
    ("train_rows", True, "train_rows"),
    ("train_interval_seconds", 29, "train_interval_seconds must be a whole number from 30"),
    ("bus_interval_seconds", 14, "bus_interval_seconds must be a whole number from 15"),
    ("weather_interval_minutes", 9, "weather_interval_minutes must be a whole number from 10"),
    ("weather_interval_minutes", "30", "weather_interval_minutes"),
    ("quiet_windows", "Mon-Sun 01:00-05:00", "quiet_windows must be a list of text"),
    ("quiet_windows", ["nightly"], "Unrecognised window"),
    ("late_minutes_train", 0, "late_minutes_train"),
    ("late_minutes_bus", 61, "late_minutes_bus"),
    ("history_days", 0, "history_days"),
    ("state_dir", "", "state_dir must be a folder path"),
    ("state_dir", 7, "state_dir must be a folder path"),
    ("allowed_hosts", "nas", "allowed_hosts must be a list of text"),
    ("allowed_hosts", ["*.example.com"], "not a host name"),
    ("allowed_hosts", ["nas:8090"], "not a host name"),
    ("allowed_hosts", ["http://nas"], "not a host name"),
    ("allowed_hosts", ["nas..local"], "not a host name"),
    ("allowed_hosts", ["-nas"], "not a host name"),
    ("allowed_hosts", [""], "not a host name"),
    ("train_details", "auto", "unknown settings: train_details"),
])
def test_bad_settings_are_named(key, value, words):
    with pytest.raises(ConfigError, match="dashboard") as error:
        dashboard_options({"dashboard": {key: value}})
    assert words in str(error.value)


def test_dashboard_must_be_a_table():
    with pytest.raises(ConfigError, match=r"\[dashboard\] must be a table"):
        dashboard_options({"dashboard": 3})


def test_default_state_folder_on_a_mac_and_elsewhere(tmp_path):
    home = Path("/home/me")
    mac = default_state_dir("darwin", {}, home)
    assert mac == home / "Library" / "Application Support" / "departureboard-dashboard"
    assert default_state_dir("linux", {}, home) == home / ".local" / "state" / "departureboard-dashboard"
    xdg = default_state_dir("linux", {"XDG_STATE_HOME": "/var/state"}, home)
    assert xdg == Path("/var/state/departureboard-dashboard")
    # A relative XDG_STATE_HOME is ignored, as the spec says.
    assert default_state_dir("linux", {"XDG_STATE_HOME": "state"}, home) == home / ".local/state/departureboard-dashboard"


# Loading


def test_loads_the_free_sources_with_the_dashboard_s_rows(config, tmp_path, opened):
    folder = tmp_path / "dash"
    dash = load(config, folder, opened=opened)

    assert dash.title == "Glossop"
    assert dash.state_dir == folder
    assert isinstance(dash.train, DarwinSource)
    assert dash.train.with_details
    assert "GetDepBoardWithDetails/GLO?numRows=10&filterCrs=man" in dash.train._url()
    assert (dash.train_crs, dash.train_filter) == ("GLO", "MAN")
    assert isinstance(dash.bus, BodsBusSource)
    assert dash.bus._rows == 10
    assert dash.bus_stops == {"A": "1000DGHS0900"}
    assert isinstance(dash.weather, OpenMeteoSource)
    assert dash.rain_threshold == 60
    assert dash.walk.train == 12 and dash.walk.bus == {"A": 5}
    assert dash.bank_holiday_division == "england-and-wales"


def test_follows_the_board_s_bank_holiday_division(tmp_path, opened):
    path = tmp_path / "config.toml"
    path.write_text('bank_holiday_division = "northern-ireland"\n' + CONFIG.format(board=tmp_path / "board"))
    assert load(path, tmp_path / "dash", opened=opened).bank_holiday_division == "northern-ireland"


def test_the_bus_timetable_lives_in_the_dashboard_s_folder(config, tmp_path, opened):
    folder = tmp_path / "dash"
    dash = load(config, folder, opened=opened)
    assert dash.bus._folder == folder
    assert dash.bus_upkeep is not dash.bus and dash.bus_upkeep._folder == folder


def test_transportapi_keys_are_dropped_so_there_is_no_fallback(config, tmp_path, opened):
    dash = load(config, tmp_path / "dash", opened=opened)
    assert dash.bus._fallback is None
    assert dash.bus_upkeep._fallback is None


def test_the_bus_must_come_from_bods(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[bus]\nrows = 2\n[bus.stops]\nA = "1000DGHS0900"\n')
    with pytest.raises(ConfigError, match='source = "bods"'):
        load_dashboard(path, ENV, tmp_path / "dash")


def test_sections_left_out_are_none(tmp_path, opened):
    path = tmp_path / "config.toml"
    path.write_text('title = "Weather only"\n[weather]\nlat = 53.4\nlon = -1.9\n')
    dash = load(path, tmp_path / "dash", env={}, opened=opened)
    assert dash.train is None and dash.bus is None and dash.bus_upkeep is None
    assert dash.weather is not None


def test_missing_keys_are_a_config_error(config, tmp_path):
    with pytest.raises(ConfigError, match="RDM_API_KEY"):
        load_dashboard(config, {"BODS_API_KEY": "k"}, tmp_path / "dash")


def test_state_dir_comes_from_the_table_unless_overridden(tmp_path, opened):
    path = tmp_path / "config.toml"
    path.write_text(f'[dashboard]\nstate_dir = "{tmp_path / "from-config"}"\n')
    assert load(path, None, env={}, opened=opened).state_dir == tmp_path / "from-config"
    opened.pop().close()
    assert load(path, tmp_path / "flag", env={}, opened=opened).state_dir == tmp_path / "flag"


def test_creates_the_folder_for_its_owner_only(config, tmp_path, opened):
    folder = tmp_path / "new" / "dash"
    load(config, folder, opened=opened)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_refuses_the_board_s_state_folder(config, tmp_path):
    with pytest.raises(ConfigError, match="board's state folder"):
        load_dashboard(config, ENV, tmp_path / "board")


def test_refuses_any_folder_holding_a_board_state_file(config, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "state.json").write_text("{}")
    with pytest.raises(ConfigError, match="state.json"):
        load_dashboard(config, ENV, other)


def test_refuses_a_file_as_the_folder(config, tmp_path):
    (tmp_path / "file").write_text("")
    with pytest.raises(ConfigError, match="not a folder"):
        load_dashboard(config, ENV, tmp_path / "file")


def test_one_dashboard_per_folder(config, tmp_path, opened):
    folder = tmp_path / "dash"
    first = load(config, folder, opened=opened)
    with pytest.raises(ConfigError, match="Another dashboard"):
        load_dashboard(config, ENV, folder)
    first.close()
    load(config, folder, opened=opened)  # free again once the first lets go


def test_a_relative_board_state_file_says_nothing_about_this_machine(tmp_path, monkeypatch, opened):
    # A config copied from a laptop has state_file = "state.json": relative to
    # wherever that board runs. In a container the working directory is the
    # dashboard's own /data, which must not then be mistaken for the board's.
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(board="x").replace('state_file = "x/state.json"', 'state_file = "state.json"'))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.chdir(data)
    dash = load(path, data, opened=opened)
    assert dash.state_dir == data


# Service details, for the trains' positions


def test_no_details_client_without_its_key(config, tmp_path, opened):
    assert load(config, tmp_path / "dash", opened=opened).train_details is None
    blank = load(config, tmp_path / "dash2", env={**ENV, "RDM_DETAILS_API_KEY": "  "}, opened=opened)
    assert blank.train_details is None


def test_a_details_key_builds_the_client(config, tmp_path, opened):
    dash = load(config, tmp_path / "dash", env={**ENV, "RDM_DETAILS_API_KEY": " details-key "}, opened=opened)
    assert isinstance(dash.train_details, ServiceDetailsClient)
    assert dash.train_details._api_key == "details-key"
    assert dash.train_details._base_url == DETAILS_BASE_URL


def test_no_details_client_without_trains(tmp_path, opened):
    path = tmp_path / "config.toml"
    path.write_text('title = "Weather only"\n[weather]\nlat = 53.4\nlon = -1.9\n')
    dash = load(path, tmp_path / "dash", env={"RDM_DETAILS_API_KEY": "details-key"}, opened=opened)
    assert dash.train_details is None


@pytest.mark.parametrize("key", ["abc\r\nX-Evil: 1", "abc def", "abc\x00def", "abc def"])
def test_a_details_key_that_could_split_a_header_is_refused_without_repeating_it(config, tmp_path, key):
    with pytest.raises(ConfigError, match="RDM_DETAILS_API_KEY") as raised:
        load_dashboard(config, {**ENV, "RDM_DETAILS_API_KEY": key}, tmp_path / "dash")
    assert "abc" not in str(raised.value)
    assert not (tmp_path / "dash" / "dashboard.lock").exists()


# Disruptions


def test_bus_disruptions_use_the_buses_key(config, tmp_path, opened):
    dash = load(config, tmp_path / "dash", opened=opened)
    assert isinstance(dash.bus_disruptions, BusDisruptionSource)
    assert dash.bus_disruptions._api_key == "bods-key"


def test_without_the_knowledgebase_key_nothing_changes(config, tmp_path, opened):
    dash = load(config, tmp_path / "dash", opened=opened)
    assert dash.train_incidents is None and dash.train_incidents_note is None
    assert dashboard_options({}).kb_incidents_url == KB_URL_PLACEHOLDER


def test_the_knowledgebase_key_without_its_address_says_so(config, tmp_path, opened):
    dash = load(config, tmp_path / "dash", env={**ENV, "RDM_KB_API_KEY": "kb-key"}, opened=opened)
    assert dash.train_incidents is None
    assert "kb_incidents_url" in dash.train_incidents_note


def test_the_knowledgebase_key_and_address_build_the_source(config, tmp_path, opened):
    config.write_text(config.read_text() + '[dashboard]\nkb_incidents_url = "https://api.example.test/kb/incidents"\n')
    dash = load(config, tmp_path / "dash", env={**ENV, "RDM_KB_API_KEY": " kb-key "}, opened=opened)
    assert isinstance(dash.train_incidents, TrainIncidentSource)
    assert (dash.train_incidents._api_key, dash.train_incidents._url) == (
        "kb-key", "https://api.example.test/kb/incidents",
    )
    assert dash.train_incidents_note is None


@pytest.mark.parametrize("url", ["http://api.example.test/kb", "https://user:pw@api.example.test/kb", "ftp://x",
                                 "https://", "https://api.example.test/a b", 42, "https://api.example.test:port/kb"])
def test_the_knowledgebase_address_must_be_plain_https(url):
    with pytest.raises(ConfigError, match="kb_incidents_url"):
        dashboard_options({"dashboard": {"kb_incidents_url": url}})


def test_a_knowledgebase_key_that_could_split_a_header_is_refused(config, tmp_path):
    with pytest.raises(ConfigError, match="RDM_KB_API_KEY") as raised:
        load_dashboard(config, {**ENV, "RDM_KB_API_KEY": "abc\r\nX-Evil: 1"}, tmp_path / "dash")
    assert "abc" not in str(raised.value)


# Settings saved from the dashboard's own settings page


def test_settings_saved_in_the_dashboard_s_folder_are_layered_over_config_toml(config, tmp_path, opened):
    folder = tmp_path / "dash"
    folder.mkdir()
    (folder / "settings.toml").write_text(
        'title = "Our house"\n[train]\ncrs = "MAN"\n[dashboard]\ntrain_rows = 4\n'
    )
    (folder / "secrets.env").write_text("BODS_API_KEY=saved-bods-key\nRDM_DETAILS_API_KEY=saved-details\n")
    dash = load(config, folder, opened=opened)
    assert dash.title == "Our house" and dash.train_crs == "MAN"
    assert dash.train_filter == "MAN"  # the rest of [train] is still config.toml's
    assert dash.options.train_rows == 4 and dash.options.bus_rows == 10
    assert dash.bus._api_key == "saved-bods-key"  # saved keys win over the environment
    assert isinstance(dash.train_details, ServiceDetailsClient)
    assert not (tmp_path / "board").exists()  # nothing written beside config.toml or the board


def test_saved_settings_cannot_move_the_folder_they_live_in(config, tmp_path, opened):
    folder = tmp_path / "dash"
    folder.mkdir()
    (folder / "settings.toml").write_text(
        f'state_file = "{tmp_path}/elsewhere/state.json"\n[dashboard]\nstate_dir = "{tmp_path}/elsewhere"\n'
    )
    dash = load(config, folder, opened=opened)
    assert dash.state_dir == folder and dash.bus._folder == folder


def test_a_broken_saved_settings_file_is_a_config_error_naming_it(config, tmp_path):
    folder = tmp_path / "dash"
    folder.mkdir()
    (folder / "settings.toml").write_text("this is [not toml")
    with pytest.raises(ConfigError, match="settings.toml"):
        load_dashboard(config, ENV, folder)


def test_checking_settings_takes_no_lock_and_makes_no_folder(config, tmp_path, opened):
    folder = tmp_path / "dash"
    load_dashboard(config, ENV, folder, lock=False)
    assert not folder.exists()
    running = load(config, folder, opened=opened)
    assert load_dashboard(config, ENV, folder, lock=False).lock is None  # beside a running one
    assert running.lock is not None


def test_the_overlay_target_saves_only_what_the_dashboard_loads(config, tmp_path):
    folder = tmp_path / "dash"
    folder.mkdir()
    target = overlay_target(config, folder)
    save_to(target, {"dashboard": {"bus_rows": 3}, "train": {"crs": "MAN"}}, {"BODS_API_KEY": "new-key"}, ENV)
    assert (folder / "settings.toml").exists() and "new-key" in (folder / "secrets.env").read_text()
    assert not (tmp_path / "board").exists()
    with pytest.raises(OverlayError, match='source = "bods"'):
        save_to(target, {"bus": {"source": "transportapi"}}, {}, ENV)
    with pytest.raises(OverlayError, match="train_rows"):
        save_to(target, {"dashboard": {"train_rows": 50}}, {}, ENV)
    assert target.current()["dashboard"] == {"bus_rows": 3}


def test_the_files_to_watch_include_the_board_s_overlay_and_the_dashboard_s(config, tmp_path):
    folder = tmp_path / "dash"
    files = settings_files(config, tmp_path / "secrets.env", folder)
    assert files[:4] == [config, tmp_path / "secrets.env", folder / "settings.toml", folder / "secrets.env"]
    assert tmp_path / "board" / "settings.toml" in files


def test_a_file_watch_notices_changes_additions_and_removals(tmp_path):
    path = tmp_path / "settings.toml"
    watch = FileWatch([path])
    assert not watch.changed()
    path.write_text("a = 1\n")
    assert watch.changed() and not watch.changed()
    replacement = tmp_path / "new.toml"
    replacement.write_text("a = 2\n")
    replacement.replace(path)  # as the overlay is saved: written aside, then renamed
    assert watch.changed()
    path.unlink()
    assert watch.changed()
