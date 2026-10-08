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
    assert settings.board.refresh_every == timedelta(minutes=10)
    assert settings.timezone == "Europe/London"
    assert settings.accent == "red"


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
