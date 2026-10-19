"""The store: which folder it is, and how settings and keys are layered."""

import logging
import os
from pathlib import Path

import pytest

from twobells.board.config import settings_from
from twobells.core.errors import ConfigError
from twobells.settings import store as store_module
from twobells.settings.installs import install_named
from twobells.settings.overlay import save_to
from twobells.settings.schema import saved_keys
from twobells.settings.store import Store, find_folder, platform_folder

HOME = Path("/home/me")
ENV = {"RDM_API_KEY": "rail-key"}


def found(data=None, given=None, install="desktop", environ=None, platform="linux"):
    return find_folder(given=given, config_data=lambda: data or {}, install=install, environ=environ or {},
                       platform=platform, home=HOME)


# Which folder: the first rule that applies wins


STATE_FILE = {"state_file": "/var/lib/board/state.json"}
DASHBOARD_DIR = {"dashboard": {"state_dir": "/srv/dash"}}
SYSTEMD = {"STATE_DIRECTORY": "/var/lib/twobells:/var/lib/other"}


@pytest.mark.parametrize("given, environ, data, install, folder", [
    # 1. --state-dir, over everything else
    ("/data", SYSTEMD, {**STATE_FILE, **DASHBOARD_DIR}, "pi", "/data"),
    # 2. systemd's StateDirectory, the first path in it
    (None, SYSTEMD, {**STATE_FILE, **DASHBOARD_DIR}, "desktop", "/var/lib/twobells"),
    # 3. the folder of config.toml's state_file
    (None, {}, {**STATE_FILE, **DASHBOARD_DIR}, "docker", "/var/lib/board"),
    # 4. the old dashboard's [dashboard] state_dir
    (None, {}, DASHBOARD_DIR, "pi", "/srv/dash"),
    # 5. the install's own
    (None, {}, {}, "pi", "/var/lib/twobells"),
    (None, {}, {}, "docker", "/data"),
    (None, {}, {}, "desktop", "/home/me/.local/state/twobells"),
])
def test_the_first_rule_that_applies_gives_the_folder(given, environ, data, install, folder):
    assert found(data, given, install, environ) == Path(folder)


def test_config_toml_is_not_read_when_the_folder_is_given_or_from_systemd():
    def unreadable():
        raise ConfigError("config.toml: broken")

    assert find_folder(given="/data", config_data=unreadable, environ={}) == Path("/data")
    assert find_folder(config_data=unreadable, environ=SYSTEMD) == Path("/var/lib/twobells")
    with pytest.raises(ConfigError, match="broken"):
        find_folder(config_data=unreadable, environ={})


def test_a_relative_systemd_folder_is_not_taken():
    assert found(environ={"STATE_DIRECTORY": "relative/state"}, install="pi") == Path("/var/lib/twobells")


def test_a_relative_state_file_is_in_the_working_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert found({"state_file": "state.json"}) == tmp_path
    assert found({"state_file": "board/state.json"}) == tmp_path / "board"


def test_a_given_folder_and_the_dashboard_s_may_start_from_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    assert found(given="~/dash") == Path("/home/someone/dash")
    assert found({"dashboard": {"state_dir": "~/dash"}}) == Path("/home/someone/dash")


@pytest.mark.parametrize("data, words", [
    ({"state_file": 7}, "state_file must be a path in quotes"),
    ({"dashboard": {"state_dir": ""}}, "state_dir must be a folder path"),
    ({"dashboard": {"state_dir": 7}}, "state_dir must be a folder path"),
])
def test_a_folder_named_wrongly_is_a_config_error(data, words):
    with pytest.raises(ConfigError, match=words):
        found(data)


def test_an_empty_state_file_names_no_folder():
    assert found({"state_file": ""}, install="docker") == Path("/data")


def test_an_unknown_install_is_refused():
    with pytest.raises(ValueError, match="Unknown install"):
        install_named("toaster")


# A desktop's own folder


def test_the_platform_folder_on_a_mac_and_elsewhere(tmp_path):
    home = tmp_path / "home"
    assert platform_folder("darwin", {}, home) == home / "Library" / "Application Support" / "twobells"
    assert platform_folder("linux", {}, home) == home / ".local" / "state" / "twobells"
    xdg = platform_folder("linux", {"XDG_STATE_HOME": "/var/state"}, home)
    assert xdg == Path("/var/state/twobells")
    # A relative XDG_STATE_HOME is ignored, as its spec says.
    assert platform_folder("linux", {"XDG_STATE_HOME": "state"}, home) == home / ".local" / "state" / "twobells"


# The store's files


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def board(tmp_path):
    """A config.toml whose state_file is in the store, as on a Pi."""
    store = tmp_path / "store"
    path = write(tmp_path / "config.toml", f'state_file = "{store / "board.json"}"\ntitle = "Base"\n'
                                           '[train]\ncrs = "GLO"\nrows = 3\n')
    return path, store


def test_the_store_s_files(board, tmp_path):
    path, folder = board
    store = Store.open(path, tmp_path / "secrets.env", environ={})
    assert store.folder == folder and store.config_path == path
    assert store.state_file == folder / "board.json"  # config.toml's name, as it is in the store
    assert (store.settings_file, store.secrets_file, store.password_file) == (
        folder / "settings.toml", folder / "secrets.env", folder / "web.json")


def test_a_state_file_elsewhere_is_named_state_json_in_the_store(board, tmp_path):
    path, _ = board
    store = Store.open(path, state_dir=tmp_path / "given", environ={})
    assert store.state_file == tmp_path / "given" / "state.json"
    assert store.data()["state_file"] == str(tmp_path / "given" / "state.json")


def test_settings_are_config_toml_then_the_saved_ones(board):
    path, folder = board
    write(folder / "settings.toml", 'title = "Saved"\n[train]\nrows = 2\n[dashboard]\ntrain_rows = 4\n')
    data = Store.open(path, environ={}).data()
    assert data["title"] == "Saved"
    assert data["train"] == {"crs": "GLO", "rows": 2}  # a table merges key by key
    assert data["dashboard"] == {"train_rows": 4}  # the dashboard's saved settings are in the same file
    assert data["state_file"] == str(folder / "board.json")


def test_the_store_s_time_zone_is_the_saved_one_or_the_default(board):
    path, folder = board
    store = Store.open(path, environ={})
    assert store.zone().key == "Europe/London"
    write(folder / "settings.toml", 'timezone = "Asia/Kolkata"\n')
    assert store.zone().key == "Asia/Kolkata"
    write(folder / "settings.toml", "timezone = [not toml")
    assert store.zone().key == "Europe/London"  # settings that can't be read can't stop a time being said


def test_saved_settings_cannot_move_the_store_or_send_keys_elsewhere(board, caplog):
    path, folder = board
    write(folder / "settings.toml", f'state_file = "{folder.parent / "evil.json"}"\n'
                                    '[train]\nbase_url = "https://example.invalid/rail"\n'
                                    '[dashboard]\nstate_dir = "/elsewhere"\n')
    caplog.set_level(logging.WARNING)
    store = Store.open(path, environ={})
    data = store.data()
    assert data["state_file"] == str(folder / "board.json") and "base_url" not in data["train"]
    assert "dashboard" not in data
    logged = caplog.text
    assert "state_file" in logged and "[train] base_url" in logged and "[dashboard] state_dir" in logged
    assert "example.invalid" not in logged and "/elsewhere" not in logged


def test_the_saved_keys_are_the_schema_s_whichever_view_saved_them(board):
    path, folder = board
    write(folder / "settings.toml", 'theme = "plain"\n[app]\nviews = ["board"]\n[train]\nrows = 2\n'
                                    '[dashboard]\ntrain_rows = 4\n')
    store = Store.open(path, environ={})
    assert store.saved() == {"theme": "plain", "app": {"views": ["board"]}, "train": {"rows": 2},
                             "dashboard": {"train_rows": 4}}
    assert store.target(settings_from).allowed == saved_keys()


def test_keys_are_the_environment_then_secrets_then_the_store_s(board, tmp_path):
    path, folder = board
    secrets = write(tmp_path / "secrets.env", "RDM_API_KEY=from-secrets\nBODS_API_KEY=from-secrets\n")
    write(folder / "secrets.env", "BODS_API_KEY=saved\n")
    environ = {"RDM_API_KEY": "from-environ", "TAPI_APP_ID": "from-environ", "BODS_API_KEY": "from-environ"}
    env = Store.open(path, secrets, environ={}).env(environ)
    assert env == {"RDM_API_KEY": "from-secrets", "TAPI_APP_ID": "from-environ", "BODS_API_KEY": "saved"}


def test_keys_without_a_secrets_file(board):
    path, folder = board
    write(folder / "secrets.env", "BODS_API_KEY=saved\n")
    assert Store.open(path, environ={}).env({"X": "1"}) == {"X": "1", "BODS_API_KEY": "saved"}


def test_a_missing_config_toml_is_no_settings_and_says_so(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = tmp_path / "missing.toml"
    store = Store.open(path, state_dir=tmp_path / "store", environ={})
    assert store.base() == {}
    assert store.data() == {"state_file": str(tmp_path / "store" / "state.json")}
    assert str(path) in caplog.text


def test_without_config_toml_a_desktop_uses_its_own_folder(tmp_path, monkeypatch):
    from twobells.settings import store as stores

    monkeypatch.setattr(stores, "_home", lambda: tmp_path / "home")
    store = Store.open(tmp_path / "missing.toml", environ={}, platform="darwin")
    assert store.folder == tmp_path / "home" / "Library" / "Application Support" / "twobells"
    assert not store.folder.exists()  # found, not made


@pytest.mark.parametrize("content, words", [
    ("folder", "is a folder"),
    (b"title = \n", "missing.toml|config.toml"),
    (b'title = "\xff"\n', "codec"),
])
def test_a_config_toml_that_can_t_be_read_is_a_config_error(tmp_path, content, words):
    path = tmp_path / "config.toml"
    if content == "folder":
        path.mkdir()  # as Docker makes one for a file to mount that isn't there
    else:
        path.write_bytes(content)
    store = Store.open(path, state_dir=tmp_path / "store", environ={})  # the folder is given, so it opens
    with pytest.raises(ConfigError, match=words):
        store.data()
    with pytest.raises(ConfigError, match=words):
        Store.open(path, environ={})  # needed to find the folder


def test_a_broken_settings_toml_is_a_config_error_naming_it(board):
    path, folder = board
    write(folder / "settings.toml", "this is [not toml")
    with pytest.raises(ConfigError, match=r"settings\.toml"):
        Store.open(path, environ={}).data()


def test_a_file_where_the_folder_should_be_is_a_config_error(board, tmp_path):
    path, _ = board
    write(tmp_path / "file", "")
    with pytest.raises(ConfigError, match="not a folder"):
        Store.open(path, state_dir=tmp_path / "file", environ={}).saved()


def test_the_files_to_watch(board, tmp_path):
    path, folder = board
    store = Store.open(path, tmp_path / "secrets.env", environ={})
    assert store.watched() == [path, tmp_path / "secrets.env", folder / "settings.toml", folder / "secrets.env"]
    assert Store.open(path, environ={}).watched() == [path, folder / "settings.toml", folder / "secrets.env"]


def test_making_the_folder_is_for_its_owner_only(tmp_path):
    store = Store.open(tmp_path / "missing.toml", state_dir=tmp_path / "new" / "store", environ={})
    store.make()
    assert os.stat(store.folder).st_mode & 0o777 == 0o700
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o750)
    os.chmod(existing, 0o750)
    Store.open(tmp_path / "missing.toml", state_dir=existing, environ={}).make()
    assert os.stat(existing).st_mode & 0o777 == 0o750  # as systemd made it


# Saving: either view's save keeps what the other saved


def test_a_board_save_keeps_the_dashboard_s_settings_and_a_dashboard_save_the_board_s(board):
    path, folder = board
    store = Store.open(path, environ={})
    save_to(store.target(settings_from), {"theme": "plain", "train": {"rows": 2}}, {}, ENV)
    save_to(store.target(lambda data, env: None), {"dashboard": {"train_rows": 4}}, {}, ENV)
    save_to(store.target(settings_from), {"title": "Ours"}, {}, ENV)
    saved = store.saved()
    assert saved == {"theme": "plain", "train": {"rows": 2}, "dashboard": {"train_rows": 4}, "title": "Ours"}


def test_the_target_checks_as_its_view_loads(board):
    from twobells.settings.overlay import OverlayError

    path, _ = board
    store = Store.open(path, environ={})
    with pytest.raises(OverlayError, match="crs"):
        save_to(store.target(settings_from), {"train": {"crs": "TOOLONG"}}, {}, ENV)
    assert store.target(settings_from).base()["state_file"] == str(store.state_file)


# Run as root on a store that is the service user's, a command would leave
# root's files there that the services can't use: it is refused instead.


@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


def test_root_is_told_to_run_as_the_stores_owner(tmp_path, as_root):
    import pwd

    me = pwd.getpwuid(os.stat(tmp_path).st_uid).pw_name  # tests never run as root
    with pytest.raises(store_module.WrongUser) as refused:
        store_module.check_owner(tmp_path, "twobells timetable --config /etc/twobells/config.toml")
    assert str(refused.value) == (f"Run this as the service's user: sudo -u {me} "
                                  "twobells timetable --config /etc/twobells/config.toml")


def test_an_owner_with_no_name_is_named_by_number(tmp_path, as_root, monkeypatch):
    monkeypatch.setattr(store_module, "_owner", lambda folder: 4242424)
    with pytest.raises(store_module.WrongUser, match=r"sudo -u '#4242424' twobells run$"):
        store_module.check_owner(tmp_path, "twobells run")


def test_root_may_write_its_own_store_or_make_a_new_one(tmp_path, as_root, monkeypatch):
    store_module.check_owner(tmp_path / "not yet made", "twobells run")
    monkeypatch.setattr(store_module, "_owner", lambda folder: 0)
    store_module.check_owner(tmp_path, "twobells run")


def test_anyone_but_root_goes_ahead_as_today(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(store_module, "_owner", lambda folder: 999)
    store_module.check_owner(tmp_path, "twobells run")
