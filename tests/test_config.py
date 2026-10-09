from datetime import timedelta
from pathlib import Path

import pytest

from departureboard.config import ConfigError, load_config
from departureboard.darwin import DarwinSource
from departureboard.transportapi import TransportApiBusSource

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"
ENV = {"RDM_API_KEY": "rdm", "TAPI_APP_ID": "id", "TAPI_APP_KEY": "key"}


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


@pytest.fixture
def example():
    return EXAMPLE


def test_example_config_loads(example):
    settings = load_config(example, ENV)
    names = [c.source.name for c in settings.board.sources]
    assert names == ["train", "bus"]
    assert isinstance(settings.board.sources[0].source, DarwinSource)
    assert isinstance(settings.board.sources[1].source, TransportApiBusSource)
    assert settings.timezone == "Europe/London"
    assert (settings.board.theme.name, settings.accent) == ("sign", "yellow")


def test_example_bus_schedule_fits_inside_its_budget(example):
    bus = load_config(example, ENV).board.sources[1]
    calls = 0
    for window in bus.policy.windows:
        span = (window.end.hour * 60 + window.end.minute) - (window.start.hour * 60 + window.start.minute)
        calls += span // int(bus.policy.interval.total_seconds() // 60) + 1
    assert calls * bus.source.cost <= bus.daily_budget


def test_bus_section_is_optional(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, {"RDM_API_KEY": "rdm"})
    assert [c.source.name for c in settings.board.sources] == ["train"]


def test_missing_secret_names_the_variable(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="RDM_API_KEY"):
        load_config(path, {})


def test_bad_window_is_reported(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["mornings"]\n')
    with pytest.raises(ConfigError, match="mornings"):
        load_config(path, ENV)


def test_example_watches_both_henry_street_stops(example):
    bus = load_config(example, ENV).board.sources[1]
    assert bus.source.cost == 2


def test_bus_without_stops_is_rejected(tmp_path):
    path = write(tmp_path, '[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n')
    with pytest.raises(ConfigError, match="stops"):
        load_config(path, ENV)


def test_bus_stop_left_as_placeholder_is_rejected(tmp_path):
    path = write(tmp_path, '[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "CHANGE_ME"\n')
    with pytest.raises(ConfigError, match="CHANGE_ME"):
        load_config(path, ENV)


def test_bus_stops_can_be_overridden(example):
    settings = load_config(example, ENV, bus_stops={"V": "1000DGVS3332"})
    assert settings.board.sources[1].source.cost == 1


def test_loading_only_bus_does_not_need_the_rail_key(example):
    env = {"TAPI_APP_ID": "id", "TAPI_APP_KEY": "key"}
    settings = load_config(example, env, only={"bus"})
    assert [c.source.name for c in settings.board.sources] == ["bus"]


def test_minimum_redraw_gap_defaults_to_two_minutes(example):
    assert load_config(example, ENV).board.min_redraw == timedelta(minutes=2)


def test_minimum_redraw_gap_can_be_set(tmp_path):
    path = write(tmp_path, 'min_redraw_minutes = 3\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).board.min_redraw == timedelta(minutes=3)


def test_old_refresh_every_setting_is_ignored(tmp_path):
    path = write(tmp_path, 'refresh_every_minutes = 10\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    board = load_config(path, ENV).board
    assert not hasattr(board, "refresh_every")


def test_walk_minutes_for_station_and_each_stop(tmp_path):
    path = write(tmp_path, (
        '[train]\ncrs = "GLO"\nwalk_minutes = 7\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n'
        '[bus.stops]\nA = "1000DGHS0900"\nB = "100000008350"\n'
        '[bus.walk_minutes]\nA = 7\nB = 6\n'
    ))
    walk = load_config(path, ENV).board.walk
    assert walk.train == 7
    assert dict(walk.bus) == {"A": 7, "B": 6}


def test_single_bus_walk_time_applies_to_every_stop(tmp_path):
    path = write(tmp_path, (
        '[bus]\nwalk_minutes = 5\nwindows = ["Mon-Fri 07:00-08:00"]\n'
        '[bus.stops]\nA = "1000DGHS0900"\n'
    ))
    assert dict(load_config(path, ENV).board.walk.bus) == {"*": 5}


def test_walk_times_are_optional(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    walk = load_config(path, ENV).board.walk
    assert walk.train is None and dict(walk.bus) == {}


def test_example_config_has_walk_times(example):
    walk = load_config(example, ENV).board.walk
    assert walk.train is not None and set(walk.bus) == {"A", "B"}


def test_theme_defaults_to_the_departure_board_look_in_yellow(tmp_path):
    path = write(tmp_path, '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert settings.board.theme.name == "sign"
    assert settings.accent == "yellow"


def test_plain_theme_defaults_to_red(tmp_path):
    path = write(tmp_path, 'theme = "plain"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    settings = load_config(path, ENV)
    assert (settings.board.theme.name, settings.accent) == ("plain", "red")


def test_explicit_accent_wins(tmp_path):
    path = write(tmp_path, 'accent = "red"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).accent == "red"


def test_unknown_theme_is_a_config_error(tmp_path):
    path = write(tmp_path, 'theme = "neon"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="neon"):
        load_config(path, ENV)


def test_display_defaults_to_the_inky(example):
    assert load_config(example, ENV).display == "inky"


def test_waveshare_display_can_be_chosen(tmp_path):
    path = write(tmp_path, 'display = "waveshare-10in85g"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).display == "waveshare-10in85g"


def test_unknown_display_is_a_config_error(tmp_path):
    path = write(tmp_path, 'display = "crt"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    with pytest.raises(ConfigError, match="crt.*inky"):
        load_config(path, ENV)


def _rotate_config(tmp_path, lines):
    return write(tmp_path, lines + '\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')


def test_rotate_defaults_to_upright(example):
    assert load_config(example, ENV).rotate == 0


def test_old_rotate_180_setting_still_turns_the_board(tmp_path):
    assert load_config(_rotate_config(tmp_path, "rotate_180 = true"), ENV).rotate == 180


def test_screen_can_turn_a_quarter(tmp_path):
    path = _rotate_config(tmp_path, 'display = "hdmi-1280x400"\nrotate = 90')
    settings = load_config(path, ENV)
    assert (settings.display, settings.rotate) == ("hdmi-1280x400", 90)


def test_epaper_cannot_turn_a_quarter(tmp_path):
    with pytest.raises(ConfigError, match="0, 180"):
        load_config(_rotate_config(tmp_path, "rotate = 90"), ENV)


ROUTE = '''
display = "hdmi-1280x400"
rotate = 90
[bus]
windows = ["Mon-Fri 07:00-08:30"]
[bus.stops]
A = "1000DGHS0900"
[bus.routes.341]
stop = "A"
stops = [
  { name = "Arundel Street", lat = 53.443519, lon = -1.95326 },
  { name = "Henry Street", lat = 53.44384, lon = -1.948999 },
]
'''
BODS_ENV = {**ENV, "BODS_API_KEY": "bods"}


def test_route_strip_is_loaded_for_a_screen(tmp_path):
    tracker = load_config(write(tmp_path, ROUTE), BODS_ENV).board.tracker
    strip = tracker.routes["341"]
    assert strip.stop == "A"
    assert [s.name for s in strip.stops] == ["Arundel Street", "Henry Street"]
    assert tracker.policy.interval == timedelta(seconds=30)
    assert tracker.policy.windows == load_config(write(tmp_path, ROUTE), BODS_ENV).board.sources[0].policy.windows


def test_route_strip_needs_the_bods_key(tmp_path):
    with pytest.raises(ConfigError, match="BODS_API_KEY"):
        load_config(write(tmp_path, ROUTE), ENV)


def test_epaper_ignores_routes_and_needs_no_bods_key(tmp_path):
    path = write(tmp_path, ROUTE.replace('display = "hdmi-1280x400"\nrotate = 90', 'display = "inky"'))
    assert load_config(path, ENV).board.tracker is None


def test_display_override_turns_the_route_page_on(tmp_path):
    path = write(tmp_path, ROUTE.replace('display = "hdmi-1280x400"\nrotate = 90', 'display = "inky"'))
    assert load_config(path, BODS_ENV, display="hdmi-1280x400").board.tracker is not None


def test_route_stop_must_be_a_configured_stop(tmp_path):
    with pytest.raises(ConfigError, match="not one of"):
        load_config(write(tmp_path, ROUTE.replace('stop = "A"', 'stop = "Z"')), BODS_ENV)


def test_route_needs_two_stops(tmp_path):
    one = ROUTE.replace('  { name = "Arundel Street", lat = 53.443519, lon = -1.95326 },\n', "")
    with pytest.raises(ConfigError, match="two stops"):
        load_config(write(tmp_path, one), BODS_ENV)


def test_route_stop_needs_a_real_position(tmp_path):
    bad = ROUTE.replace("lat = 53.443519", "lat = 153.4")
    with pytest.raises(ConfigError, match="impossible"):
        load_config(write(tmp_path, bad), BODS_ENV)


def test_positions_cannot_be_polled_faster_than_bods_updates(tmp_path):
    fast = ROUTE.replace('[bus.stops]', 'position_interval_seconds = 5\n[bus.stops]')
    with pytest.raises(ConfigError, match="at least 10"):
        load_config(write(tmp_path, fast), BODS_ENV)


def _with_state(tmp_path, body):
    return write(tmp_path, f'state_file = "{tmp_path / "state" / "state.json"}"\n' + body)


def test_saved_settings_are_layered_over_config(tmp_path):
    path = _with_state(tmp_path, 'title = "Base"\n[train]\ncrs = "GLO"\nrows = 3\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text('title = "Saved"\n[train]\nrows = 2\n')
    settings = load_config(path, ENV)
    assert settings.board.title == "Saved"
    train = settings.board.sources[0]
    assert train.source.name == "train"
    assert train.policy.windows  # untouched keys of a table survive


def test_overlay_cannot_move_the_state_file(tmp_path):
    from departureboard.config import read_config

    path = _with_state(tmp_path, "")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text('state_file = "/etc/evil.json"\n')
    assert read_config(path)["state_file"] == str(tmp_path / "state" / "state.json")


def test_broken_overlay_is_a_config_error(tmp_path):
    path = _with_state(tmp_path, "")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "settings.toml").write_text("title = \n")
    with pytest.raises(ConfigError, match="settings.toml"):
        load_config(path, ENV)


def test_merge_replaces_lists_and_merges_tables():
    from departureboard.config import merge

    base = {"a": 1, "t": {"x": 1, "y": [1, 2]}, "l": [1]}
    assert merge(base, {"t": {"y": [3]}, "l": [2]}) == {"a": 1, "t": {"x": 1, "y": [3]}, "l": [2]}


def test_merge_replaces_tables_below_the_top_level():
    from departureboard.config import merge

    base = {"bus": {"rows": 2, "stops": {"A": "x", "B": "y"}, "routes": {"341": {}}}}
    merged = merge(base, {"bus": {"stops": {"A": "x"}, "walk_minutes": {}}})
    assert merged == {"bus": {"rows": 2, "stops": {"A": "x"}, "walk_minutes": {}, "routes": {"341": {}}}}


def test_empty_values_saved_by_the_page_mean_none(tmp_path):
    path = write(tmp_path, (
        'accent = ""\n'
        '[train]\ncrs = "GLO"\nfilter_crs = ""\nwalk_minutes = ""\nwindows = ["Mon-Fri 06:30-09:30"]\n'
        '[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n[bus.walk_minutes]\n'
    ))
    settings = load_config(path, ENV)
    assert settings.accent == "yellow"
    assert settings.board.walk.train is None and dict(settings.board.walk.bus) == {}
    assert "filterCrs" not in settings.board.sources[0].source._url()


TRAIN = '[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n'
BUS = '[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n'


@pytest.mark.parametrize(
    "text, message",
    [
        ('[train]\nwindows = ["Mon-Fri 06:30-09:30"]\n', "crs"),
        ('[train]\ncrs = 5\nwindows = ["Mon-Fri 06:30-09:30"]\n', "crs"),
        ('train = "GLO"\n', r"\[train\]"),
        (TRAIN + 'interval_minutes = "5"\n', "interval_minutes"),
        (TRAIN + "interval_minutes = 0\n", "interval_minutes"),
        (TRAIN + "rows = 0\n", "rows"),
        (TRAIN + 'stale_after_minutes = "6"\n', "stale_after_minutes"),
        (TRAIN + 'walk_minutes = "5"\n', "walk_minutes"),
        (TRAIN + 'filter_crs = ["MAN"]\n', "filter_crs"),
        ('[train]\ncrs = "GLO"\nwindows = "Mon-Fri 06:30-09:30"\n', "windows"),
        ('min_redraw_minutes = "2"\n' + TRAIN, "min_redraw_minutes"),
        ("min_redraw_minutes = 0\n" + TRAIN, "min_redraw_minutes"),
        ('accent = "blue"\n' + TRAIN, "accent"),
        ('title = 5\n' + TRAIN, "title"),
        ('timezone = "Mars/Olympus"\n' + TRAIN, "timezone"),
        ('display = ["inky"]\n' + TRAIN, "display"),
        ("state_file = 5\n" + TRAIN, "state_file"),
        ('[bus]\nstops = ["1000DGHS0900"]\nwindows = ["Mon-Fri 07:00-08:00"]\n', "stops"),
        ('[bus]\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = 1000\n', "A"),
        ('[bus]\nwalk_minutes = "5"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "walk_minutes"),
        (BUS + '[bus.walk_minutes]\nA = "7"\n', "walk_minutes"),
        ('[bus]\ndaily_budget = "28"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "daily_budget"),
        ('[bus]\ndirections = "Ashton"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "directions"),
        ('display = "hdmi-1280x400"\n' + BUS + '[bus.routes]\n341 = "Glossop"\n', "341"),
        ('display = "hdmi-1280x400"\n[bus]\nroutes = "341"\nwindows = ["Mon-Fri 07:00-08:00"]\n[bus.stops]\nA = "1000DGHS0900"\n', "routes"),
        ('display = "hdmi-1280x400"\n' + BUS + '[bus.routes.341]\nstop = "A"\nstops = "Arundel Street"\n', "341"),
        ("title = \n", "config.toml"),
    ],
)
def test_wrong_types_and_ranges_are_config_errors(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), BODS_ENV)


def test_broken_config_toml_is_a_config_error_when_finding_the_overlay(tmp_path):
    from departureboard.config import overlay_paths

    with pytest.raises(ConfigError, match="config.toml"):
        overlay_paths(write(tmp_path, "title = \n"))


def test_no_state_file_means_no_overlay_from_the_working_directory(tmp_path, monkeypatch):
    from departureboard.config import overlay_paths

    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings.toml").write_text('title = "From cwd"\n')
    path = write(tmp_path, 'title = "Base"\n[train]\ncrs = "GLO"\nwindows = ["Mon-Fri 06:30-09:30"]\n')
    assert load_config(path, ENV).board.title == "Base"
    assert overlay_paths(path) is None
