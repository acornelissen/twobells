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
