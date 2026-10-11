import os
import stat
import threading

import pytest

from departureboard.core import files


def test_a_file_is_replaced_whole_with_the_mode_given_whatever_the_umask(tmp_path):
    path = tmp_path / "deep" / "web.json"
    old = os.umask(0)
    try:
        files.write_atomic(path, "first", 0o600)
        files.write_atomic(path, b"second", 0o640)
    finally:
        os.umask(old)
    assert path.read_text() == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert os.listdir(path.parent) == ["web.json"]  # nothing left aside


def test_it_is_on_disk_before_it_replaces_the_old_file(tmp_path, monkeypatch):
    calls = []
    replace = files.os.replace
    monkeypatch.setattr(files.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(files.os, "replace", lambda src, dst: calls.append("replace") or replace(src, dst))
    files.write_atomic(tmp_path / "state.json", "{}", 0o640)
    assert calls == ["fsync", "replace"]


def test_a_link_put_where_a_file_might_be_written_aside_is_never_written_through(tmp_path):
    # The file aside has a new, unguessable name each time and must be new:
    # links planted at the names it might take are left alone.
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (tmp_path / "secrets.env.tmp").symlink_to(target)
    files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert target.read_text() == "keep"
    assert (tmp_path / "secrets.env").read_text() == "KEY=x"
    assert (tmp_path / "secrets.env.tmp").is_symlink()


def test_writes_at_once_to_one_file_each_land_whole(tmp_path):
    # As the threaded web server, the wizard and the CLI may: none fails, and
    # the file is always one writer's content, never two spliced.
    path = tmp_path / "settings.toml"
    contents = [(str(n) * 50_000).encode() for n in range(8)]
    start = threading.Barrier(len(contents))
    errors, spliced = [], []

    def write(data: bytes) -> None:
        start.wait()
        for _ in range(30):
            try:
                files.write_atomic(path, data, 0o640)
            except OSError as error:
                errors.append(error)
            if path.read_bytes() not in contents:
                spliced.append(data[:1])

    threads = [threading.Thread(target=write, args=(data,)) for data in contents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and spliced == []
    assert path.read_bytes() in contents
    assert os.listdir(tmp_path) == ["settings.toml"]  # nothing left aside


def test_a_write_that_fails_leaves_the_old_file_and_nothing_aside(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    files.write_atomic(path, "KEY=old", 0o600)

    def full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(files.os, "fsync", full)
    with pytest.raises(OSError):
        files.write_atomic(path, "KEY=new", 0o600)
    assert path.read_text() == "KEY=old"
    assert os.listdir(tmp_path) == ["secrets.env"]


@pytest.mark.skipif(not files.NOFOLLOW, reason="needs O_NOFOLLOW")
def test_a_lock_file_is_private_and_no_link(tmp_path):
    os.close(files.open_lock(tmp_path / "a.lock"))
    assert stat.S_IMODE((tmp_path / "a.lock").stat().st_mode) == 0o600 & ~_umask()
    (tmp_path / "b.lock").symlink_to(tmp_path / "a.lock")
    with pytest.raises(OSError):
        files.open_lock(tmp_path / "b.lock")


def _umask() -> int:
    old = os.umask(0)
    os.umask(old)
    return old
