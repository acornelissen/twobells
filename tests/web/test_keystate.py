"""Each key's state, as the Keys page and the Overview say it."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from twobells.settings import keyresults
from twobells.settings.installs import install_named
from twobells.settings.store import Store
from twobells.web.icons import icon
from twobells.web.keystate import key_states, pill

CONFIG = """
title = "Glossop"
[train]
crs = "GLO"
[bus]
source = "bods"
[bus.stops]
A = "1000DGHS0900"
"""
NOW = 1_791_000_000


@pytest.fixture
def store(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    store = Store.open(config, state_dir=tmp_path / "store", install="docker", environ={})
    store.make()
    return store


def states(store, install="docker"):
    return {s.name: (s.state, s.words) for s in key_states(store, install_named(install))}


def test_each_key_of_the_views_on_has_a_state(store):
    store.secrets_file.write_text("BODS_API_KEY=bods-key-7654321\n")
    assert states(store) == {
        "RDM_API_KEY": ("needed", "Needed for trains"),
        "BODS_API_KEY": ("not_tested", "Saved, not tested"),
        "RDM_DETAILS_API_KEY": ("not_set", "Not set"),
        "RDM_KB_API_KEY": ("not_set", "Not set"),
    }
    assert list(states(store, "pi")) == ["RDM_API_KEY", "BODS_API_KEY", "TAPI_APP_KEY"]


def test_tfl_s_key_is_offered_only_while_the_metro_is_london_s(store):
    store.config_path.write_text(CONFIG + '[metro]\n[metro.stops]\nT = "940GZZLUBNK"\n')
    assert states(store)["TFL_APP_KEY"] == ("not_set", "Not set")  # optional: never needed
    store.config_path.write_text(CONFIG + '[metro]\n[metro.stops]\nT = "9400ZZMAPIC"\n')
    assert "TFL_APP_KEY" not in states(store)


def test_a_screen_boards_where_each_train_is_key_has_a_state_on_a_board_only_install(store):
    store.config_path.write_text('display = "hdmi-1280x400"\n' + CONFIG)
    assert states(store, "pi") == {
        "RDM_API_KEY": ("needed", "Needed for trains"),
        "BODS_API_KEY": ("needed", "Needed for buses"),
        "TAPI_APP_KEY": ("not_set", "Not set"),
        "RDM_DETAILS_API_KEY": ("not_set", "Not set"),  # optional, so never needed
    }


def test_a_tested_key_says_how_its_last_test_went_and_when(store):
    store.secrets_file.write_text("RDM_API_KEY=rail-key-1234567\nBODS_API_KEY=bods-key-7654321\n")
    keyresults.record(store.folder, "RDM_API_KEY", "works", NOW)
    keyresults.record(store.folder, "BODS_API_KEY", "refused", NOW)
    when = datetime.fromtimestamp(NOW, ZoneInfo("Europe/London"))
    said = f"{when.day} {when:%b %H:%M}"
    found = {s.name: s for s in key_states(store, install_named("docker"))}
    assert found["RDM_API_KEY"].words == f"Works, tested {said}"
    assert found["RDM_API_KEY"].tested == when.replace(tzinfo=None)
    assert pill(found["BODS_API_KEY"]) == f'<span class="pill pill-bad">{icon("x")} Refused, tested {said}</span>'


def test_a_result_kept_for_a_key_no_longer_saved_is_not_shown(store):
    keyresults.record(store.folder, "RDM_DETAILS_API_KEY", "works", NOW)
    assert states(store)["RDM_DETAILS_API_KEY"] == ("not_set", "Not set")


def test_a_key_is_needed_only_while_what_uses_it_is_on(store):
    store.config_path.write_text(CONFIG.replace('crs = "GLO"', 'crs = "GLO"\nenabled = false'))
    assert states(store)["RDM_API_KEY"] == ("not_set", "Not set")
    assert states(store)["BODS_API_KEY"] == ("needed", "Needed for buses")


def test_transportapis_pair_is_needed_while_the_boards_buses_come_from_it(store):
    store.config_path.write_text(CONFIG.replace('source = "bods"', 'source = "transportapi"'))
    store.secrets_file.write_text("TAPI_APP_ID=app-id-0001\n")  # half the pair
    assert states(store, "pi")["TAPI_APP_KEY"] == ("needed", "Needed for the board's buses")
    assert states(store, "pi")["BODS_API_KEY"] == ("not_set", "Not set")


def test_settings_that_cant_be_read_still_give_each_keys_state(store):
    store.config_path.write_text("this is [not toml")
    store.secrets_file.write_bytes(b"\xff\xfe not text")
    assert set(states(store)) == {"RDM_API_KEY", "BODS_API_KEY", "RDM_DETAILS_API_KEY", "RDM_KB_API_KEY"}


def test_no_state_holds_a_key(store):
    store.secrets_file.write_text("RDM_API_KEY=rail-key-1234567\n")
    keyresults.record(store.folder, "RDM_API_KEY", "works", NOW)
    assert "rail-key-1234567" not in repr(key_states(store, install_named("docker")))
