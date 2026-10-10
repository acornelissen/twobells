import stat
import tomllib

import pytest

from departureboard.settings.config import ConfigError, read_base, read_config
from departureboard.settings.overlay import OverlayError, Target, save_changes, save_to, to_toml

ENV = {"RDM_API_KEY": "rdm-secret-123", "BODS_API_KEY": "bods-secret-456"}

CONFIG = """
title = "Home"
state_file = "{state}"
display = "hdmi-1280x400"

[train]
crs = "GLO"
windows = ["Mon-Fri 06:30-09:30"]

[bus]
source = "bods"
operators = ["BNML"]
windows = ["Mon-Fri 07:00-08:30"]

[bus.stops]
A = "1000DGHS0900"
"""

# What the settings page leaves in the overlay; setup manages none of it.
SAVED = """
theme = "plain"
rotate = 90
text_size = 13
accent = "red"

[train]
rows = 4

[bus]
rows = 5
directions = ["Ashton"]

[weather]
enabled = false
threshold_percent = 70
"""

CHANGES = {
    "train": {"crs": "MAN", "walk_minutes": 9},
    "bus": {"stops": {"B": "1800SB12341"}, "walk_minutes": {"B": 4}, "operators": ["SCMN"],
            "routes": {"341": {"stop": "B", "stops": [
                {"name": "Hadfield", "lat": 53.46, "lon": -1.965},
                {"name": "Glossop \"Square\"", "lat": 53.443, "lon": -1.949},
            ]}}},
    "weather": {"lat": 53.44, "lon": -1.95},
    "bank_holiday_division": "england-and-wales",
}


@pytest.fixture
def board(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG.format(state=state / "state.json"))
    (state / "settings.toml").write_text(SAVED)
    return config, state


def test_changes_merge_into_the_overlay_keeping_everything_else(board):
    config, state = board
    save_changes(config, CHANGES, {}, ENV)
    saved = tomllib.loads((state / "settings.toml").read_text())
    for key in ("theme", "rotate", "text_size", "accent"):
        assert saved[key] == tomllib.loads(SAVED)[key]
    assert saved["train"] == {"rows": 4, "crs": "MAN", "walk_minutes": 9}
    assert saved["bus"]["rows"] == 5 and saved["bus"]["directions"] == ["Ashton"]
    assert saved["bus"]["stops"] == {"B": "1800SB12341"}
    assert saved["weather"] == {"enabled": False, "threshold_percent": 70, "lat": 53.44, "lon": -1.95}
    merged = read_config(config)
    assert merged["bus"]["stops"] == {"B": "1800SB12341"}  # replaced, not added to config.toml's A
    assert merged["bus"]["routes"]["341"]["stops"][1]["name"] == 'Glossop "Square"'


def test_a_save_drops_what_the_overlay_may_not_hold(board, caplog):
    config, state = board
    (state / "settings.toml").write_text(SAVED.replace("[train]\n", '[train]\nbase_url = "https://example.invalid"\n')
                                         + '\n[dashboard]\nstate_dir = "/example.invalid"\n')
    save_changes(config, CHANGES, {}, ENV)
    saved = tomllib.loads((state / "settings.toml").read_text())
    assert "base_url" not in saved["train"] and "dashboard" not in saved
    assert saved["theme"] == "plain"
    assert "example.invalid" not in caplog.text


def test_without_a_saved_overlay_the_changes_are_the_overlay(board):
    config, state = board
    (state / "settings.toml").unlink()
    written = save_changes(config, CHANGES, {}, ENV)
    assert tomllib.loads((state / "settings.toml").read_text()) == written == CHANGES


def test_nothing_is_written_if_the_settings_would_not_load(board):
    config, state = board
    before = (state / "settings.toml").read_text()
    with pytest.raises(OverlayError, match="RDM_API_KEY"):
        save_changes(config, CHANGES, {}, {"BODS_API_KEY": "x"})
    with pytest.raises(OverlayError, match="crs"):
        save_changes(config, {"train": {"crs": "TOOLONG"}}, {}, ENV)
    assert (state / "settings.toml").read_text() == before
    assert not (state / "secrets.env").exists()


def test_a_broken_overlay_is_left_alone(board):
    config, state = board
    (state / "settings.toml").write_text("this is [not toml")
    with pytest.raises(OverlayError, match="can't be read"):
        save_changes(config, CHANGES, {}, ENV)
    assert (state / "settings.toml").read_text() == "this is [not toml"


def test_without_a_state_file_the_changes_go_in_the_store(tmp_path):
    from departureboard.settings.store import Store

    config = tmp_path / "config.toml"
    config.write_text('[train]\ncrs = "GLO"\n')
    save_changes(config, {"train": {"rows": 2}}, {}, ENV)
    assert tomllib.loads(Store.open(config).settings_file.read_text()) == {"train": {"rows": 2}}
    assert not (tmp_path / "settings.toml").exists()


def test_keys_go_only_to_the_private_secrets_overlay(board):
    config, state = board
    (state / "secrets.env").write_text("RDM_DETAILS_API_KEY=details\nBODS_API_KEY=old\n")
    save_changes(config, CHANGES, {"BODS_API_KEY": "new-bods-key"}, {"RDM_API_KEY": "r"})
    assert (state / "secrets.env").read_text() == "RDM_DETAILS_API_KEY=details\nBODS_API_KEY=new-bods-key\n"
    assert stat.S_IMODE((state / "secrets.env").stat().st_mode) == 0o600
    assert "new-bods-key" not in (state / "settings.toml").read_text()


def test_a_key_is_checked_and_never_repeated(board):
    config, state = board
    with pytest.raises(OverlayError) as caught:
        save_changes(config, CHANGES, {"BODS_API_KEY": "has spaces in-it"}, ENV)
    assert "has spaces" not in str(caught.value) and "BODS_API_KEY" in str(caught.value)
    # The key is right but the settings fail: the message names no key value.
    with pytest.raises(OverlayError) as caught:
        save_changes(config, {"train": {"crs": "TOOLONG"}}, {"RDM_API_KEY": "TOOLONG"}, {})
    assert "TOOLONG" not in str(caught.value)
    assert not (state / "secrets.env").exists()


def test_a_target_saves_into_its_own_folder_checked_its_own_way(board, tmp_path):
    # As the dashboard does: its own folder, not the board's, and its own loader.
    config, state = board
    own = tmp_path / "own"
    own.mkdir()
    checked = []

    def check(data, env):
        checked.append((data["train"]["crs"], env.get("BODS_API_KEY")))
        if data["train"]["crs"] == "BAD":
            raise ConfigError("not for this host")

    target = Target(lambda: read_base(config), lambda base: own, check)
    save_to(target, {"train": {"crs": "MAN"}}, {"BODS_API_KEY": "pasted-key"}, ENV)
    assert tomllib.loads((own / "settings.toml").read_text()) == {"train": {"crs": "MAN"}}
    assert (own / "secrets.env").read_text() == "BODS_API_KEY=pasted-key\n"
    assert (state / "settings.toml").read_text() == SAVED  # the board's is untouched
    assert checked == [("MAN", "pasted-key")]
    assert target.current()["train"] == {"crs": "MAN", "windows": ["Mon-Fri 06:30-09:30"]}
    with pytest.raises(OverlayError, match="not for this host"):
        save_to(target, {"train": {"crs": "BAD"}}, {}, ENV)
    assert tomllib.loads((own / "settings.toml").read_text()) == {"train": {"crs": "MAN"}}


def test_a_target_with_no_folder_saves_nothing(board):
    config, _ = board
    with pytest.raises(OverlayError, match="state_file"):
        save_to(Target(lambda: read_base(config), lambda base: None), CHANGES, {}, ENV)


def test_to_toml_round_trips_every_kind_of_value():
    data = {"a": 1, "b": 2.5, "c": 1e-7, "d": True, "e": "x\u007f\n\"y\"", "f": [1, 2], "g": [{"x": 1.5, "y": "z"}],
            "h": {"i": {"j": {"k": "deep"}}, "l": -0.0}, "m": {}}
    assert tomllib.loads(to_toml(data)) == data


def test_to_toml_round_trips_characters_toml_is_fussy_about():
    data = {"title": 'Say "hi" \\ there', "odd key": 1,
            "bus": {"directions": ["Hyde ➜ 🚏", "Bus 🚌 café\x7f\x01\ttab"], "stops": {"A": "1000DGHS0900"}}}
    assert tomllib.loads(to_toml(data)) == data
