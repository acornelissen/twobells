import os
import stat
import threading
import time

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


def test_a_file_can_be_written_in_parts_and_one_that_fails_part_way_leaves_the_old_one(tmp_path):
    # As a timetable is saved, a journey at a time: never held whole.
    path = tmp_path / "bus-timetable.json"
    files.write_atomic(path, iter([b"{", b'"a":1', b"}"]), 0o640)
    assert path.read_text() == '{"a":1}'

    def cut_off():
        yield b"{"
        raise ValueError("too big")

    with pytest.raises(ValueError, match="too big"):
        files.write_atomic(path, cut_off(), 0o640)
    assert path.read_text() == '{"a":1}'
    assert os.listdir(tmp_path) == ["bus-timetable.json"]  # nothing left aside


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


HOUR_AGO = time.time() - 3600


def _aside(folder, name, content="left", when=HOUR_AGO):
    """A file as a write cut off by a power cut leaves aside, from `when`."""
    path = folder / name
    path.write_text(content)
    os.utime(path, (when, when))
    return path


def test_a_write_clears_away_what_a_write_cut_off_left_beside_the_same_file(tmp_path):
    # A power cut between making the file aside and renaming it leaves it for
    # good: a 50 MB timetable, or a copy of secrets.env with the keys in it.
    _aside(tmp_path, "secrets.env.0123456789abcdef.tmp", "KEY=old")
    _aside(tmp_path, "secrets.env.fedcba9876543210.tmp", "KEY=older")
    files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert os.listdir(tmp_path) == ["secrets.env"]


def test_a_file_aside_still_being_written_is_left_alone(tmp_path):
    # Another writer's, made a moment ago and not yet renamed.
    _aside(tmp_path, "timetable.json.0123456789abcdef.tmp", when=time.time() - 5)
    files.write_atomic(tmp_path / "timetable.json", "{}", 0o640)
    assert sorted(os.listdir(tmp_path)) == ["timetable.json", "timetable.json.0123456789abcdef.tmp"]


def test_only_files_aside_named_exactly_as_this_file_s_are_cleared(tmp_path):
    keep = [
        "web.json.0123456789abcdef.tmp",  # another file's
        "secrets.env.0123456789ABCDEF.tmp",  # not the hex a write uses
        "secrets.env.0123456789abcde.tmp",  # too short
        "secrets.env.0123456789abcdef0.tmp",  # too long
        "secrets.env.0123456789abcdef.tmp.bak",
        "secrets.env.tmp",
        "xsecrets.env.0123456789abcdef.tmp",
        "secrets.env.0123456789abcdef.tmp.0123456789abcdef.tmp",
    ]
    for name in keep:
        _aside(tmp_path, name)
    files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert sorted(os.listdir(tmp_path)) == sorted([*keep, "secrets.env"])


def test_a_file_name_is_never_read_as_a_pattern(tmp_path):
    _aside(tmp_path, "a.0123456789abcdef.tmp")
    _aside(tmp_path, "b.0123456789abcdef.tmp")
    files.write_atomic(tmp_path / "*", "x", 0o600)
    files.write_atomic(tmp_path / "[ab]", "x", 0o600)
    files.write_atomic(tmp_path / "?", "x", 0o600)
    assert sorted(os.listdir(tmp_path)) == ["*", "?", "[ab]", "a.0123456789abcdef.tmp", "b.0123456789abcdef.tmp"]


def test_a_link_or_folder_named_as_a_file_aside_is_left_alone(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    link = tmp_path / "secrets.env.0123456789abcdef.tmp"
    link.symlink_to(target)
    os.utime(link, (HOUR_AGO, HOUR_AGO), follow_symlinks=False)
    os.utime(target, (HOUR_AGO, HOUR_AGO))
    folder = tmp_path / "secrets.env.fedcba9876543210.tmp"
    folder.mkdir()
    os.utime(folder, (HOUR_AGO, HOUR_AGO))
    files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert link.is_symlink() and target.read_text() == "keep"
    assert folder.is_dir()


def test_a_file_aside_that_is_someone_else_s_is_left_alone(tmp_path, monkeypatch):
    left = _aside(tmp_path, "secrets.env.0123456789abcdef.tmp")
    monkeypatch.setattr(files.os, "geteuid", lambda: os.stat(left).st_uid + 1)
    files.write_atomic(tmp_path / "secrets.env", "KEY=x", 0o600)
    assert left.exists()


def test_a_folder_that_cant_be_listed_still_takes_the_write(tmp_path, monkeypatch):
    def refused(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(files.os, "scandir", refused)
    files.write_atomic(tmp_path / "state.json", "{}", 0o640)
    assert (tmp_path / "state.json").read_text() == "{}"


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
