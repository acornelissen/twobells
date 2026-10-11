"""Bringing the old dashboard's folder into the store, once."""

import fcntl
import json
import logging
import os
import sqlite3
import stat
import threading
import tomllib

import pytest

from departureboard.settings import store as stores
from departureboard.settings.migrate import adopt_legacy
from departureboard.settings.overlay import LOCK_NAME
from departureboard.settings.store import Store


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def write(path, text, file_mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, file_mode)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(stores, "_home", lambda: home)
    return home


@pytest.fixture
def old(home):
    """The old dashboard's folder on a Mac, with its settings, keys, password and history."""
    folder = home / "Library" / "Application Support" / "departureboard-dashboard"
    write(folder / "settings.toml", 'title = "Dashboard title"\n[dashboard]\ntrain_rows = 4\n'
                                    '[train]\nbase_url = "https://example.invalid"\n')
    write(folder / "secrets.env", "BODS_API_KEY=dash-bods\nRDM_DETAILS_API_KEY=dash-details\nRDM_KB_API_KEY=dash-kb\n")
    write(folder / "web.json", '{"dashboard": "password"}')
    history = sqlite3.connect(folder / "history.sqlite3")
    history.execute("PRAGMA journal_mode=WAL")
    history.execute("CREATE TABLE seen (id TEXT)")
    history.execute("INSERT INTO seen VALUES ('1A23')")
    history.commit()
    history.close()
    return folder


@pytest.fixture
def board(tmp_path):
    """A config.toml whose state_file names the board's folder: the store."""
    folder = tmp_path / "board"
    folder.mkdir()
    config = write(tmp_path / "config.toml", f'state_file = "{folder / "state.json"}"\n')
    return Store.open(config, environ={}), folder


def adopt(store, install="desktop"):
    return adopt_legacy(store, install, platform="darwin", environ={})


def snapshot(folder):
    """Every file's content, leaving out SQLite's working files, which reading the history may make."""
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if not p.name.endswith(("-wal", "-shm"))}


def test_a_pi_has_nothing_to_bring(board, old):
    store, folder = board
    assert adopt(store, "pi") == []
    assert os.listdir(folder) == []


def test_docker_has_nothing_to_bring(tmp_path, old):
    data = tmp_path / "data"
    data.mkdir()
    store = Store.open(tmp_path / "missing.toml", state_dir=data, environ={})
    assert adopt(store, "docker") == []
    assert os.listdir(data) == []


def test_without_an_old_folder_nothing_is_written(board, home):
    store, folder = board
    assert adopt(store) == []
    assert os.listdir(folder) == []  # not even a lock


def test_an_old_folder_used_in_place_has_nothing_to_bring(old, tmp_path):
    store = Store.open(tmp_path / "missing.toml", environ={}, platform="darwin")
    assert store.folder == old
    before = snapshot(old)
    assert adopt(store) == []
    assert snapshot(old) == before


def test_a_dashboard_only_folder_is_brought_into_an_empty_store(board, old):
    store, folder = board
    before = snapshot(old)
    imported = adopt(store)
    assert imported == ["settings.toml", "secrets.env", "web.json", "history.sqlite3"]
    for name in ("settings.toml", "secrets.env", "web.json"):
        assert (folder / name).read_bytes() == (old / name).read_bytes()
    assert (mode(folder / "settings.toml"), mode(folder / "secrets.env"), mode(folder / "web.json")) == (
        0o640, 0o600, 0o600)
    assert mode(folder / "history.sqlite3") == 0o600 and mode(folder / "migrated.json") == 0o600
    copied = sqlite3.connect(folder / "history.sqlite3")
    assert copied.execute("SELECT id FROM seen").fetchall() == [("1A23",)]
    copied.close()
    assert store.data()["title"] == "Dashboard title"
    assert store.env({})["RDM_DETAILS_API_KEY"] == "dash-details"
    assert snapshot(old) == before  # copied, never moved
    record = json.loads((folder / "migrated.json").read_text())
    assert record["v"] == 1 and record["from"] == str(old) and record["imported"] == imported
    assert record["at"]
    assert "dash-" not in (folder / "migrated.json").read_text()  # names, never values


def test_both_keep_the_board_s_settings_and_password_and_bring_only_the_dashboard_s_own(board, old, caplog):
    store, folder = board
    write(folder / "settings.toml", 'title = "Board title"\n[train]\nrows = 2\n', 0o640)
    write(folder / "secrets.env", "BODS_API_KEY=board-bods\nRDM_KB_API_KEY=board-kb\n")
    write(folder / "web.json", '{"board": "password"}')
    caplog.set_level(logging.INFO)
    imported = adopt(store)
    assert imported == ["[dashboard]", "RDM_DETAILS_API_KEY", "history.sqlite3"]
    assert "Kept the board's settings and password in" in caplog.text and str(old) in caplog.text
    saved = tomllib.loads((folder / "settings.toml").read_text())
    assert saved == {"title": "Board title", "train": {"rows": 2}, "dashboard": {"train_rows": 4}}
    env = store.env({})
    assert (env["BODS_API_KEY"], env["RDM_KB_API_KEY"], env["RDM_DETAILS_API_KEY"]) == (
        "board-bods", "board-kb", "dash-details")
    assert (folder / "web.json").read_text() == '{"board": "password"}'
    assert (mode(folder / "settings.toml"), mode(folder / "secrets.env")) == (0o640, 0o600)
    assert json.loads((folder / "migrated.json").read_text())["imported"] == imported


def test_a_dashboard_table_the_store_has_is_kept(board, old):
    store, folder = board
    write(folder / "settings.toml", "[dashboard]\nbus_rows = 3\n", 0o640)
    assert "[dashboard]" not in adopt(store)
    assert store.saved()["dashboard"] == {"bus_rows": 3}


def test_it_runs_once(board, old):
    store, folder = board
    adopt(store)
    after = snapshot(folder)
    write(old / "web.json", "changed later")
    (folder / "web.json").unlink()
    assert adopt(store) == []
    assert not (folder / "web.json").exists()
    assert set(snapshot(folder)) == set(after) - {"web.json"}


def test_the_folder_config_toml_names_for_the_dashboard_is_the_old_one(tmp_path, home):
    named = tmp_path / "dash"
    write(named / "web.json", '{"dashboard": "password"}')
    folder = tmp_path / "board"
    config = write(tmp_path / "config.toml", f'state_file = "{folder / "state.json"}"\n'
                                             f'[dashboard]\nstate_dir = "{named}"\n')
    store = Store.open(config, environ={})
    assert adopt(store) == ["web.json"]
    assert (folder / "web.json").read_text() == '{"dashboard": "password"}'


def test_a_linked_file_in_the_old_folder_is_refused(board, old, tmp_path, caplog):
    store, folder = board
    elsewhere = write(tmp_path / "elsewhere.toml", 'title = "Not the dashboard s"\n')
    (old / "settings.toml").unlink()
    (old / "settings.toml").symlink_to(elsewhere)
    (old / "history.sqlite3").rename(tmp_path / "history.sqlite3")
    (old / "history.sqlite3").symlink_to(tmp_path / "history.sqlite3")
    caplog.set_level(logging.WARNING)
    assert adopt(store) == ["secrets.env", "web.json"]
    assert not (folder / "settings.toml").exists() and not (folder / "history.sqlite3").exists()
    assert "link" in caplog.text and "plain file" in caplog.text


def test_it_takes_turns_with_a_save_so_neither_loses_the_other_s_changes(board, old):
    # A save (overlay.save_to) holds settings.lock from reading the saved
    # settings to writing them back; bringing [dashboard] in must hold it too.
    store, folder = board
    write(folder / "settings.toml", 'title = "Board title"\n', 0o640)
    lock = os.open(folder / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a save part way through
        imported = []
        adopting = threading.Thread(target=lambda: imported.extend(adopt(store)))
        adopting.start()
        adopting.join(0.5)
        assert adopting.is_alive(), "brought [dashboard] in while a save was part way through"
        assert tomllib.loads((folder / "settings.toml").read_text()) == {"title": "Board title"}
        write(folder / "settings.toml", 'title = "Saved meanwhile"\n', 0o640)  # the save lands
    finally:
        os.close(lock)
    adopting.join(10)
    assert not adopting.is_alive()
    assert "[dashboard]" in imported
    saved = tomllib.loads((folder / "settings.toml").read_text())
    assert saved == {"title": "Saved meanwhile", "dashboard": {"train_rows": 4}}


def test_a_store_file_already_there_is_never_overwritten(board, old):
    store, folder = board
    os.symlink(folder / "nowhere", folder / "web.json")  # a store, if an odd one
    adopt(store)
    assert os.path.islink(folder / "web.json") and not (folder / "nowhere").exists()


def test_the_store_is_made_for_its_owner_only(tmp_path, old):
    folder = tmp_path / "new" / "board"
    config = write(tmp_path / "config.toml", f'state_file = "{folder / "state.json"}"\n')
    assert adopt(Store.open(config, environ={}))
    assert mode(folder) == 0o700


def test_a_problem_never_stops_a_start(board, old, caplog):
    store, folder = board
    os.chmod(folder, 0o500)
    try:
        caplog.set_level(logging.WARNING)
        assert adopt(store) == []
    finally:
        os.chmod(folder, 0o700)
    assert "Couldn't bring" in caplog.text


def test_dashboard_brings_the_old_folder_before_setting_a_password(tmp_path, home, monkeypatch):
    import getpass

    from departureboard.cli.main import main
    from departureboard.web.auth import PasswordStore

    write(stores.platform_base() / "departureboard-dashboard" / "settings.toml", 'title = "Ours"\n')
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "a long enough password")
    folder = tmp_path / "board"
    config = write(tmp_path / "config.toml", f'state_file = "{folder / "state.json"}"\n')
    assert main(["dashboard", "--config", str(config), "--set-password"]) == 0
    assert (folder / "migrated.json").exists() and (folder / "settings.toml").exists()
    assert PasswordStore(folder / "web.json").check("a long enough password")
